#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AC0 Automated Check（自动检查）——机械、可穷举的规则全量扫描

用法:
    python3 lint.py db.json [--log netlist.log] [--intent intent.json]
    python3 lint.py db.json --evidence evidence.json \
        [--datasheet-audit datasheet-audit.json] \
        [--plan-json review-plan.json] [--json out.json]

**输出是疑似清单，不是判决。** 合法结构（Bob-Smith 终端、补偿网络、
DNP 选项、被删外设的引出脚、工具伪网络）由执行 agent 逐条排除。
规则依赖命名和已知图结构；零命中不表示检查完整或电气通过。

电源追踪只把实际输出脚或已声明的外部供电节点当作来源候选。
电感/磁珠/保险丝/0R 是导通边；二极管和 MOS 需要对应状态的有向模型。
冷跑路径和网络名均不是电压、载流能力或上电时序的 PASS 证据。
"""
import argparse
import difflib
import io
import json
import os
import re
import sys
from collections import defaultdict

from audit_datasheets import validate_datasheet_audit
from checkers import PowerTree, REGISTRY, REGISTRY_BY_ID, registry_hot_rules
from checkers.netgraph import NetGraph
from electrical_contract import db_fingerprint, load_json, readiness_gaps, validate_evidence
from fractions import Fraction
from itertools import product
from plan_review import build_review_plan, validate_intent
from solve_dividers import Solver, divider_window, linear_feedback_window, parse_resistor

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


def hot_rules():
    """内建热跑规则加注册表热跑规则；未执行的照样要列出来。"""
    return HOT_RULES + [(rule, name) for rule, (name, _)
                        in sorted(registry_hot_rules().items())]


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
    def __init__(self, db, log_text='', intent=None, evidence=None, datasheet_audit=None):
        self.db = db
        self.db_sha256 = db_fingerprint(db)
        self.datasheet_audit = datasheet_audit
        self.results = []
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
        self.graph = NetGraph(db)
        self.powertree = PowerTree(self.graph, self.intent)
        self.inventories = {checker.id: checker.build(db, self.intent) for checker in REGISTRY}

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
                'tol': parsed['tol'] if parsed else None,
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
        if extra.get('check_id'):
            evidence = next((x for x in self.evidence.get('checks', [])
                             if x.get('id') == extra['check_id']), {})
            item['state'] = (evidence.get('basis') or {}).get('state')
            if 'vref_binding' in evidence:
                item['vref_binding'] = evidence['vref_binding']
            item['review_result'] = 'INSUFFICIENT' if kind == 'CANDIDATE' else 'FAIL'
            self.results.append(item)
        self.F.append(item)

    def record_pass(self, rule, check, detail, scope=None, calculation=None):
        item = {
            'rule': rule, 'check_id': check['id'], 'detail': detail,
            'citation': check['citation'], 'review_result': 'PASS',
            'scope': scope or check['kind'], 'state': check.get('basis', {}).get('state'),
        }
        if calculation is not None:
            item['calculation'] = calculation
        if 'vref_binding' in check:
            item['vref_binding'] = check['vref_binding']
        self.passes.append(item)
        self.results.append(item)

    def power_path(self, net):
        """Return a source-to-load candidate path, never a rail signoff."""
        return self.powertree.source_of(net)

    def driven(self, net):
        return self.power_path(net) is not None

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
            self.skipped.append(('Rule-07', '关键器件计数', '意图中缺少器件计数目标 intent.expect'))

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

        # Rule-13: model/name-derived voltage is a retrieval hint, not breakdown data.
        for ref, value in parts.items():
            if value.get('nc'):
                continue
            blob = ' '.join(str(value.get(k, '')) for k in ('part', 'value', 'prim'))
            if not CLAMP_RE.search(blob):
                continue
            ends = self.ends(ref)
            if len(ends) != 2 or not any(e in GNDS for e in ends):
                continue
            rails = [e for e in ends if e not in GNDS]
            if not rails:
                continue
            rail = rails[0]
            self.add('Rule-13', '防护器件工作/击穿/钳位窗口待核',
                     f'{ref} ({blob.strip()}) 跨 {rail}；轨名提示={_volt(rail)}V，'
                     f'型号提示={clamp_volt(blob)}V。分别查 VRWM、VBR@IT、VC@Ipp、'
                     '波形、温度与能量配合；型号不能证明导通或烧毁。',
                     ref, kind='CANDIDATE')

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
        for checker in REGISTRY:
            inventory = self.inventories.get(checker.id)
            if inventory is not None:
                checker.cold_findings(self, inventory)

        if self.evidence:
            self.run_hot()
        return self.F

    def run_hot(self):
        """执行 ER1 已提供确定判据的规则；未提供的热跑规则继续保持 pending。"""
        for check in self.evidence.get('checks', []):
            rule = check['rule']
            gaps = readiness_gaps(self.db, check, self.datasheet_audit, self.db_sha256)
            if gaps:
                self.add(rule, '热跑证据/模型尚未就绪', '; '.join(gaps),
                         kind='CANDIDATE', check_id=check['id'],
                         citation=check['citation'], required_inputs=gaps)
                continue
            self.hot_executed.add(rule)
            builtin = {
                'Rule-08': self._hot_divider,
                'Rule-09': self._hot_required_passive,
                'Rule-12': self._hot_pin_bias,
                'Rule-14': self._hot_pin_map,
                'Rule-16': self._hot_strap,
            }
            if rule in builtin:
                builtin[rule](check)
            else:
                registry_hot_rules()[rule][1].hot_check(self, check)

    @staticmethod
    def _citation(check):
        return f"[{check['id']}] {check['citation']}"

    def _hot_divider(self, check):
        tolerance = check.get('resistor_tolerance')
        model = check.get('divider_model') or {}
        try:
            if len({check['net'], model.get('source_net'), model.get('reference_net')}) != 3:
                raise ValueError('FB、源端和参考地必须不同')
            solver = Solver(self.db, default_tol=tolerance, model=model)
            solution = solver.solve_net(check['net'])
            vref = check['vref']
            typ, minimum, maximum = (float(vref[k]) for k in ('typ', 'min', 'max'))
            bias = model['bias_current_a']
            if solution.get('status') == 'ok':
                window = divider_window(solution, typ, minimum, maximum)
                corners = [v * (1 + up / lo) + current * up * 1000
                           for v, up, lo, current in product(
                               (minimum, maximum), (solution['up']['min'], solution['up']['max']),
                               (solution['lo']['min'], solution['lo']['max']),
                               (bias['min'], bias['max']))]
                window.update(min=min(corners), max=max(corners))
                window['typ_note'] = 'typ 为零偏置标称值；min/max 包含偏置电流'
            else:
                network = solver.linear_network(check['net'])
                # Newly supported networks require bindings for every resistor
                # and ignored load, not only devices directly on the FB net.
                expanded = dict(check, depends_on=sorted(
                    set(check.get('depends_on') or []) | set(network['required_refs'])))
                gaps = readiness_gaps(self.db, expanded, self.datasheet_audit, self.db_sha256)
                if gaps:
                    raise ValueError('节点网络参数来源未就绪：' + '; '.join(gaps))
                window = linear_feedback_window(network, vref, bias)
        except ValueError as error:
            self.add(
                'Rule-08', '分压网络无法无歧义求解',
                f"{check['net']}: {error}；{self._citation(check)}",
                kind='CANDIDATE', check_id=check['id'], citation=check['citation'])
            return
        expected = check['expected']
        low = float(expected.get('min', float('-inf')))
        high = float(expected.get('max', float('inf')))
        detail = (
            f"{check['net']}: Vout typ={window['typ']:.6g}V, "
            f"window=[{window['min']:.6g}, {window['max']:.6g}]V, "
            f"要求=[{low:g}, {high:g}]V；{self._citation(check)}")
        if 'bounds_exact' in window:
            outside = (('min' in expected and Fraction(window['bounds_exact']['min']) < Fraction(str(expected['min'])))
                       or ('max' in expected and Fraction(window['bounds_exact']['max']) > Fraction(str(expected['max']))))
        else:
            outside = window['min'] < low or window['max'] > high
        if outside:
            self.add(
                'Rule-08', '分压最坏情况窗口不满足要求', detail,
                kind='FINDING', check_id=check['id'],
                citation=check['citation'], calculation=window)
        else:
            self.record_pass('Rule-08', check, detail,
                             scope='所给反馈/监控阈值的静态分压窗口；工作余量/启动/稳定性另查', calculation=window)

    def _hot_required_passive(self, check):
        net = self.target_net(check)
        links = self.resistor_links(net)
        destination = check.get('to')
        if destination:
            candidates = [x for x in links if x['other'] == destination]
        else:
            candidates = [x for x in links if (
                x['other'] in GNDS if check.get('direction') == 'down'
                else RAIL_RE.match(x['other']))]
        limits = check.get('resistance_ohm') or {}
        detail = f"{net}: {check['kind']} -> {destination or '*'}; candidates={candidates}"
        if not candidates:
            self.add('Rule-09', '未找到要求的直接电阻连接', detail,
                     kind='CANDIDATE' if links else 'FINDING',
                     check_id=check['id'], citation=check['citation'])
            return
        # A set of parallel pull-ups is one equivalent load on the bus driver.
        if len({x['other'] for x in candidates}) != 1:
            self.add('Rule-09', '电阻连接多个电源域，需节点分析', detail,
                     kind='CANDIDATE', check_id=check['id'], citation=check['citation'])
            return
        if limits and any(x not in candidates for x in links):
            self.add('Rule-09', '存在其他电阻支路，等效模型不完整', detail,
                     kind='CANDIDATE', check_id=check['id'], citation=check['citation'])
            return
        if not limits:
            self.record_pass('Rule-09', check, detail,
                             scope='仅电阻装配及两端连接；电平、时序和阻值窗口未判定')
            return
        if any(x['ohm'] is None or x['tol'] is None or x['ohm'] <= 0 for x in candidates):
            self.add('Rule-09', '阻值/公差不完整或含短路支路', detail,
                     kind='CANDIDATE', check_id=check['id'], citation=check['citation'])
            return
        equivalent = {key: 1 / sum(1 / (x['ohm'] * (1 + sign * x['tol']))
                                    for x in candidates)
                      for key, sign in (('min', -1), ('typ', 0), ('max', 1))}
        detail += f'; 等效阻值窗口={equivalent} ohm'
        if (equivalent['min'] < float(limits.get('min', float('-inf')))
                or equivalent['max'] > float(limits.get('max', float('inf')))):
            self.add('Rule-09', '等效电阻窗口不满足要求', detail,
                     check_id=check['id'], citation=check['citation'], calculation=equivalent)
        else:
            self.record_pass('Rule-09', check, detail,
                             scope='指定两网间直接电阻的等效值；不含总线电平/上升时间',
                             calculation=equivalent)

    def _bias_result(self, check, rule):
        net = self.target_net(check)
        required = check.get('required_default') or check.get('required')
        if not net or net not in self.nets:
            self.add(rule, '目标引脚无网络', str(check.get('node') or net),
                     check_id=check['id'], citation=check['citation'])
            return False
        if required == 'float':
            if not check.get('node'):
                self.add(rule, 'must-float 需要精确引脚', net, kind='CANDIDATE',
                         check_id=check['id'], citation=check['citation'])
                return False
            others = [x for x in self.nets[net] if x != check['node']
                      and not self.parts.get(x.split('.')[0], {}).get('nc')]
            if others:
                self.add(rule, '要求无外部连接的引脚仍有连接', f'{net}: {others}',
                         check_id=check['id'], citation=check['citation'])
                return False
            self.record_pass(rule, check, f'{net}: 无已装配的外部连接',
                             scope='仅外部连接；不推断内部上拉/电压')
            return True
        analysis = check['voltage_analysis']
        voltage = analysis['voltage_v']
        threshold = check['vih_min_v'] if required == 'high' else check['vil_max_v']
        compliant = voltage['min'] >= threshold if required == 'high' else voltage['max'] <= threshold
        issues = []
        if not compliant:
            issues.append('电压窗口不满足保证逻辑门限；不等于每颗样品必然失效')
        if check.get('abs_min_v') is not None and voltage['min'] < check['abs_min_v']:
            issues.append('引脚电压窗口低于给定负向绝对最大额定')
        if check.get('abs_max_v') is not None and voltage['max'] > float(check['abs_max_v']):
            issues.append('引脚电压窗口超过给定正向绝对最大额定')
        detail = (f"{net}: {required}, voltage={voltage}, threshold={threshold}V; "
                  f"sample={analysis['sample_window_s']}s; {analysis['calculation']}")
        if issues:
            self.add(rule, '引脚保证条件不满足', detail + '; ' + '; '.join(issues),
                     check_id=check['id'], citation=check['citation'], calculation=analysis)
            return False
        self.record_pass(rule, check, detail,
                         scope='仅所述状态/采样窗口及给定逻辑门限/电压额定；注入电流另查',
                         calculation=analysis)
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
            self.record_pass('Rule-14', check, detail, scope='仅 expected 列出的引脚及名称')


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
    ap.add_argument(
        '--datasheet-audit',
        help='audit_datasheets.py 产出的逐物料覆盖审计 JSON')
    ap.add_argument('--review-mode', choices=('first', 'revision'),
                    help='首审或复审；缺省取 intent.review_mode/first')
    ap.add_argument('--old-db', help='实际读取复审旧版 db.json 并生成变化清单')
    ap.add_argument('--old-plan', help='上一设计版本最终计划；与本版 merge-plan 分开')
    ap.add_argument('--revision-impact-json', help='另存改版影响与必需复验清单')
    ap.add_argument('--claims', help='历史评审断言 JSON（用于执行计划准备度）')
    ap.add_argument('--plan-json', help='单独写出 AC0 逐项执行计划 JSON')
    ap.add_argument('--i2c-topology-json', help='另存本次计划中的 I2C 连接覆盖清单；不是电气判决')
    ap.add_argument('--decoupling-json', help='另存本次计划中的去耦清单；不是电气判决')
    ap.add_argument('--checker-json', action='append', default=[], metavar='ID=PATH',
                    help='另存指定检查器的清单，可重复；不是电气判决')
    ap.add_argument('--merge-plan', help='合入同版旧计划的补查项；结果仍在独立台账中复核')
    ap.add_argument('--json', help='把完整命中写入 JSON')
    a = ap.parse_args()

    try:
        db = load_json(a.db)
        log = io.open(a.log, encoding='utf-8', errors='replace').read() if a.log else ''
        intent = load_json(a.intent) if a.intent else None
        evidence = load_json(a.evidence) if a.evidence else None
        datasheet_audit = load_json(a.datasheet_audit) if a.datasheet_audit else None
        previous_plan = load_json(a.merge_plan) if a.merge_plan else None
        old_db = load_json(a.old_db) if a.old_db else None
        old_plan = load_json(a.old_plan) if a.old_plan else None
    except (OSError, ValueError) as error:
        ap.error(str(error))
    if a.intent and not isinstance(intent, dict):
        ap.error('explicit intent.json root must be an object')
    for supplied, value, label in ((a.old_db, old_db, '--old-db'), (a.old_plan, old_plan, '--old-plan')):
        if supplied and not isinstance(value, dict):
            ap.error(label + ' JSON root must be an object')
    intent_errors = validate_intent(intent)
    if intent_errors:
        sys.exit('[FATAL] intent.json 无效:\n  - ' + '\n  - '.join(intent_errors))
    if a.evidence:
        errors = validate_evidence(evidence)
        if errors:
            sys.exit('[FATAL] evidence.json 无效:\n  - ' + '\n  - '.join(errors))
    if datasheet_audit is not None:
        errors = validate_datasheet_audit(datasheet_audit, db)
        if errors:
            sys.exit('[FATAL] datasheet-audit.json 无效:\n  - '
                     + '\n  - '.join(errors))
    for label, path in (('--old-db', a.old_db), ('--claims', a.claims)):
        if path and not os.path.isfile(path):
            sys.exit(f'[FATAL] {label} 文件不存在: {path}')

    try:
        review_plan = build_review_plan(
            db, intent, evidence, a.review_mode,
            old_db_available=bool(a.old_db),
            claims_available=bool(a.claims),
            datasheet_audit=datasheet_audit, previous_plan=previous_plan,
            old_db=old_db, old_plan=old_plan)
    except ValueError as error:
        sys.exit(f'[FATAL] {error}')
    if a.revision_impact_json:
        if 'revision_impact' not in review_plan:
            ap.error('--revision-impact-json requires revision mode')
        with open(a.revision_impact_json, 'w', encoding='utf-8') as stream:
            json.dump(review_plan['revision_impact'], stream, ensure_ascii=False, indent=2, allow_nan=False)
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
    # 两个旧开关是通用导出的别名，走同一条路径，不另写一份导出代码
    exports = (['i2c_topology=' + a.i2c_topology_json] if a.i2c_topology_json else []) \
        + (['decoupling=' + a.decoupling_json] if a.decoupling_json else []) + a.checker_json
    for spec in exports:
        checker_id, _, path = spec.partition('=')
        checker = REGISTRY_BY_ID.get(checker_id)
        if checker is None or checker.plan_key not in review_plan or not path:
            raise SystemExit('未知检查器或缺少输出路径: ' + spec)
        with open(path, 'w', encoding='utf-8') as stream:
            json.dump(review_plan[checker.plan_key], stream, ensure_ascii=False,
                      indent=2, allow_nan=False)

    lint = Lint(db, log, intent, evidence, datasheet_audit)
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
    for rid, name in hot_rules():
        if rid not in lint.hot_executed:
            print(f'  {rid:12s} {name:32s} 未提供对应 ER1 结构化证据')

    if a.json:
        pending = [list(item) for item in hot_rules()
                   if item[0] not in lint.hot_executed]
        json.dump({'findings': F,
                   'check_results': lint.results,
                   'passes': lint.passes,
                   'skipped': [list(s) for s in lint.skipped],
                   'hot_executed': sorted(lint.hot_executed),
                   'hot_pending': pending,
                   'hot_uncovered_instances': [x['id'] for x in review_plan['checks']
                       if x.get('rule') in {r for r, _ in hot_rules()} and x['readiness'] != 'READY'],
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
