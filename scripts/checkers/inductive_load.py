#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""感性负载续流与钳位检查器。

识别继电器线圈、由开关驱动的电感/绕组以及经连接器外接的感性负载，登记每个
装配状态下的钳位路径。只回答"有没有、接法对不对、缺什么证据"；额定值是否
足够由 ER4 按器件资料判定。开关电源储能电感不属于本检查器。
"""
import re

from . import inventory as inv
from . import netgraph as ng
from . import powertree
from . import states as state_lib
from .base import Checker
from .planutil import handoff, slug

SWITCH_KINDS = {ng.MOSFET, ng.BJT}
CLAMP_KINDS = {ng.DIODE, ng.TVS, ng.ZENER}
LOAD_KINDS = {ng.RELAY, ng.INDUCTOR, ng.TRANSFORMER}
DRIVER_PIN_RE = re.compile(r'^(OUT\w*|DRV\w*|O\d+|HO|LO|SOURCE\d*|SINK\d*)$', re.I)
INTEGRATED_CLAMP_RE = re.compile(r'^(COM|CLAMP\w*|VS|FREEWHEEL\w*)$', re.I)
LOAD_NAME_RE = re.compile(r'(^|_)(MOTOR|SOLENOID|VALVE|COIL|RELAY|PUMP|BRAKE|FAN|ACTUATOR)\w*(_|$)', re.I)
MAX_LOADS = 256


def validate_inductive_intent(intent, db=None):
    """校验可选的 inductive_loads 配置段。"""
    return state_lib.section_errors(
        intent, db, 'inductive_loads', item_field='loads', item_key='load',
        fields=('id', 'ref', 'kind', 'switch_net', 'rail_net', 'citation'),
        net_fields=('switch_net', 'rail_net'))


class _Scan:
    """单一装配状态下的识别过程。"""

    def __init__(self, db, state, declared, excluded):
        self.graph = ng.NetGraph(db, fitted=set(state['fitted']))
        self.tree = powertree.PowerTree(self.graph)
        self.state = state
        self.declared = declared
        self.excluded = excluded

    def _switch_on(self, net):
        """返回 (switch_ref, evidence) —— 该网上的开关器件或驱动输出脚。"""
        graph = self.graph
        for ref in graph.refs_on(net):
            if ref in self.excluded:
                continue
            if graph.kind(ref) in SWITCH_KINDS:
                node = graph.node_named(ref, 'drain')
                if node and graph.pin2net.get(node) == net:
                    return ref, 'switch-drain:' + node
                if node is None:
                    return ref, 'switch:' + ref + '(pin roles unknown)'
        for node in graph.nodes_on(net):
            ref = node.partition('.')[0]
            if graph.kind(ref) is not ng.IC or ref in self.excluded:
                continue
            if ng.name_matches(DRIVER_PIN_RE, graph.pinname.get(node), node.partition('.')[2]):
                return ref, 'driver-pin:' + node
        return None, None

    def _is_converter_node(self, net):
        for node in self.graph.nodes_on(net):
            if ng.name_matches(powertree.SWITCH_NODE_PIN_RE, self.graph.pinname.get(node),
                               node.partition('.')[2]):
                return True
        return False

    def _clamps(self, switch_net, rail_net):
        graph, found = self.graph, []
        for ref in graph.between(switch_net, rail_net, CLAMP_KINDS):
            anode = graph.node_named(ref, 'anode')
            cathode = graph.node_named(ref, 'cathode')
            if anode and cathode:
                orientation = ('forward' if graph.pin2net.get(anode) == switch_net
                               and graph.pin2net.get(cathode) == rail_net else 'reversed')
            else:
                orientation = 'unknown'
            found.append({'ref': ref, 'type': 'freewheel-' + graph.kind(ref),
                          'orientation': orientation, 'across': 'load'})
        for net in sorted({n for n in (self._grounds() | {rail_net}) if n != switch_net}):
            for ref in graph.between(switch_net, net, CLAMP_KINDS):
                if any(item['ref'] == ref for item in found):
                    continue
                found.append({'ref': ref, 'type': 'switch-clamp-' + graph.kind(ref),
                              'orientation': 'unknown', 'across': 'switch'})
        for ref, middle in graph.neighbors(switch_net, {ng.RESISTOR}):
            for cap in graph.between(middle, rail_net, {ng.CAPACITOR}):
                found.append({'ref': ref + '+' + cap, 'type': 'rc-snubber',
                              'orientation': 'n/a', 'across': 'load'})
            for ground in sorted(self._grounds()):
                for cap in graph.between(middle, ground, {ng.CAPACITOR}):
                    found.append({'ref': ref + '+' + cap, 'type': 'rc-snubber',
                                  'orientation': 'n/a', 'across': 'switch'})
        return sorted(found, key=lambda item: (item['ref'], item['type']))

    def _grounds(self):
        return {net for net in self.graph.nets if ng.is_ground(net)}

    def _integrated_clamp(self, switch_ref, rail_net):
        graph = self.graph
        if switch_ref is None or graph.kind(switch_ref) is not ng.IC:
            return []
        for node, net in graph.pins_of(switch_ref).items():
            if net == rail_net and ng.name_matches(
                    INTEGRATED_CLAMP_RE, graph.pinname.get(switch_ref + '.' + node), node):
                return [{'ref': switch_ref, 'type': 'integrated-clamp-candidate',
                         'orientation': 'unknown', 'across': 'driver',
                         'node': switch_ref + '.' + node}]
        return []

    def loads(self):
        graph, found = self.graph, []
        for ref in sorted(graph.parts):
            if ref in self.excluded or not graph.is_fitted(ref):
                continue
            kind = graph.kind(ref)
            if kind not in LOAD_KINDS:
                continue
            terminals = graph.terminals(ref)
            gaps = []
            if len(terminals) != 2:
                if kind is ng.RELAY:
                    coil = [graph.pin2net[node] for node in sorted(graph.pin2net)
                            if node.startswith(ref + '.') and graph.role(node) == 'coil']
                    terminals = sorted(set(coil))
                if len(terminals) != 2:
                    gaps.append('pin-roles:' + ref)
                    found.append(self._entry(ref, kind, None, None, None, 'topology', gaps))
                    continue
            switch_net = rail_net = switch_ref = None
            for candidate in terminals:
                sw, evidence = self._switch_on(candidate)
                if sw is not None:
                    switch_net, switch_ref = candidate, sw
                    rail_net = terminals[0] if terminals[1] == candidate else terminals[1]
                    break
            if switch_net is None:
                continue
            if kind is not ng.RELAY and self._is_converter_node(switch_net):
                continue
            basis = 'declared' if ref in self.declared else 'topology'
            found.append(self._entry(ref, kind, switch_net, rail_net, switch_ref, basis, gaps))
        found.extend(self._external_loads())
        return sorted(found, key=lambda item: item['id'])[:MAX_LOADS]

    def _external_loads(self):
        """经连接器外接、仅有网名线索的负载：只作候选，需 intent 确认。"""
        graph, found = self.graph, []
        for net in sorted(graph.nets):
            if not LOAD_NAME_RE.search(net) or net in graph.pseudo:
                continue
            switch_ref, _ = self._switch_on(net)
            connectors = [ref for ref in graph.refs_on(net) if graph.kind(ref) is ng.CONNECTOR]
            if switch_ref is None or not connectors:
                continue
            if any(item['switch_net'] == net for item in found):
                continue
            rails = [other for _, other in graph.neighbors(net) if self.tree.is_rail(other)]
            found.append(self._entry(connectors[0], 'external', net, rails[0] if rails else None,
                                     switch_ref, 'name-hint',
                                     ['intent.inductive_loads.loads: 外接负载需声明'],
                                     load_net=net))
        return found

    def _entry(self, ref, kind, switch_net, rail_net, switch_ref, basis, gaps, load_net=None):
        clamps = []
        if switch_net and rail_net:
            clamps = self._clamps(switch_net, rail_net) + self._integrated_clamp(switch_ref, rail_net)
        entry_gaps = list(gaps)
        if switch_net and rail_net and not clamps:
            entry_gaps.append('clamp:none-found')
        if any(item['orientation'] == 'unknown' and item['across'] == 'load' for item in clamps):
            entry_gaps.append('clamp-orientation:pin roles unknown')
        return {
            'id': slug(ref + '-' + (switch_net or 'unresolved')),
            'ref': ref,
            'kind': kind if isinstance(kind, str) else str(kind),
            'basis': basis,
            'switch_net': switch_net,
            'switch_ref': switch_ref,
            'rail_net': rail_net,
            'load_net': load_net,
            'clamps': clamps,
            'gaps': sorted(set(entry_gaps + self.state['gaps'])),
        }


def build_inventory(db, intent=None):
    """生成逐状态的感性负载与钳位清单。"""
    cfg = (intent or {}).get('inductive_loads') if isinstance(intent, dict) else None
    declared = state_lib.declared_refs(cfg, 'loads')
    excluded = state_lib.excluded_refs(cfg)
    gaps = [] if cfg else ['intent.inductive_loads: 未声明感性负载与装配状态']
    return inv.build(db, cfg, 'loads',
                     lambda state: _Scan(db, state, declared, excluded).loads(), gaps)


class InductiveLoadChecker(Checker):
    id = 'inductive_load'
    title = '感性负载续流与钳位'
    plan_key = 'inductive_load'
    version_key = 'inductive_load_version'
    version = 1
    intent_key = 'inductive_loads'
    cold_rules = {'IL-01': '感性负载无续流/钳位路径', 'IL-02': '续流二极管方向接反'}

    missing_inventory_message = 'inductive load checks require their inventory'
    inventory_type_message = 'inductive load inventory must be an object'
    requires_db_message = 'inductive load inventory validation requires --db'
    stale_message = 'inductive load inventory/binding is stale or modified'
    invalid_message = 'invalid inductive load inventory: '

    def incomplete_message(self, key):
        return key + ': incomplete inductive load planned coverage'

    def changed_message(self, key):
        return key + ': inductive load generated criterion/object changed'

    def validate_intent(self, intent, db):
        return validate_inductive_intent(intent, db)

    def build(self, db, intent):
        return build_inventory(db, intent)

    def plan(self, planner, inventory):
        for state in inventory['states']:
            for load in state['loads']:
                obj = {'ref': load['ref'], 'state': state['id'],
                       'inductive_load': load['id'],
                       'inductive_load_digest': inventory['digest']}
                if load['switch_net']:
                    obj['net'] = load['switch_net']
                    obj['nets'] = sorted({x for x in (load['switch_net'], load['rail_net']) if x})
                applicability = 'UNDETERMINED' if load['basis'] == 'name-hint' else 'APPLICABLE'
                item = planner.add_check(
                    'inductive-load-clamp-topology-' + load['id'], dict(obj),
                    '核对本状态该感性负载的续流/钳位路径是否存在、方向是否正确、钳位器件是否贴装；'
                    '驱动器内部钳位须有资料证据，网名或型号不构成结论',
                    'ER3', 'Expert Review', applicability=applicability,
                    readiness='WAITING_EVIDENCE' if load['gaps'] else 'READY',
                    required_inputs=load['gaps'],
                    trigger=['inductive-load:' + load['id'], 'basis:' + load['basis']],
                    handoff=handoff({'required': applicability == 'APPLICABLE',
                        'receivers': ['PCB Layout'],
                        'constraint': '钳位器件靠近负载与开关，续流回路面积最小',
                        'verification': '版图复核钳位回路与摆放'}, applicability))
                item['domain'] = 'INDUCTIVE_LOAD'
                item['inventory_gaps'] = load['gaps']
                item = planner.add_check(
                    'inductive-load-clamp-rating-' + load['id'], dict(obj),
                    '按线圈关断瞬间电流与电源最高电压核钳位器件额定：反向耐压、峰值/重复电流、'
                    '钳位电压加电源电压不超过开关器件耐压、重复频率下的耗散',
                    'ER4', 'Expert Review', applicability=applicability,
                    readiness='WAITING_EVIDENCE',
                    required_inputs=sorted(set(load['gaps'] + [
                        'datasheet:钳位器件额定值', 'datasheet:开关器件耐压',
                        'intent:线圈电阻/电感与电源最高电压'])),
                    trigger=['inductive-load:' + load['id']])
                item['domain'] = 'INDUCTIVE_LOAD'
                item['analysis_required'] = True
                item['inventory_gaps'] = load['gaps']

    def cold_findings(self, lint, inventory):
        for state in inventory['states']:
            for load in state['loads']:
                if load['basis'] == 'name-hint' or not load['switch_net']:
                    continue
                detail_head = '%s（%s，状态 %s）: 开关节点 %s' % (
                    load['ref'], load['kind'], state['id'], load['switch_net'])
                if not load['clamps']:
                    lint.add('IL-01', self.cold_rules['IL-01'],
                             detail_head + ' 与 ' + str(load['rail_net'])
                             + ' 之间未见续流二极管、钳位器件或 RC 吸收；'
                               '若由驱动器内部钳位需给出资料证据',
                             load['ref'])
                for clamp in load['clamps']:
                    if clamp['orientation'] == 'reversed':
                        lint.add('IL-02', self.cold_rules['IL-02'],
                                 detail_head + '：' + clamp['ref']
                                 + ' 阴极接在开关节点、阳极接电源，导通方向与续流相反',
                                 load['ref'])

    def rule_instances(self, rule, inventory):
        return sorted({load['id'] for state in inventory['states'] for load in state['loads']
                       if load['basis'] != 'name-hint' and load['switch_net']})

    def binds(self, item):
        obj = item.get('object')
        return isinstance(obj, dict) and bool(obj.get('inductive_load'))

    def object_errors(self, key, obj, inventory):
        known = {load['id'] for state in inventory['states'] for load in state['loads']}
        if obj.get('inductive_load') not in known or obj.get('inductive_load_digest') != inventory['digest']:
            return [key + ': stale inductive load object binding']
        return []

    def pass_blockers(self, key, planned, generated, inventory):
        if generated and generated.get('inventory_gaps'):
            return [key + ': inductive load gaps must be resolved in a regenerated plan before PASS']
        return []
