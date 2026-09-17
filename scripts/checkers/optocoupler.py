#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""光耦隔离传输检查器。

识别光耦的 LED 回路（限流电阻与驱动源）与输出侧（上拉电阻与参考），登记逐状态
清单。电流传输比与寿命衰减后的驱动能力按 ER1 提供的保证值热跑；寿命衰减系数
必须来自项目规定，缺规定即 INSUFFICIENT。隔离耐压与爬电距离不在本检查器。
"""
from . import hotmath
from . import inventory as inv
from . import netgraph as ng
from . import powertree
from . import states as state_lib
from .base import Checker
from .planutil import handoff, slug

MAX_OPTOS = 128


def validate_optocoupler_intent(intent, db=None):
    """校验可选的 optocouplers 配置段。"""
    return state_lib.section_errors(
        intent, db, 'optocouplers', item_field='optocouplers', item_key='optocoupler',
        fields=('id', 'ref', 'drive', 'citation'))


class _Scan:
    def __init__(self, db, state, declared, excluded):
        self.graph = ng.NetGraph(db, fitted=set(state['fitted']))
        self.state = state
        self.declared = declared
        self.excluded = excluded
        self.grounds = {net for net in self.graph.nets if ng.is_ground(net)}
        self.tree = powertree.PowerTree(self.graph)

    def _series_resistors(self, nets):
        found = []
        for net in nets:
            for ref, other in self.graph.neighbors(net, {ng.RESISTOR}):
                found.append({'ref': ref, 'from': net, 'to': other,
                              'value': self.graph.parts.get(ref, {}).get('value')})
        return sorted(found, key=lambda item: item['ref'])

    def _drivers(self, nets, opto_ref):
        found = []
        for net in nets:
            for node in self.graph.nodes_on(net):
                ref = node.partition('.')[0]
                if ref == opto_ref or not self.graph.is_fitted(ref):
                    continue
                if self.graph.kind(ref) in (ng.IC, ng.CONNECTOR, ng.MOSFET, ng.BJT):
                    found.append({'node': node, 'kind': self.graph.kind(ref)})
        return sorted(found, key=lambda item: item['node'])

    def _pullups(self, net):
        return sorted({ref for ref, other in self.graph.neighbors(net, {ng.RESISTOR})
                       if self.tree.is_rail(other)})

    def optocouplers(self):
        graph, found = self.graph, []
        for ref in sorted(graph.parts):
            if ref in self.excluded or not graph.is_fitted(ref):
                continue
            if graph.kind(ref) is not ng.OPTO:
                continue
            nodes = {role: graph.node_named(ref, role)
                     for role in ('led_anode', 'led_cathode', 'out_collector', 'out_emitter')}
            gaps = list(self.state['gaps'])
            if not nodes['led_anode'] or not nodes['led_cathode']:
                gaps.append('pin-roles:' + ref)
            nets = {role: graph.pin2net.get(node) if node else None
                    for role, node in nodes.items()}
            led_nets = [net for net in (nets['led_anode'], nets['led_cathode']) if net]
            collector = nets['out_collector']
            found.append({
                'id': slug(ref),
                'ref': ref,
                'basis': 'declared' if ref in self.declared else 'topology',
                'drive': self.declared.get(ref, 'unknown'),
                'led_nets': led_nets,
                'led_resistors': self._series_resistors(led_nets),
                'led_drivers': self._drivers(led_nets, ref),
                'collector_net': collector,
                'emitter_net': nets['out_emitter'],
                'output_pullups': self._pullups(collector) if collector else [],
                'gaps': sorted(set(gaps)),
            })
        return sorted(found, key=lambda item: item['id'])[:MAX_OPTOS]


def build_inventory(db, intent=None):
    """生成逐状态的光耦清单。"""
    cfg = (intent or {}).get('optocouplers') if isinstance(intent, dict) else None
    declared = {}
    for item in (cfg or {}).get('optocouplers', []) if isinstance(cfg, dict) else []:
        if isinstance(item, dict) and isinstance(item.get('ref'), str):
            declared[item['ref']] = item.get('drive') or 'declared'
    excluded = state_lib.excluded_refs(cfg)
    gaps = [] if cfg else ['intent.optocouplers: 未声明驱动方式与装配状态']
    return inv.build(db, cfg, 'optocouplers',
                     lambda state: _Scan(db, state, declared, excluded).optocouplers(), gaps)


class OptocouplerChecker(Checker):
    id = 'optocoupler'
    title = '光耦隔离传输'
    plan_key = 'optocoupler'
    version_key = 'optocoupler_version'
    version = 1
    intent_key = 'optocouplers'
    cold_rules = {'OC-01': 'LED 回路无限流元件', 'OC-02': '输出集电极无上拉'}
    hot_rules = {'OC-10': 'CTR 与驱动能力'}
    evidence_kinds = {'OC-10': {'opto_ctr'}}

    missing_inventory_message = 'optocoupler checks require their inventory'
    inventory_type_message = 'optocoupler inventory must be an object'
    requires_db_message = 'optocoupler inventory validation requires --db'
    stale_message = 'optocoupler inventory/binding is stale or modified'
    invalid_message = 'invalid optocoupler inventory: '

    def incomplete_message(self, key):
        return key + ': incomplete optocoupler planned coverage'

    def changed_message(self, key):
        return key + ': optocoupler generated criterion/object changed'

    def validate_intent(self, intent, db):
        return validate_optocoupler_intent(intent, db)

    def build(self, db, intent):
        return build_inventory(db, intent)

    def plan(self, planner, inventory):
        for state, item in inv.walk(inventory, 'optocouplers'):
            obj = {'ref': item['ref'], 'state': state['id'], 'optocoupler': item['id'],
                   'optocoupler_digest': inventory['digest']}
            if item['collector_net']:
                obj['net'] = item['collector_net']
            gaps = item['gaps']
            ready = planner.evidence_ready('OC-10', obj)
            check = planner.add_check(
                'optocoupler-transfer-' + item['id'], dict(obj),
                '按最小正向电流、保证 CTR 与项目规定的寿命衰减系数核输出可用电流是否满足'
                '上拉与负载要求；同时核最大正向电流不超额定',
                'ER4', 'AC0-HOT', readiness='READY' if ready else 'WAITING_EVIDENCE',
                required_inputs=sorted(set(gaps + ([] if ready else [
                    'evidence: OC-10 VF/CTR/电阻与上拉保证值',
                    'intent: CTR 寿命衰减系数的项目规定']))),
                trigger=['optocoupler:' + item['id']], rule='OC-10')
            check['domain'] = 'OPTOCOUPLER'
            check['inventory_gaps'] = gaps
            check = planner.add_check(
                'optocoupler-isolation-' + item['id'], dict(obj),
                '核隔离两侧的网络归属、参考地、跨接器件与耐压，以及输出侧速度/负载条件；'
                '爬电距离与实际隔离距离另交结构与版图',
                'ER3', 'Expert Review', readiness='WAITING_EVIDENCE',
                required_inputs=sorted(set(gaps + [
                    'datasheet:隔离耐压与爬电要求', 'requirements:隔离等级与安规标准'])),
                trigger=['optocoupler:' + item['id']],
                handoff=handoff({'required': True, 'receivers': ['PCB Layout', 'Mechanical'],
                                 'constraint': '隔离带无跨越走线/铜皮，爬电与电气间隙满足安规',
                                 'verification': '版图与结构复核隔离距离'}, 'APPLICABLE'))
            check['domain'] = 'OPTOCOUPLER'
            check['inventory_gaps'] = gaps

    def cold_findings(self, lint, inventory):
        for state, item in inv.walk(inventory, 'optocouplers'):
            head = '%s（状态 %s）' % (item['ref'], state['id'])
            if item['led_nets'] and not item['led_resistors'] and item['drive'] != 'constant-current':
                lint.add('OC-01', self.cold_rules['OC-01'],
                         head + '：LED 回路 ' + '、'.join(item['led_nets'])
                         + ' 上未见限流电阻；若为恒流驱动需在 intent 中声明并给出出处',
                         item['ref'])
            if item['collector_net'] and not item['output_pullups']:
                lint.add('OC-02', self.cold_rules['OC-02'],
                         head + '：输出集电极网 ' + item['collector_net']
                         + ' 上未见到电源轨的上拉电阻；若由接收端内部上拉或有源负载需给出证据',
                         item['ref'], kind='CANDIDATE')

    def hot_check(self, lint, check):
        drive = hotmath.span(check['drive_v'])
        vf = hotmath.span(check['vf_v'])
        drop = hotmath.span(check['driver_drop_v'])
        r_led = hotmath.span(check['r_led_ohm'])
        r_pullup = hotmath.span(check['r_pullup_ohm'])
        v_pullup = hotmath.span(check['v_pullup_v'])
        ctr_min = hotmath.number(check['ctr_min'])
        derating = hotmath.number(check['ctr_derating'])
        vol_required = hotmath.number(check['vol_required_v'])
        if_abs_max = hotmath.number(check['if_abs_max_a'])
        if_min = (drive[0] - vf[1] - drop[1]) / r_led[1]
        if_max = (drive[1] - vf[0] - drop[0]) / r_led[0]
        ic_available = if_min * ctr_min * derating
        ic_required = (v_pullup[1] - vol_required) / r_pullup[0]
        problems = []
        if if_min <= 0:
            problems.append('最小正向电流为 %s，最坏条件下 LED 不导通'
                            % hotmath.fmt(if_min, 'A'))
        elif ic_available < ic_required:
            problems.append('寿命衰减后可用集电极电流 %s 低于拉低上拉所需 %s'
                            % (hotmath.fmt(ic_available, 'A'), hotmath.fmt(ic_required, 'A')))
        if if_max > if_abs_max:
            problems.append('最大正向电流 %s 超过额定 %s'
                            % (hotmath.fmt(if_max, 'A'), hotmath.fmt(if_abs_max, 'A')))
        calculation = {'if_min_a': if_min, 'if_max_a': if_max, 'ctr_min': ctr_min,
                       'ctr_derating': derating, 'ic_available_a': ic_available,
                       'ic_required_a': ic_required, 'if_abs_max_a': if_abs_max}
        detail = '%s: IF=[%s, %s]A，CTR_min=%s×衰减%s，可用 IC=%s vs 需求 %s；%s' % (
            check.get('ref') or check.get('net'), hotmath.fmt(if_min), hotmath.fmt(if_max),
            hotmath.fmt(ctr_min), hotmath.fmt(derating), hotmath.fmt(ic_available, 'A'),
            hotmath.fmt(ic_required, 'A'), lint._citation(check))
        if problems:
            lint.add('OC-10', '光耦传输电流不满足要求', detail + '；' + '；'.join(problems),
                     check.get('ref'), check_id=check['id'], citation=check['citation'],
                     calculation=calculation)
        else:
            lint.record_pass('OC-10', check, detail,
                             scope='所给保证值与项目衰减系数下的静态传输能力；'
                                   '开关速度、温度角与隔离耐压未判定',
                             calculation=calculation)

    def evidence_errors(self, check, label):
        errors = hotmath.structure_errors(
            check, label,
            spans=('drive_v', 'vf_v', 'driver_drop_v', 'r_led_ohm', 'r_pullup_ohm', 'v_pullup_v'),
            numbers=('ctr_min', 'ctr_derating', 'vol_required_v', 'if_abs_max_a'))
        for field in ('r_led_ohm', 'r_pullup_ohm'):
            bounds = hotmath.span(check.get(field))
            if bounds is not None and bounds[0] <= 0:
                errors.append('%s.%s.min 必须为正数' % (label, field))
        for field in ('ctr_min', 'if_abs_max_a'):
            value = hotmath.number(check.get(field))
            if value is not None and value <= 0:
                errors.append('%s.%s 必须为正数' % (label, field))
        derating = hotmath.number(check.get('ctr_derating'))
        if derating is not None and not 0 < derating <= 1:
            errors.append('%s.ctr_derating 必须在 (0, 1] 内' % label)
        return errors

    def model_gaps(self, check):
        return hotmath.missing_gaps(
            check,
            spans=('drive_v', 'vf_v', 'driver_drop_v', 'r_led_ohm', 'r_pullup_ohm', 'v_pullup_v'),
            numbers=('ctr_min', 'ctr_derating', 'vol_required_v', 'if_abs_max_a'))

    def rule_instances(self, rule, inventory):
        return sorted({item['id'] for _, item in inv.walk(inventory, 'optocouplers')})

    def binds(self, item):
        obj = item.get('object')
        return isinstance(obj, dict) and bool(obj.get('optocoupler'))

    def object_errors(self, key, obj, inventory):
        known = {item['id'] for _, item in inv.walk(inventory, 'optocouplers')}
        if (obj.get('optocoupler') not in known
                or obj.get('optocoupler_digest') != inventory['digest']):
            return [key + ': stale optocoupler object binding']
        return []

    def pass_blockers(self, key, planned, generated, inventory):
        if generated and generated.get('inventory_gaps'):
            return [key + ': optocoupler gaps must be resolved in a regenerated plan before PASS']
        return []
