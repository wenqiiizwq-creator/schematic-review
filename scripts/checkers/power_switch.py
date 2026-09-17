#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""功率开关：栅极驱动、开关节点吸收与安全工作区检查器。

按引脚角色识别分立开关管，登记每个装配状态下的栅极驱动源、栅源下拉、
开关节点上的感性元件与吸收网络。栅源驱动窗口用 ER1 提供的保证值热跑；
安全工作区、死区、自举欠压与热由专家按器件资料判定，本检查器不代判。
"""
import re

from . import inventory as inv
from . import netgraph as ng
from . import powertree
from . import states as state_lib
from . import hotmath
from .base import Checker
from .planutil import handoff, slug

SWITCH_KINDS = {ng.MOSFET, ng.BJT}
CLAMP_KINDS = {ng.DIODE, ng.TVS, ng.ZENER}
INDUCTIVE_KINDS = {ng.INDUCTOR, ng.TRANSFORMER, ng.RELAY}
DRIVER_PIN_RE = re.compile(r'^(HO|LO|HB|OUT\w*|DRV\w*|GATE\w*|SRC|SINK|O\d+)$', re.I)
OUT_PINTYPES = {'OUT', 'OUTPUT', 'BI', 'BIDI', 'BIDIR', 'BIDIRECTIONAL', 'IO', 'I/O',
                'TRISTATE', '3STATE'}
MAX_SWITCHES = 256


def validate_power_switch_intent(intent, db=None):
    """校验可选的 power_switches 配置段。"""
    return state_lib.section_errors(
        intent, db, 'power_switches', item_field='switches', item_key='switch',
        fields=('id', 'ref', 'role', 'gate_net', 'citation'),
        net_fields=('gate_net',))


class _Scan:
    """单一装配状态下的开关识别。"""

    def __init__(self, db, state, declared, excluded):
        self.graph = ng.NetGraph(db, fitted=set(state['fitted']))
        self.state = state
        self.declared = declared
        self.excluded = excluded
        self.grounds = {net for net in self.graph.nets if ng.is_ground(net)}
        self.tree = powertree.PowerTree(self.graph)

    def _driver_pins(self, net, via):
        """网上的驱动输出脚；引脚名或引脚类型成立才算，名字本身不构成结论。"""
        found = []
        for node in self.graph.nodes_on(net):
            ref = node.partition('.')[0]
            if ref in self.excluded or not self.graph.is_fitted(ref):
                continue
            kind = self.graph.kind(ref)
            pin = node.partition('.')[2]
            pintype = ng.normalize(self.graph.pintype.get(node))
            if kind is ng.IC and (ng.name_matches(DRIVER_PIN_RE, self.graph.pinname.get(node), pin)
                                  or pintype in OUT_PINTYPES):
                found.append({'node': node, 'via': via, 'source': 'driver-pin'})
            elif kind in SWITCH_KINDS and self.graph.role(node) == 'drain':
                found.append({'node': node, 'via': via, 'source': 'discrete-stage'})
            elif kind is ng.CONNECTOR:
                found.append({'node': node, 'via': via, 'source': 'external'})
        return found

    def drivers(self, gate_net):
        found = self._driver_pins(gate_net, 'direct')
        for ref, other in self.graph.neighbors(gate_net, ng.PASSIVE_LINKS):
            found.extend(self._driver_pins(other, 'series:' + ref))
        unique = {item['node'] + '|' + item['via']: item for item in found}
        return [unique[key] for key in sorted(unique)]

    def pulls(self, gate_net, source_net):
        found = []
        for ref, other in self.graph.neighbors(gate_net, {ng.RESISTOR}):
            if other == source_net or other in self.grounds or self.tree.is_rail(other):
                found.append({'ref': ref, 'to': other,
                              'value': self.graph.parts.get(ref, {}).get('value')})
        return found

    def snubbers(self, drain_net, source_net):
        """开关节点到地/源/电源轨的吸收或钳位路径。

        跨负载接到电源轨的续流二极管也是钳位路径，不能只看漏源之间。
        """
        graph, found = self.graph, []
        targets = set(self.grounds)
        if source_net:
            targets.add(source_net)
        targets |= {other for _, other in graph.neighbors(drain_net)
                    if self.tree.is_rail(other) or other in self.grounds}
        for target in sorted(targets):
            if target == drain_net:
                continue
            for ref in graph.between(drain_net, target, {ng.CAPACITOR}):
                found.append({'ref': ref, 'type': 'capacitor', 'to': target})
            for ref in graph.between(drain_net, target, CLAMP_KINDS):
                found.append({'ref': ref, 'type': 'clamp-' + graph.kind(ref), 'to': target})
        for ref, middle in graph.neighbors(drain_net, {ng.RESISTOR}):
            for target in sorted(targets):
                for cap in graph.between(middle, target, {ng.CAPACITOR}):
                    found.append({'ref': ref + '+' + cap, 'type': 'rc-snubber', 'to': target})
        return sorted(found, key=lambda item: (item['ref'], item['type']))

    def _switch_node_evidence(self, drain_net, ref):
        """开关节点上是否有感性负载或半桥对管。"""
        reasons = []
        for other in self.graph.refs_on(drain_net):
            if other == ref or other in self.excluded:
                continue
            kind = self.graph.kind(other)
            if kind in INDUCTIVE_KINDS:
                reasons.append('inductive:' + other)
            elif kind in SWITCH_KINDS:
                node = self.graph.node_named(other, 'source')
                if node and self.graph.pin2net.get(node) == drain_net:
                    reasons.append('half-bridge:' + other)
        for node in self.graph.nodes_on(drain_net):
            if ng.name_matches(powertree.SWITCH_NODE_PIN_RE, self.graph.pinname.get(node),
                               node.partition('.')[2]):
                reasons.append('converter-node:' + node)
        return sorted(set(reasons))

    def switches(self):
        graph, found = self.graph, []
        for ref in sorted(graph.parts):
            if ref in self.excluded or not graph.is_fitted(ref):
                continue
            if graph.kind(ref) not in SWITCH_KINDS:
                continue
            nodes = {role: graph.node_named(ref, role)
                     for role in ('gate', 'drain', 'source')}
            gaps = list(self.state['gaps'])
            if not all(nodes.values()):
                gaps.append('pin-roles:' + ref)
            nets = {role: graph.pin2net.get(node) if node else None
                    for role, node in nodes.items()}
            topology, source_basis = 'unknown', None
            if nets['source'] in self.grounds:
                topology = 'low-side'
            elif nets['source'] and self.tree.is_rail(nets['source']):
                topology = 'high-side'
                source_basis = self.tree.rail_basis(nets['source'])
            elif nets['source']:
                topology = 'floating'
            drivers = self.drivers(nets['gate']) if nets['gate'] else []
            pulls = self.pulls(nets['gate'], nets['source']) if nets['gate'] else []
            snubbers = self.snubbers(nets['drain'], nets['source']) if nets['drain'] else []
            switch_node = self._switch_node_evidence(nets['drain'], ref) if nets['drain'] else []
            found.append({
                'id': slug(ref + '-' + (nets['gate'] or 'unresolved')),
                'ref': ref,
                'kind': graph.kind(ref),
                'basis': 'declared' if ref in self.declared else 'topology',
                'topology': topology,
                'source_rail_basis': source_basis,
                'gate_node': nodes['gate'],
                'gate_net': nets['gate'],
                'drain_net': nets['drain'],
                'source_net': nets['source'],
                'drivers': drivers,
                'gate_pulls': pulls,
                'snubbers': snubbers,
                'switch_node_evidence': switch_node,
                'gaps': sorted(set(gaps)),
            })
        return sorted(found, key=lambda item: item['id'])[:MAX_SWITCHES]


def build_inventory(db, intent=None):
    """生成逐状态的功率开关清单。"""
    cfg = (intent or {}).get('power_switches') if isinstance(intent, dict) else None
    declared = state_lib.declared_refs(cfg, 'switches')
    excluded = state_lib.excluded_refs(cfg)
    gaps = [] if cfg else ['intent.power_switches: 未声明开关角色与装配状态']
    return inv.build(db, cfg, 'switches',
                     lambda state: _Scan(db, state, declared, excluded).switches(), gaps)


class PowerSwitchChecker(Checker):
    id = 'power_switch'
    title = '功率开关栅驱动与吸收'
    plan_key = 'power_switch'
    version_key = 'power_switch_version'
    version = 1
    intent_key = 'power_switches'
    cold_rules = {'PS-01': '栅极无驱动源且无下拉', 'PS-02': '开关节点无吸收/钳位'}
    hot_rules = {'PS-10': '栅源驱动窗口'}
    evidence_kinds = {'PS-10': {'gate_drive'}}

    missing_inventory_message = 'power switch checks require their inventory'
    inventory_type_message = 'power switch inventory must be an object'
    requires_db_message = 'power switch inventory validation requires --db'
    stale_message = 'power switch inventory/binding is stale or modified'
    invalid_message = 'invalid power switch inventory: '

    def incomplete_message(self, key):
        return key + ': incomplete power switch planned coverage'

    def changed_message(self, key):
        return key + ': power switch generated criterion/object changed'

    def validate_intent(self, intent, db):
        return validate_power_switch_intent(intent, db)

    def build(self, db, intent):
        return build_inventory(db, intent)

    def plan(self, planner, inventory):
        for state, switch in inv.walk(inventory, 'switches'):
            obj = {'ref': switch['ref'], 'state': state['id'],
                   'power_switch': switch['id'],
                   'power_switch_digest': inventory['digest']}
            if switch['gate_net']:
                obj['net'] = switch['gate_net']
            if switch['gate_node']:
                obj['node'] = switch['gate_node']
            gaps = switch['gaps']
            ready = planner.evidence_ready('PS-10', obj)
            item = planner.add_check(
                'power-switch-gate-drive-' + switch['id'], dict(obj),
                '按保证值核栅源驱动窗口：最小驱动不低于 RDS(on) 保证条件的 VGS，'
                '最大驱动不超栅源绝限；驱动源与下拉在上电、故障与高阻态下均确定',
                'ER4', 'AC0-HOT', readiness='READY' if ready else 'WAITING_EVIDENCE',
                required_inputs=sorted(set(gaps + ([] if ready else [
                    'evidence: PS-10 栅源驱动/绝限/RDS(on) 条件保证值']))),
                trigger=['power-switch:' + switch['id'], 'topology:' + switch['topology']],
                rule='PS-10')
            item['domain'] = 'POWER_SWITCH'
            item['inventory_gaps'] = gaps
            item = planner.add_check(
                'power-switch-soa-' + switch['id'], dict(obj),
                '按实际电流、电压、脉宽、换流速度与栅偏共同核安全工作区与降额；'
                '不能只用 I²·RDS(on) 代替 SOA，重复脉冲与单次脉冲分别核',
                'ER4', 'Expert Review', readiness='WAITING_EVIDENCE',
                required_inputs=sorted(set(gaps + [
                    'datasheet:SOA 曲线与脉宽条件', 'intent:最坏工况电流/电压/重复率'])),
                trigger=['power-switch:' + switch['id']],
                handoff=handoff({'required': True, 'receivers': ['Thermal', 'PCB Layout'],
                                 'constraint': '结温按实际散热路径核算，栅极回路与开关回路面积最小',
                                 'verification': '热仿真/实测结温与开关波形复核'}, 'APPLICABLE'))
            item['domain'] = 'POWER_SWITCH'
            item['analysis_required'] = True
            item['inventory_gaps'] = gaps
            if switch['switch_node_evidence']:
                item = planner.add_check(
                    'power-switch-node-damping-' + switch['id'], dict(obj),
                    '核开关节点尖峰与振铃：吸收/钳位网络的存在、参数与耗散，'
                    '死区、自举欠压、负压与短路软关断按驱动器条款逐项核',
                    'ER3', 'Expert Review', readiness='WAITING_EVIDENCE',
                    required_inputs=sorted(set(gaps + [
                        'datasheet:开关器件耐压与驱动器条款', 'evidence:开关节点波形或仿真'])),
                    trigger=['power-switch:' + switch['id']] + switch['switch_node_evidence'],
                    handoff=handoff({'required': True, 'receivers': ['PCB Layout'],
                                     'constraint': '吸收网络紧靠开关节点，换流回路面积最小',
                                     'verification': '版图复核吸收回路与实测尖峰'}, 'APPLICABLE'))
                item['domain'] = 'POWER_SWITCH'
                item['inventory_gaps'] = gaps

    def cold_findings(self, lint, inventory):
        for state, switch in inv.walk(inventory, 'switches'):
            if not switch['gate_net']:
                continue
            head = '%s（%s，状态 %s）' % (switch['ref'], switch['kind'], state['id'])
            if not switch['drivers'] and not switch['gate_pulls']:
                lint.add('PS-01', self.cold_rules['PS-01'],
                         head + '：栅极网 ' + switch['gate_net']
                         + ' 上既无驱动输出脚也无下拉/上拉电阻，上电与驱动高阻时开关状态不确定',
                         switch['ref'])
            if switch['switch_node_evidence'] and not switch['snubbers']:
                lint.add('PS-02', self.cold_rules['PS-02'],
                         head + '：开关节点 ' + str(switch['drain_net'])
                         + ' 上有 ' + '、'.join(switch['switch_node_evidence'])
                         + '，但未见 RC/RCD 吸收或钳位；关断尖峰与振铃需按器件耐压核实',
                         switch['ref'], kind='CANDIDATE')

    def hot_check(self, lint, check):
        drive = hotmath.span(check['vgs_drive_v'])
        limits = hotmath.span(check['vgs_abs_v'])
        spec = hotmath.number(check['vgs_rds_on_v'])
        if check.get('channel') == 'p':
            drive, limits, spec = hotmath.negate(drive), hotmath.negate(limits), -spec
        problems = []
        if drive[0] < spec:
            problems.append('最小栅源驱动 %s 低于 RDS(on) 保证条件 %s'
                            % (hotmath.fmt(drive[0], 'V'), hotmath.fmt(spec, 'V')))
        if drive[1] > limits[1] or drive[0] < limits[0]:
            problems.append('栅源驱动窗口 [%s, %s] 超出绝对最大范围 [%s, %s]'
                            % (hotmath.fmt(drive[0]), hotmath.fmt(drive[1]),
                               hotmath.fmt(limits[0]), hotmath.fmt(limits[1])))
        calculation = {'channel': check.get('channel'),
                       'vgs_drive_v': {'min': drive[0], 'max': drive[1]},
                       'vgs_rds_on_v': spec,
                       'vgs_abs_v': {'min': limits[0], 'max': limits[1]},
                       'normalized': check.get('channel') == 'p'}
        target = check.get('node') or check.get('net') or check.get('ref')
        detail = '%s: 栅源驱动 [%s, %s]V，RDS(on) 条件 %sV，绝限 [%s, %s]V；%s' % (
            target, hotmath.fmt(drive[0]), hotmath.fmt(drive[1]), hotmath.fmt(spec),
            hotmath.fmt(limits[0]), hotmath.fmt(limits[1]), lint._citation(check))
        if problems:
            lint.add('PS-10', '栅源驱动窗口不满足保证条件', detail + '；' + '；'.join(problems),
                     str(check.get('ref') or '').split('.')[0] or None,
                     check_id=check['id'], citation=check['citation'], calculation=calculation)
        else:
            lint.record_pass('PS-10', check, detail,
                             scope='所给保证值下的栅源驱动窗口；开关速度、米勒导通、SOA 与热未判定',
                             calculation=calculation)

    def evidence_errors(self, check, label):
        return hotmath.structure_errors(
            check, label, spans=('vgs_drive_v', 'vgs_abs_v'),
            numbers=('vgs_rds_on_v',), choices=(('channel', {'n', 'p'}),))

    def model_gaps(self, check):
        return hotmath.missing_gaps(
            check, spans=('vgs_drive_v', 'vgs_abs_v'), numbers=('vgs_rds_on_v',),
            texts=('channel',))

    def rule_instances(self, rule, inventory):
        return sorted({switch['id'] for _, switch in inv.walk(inventory, 'switches')
                       if switch['gate_net']})

    def binds(self, item):
        obj = item.get('object')
        return isinstance(obj, dict) and bool(obj.get('power_switch'))

    def object_errors(self, key, obj, inventory):
        known = {switch['id'] for _, switch in inv.walk(inventory, 'switches')}
        if (obj.get('power_switch') not in known
                or obj.get('power_switch_digest') != inventory['digest']):
            return [key + ': stale power switch object binding']
        return []

    def pass_blockers(self, key, planned, generated, inventory):
        if generated and generated.get('inventory_gaps'):
            return [key + ': power switch gaps must be resolved in a regenerated plan before PASS']
        return []
