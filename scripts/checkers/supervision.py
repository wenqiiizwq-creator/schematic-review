#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""监控与看门狗检查器：复位链、喂狗输入与逐电源域监控覆盖。

识别监控器/看门狗器件、被监测的轨、复位输出到复位输入的路径与喂狗来源。
哪些轨必须监测由需求裁定，本检查器只登记覆盖与缺口；脉宽是否足够按保证值热跑。
"""
import re

from . import hotmath
from . import inventory as inv
from . import netgraph as ng
from . import powertree
from . import states as state_lib
from .base import Checker
from .planutil import handoff, slug

WATCHDOG_IN_RE = re.compile(r'^(WDI|WDT_IN|WD_IN|WATCHDOG_IN|SET\d?)$', re.I)
RESET_OUT_RE = re.compile(r'^(N?RESET\w*|N?RST\w*|WDO|POR|PFO|RESETB|RESET_N)$', re.I)
RESET_IN_RE = re.compile(r'^(N?RESET\w*|N?RST\w*|MR|POR|XRES|RESETN|NRST)$', re.I)
SENSE_RE = re.compile(r'^(SENSE\d*|VMON\d*|VSENSE\d*|PFI|IN\d*_MON|MON\d*)$', re.I)
MANUAL_RESET_RE = re.compile(r'^(MR|MRESET|PB_RST)$', re.I)
SUPPLY_PIN_RE = re.compile(r'^(VDD\w*|VCC\w*|AVDD\w*|AVCC\w*|DVDD\w*|VIN\d*|PVIN\d*|VBAT)$', re.I)
OUT_PINTYPES = {'OUT', 'OUTPUT', 'TRISTATE', '3STATE', 'BI', 'BIDI', 'BIDIR', 'IO', 'I/O'}
MAX_RAILS = 64


def validate_supervision_intent(intent, db=None):
    """校验可选的 supervision 配置段。"""
    return state_lib.section_errors(
        intent, db, 'supervision', item_field='supervisors', item_key='supervisor',
        fields=('id', 'ref', 'monitored_nets', 'citation'))


class _Scan:
    def __init__(self, db, state, declared, excluded):
        self.graph = ng.NetGraph(db, fitted=set(state['fitted']))
        self.state = state
        self.declared = declared
        self.excluded = excluded
        self.grounds = {net for net in self.graph.nets if ng.is_ground(net)}
        self.tree = powertree.PowerTree(self.graph)

    def _reset_destinations(self, net, source_ref):
        found = []
        for node in self.graph.nodes_on(net):
            ref = node.partition('.')[0]
            if ref == source_ref or not self.graph.is_fitted(ref):
                continue
            kind = self.graph.kind(ref)
            name = ng.normalize(self.graph.pinname.get(node))
            if kind is ng.IC and name and RESET_IN_RE.match(name):
                found.append({'node': node, 'role': 'reset-input'})
            elif kind is ng.IC:
                found.append({'node': node, 'role': 'other-pin'})
            elif kind is ng.CONNECTOR:
                found.append({'node': node, 'role': 'external'})
        return sorted(found, key=lambda item: item['node'])

    def _watchdog_input(self, net, source_ref):
        drivers = []
        for node in self.graph.nodes_on(net):
            ref = node.partition('.')[0]
            if ref == source_ref or not self.graph.is_fitted(ref):
                continue
            if self.graph.kind(ref) in (ng.IC, ng.CONNECTOR):
                drivers.append(node)
        if ng.is_rail(net) or net in self.grounds:
            state = 'tied'          # 直接坐在电源/地上，喂狗不可能翻转
        elif drivers:
            state = 'driven'
        elif any(ng.is_rail(other) or other in self.grounds
                 for _, other in self.graph.neighbors(net, {ng.RESISTOR})):
            state = 'pulled-only'
        else:
            state = 'floating'
        return {'net': net, 'state': state, 'drivers': sorted(drivers)}

    def supervisors(self):
        graph, found = self.graph, []
        for ref in sorted(graph.parts):
            if ref in self.excluded or not graph.is_fitted(ref) or graph.kind(ref) is not ng.IC:
                continue
            watchdogs = self.graph.named_pins(ref, WATCHDOG_IN_RE)
            senses = self.graph.named_pins(ref, SENSE_RE)
            manual = self.graph.named_pins(ref, MANUAL_RESET_RE)
            outputs = {node: net for node, net in self.graph.named_pins(ref, RESET_OUT_RE).items()
                       if ng.normalize(graph.pintype.get(node)) in OUT_PINTYPES
                       or ng.normalize(graph.pinname.get(node)) in {'WDO', 'PFO'}
                       or watchdogs or senses or manual}
            if not (watchdogs or senses or manual) or not outputs:
                continue
            supplies = sorted(set(self.graph.named_pins(ref, SUPPLY_PIN_RE).values()))
            found.append({
                'id': slug(ref),
                'ref': ref,
                'basis': 'declared' if ref in self.declared else 'topology',
                'watchdog_inputs': [self._watchdog_input(net, ref)
                                    for net in sorted(set(watchdogs.values()))],
                'monitored_nets': sorted(set(senses.values())),
                'supply_nets': supplies,
                'reset_outputs': [
                    {'node': node, 'net': net,
                     'destinations': self._reset_destinations(net, ref),
                     'pulls': sorted({other for _, other in graph.neighbors(net, {ng.RESISTOR})
                                      if self.tree.is_rail(other)})}
                    for node, net in sorted(outputs.items())],
                'gaps': sorted(set(self.state['gaps'])),
            })
        return sorted(found, key=lambda item: item['id'])

    def rails(self, supervisors):
        """给 IC 供电的电源轨及其监测覆盖；哪些必须监测由需求裁定。"""
        graph = self.graph
        sensed = {net for item in supervisors for net in item['monitored_nets']}
        supplied = {net for item in supervisors for net in item['supply_nets']}
        found = []
        for net in sorted(graph.nets):
            basis = self.tree.rail_basis(net)
            if basis is None:
                continue
            loads = [node for node in graph.nodes_on(net)
                     if graph.kind(node.partition('.')[0]) is ng.IC
                     and SUPPLY_PIN_RE.match(ng.normalize(graph.pinname.get(node)) or '')]
            if not loads:
                continue
            divided = [other for _, other in graph.neighbors(net, {ng.RESISTOR})
                       if other in sensed]
            monitor = ('sense-pin' if net in sensed else
                       'divider' if divided else
                       'supervisor-supply' if net in supplied else None)
            found.append({'net': net, 'basis': basis, 'monitor': monitor,
                          'loads': sorted({node.partition('.')[0] for node in loads}),
                          'gaps': ([] if basis != powertree.NAME_HINT
                                   else ['rail-identity:' + net])})
        return found[:MAX_RAILS]


def build_inventory(db, intent=None):
    """生成逐状态的监控清单。"""
    cfg = (intent or {}).get('supervision') if isinstance(intent, dict) else None
    declared = state_lib.declared_refs(cfg, 'supervisors')
    excluded = state_lib.excluded_refs(cfg)
    gaps = [] if cfg else ['intent.supervision: 未声明必须监测的电源域与喂狗策略']
    scans = {}

    def scanner(state):
        scan = scans.setdefault(state['id'], _Scan(db, state, declared, excluded))
        return scan.supervisors()

    def extra(state):
        scan = scans.setdefault(state['id'], _Scan(db, state, declared, excluded))
        return {'rails': scan.rails(scan.supervisors())}

    return inv.build(db, cfg, 'supervisors', scanner, gaps, extra=extra)


class SupervisionChecker(Checker):
    id = 'supervision'
    title = '监控与看门狗'
    plan_key = 'supervision'
    version_key = 'supervision_version'
    version = 1
    intent_key = 'supervision'
    cold_rules = {'SV-01': '喂狗输入悬空或固定电平',
                  'SV-02': '复位/看门狗输出未到复位输入',
                  'SV-03': '存在未被监测的电源轨'}
    hot_rules = {'SV-10': '复位脉宽与上拉'}
    evidence_kinds = {'SV-10': {'reset_pulse'}}

    missing_inventory_message = 'supervision checks require their inventory'
    inventory_type_message = 'supervision inventory must be an object'
    requires_db_message = 'supervision inventory validation requires --db'
    stale_message = 'supervision inventory/binding is stale or modified'
    invalid_message = 'invalid supervision inventory: '

    def incomplete_message(self, key):
        return key + ': incomplete supervision planned coverage'

    def changed_message(self, key):
        return key + ': supervision generated criterion/object changed'

    def validate_intent(self, intent, db):
        return validate_supervision_intent(intent, db)

    def build(self, db, intent):
        return build_inventory(db, intent)

    def plan(self, planner, inventory):
        for state, item in inv.walk(inventory, 'supervisors'):
            obj = {'ref': item['ref'], 'state': state['id'], 'supervision': item['id'],
                   'supervision_digest': inventory['digest']}
            if item['reset_outputs']:
                obj['net'] = item['reset_outputs'][0]['net']
            gaps = item['gaps']
            check = planner.add_check(
                'supervision-reset-path-' + item['id'], dict(obj),
                '逐跳核复位链：输出类型与上拉电源域、极性、到每个复位输入的连通、'
                '喂狗来源在启动期与固件异常时的行为，以及手动复位/去抖接法',
                'ER3', 'Expert Review', readiness='WAITING_EVIDENCE',
                required_inputs=sorted(set(gaps + [
                    'datasheet:监控器阈值/脉宽/输出类型', 'firmware:启动期喂狗与超时窗口'])),
                trigger=['supervision:' + item['id']])
            check['domain'] = 'SUPERVISION'
            check['inventory_gaps'] = gaps
            ready = planner.evidence_ready('SV-10', obj)
            check = planner.add_check(
                'supervision-reset-pulse-' + item['id'], dict(obj),
                '按保证值核复位输出最小脉宽不低于目标复位输入要求；开漏输出须有上拉且电源域正确',
                'ER4', 'AC0-HOT', readiness='READY' if ready else 'WAITING_EVIDENCE',
                required_inputs=sorted(set(gaps + ([] if ready else [
                    'evidence: SV-10 复位脉宽保证值与目标器件要求']))),
                trigger=['supervision:' + item['id']], rule='SV-10')
            check['domain'] = 'SUPERVISION'
            check['inventory_gaps'] = gaps
        for state in inventory['states']:
            rails = state.get('rails', [])
            if not rails:
                continue
            obj = {'feature': 'supervision-rails', 'state': state['id'],
                   'supervision': 'rails-' + slug(state['id']),
                   'supervision_digest': inventory['digest']}
            check = planner.add_check(
                'supervision-rail-coverage-' + slug(state['id']), obj,
                '按需求确定哪些电源域必须监测，逐轨核监测点、阈值与动作；'
                '未监测的轨需给出书面依据，不能因为有一颗监控器就判全板覆盖',
                'ER2', 'Expert Review', readiness='WAITING_EVIDENCE',
                required_inputs=sorted(set(list(state['gaps']) + [
                    'requirements:必须监测的电源域与动作要求'])),
                trigger=['supervision-rails:' + state['id']],
                handoff=handoff({'required': False}, 'APPLICABLE'))
            check['domain'] = 'SUPERVISION'
            check['inventory_gaps'] = sorted(
                {'rail-unmonitored:' + rail['net'] for rail in rails if not rail['monitor']}
                | {gap for rail in rails for gap in rail['gaps']})

    def cold_findings(self, lint, inventory):
        for state in inventory['states']:
            for item in state['supervisors']:
                head = '%s（状态 %s）' % (item['ref'], state['id'])
                for watchdog in item['watchdog_inputs']:
                    if watchdog['state'] in ('floating', 'tied'):
                        lint.add('SV-01', self.cold_rules['SV-01'],
                                 head + '：喂狗输入网 ' + watchdog['net'] + ' 为 '
                                 + watchdog['state'] + '，看门狗可能被有意禁用，需书面确认',
                                 item['ref'], kind='CANDIDATE')
                for output in item['reset_outputs']:
                    targets = [x for x in output['destinations'] if x['role'] == 'reset-input']
                    if targets:
                        continue
                    detail = (head + '：复位/看门狗输出 ' + output['node'] + ' 所在网 '
                              + output['net'] + ' 未接到任何复位输入')
                    if output['destinations']:
                        lint.add('SV-02', self.cold_rules['SV-02'],
                                 detail + '（仅接到 '
                                 + '、'.join(x['node'] for x in output['destinations'])
                                 + '），需确认由该路径完成复位', item['ref'], kind='CANDIDATE')
                    else:
                        lint.add('SV-02', self.cold_rules['SV-02'],
                                 detail + '，复位链在此中断', item['ref'])
            if not state['supervisors']:
                continue
            unmonitored = [rail['net'] for rail in state.get('rails', []) if not rail['monitor']]
            if unmonitored:
                lint.add('SV-03', self.cold_rules['SV-03'],
                         '状态 %s：%s 未见监测点（sense 脚或到 sense 的分压）；'
                         '哪些轨必须监测由需求裁定'
                         % (state['id'], '、'.join(unmonitored)), kind='CANDIDATE')

    def hot_check(self, lint, check):
        pulse = hotmath.span(check['pulse_width_s'])
        required = hotmath.span(check['required_width_s'])
        net = lint.target_net(check)
        problems = []
        if pulse[0] < required[1]:
            problems.append('最小复位脉宽 %s 低于目标器件要求 %s'
                            % (hotmath.fmt(pulse[0], 's'), hotmath.fmt(required[1], 's')))
        pulls = [x for x in lint.pulls(net or '') if x['state'] == 'high']
        if check.get('output_type') == 'open_drain' and not pulls:
            problems.append('开漏输出所在网 %s 上未找到到电源轨的上拉电阻' % net)
        calculation = {'pulse_width_s': {'min': pulse[0], 'max': pulse[1]},
                       'required_width_s': {'min': required[0], 'max': required[1]},
                       'output_type': check.get('output_type'),
                       'pullups': [x['ref'] for x in pulls]}
        detail = '%s: 复位脉宽 [%s, %s]s，要求 [%s, %s]s，输出类型 %s；%s' % (
            net, hotmath.fmt(pulse[0]), hotmath.fmt(pulse[1]), hotmath.fmt(required[0]),
            hotmath.fmt(required[1]), check.get('output_type'), lint._citation(check))
        if problems:
            lint.add('SV-10', '复位脉宽/输出条件不满足要求', detail + '；' + '；'.join(problems),
                     check.get('ref'), check_id=check['id'], citation=check['citation'],
                     calculation=calculation)
        else:
            lint.record_pass('SV-10', check, detail,
                             scope='所给保证值下的复位脉宽与上拉存在性；阈值精度、迟滞与喂狗时序未判定',
                             calculation=calculation)

    def evidence_errors(self, check, label):
        return hotmath.structure_errors(
            check, label, spans=('pulse_width_s', 'required_width_s'),
            choices=(('output_type', {'open_drain', 'push_pull'}),))

    def model_gaps(self, check):
        return hotmath.missing_gaps(
            check, spans=('pulse_width_s', 'required_width_s'), texts=('output_type',))

    def rule_instances(self, rule, inventory):
        if rule == 'SV-03':
            return sorted({rail['net'] for state in inventory['states']
                           for rail in state.get('rails', []) if not rail['monitor']})
        return sorted({item['id'] for _, item in inv.walk(inventory, 'supervisors')})

    def binds(self, item):
        obj = item.get('object')
        return isinstance(obj, dict) and bool(obj.get('supervision'))

    def object_errors(self, key, obj, inventory):
        known = {item['id'] for _, item in inv.walk(inventory, 'supervisors')}
        known |= {'rails-' + slug(state['id']) for state in inventory['states']}
        if (obj.get('supervision') not in known
                or obj.get('supervision_digest') != inventory['digest']):
            return [key + ': stale supervision object binding']
        return []

    def pass_blockers(self, key, planned, generated, inventory):
        if generated and generated.get('inventory_gaps'):
            return [key + ': supervision gaps must be resolved in a regenerated plan before PASS']
        return []
