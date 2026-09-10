#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ER4-WCA：反馈/监控分压自动求解。

特性：
- 穷举电阻路径，不再静默采用“找到的第一条路径”。
- 正确处理串联臂，以及互不共享电阻的并联支路。
- 从 VALUE 提取单颗电阻公差；缺失时不生成保证窗口，--res-tol 仅接受有证据的显式输入。
- 输出 Vref 与电阻公差叠加后的 min/typ/max。

用法:
    python3 solve_dividers.py db.json --vfb U1=0.815 U2=0.6
    python3 solve_dividers.py db.json --net FB_NET --vfb 0.62
    python3 solve_dividers.py db.json --vfb U1=0.8 --vfb-tol 0.02 --json wca.json
"""
import argparse
import io
import json
import math
import re
import sys

GNDS = {'GND', 'PGND', 'AGND', 'DGND', 'EGND'}
RAIL_RE = re.compile(
    r'^(VCC|VBAT|VDD|VOUT|AVDD|DVDD|VIN|VBUS|3V3|5V|1V|2V|0V)', re.I)
FB_NAMES = {
    'FB', 'ADJ', 'VFB', 'FBX', 'VSENSE', 'VOSNS', 'VOUT_SENSE',
}


def parse_resistor(value, default_tol=None):
    """解析电阻值，返回 {'kohm', 'tol'}；支持 4K7、2.49K、0R、1M0。"""
    raw = str(value or '').strip()
    upper = raw.upper().replace('Ω', 'R')
    leading_unit = re.match(r'^([RKM])(\d+)', upper)
    embedded = re.match(r'^(\d+)([RKM])(\d+)', upper)
    if leading_unit:
        number = float(f'0.{leading_unit.group(2)}')
        unit = leading_unit.group(1)
        end = leading_unit.end()
    elif embedded:
        number = float(f'{embedded.group(1)}.{embedded.group(3)}')
        unit = embedded.group(2)
        end = embedded.end()
    else:
        normal = re.match(r'^(\d+(?:\.\d+)?)\s*([RKM]?)', upper)
        if not normal:
            return None
        number = float(normal.group(1))
        unit = normal.group(2)
        end = normal.end()
    remainder = upper[end:]
    remainder = re.sub(r'^OHMS?', '', remainder)
    if remainder and remainder[0] not in '/ ±+':
        return None
    if not math.isfinite(number):
        return None
    scale = {'R': 0.001, '': 0.001, 'K': 1.0, 'M': 1000.0}[unit]
    tol_match = re.search(r'(?:/|±|\+/-|\s)(\d+(?:\.\d+)?)\s*%', raw)
    tol = float(tol_match.group(1)) / 100.0 if tol_match else (float(default_tol) if default_tol is not None else None)
    if tol is not None and (not math.isfinite(tol) or tol < 0 or tol >= 1):
        return None
    return {'kohm': number * scale, 'tol': tol}


def r_kohm(value):
    """兼容旧调用：只返回标称 kΩ。"""
    parsed = parse_resistor(value)
    return parsed['kohm'] if parsed else None


def rail_hint(name):
    """从轨名推标称电压：VBAT_4G1_3V8 -> 3.8；VCC_3V3 -> 3.3。"""
    matches = list(re.finditer(r'(\d+)V(\d*)', name or ''))
    if not matches:
        return None
    whole, frac = matches[-1].group(1), matches[-1].group(2)
    return float(f'{whole}.{frac}') if frac else float(whole)


def _parallel(values):
    if not values:
        return None
    if any(v <= 0 for v in values):
        return 0.0
    return 1.0 / sum(1.0 / v for v in values)


def _branch_summary(branch):
    nominal = sum(s['kohm'] for s in branch['segments'])
    minimum = sum(s['kohm'] * (1.0 - (s['tol'] or 0.0)) for s in branch['segments'])
    maximum = sum(s['kohm'] * (1.0 + (s['tol'] or 0.0)) for s in branch['segments'])
    return {'nominal': nominal, 'min': minimum, 'max': maximum}


def _combine_branches(branches):
    """合并互不共享电阻的并联支路；共享电阻时拒绝猜测。"""
    if not branches:
        return None, 'missing branch'
    used = set()
    summaries = []
    for branch in branches:
        refs = {s['ref'] for s in branch['segments']}
        if used & refs:
            return None, 'branches share resistor(s); requires circuit analysis'
        used |= refs
        summaries.append(_branch_summary(branch))
    nominal = _parallel([x['nominal'] for x in summaries])
    minimum = _parallel([x['min'] for x in summaries])
    maximum = _parallel([x['max'] for x in summaries])
    if nominal is None or minimum is None or maximum is None or minimum <= 0:
        return None, 'zero/invalid equivalent resistance'
    if any(s['tol'] is None for branch in branches for s in branch['segments']):
        minimum = maximum = None
    return {'nominal': nominal, 'min': minimum, 'max': maximum}, None


def divider_window(solution, vref_typ, vref_min=None, vref_max=None):
    """按最坏方向叠加 Vref 与上下臂公差。"""
    if solution.get('status') != 'ok':
        raise ValueError(solution.get('reason', 'divider unresolved'))
    if vref_min is None or vref_max is None:
        raise ValueError('Vref min/max 缺失；不能生成 WCA 窗口')
    if not solution.get('tolerances_complete'):
        raise ValueError('电阻公差缺失；不能生成 WCA 窗口')
    up, lo = solution['up'], solution['lo']
    return {
        'typ': vref_typ * (1.0 + up['nominal'] / lo['nominal']),
        'min': vref_min * (1.0 + up['min'] / lo['max']),
        'max': vref_max * (1.0 + up['max'] / lo['min']),
    }


class Solver:
    def __init__(self, db, default_tol=None, max_depth=8, model=None):
        self.nets = db['nets']
        self.parts = db['parts']
        self.pinname = db.get('pinname', {})
        self.pin2net = db['pin2net']
        self.page = db.get('ref2page', {})
        self.default_tol = default_tol
        self.max_depth = max_depth
        self._ends = {}
        self.model = model or {}
        self.issues = []
        self.start_net = None

    def ends(self, ref):
        if ref not in self._ends:
            self._ends[ref] = sorted(
                {v for k, v in self.pin2net.items() if k.startswith(ref + '.')})
        return self._ends[ref]

    def _walk(self, net, seen_nets, seen_refs, segments, depth):
        # 起始反馈网本身可能叫 VOUT_SENSE，不能在尚未跨过电阻时误当电源端。
        reference = self.model.get('reference_net')
        source = self.model.get('source_net')
        if segments and ((reference and net == reference) or (not reference and net in GNDS)):
            return [{'terminal': net, 'is_reference': True, 'segments': segments}]
        if segments and ((source and net == source) or (not source and RAIL_RE.match(net))):
            return [{'terminal': net, 'segments': segments}]
        if segments and ((reference and net in GNDS and net != reference)
                         or (source and RAIL_RE.match(net) and net != source)):
            self.issues.append(f'{net}: 未声明的电源/参考边界')
            return []
        if depth >= self.max_depth or net in seen_nets:
            self.issues.append(f'{net}: 递归截断或电阻回路，拓扑不完整')
            return []
        out = []
        next_seen_nets = seen_nets | {net}
        for node in self.nets.get(net, []):
            ref = node.split('.')[0]
            if ref in seen_refs:
                continue
            part = self.parts.get(ref, {})
            if part.get('nc'):
                continue
            if not ref.startswith('R'):
                ignored = self.model.get('ignored_nodes', {})
                # Exploratory mode can locate FB arms, but has no signoff authority.
                exploratory_fb = (not self.model and net == self.start_net
                                  and str(self.pinname.get(node, '')).upper() in FB_NAMES)
                allowed_ignored = (re.match(r'^(U|M|C)\d', ref)
                                   and ignored.get(node))
                if not allowed_ignored and not exploratory_fb:
                    self.issues.append(f'{node}: 未建模的非电阻支路/输入负载')
                continue
            ends = self.ends(ref)
            if len(ends) != 2 or net not in ends:
                self.issues.append(f'{ref}: 电阻不是可解析的两端连接')
                continue
            parsed = parse_resistor(part.get('value'), self.default_tol)
            if not parsed:
                self.issues.append(f'{ref}: 电阻值无法解析')
                continue
            other = ends[0] if ends[1] == net else ends[1]
            segment = {'ref': ref, 'kohm': parsed['kohm'], 'tol': parsed['tol']}
            out.extend(self._walk(
                other, next_seen_nets, seen_refs | {ref},
                segments + [segment], depth + 1))
        return out

    @staticmethod
    def _dedupe(paths):
        found = {}
        for path in paths:
            key = (path['terminal'], tuple(s['ref'] for s in path['segments']))
            found[key] = path
        return list(found.values())

    def solve_net(self, fbnet):
        """求解反馈节点；无法无歧义归并时返回 status=ambiguous。"""
        self.issues = []
        self.start_net = fbnet
        paths = self._dedupe(self._walk(fbnet, set(), set(), [], 0))
        if self.issues:
            return {'status': 'ambiguous', 'reason': '; '.join(sorted(set(self.issues))),
                    'paths': paths}
        ground = [p for p in paths if p.get('is_reference') and p['segments']]
        upper = [p for p in paths if not p.get('is_reference') and p['segments']]
        if not ground or not upper:
            return {
                'status': 'ambiguous',
                'reason': '未找到完整的上臂和下臂',
                'paths': paths,
            }
        references = {p['terminal'] for p in ground}
        if len(references) != 1:
            return {'status': 'ambiguous', 'reason': '下臂跨多个地/参考域，禁止按名称合并', 'paths': paths}
        sources = sorted({p['terminal'] for p in upper})
        if len(sources) != 1:
            return {
                'status': 'ambiguous',
                'reason': f'上臂连接多个电源轨: {sources}',
                'paths': paths,
            }
        if ({s['ref'] for p in upper for s in p['segments']}
                & {s['ref'] for p in ground for s in p['segments']}):
            return {'status': 'ambiguous', 'reason': '上下臂共享电阻，需要节点分析', 'paths': paths}
        up, up_error = _combine_branches(upper)
        lo, lo_error = _combine_branches(ground)
        if up_error or lo_error:
            return {
                'status': 'ambiguous',
                'reason': up_error or lo_error,
                'paths': paths,
            }
        return {
            'status': 'ok',
            'src': sources[0],
            'reference_net': next(iter(references)),
            'tolerances_complete': all(s['tol'] is not None for p in paths for s in p['segments']),
            'model_bound': bool(self.model.get('source_net') and self.model.get('reference_net')),
            'up': up,
            'lo': lo,
            'r_up': up['nominal'],
            'r_lo': lo['nominal'],
            'branches_up': upper,
            'branches_lo': ground,
            'path_up': self._display_paths(upper),
            'path_lo': self._display_paths(ground),
        }

    @staticmethod
    def _display_paths(paths):
        rendered = []
        for path in paths:
            rendered.append(' + '.join(
                f"{s['ref']}({s['kohm']:.6g}k/tol={s['tol']})"
                for s in path['segments']))
        return rendered

    def find_fb_nets(self):
        out = []
        for node, pin_name in self.pinname.items():
            if str(pin_name).upper() in FB_NAMES:
                out.append((node.split('.')[0], self.pin2net.get(node)))
        return sorted(set(out))


def fmt_path(paths):
    return ' || '.join(paths) if paths else '-'


def _parse_vfb(items):
    per_ref, single = {}, None
    for item in items:
        if '=' in item:
            key, value = item.split('=', 1)
            per_ref[key] = float(value)
        else:
            single = float(item)
    return per_ref, single


def _row(ref, fbnet, result, vref, vref_tol):
    row = {'ref': ref, 'fbnet': fbnet, 'solution': result}
    if result.get('status') != 'ok':
        row['status'] = result.get('status')
        row['reason'] = result.get('reason')
        return row
    row['status'] = 'ok'
    row['factor'] = 1.0 + result['r_up'] / result['r_lo']
    row['rail_hint'] = rail_hint(result['src'])
    row['review_result'] = 'INSUFFICIENT'
    row['scope'] = 'exploratory calculation; source/load model requires ER4 verification'
    row['missing_inputs'] = ['CLI 未绑定源/负载及原始证据；需 ER4 复核']
    if vref is not None:
        row['voltage_nominal'] = vref * row['factor']
    else:
        row['missing_inputs'].append('未指定 Vref 标称值')
    if vref_tol is None:
        row['missing_inputs'].append('未指定 Vref 保证公差')
    if not result.get('tolerances_complete'):
        row['missing_inputs'].append('电阻公差不完整')
    if vref is not None and vref_tol is not None and result.get('tolerances_complete'):
        row['vref'] = {
            'typ': vref,
            'min': vref * (1.0 - vref_tol),
            'max': vref * (1.0 + vref_tol),
        }
        row['voltage'] = divider_window(
            result, vref, row['vref']['min'], row['vref']['max'])
    return row


def main():
    parser = argparse.ArgumentParser(
        description='反馈/监控分压自动求解（串联、并联、公差窗口）')
    parser.add_argument('db')
    parser.add_argument('--vfb', nargs='*', default=[],
                        help='REF=电压，如 U1=0.815；单网时可直接写 0.62')
    parser.add_argument('--default-vfb', type=float, default=None)
    parser.add_argument('--vfb-tol', type=float, default=None,
                        help='Vref 相对公差，例如 0.02 表示 ±2%%')
    parser.add_argument('--res-tol', type=float, default=None,
                        help='仅在有物料证据时显式给出公差；缺省不假定' )
    parser.add_argument('--net', help='只解指定反馈网')
    parser.add_argument('--tol', type=float, default=0.06,
                        help='实算窗口相对轨名标称值的允许偏差，默认 6%%')
    parser.add_argument('--json', help='写出结构化 WCA 结果')
    args = parser.parse_args()

    if any(x is not None and not 0 <= x < 1 for x in (args.res_tol, args.vfb_tol)):
        sys.exit('[FATAL] --res-tol/--vfb-tol 必须在 [0, 1) 内')

    db = json.load(io.open(args.db, encoding='utf-8'))
    solver = Solver(db, default_tol=args.res_tol)
    per_ref, single = _parse_vfb(args.vfb)

    if args.net and per_ref and single is None:
        sys.exit('[FATAL] --net 单网模式需用 --vfb 0.8 形式；不接受 REF=0.8')
    if any(v is not None and (not math.isfinite(v) or v <= 0)
           for v in list(per_ref.values()) + [single, args.default_vfb]):
        sys.exit('[FATAL] Vref 必须为有限正数')
    targets = [('MANUAL', args.net)] if args.net else solver.find_fb_nets()
    rows = []
    for ref, fbnet in targets:
        if not fbnet:
            continue
        result = solver.solve_net(fbnet)
        vref = single if args.net else per_ref.get(ref, args.default_vfb)
        rows.append(_row(ref, fbnet, result, vref, args.vfb_tol))

    for row in rows:
        if row['status'] != 'ok':
            print(f"  [未判定] {row['ref']} {row['fbnet']}: {row['reason']}")
            continue
        result = row['solution']
        print(f"{row['ref']} {row['fbnet']} -> {result['src']}")
        print(f"  上臂 {result['r_up']:.6g}k = {fmt_path(result['path_up'])}")
        print(f"  下臂 {result['r_lo']:.6g}k = {fmt_path(result['path_lo'])}")
        print(f"  系数 1+R1/R2 = {row['factor']:.6f}")
        if 'voltage_nominal' in row and 'voltage' not in row:
            print(f"  仅标称估算 Vout={row['voltage_nominal']:.6f} V；无保证窗口")
        print('  [INSUFFICIENT] ' + '; '.join(row.get('missing_inputs', [])))
        if 'voltage' in row:
            voltage = row['voltage']
            print(f"  Vout = {voltage['typ']:.6f} V "
                  f"[{voltage['min']:.6f}, {voltage['max']:.6f}] V")
            hint = row.get('rail_hint')
            if hint:
                ok = (voltage['min'] >= hint * (1.0 - args.tol)
                      and voltage['max'] <= hint * (1.0 + args.tol))
                print(f"  轨名交叉校验 {hint:g} V: {'OK' if ok else '不符'}")

    payload = {
        'schema_version': 1,
        'default_resistor_tolerance': args.res_tol,
        'vref_tolerance': args.vfb_tol,
        'results': rows,
    }
    if args.json:
        json.dump(payload, io.open(args.json, 'w', encoding='utf-8'),
                  ensure_ascii=False, indent=2)
        print(f'  -> {args.json}')

    if args.net and (not rows or rows[0]['status'] != 'ok'):
        sys.exit(2)


if __name__ == '__main__':
    main()
