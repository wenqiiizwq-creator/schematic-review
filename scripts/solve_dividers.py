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
from fractions import Fraction
from itertools import product

GNDS = {'GND', 'PGND', 'AGND', 'DGND', 'EGND'}
RAIL_RE = re.compile(
    r'^(VCC|VBAT|VDD|VOUT|AVDD|DVDD|VIN|VBUS|3V3|5V|1V|2V|0V)', re.I)
LINEAR_MAX_RESISTORS = 20
LINEAR_MAX_VARIABLE_RESISTORS = 10
LINEAR_MAX_NETS = 12

FB_NAMES = {
    'FB', 'ADJ', 'VFB', 'FBX', 'VSENSE', 'VOSNS', 'VOUT_SENSE',
}


def parse_resistor(value, default_tol=None, exact=False):
    """解析 R/K/M 与公差；exact=True 另保留原始十进制的 Ω/公差分数。"""
    raw = str(value or '').strip()
    upper = raw.upper().replace('Ω', 'R')
    leading_unit = re.match(r'^([RKM])(\d+)', upper)
    embedded = re.match(r'^(\d+)([RKM])(\d+)', upper)
    if leading_unit:
        decimal_number = f'0.{leading_unit.group(2)}'
        number = float(decimal_number)
        unit = leading_unit.group(1)
        end = leading_unit.end()
    elif embedded:
        decimal_number = f'{embedded.group(1)}.{embedded.group(3)}'
        number = float(decimal_number)
        unit = embedded.group(2)
        end = embedded.end()
    else:
        normal = re.match(r'^(\d+(?:\.\d+)?)\s*([RKM]?)', upper)
        if not normal:
            return None
        decimal_number = normal.group(1)
        number = float(decimal_number)
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
    result = {'kohm': number * scale, 'tol': tol}
    if exact:
        result['ohm_exact'] = str(Fraction(decimal_number) * {'R': 1, '': 1, 'K': 1000, 'M': 1000000}[unit])
        result['tol_exact'] = (str(Fraction(tol_match.group(1)) / 100) if tol_match
                               else str(Fraction(str(default_tol))) if default_tol is not None else None)
    return result


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



def _exact(value):
    """Preserve the stated decimal parameter, not a binary rounding artifact."""
    return value if isinstance(value, Fraction) else Fraction(str(value))


def _linear_coefficients(network, ohms):
    """Exact Dirichlet LDL^T solve: Vfb = gain*Vsource - Rth*Ibias.

    Positive resistor stamps produce a symmetric positive-definite grounded
    matrix. Rational factorization avoids accepting a small residual with a
    large forward error near a decision threshold. It does not import the
    benchmark oracle or use its inverse-feedback Gaussian elimination.
    """
    source, reference, fbnet = (network[k] for k in ('source_net', 'reference_net', 'fbnet'))
    unknown = sorted(set(network['nets']) - {source, reference})
    index = {net: i for i, net in enumerate(unknown)}
    n = len(unknown)
    ohms = list(map(_exact, ohms))
    if not ohms or any(r <= 0 for r in ohms):
        raise ValueError('角点阻值必须为有限正数')
    if max(ohms) / min(ohms) > 10 ** 10:
        raise ValueError('电阻动态范围超过节点求解资源边界')
    zero = Fraction(0)
    matrix = [[zero] * n for _ in range(n)]
    drive = [zero] * n
    for resistor, resistance in zip(network['resistors'], ohms):
        a, b = resistor['nets']
        conductance = 1 / resistance
        for node, other in ((a, b), (b, a)):
            if node not in index:
                continue
            i = index[node]
            matrix[i][i] += conductance
            if other in index:
                matrix[i][index[other]] -= conductance
            elif other == source:
                drive[i] += conductance
    lower = [[zero] * n for _ in range(n)]
    diagonal = [zero] * n
    for i in range(n):
        lower[i][i] = Fraction(1)
        for j in range(i):
            lower[i][j] = (matrix[i][j] - sum(lower[i][k] * diagonal[k] * lower[j][k]
                                            for k in range(j))) / diagonal[j]
        diagonal[i] = matrix[i][i] - sum(lower[i][k] ** 2 * diagonal[k] for k in range(i))
        if diagonal[i] <= 0:
            raise ValueError('节点矩阵奇异或未接参考边界')

    def solve(rhs):
        y, x = [zero] * n, [zero] * n
        for i in range(n):
            y[i] = rhs[i] - sum(lower[i][j] * y[j] for j in range(i))
        for i in range(n - 1, -1, -1):
            x[i] = y[i] / diagonal[i] - sum(lower[j][i] * x[j] for j in range(i + 1, n))
        if any(sum(matrix[i][j] * x[j] for j in range(n)) != rhs[i] for i in range(n)):
            raise ValueError('节点方程精确残差不为零')
        return x

    response = solve(drive)
    injection = [zero] * n
    injection[index[fbnet]] = Fraction(1)
    impedance = solve(injection)
    gain, rth = response[index[fbnet]], impedance[index[fbnet]]
    if (not Fraction(1, 10 ** 10) < gain <= 1 or rth <= 0
            or any(v < 0 or v > 1 for v in response)):
        raise ValueError('反馈节点缺少可靠源控制或无源模型无效')
    return gain, rth


def _finite_float(value):
    try:
        converted = float(value)
    except OverflowError as error:
        raise ValueError('节点输出超出有限数值范围') from error
    if not math.isfinite(converted):
        raise ValueError('节点输出超出有限数值范围')
    return converted


def _outward_float(value, upper):
    converted = _finite_float(value)
    if (upper and Fraction.from_float(converted) < value
            or not upper and Fraction.from_float(converted) > value):
        converted = math.nextafter(converted, math.inf if upper else -math.inf)
    if not math.isfinite(converted):
        raise ValueError('节点输出不能转换为有限外包络')
    return converted


def linear_feedback_window(network, vref, bias):
    """Bound a fully specified positive-resistance network at all box vertices.

    For each conductance separately, the inverse-feedback output is a
    linear-fractional function (a rank-one network update). With a connected
    positive network its denominator does not cross zero; each coordinate
    extremum is at an endpoint. Vref and FB bias enter affinely. Thus all
    independent resistor/Vref/bias endpoint combinations bound this DC model.
    Resource limits reject rather than sample/truncate. Exact fraction bounds
    decide acceptance; floating report bounds are rounded outward.
    """
    if (not isinstance(vref, dict) or not isinstance(bias, dict)
            or not all(isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)
                       for x in (vref.get('min'), vref.get('typ'), vref.get('max'),
                                 bias.get('min'), bias.get('max')))
            or not 0 < vref['min'] <= vref['typ'] <= vref['max']
            or bias['min'] > bias['max']):
        raise ValueError('Vref 或输入偏置缺少有限保证范围')
    resistors = network['resistors']
    if not 1 <= len(resistors) <= LINEAR_MAX_RESISTORS or len(network['nets']) > LINEAR_MAX_NETS:
        raise ValueError('节点求解限于 20 颗电阻和 12 个网络，超限不抽样放行')
    if len({r['ref'] for r in resistors}) != len(resistors):
        raise ValueError('节点模型重复计入同一电阻')
    if any(r['tol'] is None for r in resistors):
        raise ValueError('电阻公差缺失；不能生成节点分析保证窗口')
    options = [sorted({_exact(r.get('ohm_exact', r['ohm'])) * (1 - _exact(r.get('tol_exact', r['tol']))),
                       _exact(r.get('ohm_exact', r['ohm'])) * (1 + _exact(r.get('tol_exact', r['tol'])))})
               for r in resistors]
    if sum(len(values) > 1 for values in options) > LINEAR_MAX_VARIABLE_RESISTORS:
        raise ValueError('超过 10 颗非零公差电阻，禁止抽样代替全角')
    extrema, exact_bounds = {'min': None, 'max': None}, {}
    count = 0
    for ohms in product(*options):
        gain, rth = _linear_coefficients(network, ohms)
        for voltage, current in product(sorted({_exact(vref['min']), _exact(vref['max'])}),
                                        sorted({_exact(bias['min']), _exact(bias['max'])})):
            value = (voltage + rth * current) / gain
            count += 1
            corner = {'value': _finite_float(value), 'value_exact': str(value),
                      'vref_v': float(voltage), 'bias_current_a': float(current),
                      'resistance_ohm': {r['ref']: float(ohm) for r, ohm in zip(resistors, ohms)},
                      'resistance_ohm_exact': {r['ref']: str(ohm) for r, ohm in zip(resistors, ohms)}}
            for key, better in (('min', lambda a, b: a < b), ('max', lambda a, b: a > b)):
                if key not in exact_bounds or better(value, exact_bounds[key]):
                    exact_bounds[key], extrema[key] = value, corner
    gain, _ = _linear_coefficients(network, [r.get('ohm_exact', r['ohm']) for r in resistors])
    return {'typ': _finite_float(_exact(vref['typ']) / gain),
            'min': _outward_float(exact_bounds['min'], False), 'max': _outward_float(exact_bounds['max'], True),
            'bounds_exact': {k: str(v) for k, v in exact_bounds.items()},
            'method': 'linear-nodal-dc', 'arithmetic': 'exact-rational-LDLt',
            'corner_count': count, 'extreme_corners': extrema,
            'resistors': resistors, 'source_net': network['source_net'],
            'reference_net': network['reference_net'],
            'typ_note': 'typ 为零偏置标称值；min/max 包含偏置电流且向外舍入，判据使用精确分数',
            'scope': '有限正电阻网络、单一理想源/参考地、FB 集总输入偏置；非线性/启动/稳定性另查'}


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
        self.db_pseudo = db.get('pseudo_nets', [])

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


    def linear_network(self, fbnet):
        """Collect all internal branches once; explicit source/reference stop traversal."""
        source, reference = (self.model.get(k) for k in ('source_net', 'reference_net'))
        if (not all(isinstance(net, str) and net.strip() for net in (source, reference, fbnet))
                or len({source, reference, fbnet}) != 3):
            raise ValueError('FB、源端和参考地必须为三个不同的显式网络')
        if any(net not in self.nets for net in (source, reference, fbnet)):
            raise ValueError('声明的 FB/源/参考网络不存在')
        pseudo = set(self.db_pseudo)
        pending, visited, resistors, required = [fbnet], set(), {}, set()
        ignored = self.model.get('ignored_nodes', {})
        while pending:
            net = pending.pop()
            if net in visited:
                continue
            visited.add(net)
            if len(visited) > LINEAR_MAX_NETS:
                raise ValueError('节点求解超过 12 个网络，禁止截断')
            if net in pseudo:
                raise ValueError(f'{net}: 伪网不能进入节点模型')
            if net in (source, reference):
                continue
            if net != fbnet and (net in GNDS or RAIL_RE.match(net)):
                raise ValueError(f'{net}: 未声明的电源/参考边界')
            for node in self.nets[net]:
                ref = node.split('.')[0]
                part = self.parts.get(ref)
                if part is None or self.pin2net.get(node) != net:
                    raise ValueError(f'{node}: 元件/网络索引不一致')
                if part.get('nc'):
                    continue
                if not re.match(r'^R[0-9]', ref):
                    allowed = (ignored.get(node) and
                               (re.match(r'^C[0-9]', ref) or
                                (net == fbnet and re.match(r'^(U|M)[0-9]', ref))))
                    if not allowed:
                        raise ValueError(f'{node}: 未建模的非电阻支路/输入负载')
                    required.add(ref)
                    continue
                if ref in resistors:
                    continue
                pins = [pin for pin in self.pin2net if pin.startswith(ref + '.')]
                ends = self.ends(ref)
                if (len(pins) != 2 or len(ends) != 2 or net not in ends
                        or any(pin not in self.nets.get(self.pin2net[pin], []) for pin in pins)):
                    raise ValueError(f'{ref}: 电阻必须恰有两个有效物理脚和两个端点')
                value = parse_resistor(part.get('value'), self.default_tol, exact=True)
                if not value or not math.isfinite(value['kohm'] * 1000) or value['kohm'] <= 0:
                    raise ValueError(f'{ref}: 只支持可解析的严格正电阻')
                resistors[ref] = {'ref': ref, 'nets': ends, 'ohm': value['kohm'] * 1000, 'tol': value['tol'],
                                  'ohm_exact': value['ohm_exact'], 'tol_exact': value['tol_exact']}
                required.add(ref)
                if len(resistors) > LINEAR_MAX_RESISTORS:
                    raise ValueError('节点求解超过 20 颗电阻，禁止截断')
                pending.extend(ends)
        if source not in visited or reference not in visited:
            raise ValueError('反馈网络未连接到声明的源和参考地')
        return {'fbnet': fbnet, 'source_net': source, 'reference_net': reference,
                'nets': sorted(visited), 'resistors': [resistors[r] for r in sorted(resistors)],
                'required_refs': sorted(required)}

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
