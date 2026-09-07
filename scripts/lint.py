#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AC0 Automated Check（自动检查）——机械、可穷举的规则全量扫描

用法:
    python3 lint.py db.json [--log netlist.log] [--intent intent.json]
    python3 lint.py db.json --evidence evidence.json \
        [--plan-json review-plan.json] [--json out.json]

**输出是疑似清单，不是判决。** 合法结构（Bob-Smith 终端、补偿网络、
DNP 选项、被删外设的引出脚、工具伪网络）由执行 agent 逐条排除。
规则依赖命名和已知图结构；零命中不表示检查完整或电气通过。

驱动源候选识别
--------------
Rule-04/05 穿过已贴装的低阻电阻、电感、磁珠或保险丝，寻找输出脚/外部连接器。
无源桥接件本身不能供电，DNP 通路断开；二极管/MOS 方向与受控开关交 ER2 判断。
识别到候选源仍须查物理引脚身份、端口方向、实际装配、开关状态和电源时序。

"""
import argparse
import difflib
import io
import json
import math
import os
import re
import sys
from collections import defaultdict

from plan_review import build_review_plan, validate_intent
from solve_dividers import Solver, divider_window, parse_resistor

GNDS = {'GND', 'PGND', 'AGND', 'DGND', 'EGND'}
RAIL_RE = re.compile(r'^(VCC|VDD|VDDA|VCCA|VOUT|VBAT|AVDD|DVDD|VIN|VBUS|V\d)', re.I)
OUTPIN_RE = re.compile(r'^(VOUT|SW|OUT|VO|LX|\+VO)', re.I)
EN_RE = re.compile(r'(^|_)(EN|ENABLE|SHDN|SHUTDOWN|PWREN)(_|\d|$)', re.I)
CLAMP_RE = re.compile(r'ZENER|TVS|BZT|SMBJ|SMAJ|SMCJ|MMSZ|1SMB|ESDA|PESD', re.I)
# EN 脚耐压绝大多数 <=6V；超过此值的上拉优先送 ER1 查 Abs Max（提示，非判定）
EN_PULL_ALERT_V = 6.0
HOT_RULES = [('Rule-08', '参数验算不符'), ('Rule-09', '必需上拉/串阻缺失'),
             ('Rule-12', 'EN 极性/耐压定判'), ('Rule-14', '新增符号引脚映射'),
             ('Rule-16', 'strap 违反强制条款')]
HOT_RULE_IDS = {x[0] for x in HOT_RULES}


def _pin_class(value):
    value = str(value or '').strip().upper()
    if value in {'OUT', 'OUTPUT', 'TRISTATE', '3STATE'}:
        return 'OUT'
    if value in {'IN', 'INPUT'}:
        return 'IN'
    if value in {'BI', 'BIDI', 'BIDIR', 'BIDIRECTIONAL', 'IO', 'I/O'}:
        return 'BIDI'
    if value in {'GROUND', 'GND'}:
        return 'GROUND'
    if value in {'POWER', 'PWR'}:
        return 'POWER'
    return value or 'UNSPEC'


def validate_evidence(evidence):
    """验证 ER1 结构化证据；拒绝让残缺判据静默进入热跑。"""
    errors = []
    seen_ids = set()

    def number(value):
        if isinstance(value, bool):
            return None
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return None
        return parsed if math.isfinite(parsed) else None

    def range_errors(value, label, *, nonnegative=False):
        if not isinstance(value, dict):
            return [f'{label} 必须为 object']
        found = {}
        result = []
        for key in ('min', 'max'):
            if key not in value:
                continue
            parsed = number(value[key])
            if parsed is None:
                result.append(f'{label}.{key} 必须为有限数值')
            elif nonnegative and parsed < 0:
                result.append(f'{label}.{key} 不得小于 0')
            else:
                found[key] = parsed
        if 'min' in found and 'max' in found and found['min'] > found['max']:
            result.append(f'{label}.min 不得大于 max')
        return result

    def text_value(value):
        return isinstance(value, str) and bool(value.strip())

    if not isinstance(evidence, dict):
        return ['根对象必须是 JSON object']
    if evidence.get('schema_version') != 1:
        errors.append('schema_version 必须为 1')
    checks = evidence.get('checks')
    if not isinstance(checks, list):
        return errors + ['checks 必须为数组']
    for index, check in enumerate(checks):
        label = f'checks[{index}]'
        if not isinstance(check, dict):
            errors.append(f'{label} 必须为 object')
            continue
        rule = check.get('rule')
        if not isinstance(rule, str) or rule not in HOT_RULE_IDS:
            errors.append(f'{label}.rule 不支持: {rule!r}')
        check_id = check.get('id')
        if not text_value(check_id):
            errors.append(f'{label}.id 缺失')
        elif check_id in seen_ids:
            errors.append(f'{label}.id 重复: {check_id!r}')
        else:
            seen_ids.add(check_id)
        if not text_value(check.get('citation')):
            errors.append(f'{label}.citation 缺失（需文档/版本/页码或表号）')
        kind = check.get('kind')
        kind_by_rule = {
            'Rule-08': {'divider'},
            'Rule-09': {'required_pull', 'required_series'},
            'Rule-12': {'pin_bias'},
            'Rule-14': {'pin_map'},
            'Rule-16': {'strap'},
        }
        expected_kind = kind_by_rule.get(rule, set()) if isinstance(
            rule, str) else set()
        if not isinstance(kind, str) or kind not in expected_kind:
            errors.append(f'{label}.kind={kind!r} 与 {rule} 不匹配')
        if rule == 'Rule-08':
            for key in ('net', 'vref', 'expected'):
                if key not in check:
                    errors.append(f'{label}.{key} 缺失')
            if not text_value(check.get('net')):
                errors.append(f'{label}.net 必须为非空字符串')
            vref = check.get('vref')
            if isinstance(vref, dict):
                if 'typ' not in vref:
                    errors.append(f'{label}.vref.typ 缺失')
                parsed_vref = {}
                for key in ('min', 'typ', 'max'):
                    if key not in vref:
                        continue
                    parsed = number(vref[key])
                    if parsed is None or parsed <= 0:
                        errors.append(f'{label}.vref.{key} 必须为有限正数')
                    else:
                        parsed_vref[key] = parsed
                if all(key in parsed_vref for key in ('min', 'typ', 'max')) and not (
                        parsed_vref['min'] <= parsed_vref['typ']
                        <= parsed_vref['max']):
                    errors.append(f'{label}.vref 必须满足 min <= typ <= max')
            elif number(vref) is None or number(vref) <= 0:
                errors.append(f'{label}.vref 必须为有限正数或 object')
            if not isinstance(check.get('expected'), dict):
                errors.append(f'{label}.expected 必须为 object')
            elif not ({'min', 'max'} & set(check['expected'])):
                errors.append(f'{label}.expected 至少给 min 或 max')
            else:
                errors.extend(range_errors(check['expected'], f'{label}.expected'))
            if 'resistor_tolerance' in check:
                tolerance = number(check['resistor_tolerance'])
                if tolerance is None or not 0 <= tolerance < 1:
                    errors.append(
                        f'{label}.resistor_tolerance 必须在 [0, 1) 内')
        elif rule in ('Rule-09', 'Rule-12', 'Rule-16'):
            if not (text_value(check.get('net'))
                    or text_value(check.get('node'))):
                errors.append(f'{label} 必须给 net 或 node')
            if 'to' in check and not text_value(check.get('to')):
                errors.append(f'{label}.to 必须为非空字符串')
            if rule == 'Rule-09' and kind == 'required_pull' and (
                    check.get('direction') not in ('up', 'down')):
                errors.append(f'{label}.direction 必须为 up/down')
            if rule == 'Rule-12' and check.get('required_default') not in (
                    'high', 'low', 'float'):
                errors.append(f'{label}.required_default 必须为 high/low/float')
            if rule == 'Rule-16' and check.get('required') not in (
                    'high', 'low', 'float'):
                errors.append(f'{label}.required 必须为 high/low/float')
            if rule == 'Rule-09' and 'resistance_ohm' in check:
                errors.extend(range_errors(
                    check['resistance_ohm'], f'{label}.resistance_ohm',
                    nonnegative=True))
            if rule == 'Rule-12' and 'abs_max_v' in check:
                abs_max = number(check['abs_max_v'])
                if abs_max is None or abs_max <= 0:
                    errors.append(f'{label}.abs_max_v 必须为有限正数')
        elif rule == 'Rule-14':
            if (not text_value(check.get('ref'))
                    or not isinstance(check.get('expected'), dict)
                    or not check.get('expected')):
                errors.append(f'{label} 必须给 ref 与 expected pin map')
    return errors


def _volt(s):
    """从名字里推电压：3V3->3.3, 24V->24, 5.0V->5.0, V5P0->5.0；推不出返回 None"""
    if not s:
        return None
    u = s.upper()
    m = re.search(r'(\d{1,3})V(\d)(?![\dA-Z])', u)      # 3V3 / 24V0
    if m:
        return float(m.group(1)) + float(m.group(2)) / 10
    m = re.search(r'(\d{1,3}\.\d)V', u)                 # 5.0V
    if m:
        return float(m.group(1))
    m = re.search(r'(\d{1,3})V(?![\dA-Z])', u)           # 24V / _5V
    if m:
        return float(m.group(1))
    m = re.search(r'V(\d{1,2})P(\d)(?![\d])', u)        # V5P0
    if m:
        return float(m.group(1)) + float(m.group(2)) / 10
    return None


def clamp_volt(blob):
    """钳位器件的 Vz/Vrwm：先试 TVS 型号规则，再退回通用电压推断"""
    m = re.search(r'SM[ABCF]J(\d{1,3}(?:\.\d)?)', blob.upper())   # SMBJ5.0A
    if m:
        return float(m.group(1))
    return _volt(blob)


class Lint:
    def __init__(self, db, log_text='', intent=None, evidence=None):
        self.db = db
        self.intent = intent or {}
        self.evidence = evidence or {}
        self.nets = db['nets']
        self.parts = db['parts']
        self.pinname = db['pinname']
        self.pin2net = db['pin2net']
        self.pintype = db.get('pintype', {})
        self.page = db.get('ref2page', {})
        self.pseudo = set(db.get('pseudo_nets', []))
        self.log = log_text
        self.F = []
        self.passes = []
        self.skipped = []      # 未执行的规则及原因——绝不静默跳过
        self.hot_executed = set()
        self._ends_cache = {}

    # -- helpers ---------------------------------------------------------
    def ends(self, ref):
        if ref not in self._ends_cache:
            self._ends_cache[ref] = sorted(
                {v for k, v in self.pin2net.items() if k.startswith(ref + '.')})
        return self._ends_cache[ref]

    def refs_of(self, net):
        return {x.split('.')[0] for x in self.nets.get(net, [])}

    def target_net(self, check):
        return check.get('net') or self.pin2net.get(check.get('node'))

    def resistor_links(self, net):
        links = []
        seen = set()
        for node in self.nets.get(net, []):
            ref = node.split('.')[0]
            if not ref.startswith('R') or ref in seen:
                continue
            seen.add(ref)
            part = self.parts.get(ref, {})
            if part.get('nc'):
                continue
            ends = self.ends(ref)
            if len(ends) != 2 or net not in ends:
                continue
            parsed = parse_resistor(part.get('value'))
            other = ends[0] if ends[1] == net else ends[1]
            links.append({
                'ref': ref,
                'other': other,
                'ohm': parsed['kohm'] * 1000.0 if parsed else None,
            })
        return links

    def pulls(self, net):
        pulls = []
        for link in self.resistor_links(net):
            other = link['other']
            if other in GNDS:
                pulls.append(dict(link, state='low', voltage=0.0))
            elif RAIL_RE.match(other) or _volt(other) is not None:
                pulls.append(dict(link, state='high', voltage=_volt(other)))
        return pulls

    def add(self, rid, name, detail, ref=None, kind='FINDING', **extra):
        """kind: FINDING=疑似缺陷，逐条排除；CANDIDATE=待 ER1 定夺的优先级清单"""
        item = {'rule': rid, 'name': name, 'detail': detail, 'kind': kind,
                'page': self.page.get(ref, 0) if ref else 0}
        item.update(extra)
        self.F.append(item)

    def record_pass(self, rule, check, detail):
        self.passes.append({
            'rule': rule,
            'check_id': check['id'],
            'detail': detail,
            'citation': check['citation'],
        })

    def driven(self, net, seen=None):
        """Cold heuristic: trace passive links to a possible source, never to a jumper alone.
        Diodes and MOSFETs need direction/operating-state evidence in ER2.
        A positive result suppresses a candidate; it is not electrical PASS.
        """
        seen = set() if seen is None else seen
        if net in seen or net in self.pseudo or len(seen) >= 128:
            return False
        seen.add(net)
        for node in self.nets.get(net, []):
            ref = node.split('.')[0]
            part = self.parts.get(ref, {})
            if part.get('nc'):
                continue
            match = re.match(r'[A-Za-z]+', ref)
            head = match.group().upper() if match else ''
            if head in ('J', 'P', 'CN'):
                # Possible external source only; its role must be proved in ER2.
                return True
            if head in ('U', 'M') and re.match(
                    r'^(VOUT|OUT|SW|VO|LX|VDD_EXT|VREG|\+VO)(?:$|[_\d])',
                    self.pinname.get(node, ''), re.I):
                return True
            parsed = parse_resistor(part.get('value')) if head == 'R' else None
            passive = head in ('L', 'FB', 'F') or (
                parsed is not None and parsed['kohm'] <= 0.001)
            ends = self.ends(ref)
            if passive and len(ends) == 2:
                other = ends[0] if ends[1] == net else ends[1]
                if other not in GNDS and self.driven(other, seen):
                    return True
        return False

    # -- rules -----------------------------------------------------------
    def run(self):
        nets, parts, pinname, pin2net = (
            self.nets, self.parts, self.pinname, self.pin2net)

        # Rule-01 单节点悬空网
        for n, nds in nets.items():
            if n in self.pseudo:
                continue
            if len(nds) == 1:
                ref = nds[0].split('.')[0]
                self.add('Rule-01', '单节点悬空网',
                         f'{n} <- {nds[0]} ({parts.get(ref, {}).get("value", "")})', ref)

        # Rule-02 双胞胎网络名（剔除同族总线/差分对/序号兄弟）
        names = sorted(nets)
        for a, b in zip(names, names[1:]):
            if a == b or difflib.SequenceMatcher(None, a, b).ratio() <= 0.90:
                continue
            if re.sub(r'\d+$', '', a) == re.sub(r'\d+$', '', b):
                continue          # 仅差末位序号 -> 同族
            if re.sub(r'_?[NP]$', '', a) == re.sub(r'_?[NP]$', '', b):
                continue          # 差分对
            if re.fullmatch(r'N\d{6,}', a) or re.fullmatch(r'N\d{6,}', b):
                continue
            self.add('Rule-02', '疑似网络名分裂',
                     f'{a}({len(nets[a])}节点) <-> {b}({len(nets[b])}节点)')

        # Rule-03 自动命名网仅含无源件
        for n, nds in nets.items():
            if not re.fullmatch(r'N\d{6,}', n):
                continue
            rs = self.refs_of(n)
            if not any(r[0] in 'UJMY' for r in rs):
                self.add('Rule-03', '自动命名网仅含无源件', f'{n}: {sorted(rs)}')

        # Rule-04 电源轨无驱动
        for n, nds in nets.items():
            if n in GNDS or n in self.pseudo or not RAIL_RE.match(n):
                continue
            if not self.driven(n):
                self.add('Rule-04', '电源轨疑似无驱动',
                         f'{n} ({len(nds)}节点): {sorted(self.refs_of(n))[:6]}')

        # Rule-05 电源球无驱动 / 无网络
        for node, pn in pinname.items():
            if not re.search(r'(VDD|VCC|AVDD|DVDD|VBAT)', pn, re.I):
                continue
            n = pin2net.get(node)
            ref = node.split('.')[0]
            if n is None:
                self.add('Rule-05', '电源球无网络', f'{node} ({pn})', ref)
            elif n not in self.pseudo and not self.driven(n):
                self.add('Rule-05', '电源球所在轨无驱动', f'{node} ({pn}) <- {n}', ref)

        # Rule-06 VSS 球未入地
        for node, pn in pinname.items():
            if re.match(r'^(VSS|AVSS|DVSS)', pn, re.I):
                n = pin2net.get(node)
                if n not in GNDS:
                    self.add('Rule-06', 'VSS 球未入地',
                             f'{node} ({pn}) <- {n}', node.split('.')[0])

        # Rule-19 PINUSE/ERC：只自动定判无歧义冲突；输入-only 作为候选
        if not self.pintype:
            self.skipped.append(
                ('Rule-19', 'PINUSE/ERC 引脚类型检查', '网表未提供 pintype'))
        else:
            coverage = len(self.pintype) / max(len(self.pin2net), 1)
            if coverage < 1.0:
                self.skipped.append((
                    'Rule-19', 'PINUSE/ERC 引脚类型检查',
                    f'仅部分执行：pintype 覆盖 {len(self.pintype)}/'
                    f'{len(self.pin2net)} = {coverage:.1%}'))
            for net, nodes in nets.items():
                if net in self.pseudo:
                    continue
                typed = [(node, _pin_class(self.pintype.get(node)))
                         for node in nodes if node in self.pintype]
                outputs = [node for node, kind in typed if kind == 'OUT']
                if len(outputs) > 1:
                    self.add(
                        'Rule-19', '多个输出引脚直连',
                        f'{net}: {outputs}；开漏/三态等合法结构须逐条排除')
                if nodes and len(typed) == len(nodes) and typed and all(
                        kind == 'IN' for _, kind in typed):
                    self.add(
                        'Rule-19', '网络仅含输入引脚（确认外部源/漏驱动）',
                        f'{net}: {[node for node, _ in typed]}',
                        kind='CANDIDATE')
                for node, kind in typed:
                    if kind == 'GROUND' and net not in GNDS:
                        self.add(
                            'Rule-19', 'GROUND 类型引脚未接已知地网',
                            f'{node} -> {net}', node.split('.')[0],
                            kind='CANDIDATE')

        # Rule-10 ESD/TVS 挂残网
        for ref, v in parts.items():
            blob = (v.get('part', '') + ' ' + v.get('prim', '')).upper()
            if not re.search(r'ESD|TVS', blob):
                continue
            for node, n in ((k, x) for k, x in pin2net.items()
                            if k.startswith(ref + '.')):
                if n and n not in self.pseudo and len(nets.get(n, [])) < 2:
                    self.add('Rule-10', 'ESD/TVS 挂残网',
                             f'{ref} {node} -> {n} (仅{len(nets.get(n, []))}节点)', ref)

        # Rule-15 "NC" 网络 —— 必须先判别是真短路还是工具伪网络
        for n, nds in nets.items():
            if not re.fullmatch(r'NC[_\-\d]*', n, re.I) or len(nds) <= 1:
                continue
            if n in self.pseudo:
                self.add('Rule-15-INFO', '"NC" 为工具伪网络（非缺陷）',
                         f'{n}: {len(nds)} 个引脚。C_SIGNAL 为裸字面量、无层次路径 '
                         '-> PSTWRITER 的 No-Connect 汇集网，不构成电气短路')
            else:
                self.add('Rule-15', '"NC" 被当作网络名导致短接',
                         f'{n}: {len(nds)} 个引脚被电气短接（该网带层次路径，'
                         '系设计者所画，非工具伪网络）')

        # Rule-18 同基名多轨
        rails = defaultdict(list)
        for n in nets:
            m = re.match(r'((?:VCC|VDD|V)[A-Z0-9]*_?\d+V\d*)', n, re.I)
            if m:
                rails[m.group(1).upper()].append(n)
        for base, grp in rails.items():
            if len(grp) > 1:
                self.add('Rule-18', '同基名多轨（确认非张冠李戴）',
                         f'{base}: {sorted(grp)}')

        # Rule-20 BOM 字段卫生；Rule-17 真闭环由 diff_netlists.py 负责
        for ref, v in parts.items():
            for field in ('part', 'value', 'jedec', 'prim'):
                value = v.get(field, '')
                if isinstance(value, str) and value != value.strip():
                    self.add(
                        'Rule-20', 'BOM/库字段含首尾空白（影响比对）',
                        f'{ref}.{field}: {value!r}', ref)

        # Rule-07 关键器件计数（冷跑·参数化：需第 0 步意图清单）
        expect = (self.intent or {}).get('expect') or {}
        if expect:
            for key, want in expect.items():
                got = sorted(r for r, v in parts.items() if not v.get('nc') and key.upper()
                             in (v.get('part', '') + ' ' + v.get('value', '') + ' '
                                 + v.get('prim', '')).upper())
                if len(got) != want:
                    self.add('Rule-07', '关键器件计数不符',
                             f'{key}: 意图 {want} 实为 {len(got)}'
                             + (f' {got}' if got else ''))
        else:
            self.skipped.append(('Rule-07', '关键器件计数', '未提供 --intent 意图清单'))

        # Rule-12 EN 极性/耐压：网络名或引脚功能名命中；有 ER1 证据时交热跑定判
        covered_rule12_nets = {
            self.target_net(check) for check in self.evidence.get('checks', [])
            if check.get('rule') == 'Rule-12'
        }
        for n, nds in nets.items():
            pin_hit = any(EN_RE.search(pinname.get(node, '')) for node in nds)
            if n in self.pseudo or n in covered_rule12_nets or not (
                    EN_RE.search(n) or pin_hit):
                continue
            pulls = []
            for pull in self.pulls(n):
                label = '上拉' if pull['state'] == 'high' else '下拉'
                voltage = pull['voltage']
                pulls.append(
                    f"{pull['ref']} {label}->{pull['other']}"
                    + (f'({voltage}V)' if voltage is not None else '')
                    + (' [优先]' if voltage and voltage >= EN_PULL_ALERT_V else ''))
            if pulls:
                self.add('Rule-12', 'EN 脚上拉/下拉待核（极性+Abs Max）',
                         f'{n}: {"; ".join(sorted(set(pulls)))}',
                         kind='CANDIDATE')

        # Rule-13 钳位器件直连电源（Vz 可从型号推出即冷跑定判，推不出转候选）
        for ref, v in parts.items():
            if v.get('nc'):
                continue
            blob = (v.get('part', '') + ' ' + v.get('value', '') + ' '
                    + v.get('prim', ''))
            if not CLAMP_RE.search(blob):
                continue
            ends = self.ends(ref)
            if len(ends) != 2 or not any(e in GNDS for e in ends):
                continue
            rail = [e for e in ends if e not in GNDS][0]
            vr = _volt(rail)
            if vr is None:
                continue                      # 轨电压未知，交 ER2 电源树处理
            vz = clamp_volt(blob)
            if vz is None:
                self.add('Rule-13', '钳位器件跨接电源轨（Vz 待查）',
                         f'{ref} ({blob.strip()}) 跨 {rail}({vr}V)-GND，'
                         f'型号推不出 Vz/Vrwm', ref, kind='CANDIDATE')
            elif vz < vr:
                self.add('Rule-13', '钳位器件 Vz 低于所跨电源轨',
                         f'{ref} ({blob.strip()}) Vz/Vrwm≈{vz}V < {rail} 的 {vr}V'
                         f' —— 需核实实际轨压、器件曲线与源阻抗', ref, kind='CANDIDATE')

        # 导出日志：No_connect 被忽略 —— 免费证据，别丢
        if self.log:
            for line in self.log.splitlines():
                if re.search(r'ERROR\s*\(|Aborting Netlisting', line, re.I):
                    self.add('INPUT-EXPORT', '网表导出错误/中止，核实是否为本次有效导出', line)
            ig = re.findall(
                r'"No_connect" property on Pin "([^"]+)" ignored.*?net "([^"]+)"',
                self.log)
            for pin, net in ig:
                actual = pin2net.get(pin)
                tag = '' if actual == net else f'（网表实为 {actual}）'
                self.add('LOG-36038', 'No_connect 属性被忽略并强行连线',
                         f'{pin} -> {net}{tag}', pin.split('.')[0])
        if self.evidence:
            self.run_hot()
        return self.F

    def run_hot(self):
        """执行 ER1 已提供确定判据的规则；未提供的热跑规则继续保持 pending。"""
        for check in self.evidence.get('checks', []):
            rule = check['rule']
            self.hot_executed.add(rule)
            {
                'Rule-08': self._hot_divider,
                'Rule-09': self._hot_required_passive,
                'Rule-12': self._hot_pin_bias,
                'Rule-14': self._hot_pin_map,
                'Rule-16': self._hot_strap,
            }[rule](check)

    @staticmethod
    def _citation(check):
        return f"[{check['id']}] {check['citation']}"

    def _hot_divider(self, check):
        tolerance = float(check.get('resistor_tolerance', 0.01))
        solution = Solver(self.db, default_tol=tolerance).solve_net(check['net'])
        if solution.get('status') != 'ok':
            self.add(
                'Rule-08', '分压网络无法无歧义求解',
                f"{check['net']}: {solution.get('reason')}；{self._citation(check)}",
                kind='CANDIDATE', check_id=check['id'],
                citation=check['citation'])
            return
        assumed = sorted({segment['ref']
                          for branch in solution['branches_up'] + solution['branches_lo']
                          for segment in branch['segments']
                          if segment.get('tolerance_source') == 'default'})
        if assumed and 'resistor_tolerance' not in check:
            self.add('Rule-08', '缺少电阻公差依据，不能验证 WCA',
                     f"未注明公差: {', '.join(assumed)}；{self._citation(check)}",
                     kind='CANDIDATE', check_id=check['id'], citation=check['citation'])
            return
        vref = check['vref']
        if not isinstance(vref, dict) or not all(k in vref for k in ('min', 'typ', 'max')):
            self.add('Rule-08', '缺少基准全角范围，不能验证 WCA',
                     self._citation(check), kind='CANDIDATE',
                     check_id=check['id'], citation=check['citation'])
            return
        if isinstance(vref, dict):
            typ = float(vref['typ'])
            minimum = float(vref.get('min', typ))
            maximum = float(vref.get('max', typ))
        else:
            typ = minimum = maximum = float(vref)
        window = divider_window(solution, typ, minimum, maximum)
        expected = check['expected']
        low = float(expected.get('min', float('-inf')))
        high = float(expected.get('max', float('inf')))
        detail = (
            f"{check['net']}: Vout typ={window['typ']:.6g}V, "
            f"window=[{window['min']:.6g}, {window['max']:.6g}]V, "
            f"要求=[{low:g}, {high:g}]V；{self._citation(check)}")
        if window['min'] < low or window['max'] > high:
            self.add(
                'Rule-08', '分压最坏情况窗口不满足要求', detail,
                kind='FINDING', check_id=check['id'],
                citation=check['citation'], calculation=window)
        else:
            self.record_pass('Rule-08', check, detail)

    @staticmethod
    def _resistance_ok(link, check):
        limits = check.get('resistance_ohm') or {}
        if link['ohm'] is None:
            return not limits
        return (link['ohm'] >= float(limits.get('min', float('-inf')))
                and link['ohm'] <= float(limits.get('max', float('inf'))))

    def _hot_required_passive(self, check):
        net = self.target_net(check)
        if not net or net not in self.nets:
            self.add(
                'Rule-09', '必需无源网络无法定位',
                f"{check.get('node') or check.get('net')}: 无网络；"
                f"{self._citation(check)}",
                check_id=check['id'], citation=check['citation'])
            return
        links = self.resistor_links(net)
        expected_other = check.get('to')
        if check['kind'] == 'required_pull':
            direction = check.get('direction')
            candidates = [
                link for link in links
                if ((direction == 'down' and link['other'] in GNDS)
                    or (direction == 'up' and (
                        RAIL_RE.match(link['other'])
                        or _volt(link['other']) is not None)))
            ]
        else:
            # 明确给出 to 时按该网络精确查找；否则只找信号网间串阻。
            candidates = ([link for link in links if link['other'] == expected_other]
                          if expected_other else [
                              link for link in links
                              if link['other'] not in GNDS
                              and not RAIL_RE.match(link['other'])])
        if expected_other and check['kind'] == 'required_pull':
            candidates = [x for x in candidates if x['other'] == expected_other]
        valid = [x for x in candidates if self._resistance_ok(x, check)]
        detail = (
            f"{net}: 期望 {check['kind']} "
            f"{check.get('direction', '')} -> {expected_other or '*'}；"
            f"候选={candidates}；{self._citation(check)}")
        if not valid:
            self.add(
                'Rule-09', '必需上拉/下拉/串阻缺失或阻值不符', detail,
                check_id=check['id'], citation=check['citation'])
        else:
            self.record_pass('Rule-09', check, detail)

    def _bias_result(self, check, rule):
        net = self.target_net(check)
        if not net or net not in self.nets:
            self.add(
                rule, '目标引脚无网络',
                f"{check.get('node') or check.get('net')}；{self._citation(check)}",
                check_id=check['id'], citation=check['citation'])
            return False
        pulls = self.pulls(net)
        if net in GNDS:
            pulls.append({'ref': 'DIRECT', 'other': net, 'state': 'low', 'voltage': 0.0})
        elif RAIL_RE.match(net) and not pulls:
            pulls.append({'ref': 'DIRECT', 'other': net, 'state': 'high', 'voltage': _volt(net)})
        required = check.get('required_default') or check.get('required')
        states = {p['state'] for p in pulls}
        problems = []
        unknown = []
        if required == 'float':
            node = check.get('node')
            others = [x for x in self.nets.get(net, []) if x != node]
            if others:
                problems.append(f'must float，但同网还有 {others}')
        elif required in {'high', 'low'} and required not in states:
            problems.append(f'要求默认 {required}，实际 pulls={pulls}')
        elif required in {'high', 'low'} and states == {'high', 'low'}:
            unknown.append('同时存在上下拉，需结合阻值与输入阈值判定默认电平')
        abs_max = check.get('abs_max_v')
        if abs_max is not None:
            for pull in pulls:
                if pull['state'] != 'high':
                    continue
                if pull['voltage'] is None:
                    unknown.append(f"{pull['other']} 电压未知")
                elif states == {'high', 'low'}:
                    unknown.append('耐压需用分压后的实际脚压，不能直接比较上拉源轨')
                elif pull['voltage'] > float(abs_max):
                    problems.append(
                        f"{pull['ref']} 上拉 {pull['voltage']}V > Abs Max "
                        f"{float(abs_max):g}V")
        detail = (
            f"{net}: required={required}, pulls={pulls}; "
            f"{self._citation(check)}")
        if problems:
            self.add(
                rule, '引脚默认态/耐压违反 datasheet',
                detail + '; ' + '; '.join(problems),
                ref=(check.get('node') or '').split('.')[0] or None,
                check_id=check['id'], citation=check['citation'])
            return False
        if unknown:
            self.add(
                rule, '引脚默认态/耐压仍需补充判据',
                detail + '; ' + '; '.join(unknown),
                kind='CANDIDATE', check_id=check['id'],
                citation=check['citation'])
            return False
        self.record_pass(rule, check, detail)
        return True

    def _hot_pin_bias(self, check):
        self._bias_result(check, 'Rule-12')

    def _hot_strap(self, check):
        self._bias_result(check, 'Rule-16')

    def _hot_pin_map(self, check):
        ref = check['ref']
        mismatches = []
        for pin, expected in sorted(check['expected'].items()):
            node = f'{ref}.{pin}'
            actual = self.pinname.get(node)
            allowed = expected if isinstance(expected, list) else [expected]
            allowed = [str(x).strip().upper() for x in allowed]
            if actual is None or str(actual).strip().upper() not in allowed:
                mismatches.append(
                    f'{node}: actual={actual!r}, expected={allowed}')
        detail = (
            f"{ref}: checked={len(check['expected'])}, "
            f"mismatches={mismatches}; {self._citation(check)}")
        if mismatches:
            self.add(
                'Rule-14', '符号引脚映射与官方定义不符', detail, ref,
                check_id=check['id'], citation=check['citation'])
        else:
            self.record_pass('Rule-14', check, detail)


def _table(by, keys):
    for k in keys:
        print(f'  {k[0]:12s} {by[k][0]["name"]:32s} {len(by[k]):5d} 条')


def main():
    ap = argparse.ArgumentParser(description='AC0 Automated Check（输出为疑似清单）')
    ap.add_argument('db', help='parse_netlist.py 产出的 db.json')
    ap.add_argument('--log', help='netlist.log（导出日志，含 No_connect 等免费证据）')
    ap.add_argument('--intent',
                    help='第 0 步意图清单 JSON（适用性发现与 Rule-07 所需）')
    ap.add_argument('--evidence',
                    help='ER1 结构化 datasheet 证据 JSON；提供后执行对应热跑规则')
    ap.add_argument('--review-mode', choices=('first', 'revision'),
                    help='首审或复审；缺省取 intent.review_mode/first')
    ap.add_argument('--old-db', help='复审旧版 db.json（用于执行计划准备度）')
    ap.add_argument('--claims', help='历史评审断言 JSON（用于执行计划准备度）')
    ap.add_argument('--plan-json', help='单独写出 AC0 逐项执行计划 JSON')
    ap.add_argument('--json', help='把完整命中写入 JSON')
    a = ap.parse_args()

    db = json.load(io.open(a.db, encoding='utf-8'))
    log = io.open(a.log, encoding='utf-8', errors='replace').read() if a.log else ''
    intent = json.load(io.open(a.intent, encoding='utf-8')) if a.intent else None
    evidence = json.load(io.open(a.evidence, encoding='utf-8')) if a.evidence else None
    intent_errors = validate_intent(intent)
    if intent_errors:
        sys.exit('[FATAL] intent.json 无效:\n  - ' + '\n  - '.join(intent_errors))
    if a.evidence:
        errors = validate_evidence(evidence)
        if errors:
            sys.exit('[FATAL] evidence.json 无效:\n  - ' + '\n  - '.join(errors))
    for label, path in (('--old-db', a.old_db), ('--claims', a.claims)):
        if path and not os.path.isfile(path):
            sys.exit(f'[FATAL] {label} 文件不存在: {path}')

    review_plan = build_review_plan(
        db, intent, evidence, a.review_mode,
        old_db_available=bool(a.old_db),
        claims_available=bool(a.claims))
    summary = review_plan['summary']
    print('=== AC0 检查适用性与执行计划 ===')
    print(f"  checks={summary['checks_total']}  "
          f"applicability={summary['applicability']}")
    print(f"  readiness={summary['readiness']}  "
          f"handoff_required={summary['handoff_required']}")
    if a.plan_json:
        json.dump(review_plan, io.open(a.plan_json, 'w', encoding='utf-8'),
                  ensure_ascii=False, indent=2)
        print(f'  -> {a.plan_json}')

    lint = Lint(db, log, intent, evidence)
    F = lint.run()

    # 分组键含 kind——同一条规则可同时产出 FINDING 与 CANDIDATE（如 Rule-13）
    by = defaultdict(list)
    for f in F:
        by[(f['rule'], f['kind'])].append(f)
    # Rule-* 在前、其余（导出日志等）在后
    order = sorted(by, key=lambda k: (not k[0].startswith('Rule-'), k[0]))
    find = [k for k in order if k[1] == 'FINDING']
    cand = [k for k in order if k[1] == 'CANDIDATE']

    print('=== AC0 Automated Check 汇总（疑似清单，非判决）===')
    _table(by, find)
    n_find = sum(len(by[k]) for k in find)
    print(f'  {"合计":45s} {n_find:5d} 条')
    print('\n  逐条排除后才是发现项——由执行 agent 完成，排除依据须留痕。合法结构举例：'
          'Bob-Smith 终端、补偿网络、DNP 选项、\n  被删外设的 SoC 引出脚、工具伪网络。')

    if cand:
        print('\n=== CANDIDATE：待 ER1 定夺（决定优先读哪几份 datasheet）===')
        _table(by, cand)

    if lint.passes:
        print('\n=== 自动验证通过（证据留痕）===')
        passed = defaultdict(list)
        for item in lint.passes:
            passed[item['rule']].append(item)
        for rule in sorted(passed):
            print(f'  {rule:12s} {len(passed[rule]):5d} 条')

    # 未执行的规则必须报出来——扫出 0 条与根本没扫，绝不能长得一样
    print('\n=== 本趟未执行（0 条 ≠ 通过）===')
    for rid, name, why in lint.skipped:
        print(f'  {rid:12s} {name:32s} {why}')
    for rid, name in HOT_RULES:
        if rid not in lint.hot_executed:
            print(f'  {rid:12s} {name:32s} 未提供对应 ER1 结构化证据')

    if a.json:
        pending = [list(item) for item in HOT_RULES
                   if item[0] not in lint.hot_executed]
        json.dump({'findings': F,
                   'passes': lint.passes,
                   'skipped': [list(s) for s in lint.skipped],
                   'hot_executed': sorted(lint.hot_executed),
                   'hot_pending': pending,
                   'hot_uncovered_instances': [x['id'] for x in review_plan['checks']
                       if x.get('rule') in HOT_RULE_IDS and x['readiness'] != 'READY'],
                   'review_plan': review_plan,
                   'coverage': {
                       'pintype_available': bool(db.get('pintype')),
                       'pintype_nodes': len(db.get('pintype', {})),
                       'pin_nodes': len(db.get('pin2net', {})),
                       'pintype_ratio': (
                           len(db.get('pintype', {}))
                           / max(len(db.get('pin2net', {})), 1)),
                       'evidence_checks': len((evidence or {}).get('checks', [])),
                       'finding_count': len([x for x in F
                                             if x['kind'] == 'FINDING']),
                       'candidate_count': len([x for x in F
                                               if x['kind'] == 'CANDIDATE']),
                   }},
                  io.open(a.json, 'w', encoding='utf-8'),
                  ensure_ascii=False, indent=1)
        print(f'\n  -> {a.json}')


if __name__ == '__main__':
    main()
