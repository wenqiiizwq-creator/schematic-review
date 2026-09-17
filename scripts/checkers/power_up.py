#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""上电过程检查器：使能来源、UVLO/延时与最坏压差。

识别带使能脚的稳压器、使能来源形态、软启动与 PG 去向，并登记使能与被供电
器件同轨同时建立的候选。时序是否满足需求由专家按需求与器件条款判定；
单调性、台阶与实际爬升波形属实测 HANDOFF。
"""
import re

from . import hotmath
from . import inventory as inv
from . import netgraph as ng
from . import powertree
from . import states as state_lib
from .base import Checker
from .planutil import handoff, slug

ENABLE_PIN_RE = re.compile(r'^(EN|ENABLE|SHDN|_?SHDN|PWREN|EN\d|ENA|ON_OFF)$', re.I)
RESET_IN_RE = re.compile(r'^(N?RESET\w*|N?RST\w*|MR|POR|XRES|RESETN)$', re.I)
OUTPUT_PIN_RE = re.compile(r'^(VOUT\d*|OUT\d*|VO)$', re.I)
FEEDBACK_PIN_RE = re.compile(r'^(FB\d*|VFB|VSENSE|VS_FB)$', re.I)
SWITCH_PIN_RE = re.compile(r'^(SW\d*|LX\d*|PH\d*|HG|LG|HO|LO|BOOT\w*|BST\w*)$', re.I)
POWER_PIN_RE = re.compile(r'^(VDD\w*|VCC\w*|AVDD\w*|AVCC\w*|DVDD\w*|VIN\d*|PVIN\d*|VBAT|VS)$', re.I)
SOFTSTART_PIN_RE = re.compile(r'^(SS|SST|SSTART|SOFTSTART|TR|TRACK)$', re.I)
GOOD_PIN_RE = re.compile(r'^(PG|PGOOD|POK|PWRGD|RESET\w*|RST\w*)$', re.I)
MAX_RAILS = 128


def validate_power_up_intent(intent, db=None):
    """校验可选的 power_up 配置段。"""
    return state_lib.section_errors(
        intent, db, 'power_up', item_field='regulators', item_key='regulator',
        fields=('id', 'ref', 'kind', 'enable_net', 'citation'), net_fields=('enable_net',))


class _Scan:
    def __init__(self, db, state, declared, excluded):
        self.graph = ng.NetGraph(db, fitted=set(state['fitted']))
        self.state = state
        self.declared = declared
        self.excluded = excluded
        self.grounds = {net for net in self.graph.nets if ng.is_ground(net)}
        self.tree = powertree.PowerTree(self.graph)
        self._regulators = None

    def _enable_source(self, enable_net, input_nets):
        """使能来源形态；名字不构成结论，只按连接关系归类。"""
        graph = self.graph
        if enable_net in input_nets:
            return 'tied-to-input', []
        to_rail = [(ref, other) for ref, other in graph.neighbors(enable_net, {ng.RESISTOR})
                   if self.tree.is_rail(other)]
        to_ground = [(ref, other) for ref, other in graph.neighbors(enable_net, {ng.RESISTOR})
                     if other in self.grounds]
        caps = [ref for ground in sorted(self.grounds)
                for ref in graph.between(enable_net, ground, {ng.CAPACITOR})]
        drivers = []
        for node in graph.nodes_on(enable_net):
            ref = node.partition('.')[0]
            if ref in self.excluded or not graph.is_fitted(ref) or graph.kind(ref) is not ng.IC:
                continue
            name = ng.normalize(graph.pinname.get(node))
            pintype = ng.normalize(graph.pintype.get(node))
            if name and GOOD_PIN_RE.match(name):
                drivers.append({'node': node, 'source': 'power-good'})
            elif pintype in {'OUT', 'OUTPUT', 'TRISTATE', '3STATE', 'BI', 'BIDI', 'BIDIR'}:
                drivers.append({'node': node, 'source': 'controller-output'})
        refs = sorted({ref for ref, _ in to_rail + to_ground} | {x for x in caps})
        if drivers:
            return ('sequenced' if any(x['source'] == 'power-good' for x in drivers)
                    else 'controlled'), drivers
        if to_rail and to_ground:
            return 'uvlo-divider', refs
        if to_rail and caps:
            return 'rc-delay', refs
        if to_rail or to_ground:
            return 'pulled', refs
        return 'unknown', refs

    def regulators(self):
        if self._regulators is not None:
            return self._regulators
        graph, found = self.graph, []
        for ref in sorted(graph.parts):
            if ref in self.excluded or not graph.is_fitted(ref) or graph.kind(ref) is not ng.IC:
                continue
            enables = self.graph.named_pins(ref, ENABLE_PIN_RE)
            outputs = self.graph.named_pins(ref, OUTPUT_PIN_RE)
            switches = self.graph.named_pins(ref, SWITCH_PIN_RE)
            feedback = self.graph.named_pins(ref, FEEDBACK_PIN_RE)
            # 反馈脚只用于认出稳压器，不算输出轨。
            if not enables or not (outputs or switches or feedback):
                continue
            inputs = self.graph.named_pins(ref, POWER_PIN_RE)
            for node, enable_net in enables.items():
                source, evidence = self._enable_source(enable_net, set(inputs.values()))
                found.append({
                    'id': slug(ref + '-' + enable_net),
                    'ref': ref,
                    'basis': 'declared' if ref in self.declared else 'topology',
                    'kind': 'switching' if switches else 'linear',
                    'enable_node': node,
                    'enable_net': enable_net,
                    'enable_source': source,
                    'enable_evidence': evidence,
                    'input_nets': sorted(set(inputs.values())),
                    'output_nets': sorted(set(outputs.values())),
                    'softstart': sorted(self.graph.named_pins(ref, SOFTSTART_PIN_RE)),
                    'power_good': sorted(self.graph.named_pins(ref, GOOD_PIN_RE).values()),
                    'gaps': sorted(set(self.state['gaps'])),
                })
        self._regulators = sorted(found, key=lambda item: item['id'])[:MAX_RAILS]
        return self._regulators

    def coupled_loads(self):
        """使能/复位输入与本器件供电轨同网的负载：上电同时建立的候选。

        稳压器自身的使能直连由 PU-02 覆盖，这里不重复登记。
        """
        graph, found = self.graph, []
        regulators = {item['ref'] for item in self.regulators()}
        for ref in sorted(graph.parts):
            if ref in self.excluded or not graph.is_fitted(ref) or graph.kind(ref) is not ng.IC:
                continue
            if ref in regulators:
                continue
            supplies = set(self.graph.named_pins(ref, POWER_PIN_RE).values())
            if not supplies:
                continue
            controls = dict(self.graph.named_pins(ref, ENABLE_PIN_RE))
            controls.update(self.graph.named_pins(ref, RESET_IN_RE))
            for node, net in sorted(controls.items()):
                if net in supplies and ng.is_rail(net):
                    found.append({'id': slug(ref + '-' + node.partition('.')[2]),
                                  'ref': ref, 'node': node, 'net': net})
        return found


def build_inventory(db, intent=None):
    """生成逐状态的上电清单：稳压器使能形态与使能随轨建立的负载候选。"""
    cfg = (intent or {}).get('power_up') if isinstance(intent, dict) else None
    declared = state_lib.declared_refs(cfg, 'regulators')
    excluded = state_lib.excluded_refs(cfg)
    gaps = [] if cfg else ['intent.power_up: 未声明上电顺序要求与装配状态']
    scans = {}

    def scan(state):
        scanner = scans.setdefault(state['id'], _Scan(db, state, declared, excluded))
        regulators = scanner.regulators()
        coupled = scanner.coupled_loads()
        for item in regulators:
            item['coupled_loads'] = [x['id'] for x in coupled if x['net'] in item['output_nets']]
        return regulators

    def extra(state):
        scanner = scans.setdefault(state['id'], _Scan(db, state, declared, excluded))
        return {'coupled_loads': scanner.coupled_loads()}

    return inv.build(db, cfg, 'regulators', scan, gaps, extra=extra)


class PowerUpChecker(Checker):
    id = 'power_up'
    title = '上电使能与压差'
    plan_key = 'power_up'
    version_key = 'power_up_version'
    version = 1
    intent_key = 'power_up'
    cold_rules = {'PU-01': '使能/复位与供电轨同时建立', 'PU-02': '使能直连输入轨无 UVLO/延时',
                  'PU-03': '使能来源不确定'}
    hot_rules = {'PU-10': '最坏压差裕量'}
    evidence_kinds = {'PU-10': {'dropout'}}

    missing_inventory_message = 'power up checks require their inventory'
    inventory_type_message = 'power up inventory must be an object'
    requires_db_message = 'power up inventory validation requires --db'
    stale_message = 'power up inventory/binding is stale or modified'
    invalid_message = 'invalid power up inventory: '

    def incomplete_message(self, key):
        return key + ': incomplete power up planned coverage'

    def changed_message(self, key):
        return key + ': power up generated criterion/object changed'

    def validate_intent(self, intent, db):
        return validate_power_up_intent(intent, db)

    def build(self, db, intent):
        return build_inventory(db, intent)

    def plan(self, planner, inventory):
        for state, item in inv.walk(inventory, 'regulators'):
            obj = {'ref': item['ref'], 'net': item['enable_net'], 'node': item['enable_node'],
                   'state': state['id'], 'power_up': item['id'],
                   'power_up_digest': inventory['digest']}
            gaps = item['gaps']
            check = planner.add_check(
                'power-up-enable-source-' + item['id'], dict(obj),
                '核使能来源在上电、掉电与故障恢复下的确定性：来源形态、UVLO/迟滞窗口、'
                '延时与被供电器件的要求顺序；使能脚耐压与所接轨按绝限核',
                'ER3', 'Expert Review', readiness='WAITING_EVIDENCE',
                required_inputs=sorted(set(gaps + [
                    'datasheet:使能门限/迟滞/耐压', 'requirements:上电顺序与时序要求'])),
                trigger=['power-up:' + item['id'], 'enable-source:' + item['enable_source']],
                handoff=handoff({'required': True, 'receivers': ['Test'],
                                 'constraint': '上电/掉电单调性与台阶需实测覆盖最坏负载',
                                 'verification': '上电波形实测与最坏工况复核'}, 'APPLICABLE'))
            check['domain'] = 'POWER_UP'
            check['inventory_gaps'] = gaps
            if item['kind'] == 'linear':
                ready = planner.evidence_ready('PU-10', obj)
                check = planner.add_check(
                    'power-up-dropout-' + item['id'], dict(obj),
                    '按最低输入电压与最坏压差（最低温度、最大负载）核输出是否仍高于负载要求下限',
                    'ER4', 'AC0-HOT', readiness='READY' if ready else 'WAITING_EVIDENCE',
                    required_inputs=sorted(set(gaps + ([] if ready else [
                        'evidence: PU-10 输入下限、最坏压差与负载要求下限']))),
                    trigger=['power-up:' + item['id']], rule='PU-10')
                check['domain'] = 'POWER_UP'
                check['inventory_gaps'] = gaps
            else:
                check = planner.add_check(
                    'power-up-prebias-' + item['id'], dict(obj),
                    '核同步变换器的预偏置启动支持与软启动配置：资料是否明确支持预偏置、'
                    '软启动时间与输入浪涌/限流的配合',
                    'ER3', 'Expert Review', readiness='WAITING_EVIDENCE',
                    required_inputs=sorted(set(gaps + [
                        'datasheet:预偏置启动与软启动条款', 'intent:输出并联/保持电路'])),
                    trigger=['power-up:' + item['id']])
                check['domain'] = 'POWER_UP'
                check['inventory_gaps'] = gaps

    def cold_findings(self, lint, inventory):
        for state in inventory['states']:
            for load in state.get('coupled_loads', []):
                lint.add('PU-01', self.cold_rules['PU-01'],
                         '%s（状态 %s）：%s 与本器件供电轨同为 %s，使能/复位随电源同时建立；'
                         '需确认器件允许该时序或另加延时'
                         % (load['ref'], state['id'], load['node'], load['net']),
                         load['ref'], kind='CANDIDATE')
            for item in state['regulators']:
                if item['enable_source'] == 'unknown':
                    lint.add('PU-03', self.cold_rules['PU-03'],
                             '%s（状态 %s）：使能脚 %s 所在网 %s 上未找到驱动源、UVLO 分压、'
                             'RC 或任何上/下拉；悬空或来源不明时开启行为不确定'
                             % (item['ref'], state['id'], item['enable_node'],
                                item['enable_net']),
                             item['ref'], kind='CANDIDATE')
                if item['enable_source'] != 'tied-to-input':
                    continue
                lint.add('PU-02', self.cold_rules['PU-02'],
                         '%s（状态 %s）：使能脚 %s 直连输入网 %s，未见 UVLO 分压或 RC 延时；'
                         '输入缓慢爬升或跌落时的开启点由器件内部门限决定'
                         % (item['ref'], state['id'], item['enable_node'], item['enable_net']),
                         item['ref'], kind='CANDIDATE')

    def hot_check(self, lint, check):
        vin_min = hotmath.number(check['vin_min_v'])
        dropout = hotmath.number(check['dropout_max_v'])
        required = hotmath.number(check['vout_required_min_v'])
        available = vin_min - dropout
        margin = available - required
        calculation = {'vin_min_v': vin_min, 'dropout_max_v': dropout,
                       'available_v': available, 'vout_required_min_v': required,
                       'margin_v': margin}
        detail = '%s: 最低输入 %s − 最坏压差 %s = %s，负载要求下限 %s，裕量 %s；%s' % (
            check.get('ref') or check.get('net'), hotmath.fmt(vin_min, 'V'),
            hotmath.fmt(dropout, 'V'), hotmath.fmt(available, 'V'),
            hotmath.fmt(required, 'V'), hotmath.fmt(margin, 'V'), lint._citation(check))
        if margin < 0:
            lint.add('PU-10', '最坏压差下输出低于负载要求', detail,
                     check.get('ref'), check_id=check['id'], citation=check['citation'],
                     calculation=calculation)
        else:
            lint.record_pass('PU-10', check, detail,
                             scope='所给保证值下的静态压差裕量；负载瞬态、启动与热关断未判定',
                             calculation=calculation)

    def evidence_errors(self, check, label):
        errors = hotmath.structure_errors(
            check, label, numbers=('vin_min_v', 'dropout_max_v', 'vout_required_min_v'))
        for field in ('vin_min_v', 'vout_required_min_v'):
            value = hotmath.number(check.get(field))
            if value is not None and value <= 0:
                errors.append('%s.%s 必须为正数' % (label, field))
        dropout = hotmath.number(check.get('dropout_max_v'))
        if dropout is not None and dropout < 0:
            errors.append('%s.dropout_max_v 不得为负' % label)
        return errors

    def model_gaps(self, check):
        return hotmath.missing_gaps(
            check, numbers=('vin_min_v', 'dropout_max_v', 'vout_required_min_v'))

    def rule_instances(self, rule, inventory):
        if rule == 'PU-01':
            return sorted({load['id'] for state in inventory['states']
                           for load in state.get('coupled_loads', [])})
        wanted = 'unknown' if rule == 'PU-03' else 'tied-to-input'
        return sorted({item['id'] for _, item in inv.walk(inventory, 'regulators')
                       if item['enable_source'] == wanted})

    def binds(self, item):
        obj = item.get('object')
        return isinstance(obj, dict) and bool(obj.get('power_up'))

    def object_errors(self, key, obj, inventory):
        known = {item['id'] for _, item in inv.walk(inventory, 'regulators')}
        if (obj.get('power_up') not in known
                or obj.get('power_up_digest') != inventory['digest']):
            return [key + ': stale power up object binding']
        return []

    def pass_blockers(self, key, planned, generated, inventory):
        if generated and generated.get('inventory_gaps'):
            return [key + ': power up gaps must be resolved in a regenerated plan before PASS']
        return []
