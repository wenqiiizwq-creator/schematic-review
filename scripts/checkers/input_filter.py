#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""开关稳压器输入滤波与阻尼检查器。

识别开关稳压器输入网上的串联电感/磁珠、两侧电容与阻尼支路，登记逐状态清单。
负输入阻抗判据用 ER1 提供的保证值热跑，并声明其为一阶判据：全频阻抗裕量、
阶跃响应与温度角仍需仿真或实测。
"""
import re

from decoupling import parse_capacitance

from . import hotmath
from . import inventory as inv
from . import netgraph as ng
from . import powertree
from . import states as state_lib
from .base import Checker
from .planutil import handoff, slug

VIN_PIN_RE = re.compile(r'^(VIN|PVIN|VINA|VCC_IN|VDD_IN|VBUS_IN)\d*$', re.I)
SERIES_KINDS = {ng.INDUCTOR, ng.FERRITE}
BULK_RE = re.compile(r'ELEC|ALUM|TANT|POLYMER|OSCON|EEE|EEU|TPS[A-Z]|铝电解|钽', re.I)
MAX_INPUTS = 128


def validate_input_filter_intent(intent, db=None):
    """校验可选的 input_filters 配置段。"""
    return state_lib.section_errors(
        intent, db, 'input_filters', item_field='converters', item_key='converter',
        fields=('id', 'ref', 'input_net', 'citation'), net_fields=('input_net',))


class _Scan:
    def __init__(self, db, state, declared, excluded):
        self.graph = ng.NetGraph(db, fitted=set(state['fitted']))
        self.state = state
        self.declared = declared
        self.excluded = excluded
        self.grounds = {net for net in self.graph.nets if ng.is_ground(net)}

    def _pin_nets(self, ref, pattern):
        return sorted(set(self.graph.named_pins(ref, pattern).values()))

    def _caps_to_ground(self, net):
        found = []
        for ground in sorted(self.grounds):
            for ref in self.graph.between(net, ground, {ng.CAPACITOR}):
                value = self.graph.parts.get(ref, {}).get('value')
                found.append({'ref': ref, 'to': ground, 'farads': parse_capacitance(value),
                              'bulk_candidate': bool(BULK_RE.search(str(value or '')
                                                     + str(self.graph.parts.get(ref, {}).get('part') or '')))})
        return sorted(found, key=lambda item: item['ref'])

    def _series(self, net):
        """输入网上游的串联滤波元件；多于两端的共模扼流需另行声明。"""
        found, gaps = [], []
        for ref, other in self.graph.neighbors(net, SERIES_KINDS):
            found.append({'ref': ref, 'kind': self.graph.kind(ref), 'upstream_net': other})
        for ref in self.graph.refs_on(net):
            if self.graph.kind(ref) in SERIES_KINDS and not self.graph.terminals(ref):
                gaps.append('series-element-topology:' + ref)
        return sorted(found, key=lambda item: item['ref']), sorted(set(gaps))

    def _dampers(self, net):
        """串联 RC 阻尼支路：输入网 → 电阻 → 中间节点 → 电容 → 地。"""
        found = []
        for ref, middle in self.graph.neighbors(net, {ng.RESISTOR}):
            for ground in sorted(self.grounds):
                for cap in self.graph.between(middle, ground, {ng.CAPACITOR}):
                    found.append({'ref': ref + '+' + cap, 'resistor': ref, 'capacitor': cap,
                                  'farads': parse_capacitance(
                                      self.graph.parts.get(cap, {}).get('value'))})
        return sorted(found, key=lambda item: item['ref'])

    def converters(self):
        graph, found = self.graph, []
        for ref in sorted(graph.parts):
            if ref in self.excluded or not graph.is_fitted(ref) or graph.kind(ref) is not ng.IC:
                continue
            input_nets = self._pin_nets(ref, VIN_PIN_RE)
            if not input_nets or not self._pin_nets(ref, powertree.SWITCH_NODE_PIN_RE):
                continue
            for net in input_nets:
                series, gaps = self._series(net)
                caps = self._caps_to_ground(net)
                dampers = self._dampers(net)
                upstream = {item['upstream_net'] for item in series}
                found.append({
                    'id': slug(ref + '-' + net),
                    'ref': ref,
                    'basis': 'declared' if ref in self.declared else 'topology',
                    'input_net': net,
                    'series_elements': series,
                    'upstream_caps': sorted(
                        (cap for other in sorted(upstream) for cap in self._caps_to_ground(other)),
                        key=lambda item: item['ref']),
                    'input_caps': caps,
                    'dampers': dampers,
                    'bulk_candidates': [cap['ref'] for cap in caps if cap['bulk_candidate']],
                    'gaps': sorted(set(list(self.state['gaps']) + gaps)),
                })
        return sorted(found, key=lambda item: item['id'])[:MAX_INPUTS]


def build_inventory(db, intent=None):
    """生成逐状态的输入滤波清单。"""
    cfg = (intent or {}).get('input_filters') if isinstance(intent, dict) else None
    declared = state_lib.declared_refs(cfg, 'converters')
    excluded = state_lib.excluded_refs(cfg)
    gaps = [] if cfg else ['intent.input_filters: 未声明变换器输入与装配状态']
    return inv.build(db, cfg, 'converters',
                     lambda state: _Scan(db, state, declared, excluded).converters(), gaps)


class InputFilterChecker(Checker):
    id = 'input_filter'
    title = '开关电源输入滤波与阻尼'
    plan_key = 'input_filter'
    version_key = 'input_filter_version'
    version = 1
    intent_key = 'input_filters'
    cold_rules = {'IF-01': '输入串联滤波无阻尼元件'}
    hot_rules = {'IF-10': '负输入阻抗与阻尼一阶判据'}
    evidence_kinds = {'IF-10': {'input_filter_damping'}}

    missing_inventory_message = 'input filter checks require their inventory'
    inventory_type_message = 'input filter inventory must be an object'
    requires_db_message = 'input filter inventory validation requires --db'
    stale_message = 'input filter inventory/binding is stale or modified'
    invalid_message = 'invalid input filter inventory: '

    def incomplete_message(self, key):
        return key + ': incomplete input filter planned coverage'

    def changed_message(self, key):
        return key + ': input filter generated criterion/object changed'

    def validate_intent(self, intent, db):
        return validate_input_filter_intent(intent, db)

    def build(self, db, intent):
        return build_inventory(db, intent)

    def plan(self, planner, inventory):
        for state, item in inv.walk(inventory, 'converters'):
            obj = {'ref': item['ref'], 'net': item['input_net'], 'state': state['id'],
                   'input_filter': item['id'], 'input_filter_digest': inventory['digest']}
            gaps = item['gaps']
            ready = planner.evidence_ready('IF-10', obj)
            check = planner.add_check(
                'input-filter-damping-' + item['id'], dict(obj),
                '按最低输入电压与最大输入功率求负输入阻抗，核体电容 ESR 与滤波电感的一阶阻尼'
                '判据及体电容/输入电容比值（比值须由项目规定，不得默认）',
                'ER4', 'AC0-HOT', readiness='READY' if ready else 'WAITING_EVIDENCE',
                required_inputs=sorted(set(gaps + ([] if ready else [
                    'evidence: IF-10 输入电压/功率、ESR、电感与电容保证值',
                    'intent: 体电容与输入电容比值的项目规定']))),
                trigger=['input-filter:' + item['id']], rule='IF-10')
            check['domain'] = 'INPUT_FILTER'
            check['inventory_gaps'] = gaps
            check = planner.add_check(
                'input-filter-attenuation-' + item['id'], dict(obj),
                '核滤波元件的饱和电流、直流压降、温升与所需衰减量；'
                '截止频率与开关频率的关系按 EMC 需求确认，不以有磁珠即判合格',
                'ER3', 'Expert Review', readiness='WAITING_EVIDENCE',
                required_inputs=sorted(set(gaps + [
                    'datasheet:滤波元件阻抗/饱和曲线', 'requirements:传导发射限值与裕量'])),
                trigger=['input-filter:' + item['id']],
                handoff=handoff({'required': True, 'receivers': ['PCB Layout', 'EMC'],
                                 'constraint': '滤波元件与输入电容回路最短，输入回路与开关回路分离',
                                 'verification': '传导发射实测与输入阻抗测量'}, 'APPLICABLE'))
            check['domain'] = 'INPUT_FILTER'
            check['inventory_gaps'] = gaps

    def cold_findings(self, lint, inventory):
        for state, item in inv.walk(inventory, 'converters'):
            if not item['series_elements'] or item['dampers'] or item['bulk_candidates']:
                continue
            lint.add('IF-01', self.cold_rules['IF-01'],
                     '%s（状态 %s）：输入网 %s 经 %s 串联滤波，但未见 RC 阻尼支路或带 ESR 的'
                     '体电容；负输入阻抗与滤波谐振的配合需按保证值核算'
                     % (item['ref'], state['id'], item['input_net'],
                        '、'.join(x['ref'] for x in item['series_elements'])),
                     item['ref'], kind='CANDIDATE')

    def hot_check(self, lint, check):
        vin_min = hotmath.number(check['vin_min_v'])
        p_max = hotmath.number(check['pin_max_w'])
        esr = hotmath.span(check['esr_bulk_ohm'])
        c_bulk = hotmath.span(check['c_bulk_f'])
        c_in = hotmath.span(check['c_in_f'])
        inductance = hotmath.span(check['l_filter_h'])
        ratio_min = hotmath.number(check['c_bulk_ratio_min'])
        r_in = vin_min * vin_min / p_max
        esr_floor = inductance[1] / (c_bulk[0] * r_in)
        ratio = c_bulk[0] / c_in[1]
        problems = []
        if esr[1] >= r_in:
            problems.append('体电容 ESR 上限 %s 不低于负输入阻抗 %s'
                            % (hotmath.fmt(esr[1], 'Ω'), hotmath.fmt(r_in, 'Ω')))
        if esr[0] <= esr_floor:
            problems.append('体电容 ESR 下限 %s 不高于阻尼下界 L/(C·|Rin|)=%s'
                            % (hotmath.fmt(esr[0], 'Ω'), hotmath.fmt(esr_floor, 'Ω')))
        if ratio < ratio_min:
            problems.append('体电容/输入电容比值 %s 低于项目规定 %s'
                            % (hotmath.fmt(ratio), hotmath.fmt(ratio_min)))
        calculation = {'r_in_ohm': r_in, 'esr_floor_ohm': esr_floor,
                       'esr_bulk_ohm': {'min': esr[0], 'max': esr[1]},
                       'c_bulk_over_c_in': ratio, 'c_bulk_ratio_min': ratio_min}
        detail = '%s: |Rin|=%s，ESR=[%s, %s]Ω，阻尼下界=%s，C_bulk/C_in=%s（规定≥%s）；%s' % (
            check.get('net') or check.get('ref'), hotmath.fmt(r_in, 'Ω'),
            hotmath.fmt(esr[0]), hotmath.fmt(esr[1]), hotmath.fmt(esr_floor, 'Ω'),
            hotmath.fmt(ratio), hotmath.fmt(ratio_min), lint._citation(check))
        if problems:
            lint.add('IF-10', '输入滤波阻尼一阶判据不满足', detail + '；' + '；'.join(problems),
                     check.get('ref'), check_id=check['id'], citation=check['citation'],
                     calculation=calculation)
        else:
            lint.record_pass('IF-10', check, detail,
                             scope='所给保证值下的一阶阻尼判据；全频输入阻抗裕量、阶跃与温度角未判定',
                             calculation=calculation)

    def evidence_errors(self, check, label):
        errors = hotmath.structure_errors(
            check, label, spans=('esr_bulk_ohm', 'c_bulk_f', 'c_in_f', 'l_filter_h'),
            numbers=('vin_min_v', 'pin_max_w', 'c_bulk_ratio_min'))
        for field in ('vin_min_v', 'pin_max_w'):
            value = hotmath.number(check.get(field))
            if value is not None and value <= 0:
                errors.append('%s.%s 必须为正数' % (label, field))
        for field in ('esr_bulk_ohm', 'c_bulk_f', 'c_in_f', 'l_filter_h'):
            bounds = hotmath.span(check.get(field))
            if bounds is not None and bounds[0] <= 0:
                errors.append('%s.%s.min 必须为正数' % (label, field))
        return errors

    def model_gaps(self, check):
        return hotmath.missing_gaps(
            check, spans=('esr_bulk_ohm', 'c_bulk_f', 'c_in_f', 'l_filter_h'),
            numbers=('vin_min_v', 'pin_max_w', 'c_bulk_ratio_min'))

    def rule_instances(self, rule, inventory):
        return sorted({item['id'] for _, item in inv.walk(inventory, 'converters')})

    def binds(self, item):
        obj = item.get('object')
        return isinstance(obj, dict) and bool(obj.get('input_filter'))

    def object_errors(self, key, obj, inventory):
        known = {item['id'] for _, item in inv.walk(inventory, 'converters')}
        if (obj.get('input_filter') not in known
                or obj.get('input_filter_digest') != inventory['digest']):
            return [key + ': stale input filter object binding']
        return []

    def pass_blockers(self, key, planned, generated, inventory):
        if generated and generated.get('inventory_gaps'):
            return [key + ': input filter gaps must be resolved in a regenerated plan before PASS']
        return []
