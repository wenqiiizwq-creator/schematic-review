#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""高速差分电平互连检查器。

按网名成对与共同器件识别差分对，登记耦合方式、端接与偏置网络。电平标准来自
声明或名称线索：名称只产生待核项，不产生结论。共模/摆幅是否兼容按 ER1 提供
的保证范围热跑；阻抗、等长、间距与回流属 PCB HANDOFF。
"""
import re

from . import hotmath
from . import inventory as inv
from . import netgraph as ng
from . import powertree
from . import states as state_lib
from .base import Checker
from .planutil import handoff, slug

SUFFIXES = (('_P', '_N'), ('_DP', '_DN'), ('_DP', '_DM'), ('P', 'N'), ('+', '-'))
STANDARD_RE = re.compile(r'(LVDS|LVPECL|PECL|CML|HCSL|SSTL|HSTL)', re.I)
DC_PATH_KINDS = {ng.RESISTOR, ng.INDUCTOR, ng.FERRITE}
OUT_PINTYPES = {'OUT', 'OUTPUT', 'TRISTATE', '3STATE'}
IN_PINTYPES = {'IN', 'INPUT'}
MAX_PAIRS = 128


def validate_diff_levels_intent(intent, db=None):
    """校验可选的 diff_levels 配置段。"""
    return state_lib.section_errors(
        intent, db, 'diff_levels', item_field='pairs', item_key='pair',
        fields=('id', 'ref', 'standard', 'p_net', 'n_net', 'citation'),
        net_fields=('p_net', 'n_net'))


class _Scan:
    def __init__(self, db, state, declared, excluded):
        self.graph = ng.NetGraph(db, fitted=set(state['fitted']))
        self.state = state
        self.declared = declared
        self.excluded = excluded
        self.grounds = {net for net in self.graph.nets if ng.is_ground(net)}
        self.tree = powertree.PowerTree(self.graph)

    def _pairs(self):
        """成对网名 + 共同器件；只有一侧或无共同器件的不算差分对。"""
        nets = set(self.graph.nets) - self.graph.pseudo
        found = []
        for net in sorted(nets):
            upper = net.upper()
            for positive, negative in SUFFIXES:
                if not upper.endswith(positive):
                    continue
                base = net[:len(net) - len(positive)]
                partner = base + negative
                candidates = [x for x in nets if x.upper() == (base + negative).upper()]
                if not candidates:
                    continue
                partner = candidates[0]
                # 同一器件同时出现在两条腿上才算一对；交流耦合的两侧各自成对。
                if not set(self._devices(net)) & set(self._devices(partner)):
                    continue
                found.append((base or net, net, partner))
                break
        return found

    def _devices(self, net):
        return [ref for ref in self.graph.refs_on(net)
                if self.graph.kind(ref) in (ng.IC, ng.CONNECTOR)]

    def _leg(self, net, other_leg):
        graph = self.graph
        series = [(ref, other) for ref, other in graph.neighbors(net, {ng.CAPACITOR})
                  if other not in self.grounds]
        terminations = [{'ref': ref, 'to': other}
                        for ref, other in graph.neighbors(net, {ng.RESISTOR})]
        dc_path = sorted({other for ref, other in graph.neighbors(net, DC_PATH_KINDS)
                          if other in self.grounds or self.tree.is_rail(other)})
        direction = 'unknown'
        for node in graph.nodes_on(net):
            pintype = ng.normalize(graph.pintype.get(node))
            if pintype in OUT_PINTYPES:
                direction = 'driver'
            elif pintype in IN_PINTYPES and direction == 'unknown':
                direction = 'receiver'
        return {
            'net': net,
            'direction': direction,
            'series_caps': [{'ref': ref, 'to': other} for ref, other in sorted(series)],
            'terminations': sorted(terminations, key=lambda item: item['ref']),
            'differential_terminations': sorted(
                item['ref'] for item in terminations if item['to'] == other_leg),
            'dc_path': dc_path,
        }

    def _standard(self, base, nets, refs):
        for key in [slug(base)] + sorted(nets):
            if key in self.declared:
                return self.declared[key], 'declared'
        blob = base + ' ' + ' '.join(
            str(self.graph.parts.get(ref, {}).get(field) or '')
            for ref in refs for field in ('part', 'value', 'prim'))
        match = STANDARD_RE.search(blob)
        return (match.group(1).upper(), 'name-hint') if match else (None, 'unknown')

    def pairs(self):
        found = []
        for base, positive, negative in self._pairs():
            refs = sorted(set(self._devices(positive)) | set(self._devices(negative)))
            if any(ref in self.excluded for ref in refs):
                continue
            legs = [self._leg(positive, negative), self._leg(negative, positive)]
            standard, basis = self._standard(base, (positive, negative), refs)
            coupled = all(leg['series_caps'] for leg in legs)
            gaps = list(self.state['gaps'])
            if coupled and {leg['direction'] for leg in legs} == {'unknown'}:
                gaps.append('pair-direction:' + base)
            if standard is None:
                gaps.append('level-standard:' + base)
            found.append({
                'id': slug(base),
                'base': base,
                'refs': refs,
                'standard': standard,
                'basis': basis,
                'coupling': 'ac' if coupled else 'dc',
                'legs': legs,
                'gaps': sorted(set(gaps)),
            })
        return sorted(found, key=lambda item: item['id'])[:MAX_PAIRS]


def build_inventory(db, intent=None):
    """生成逐状态的差分对清单。"""
    cfg = (intent or {}).get('diff_levels') if isinstance(intent, dict) else None
    declared = {}
    for item in (cfg or {}).get('pairs', []) if isinstance(cfg, dict) else []:
        if not isinstance(item, dict) or not isinstance(item.get('standard'), str):
            continue
        # 声明可按对象 id 或任一条腿的网名落位，二者都指向同一对。
        for key in (slug(item.get('id')), item.get('p_net'), item.get('n_net')):
            if key:
                declared[key] = item['standard'].upper()
    excluded = state_lib.excluded_refs(cfg)
    gaps = [] if cfg else ['intent.diff_levels: 未声明差分电平标准与装配状态']
    return inv.build(db, cfg, 'pairs',
                     lambda state: _Scan(db, state, declared, excluded).pairs(), gaps)


class DiffLevelsChecker(Checker):
    id = 'diff_levels'
    title = '高速差分电平互连'
    plan_key = 'diff_levels'
    version_key = 'diff_levels_version'
    version = 1
    intent_key = 'diff_levels'
    cold_rules = {'DL-01': '交流耦合发送端无直流通路', 'DL-02': '交流耦合接收端无偏置/端接'}
    hot_rules = {'DL-10': '共模与摆幅兼容'}
    evidence_kinds = {'DL-10': {'diff_level'}}

    missing_inventory_message = 'diff level checks require their inventory'
    inventory_type_message = 'diff level inventory must be an object'
    requires_db_message = 'diff level inventory validation requires --db'
    stale_message = 'diff level inventory/binding is stale or modified'
    invalid_message = 'invalid diff level inventory: '

    def incomplete_message(self, key):
        return key + ': incomplete diff level planned coverage'

    def changed_message(self, key):
        return key + ': diff level generated criterion/object changed'

    def validate_intent(self, intent, db):
        return validate_diff_levels_intent(intent, db)

    def build(self, db, intent):
        return build_inventory(db, intent)

    def plan(self, planner, inventory):
        for state, pair in inv.walk(inventory, 'pairs'):
            obj = {'net': pair['legs'][0]['net'], 'state': state['id'],
                   'nets': sorted(leg['net'] for leg in pair['legs']),
                   'diff_pair': pair['id'], 'diff_levels_digest': inventory['digest']}
            applicability = 'APPLICABLE' if pair['basis'] == 'declared' else 'UNDETERMINED'
            gaps = pair['gaps']
            ready = applicability == 'APPLICABLE' and planner.evidence_ready('DL-10', obj)
            check = planner.add_check(
                'diff-level-compatibility-' + pair['id'], dict(obj),
                '按两端保证范围核电平兼容：直流耦合时发送共模与摆幅落在接收端共模/差分输入'
                '范围内；交流耦合时核接收端偏置共模、耦合电容与低频截止',
                'ER4', 'AC0-HOT', applicability=applicability,
                readiness='READY' if ready else 'WAITING_EVIDENCE',
                required_inputs=sorted(set(gaps + ([] if ready else [
                    'evidence: DL-10 两端共模/摆幅保证范围',
                    'intent.diff_levels: 电平标准与出处']))),
                trigger=['diff-pair:' + pair['id'], 'basis:' + pair['basis']], rule='DL-10')
            check['domain'] = 'DIFF_LEVELS'
            check['inventory_gaps'] = gaps
            check = planner.add_check(
                'diff-level-termination-' + pair['id'], dict(obj),
                '核端接与偏置网络的位置、阻值与电源域：差分端接、接收端偏置、'
                '发送端直流通路，以及未用通道与掉电状态的处置',
                'ER3', 'Expert Review', applicability=applicability,
                readiness='WAITING_EVIDENCE',
                required_inputs=sorted(set(gaps + [
                    'datasheet:收发两端电平与端接要求'])),
                trigger=['diff-pair:' + pair['id']],
                handoff=handoff({'required': True, 'receivers': ['PCB Layout', 'SI'],
                                 'constraint': '差分阻抗、等长、间距、参考平面连续与端接就近',
                                 'verification': 'SI 仿真或眼图实测'}, applicability))
            check['domain'] = 'DIFF_LEVELS'
            check['inventory_gaps'] = gaps

    def cold_findings(self, lint, inventory):
        for state, pair in inv.walk(inventory, 'pairs'):
            if pair['coupling'] != 'ac':
                continue
            head = '%s（%s，状态 %s）' % (pair['base'], pair['standard'] or '电平标准未定',
                                       state['id'])
            anchor = pair['refs'][0] if pair['refs'] else None
            # 发送端直流通路只对电流型 ECL 类电平成立，其余电平不据此下结论。
            current_mode = pair['standard'] in ('LVPECL', 'PECL')
            kind = 'FINDING' if pair['basis'] == 'declared' else 'CANDIDATE'
            for leg in pair['legs']:
                if current_mode and leg['direction'] == 'driver' and not leg['dc_path']:
                    lint.add('DL-01', self.cold_rules['DL-01'],
                             head + '：发送侧 ' + leg['net']
                             + ' 经耦合电容隔直，但未见到地/到轨的直流通路，'
                               '发射极或偏置电流路径需确认',
                             anchor, kind=kind)
                if leg['direction'] == 'receiver' and not leg['terminations'] and not leg['dc_path']:
                    lint.add('DL-02', self.cold_rules['DL-02'],
                             head + '：接收侧 ' + leg['net']
                             + ' 交流耦合后未见偏置或端接网络；'
                               '若由接收端内部偏置/端接需给出资料证据',
                             anchor, kind='CANDIDATE')

    def hot_check(self, lint, check):
        coupling = check['coupling']
        swing = hotmath.span(check['driver_swing_v'])
        receiver_cm = hotmath.span(check['receiver_common_mode_v'])
        receiver_diff = hotmath.span(check['receiver_input_diff_v'])
        source = ('bias_common_mode_v' if coupling == 'ac' else 'driver_common_mode_v')
        common_mode = hotmath.span(check[source])
        problems = []
        if common_mode[0] < receiver_cm[0] or common_mode[1] > receiver_cm[1]:
            problems.append('共模 [%s, %s]V 超出接收端范围 [%s, %s]V'
                            % (hotmath.fmt(common_mode[0]), hotmath.fmt(common_mode[1]),
                               hotmath.fmt(receiver_cm[0]), hotmath.fmt(receiver_cm[1])))
        if swing[0] < receiver_diff[0] or swing[1] > receiver_diff[1]:
            problems.append('差分摆幅 [%s, %s]V 超出接收端差分输入范围 [%s, %s]V'
                            % (hotmath.fmt(swing[0]), hotmath.fmt(swing[1]),
                               hotmath.fmt(receiver_diff[0]), hotmath.fmt(receiver_diff[1])))
        calculation = {'coupling': coupling, 'common_mode_source': source,
                       'common_mode_v': {'min': common_mode[0], 'max': common_mode[1]},
                       'driver_swing_v': {'min': swing[0], 'max': swing[1]},
                       'receiver_common_mode_v': {'min': receiver_cm[0], 'max': receiver_cm[1]},
                       'receiver_input_diff_v': {'min': receiver_diff[0], 'max': receiver_diff[1]}}
        detail = '%s: %s 耦合，共模 [%s, %s]V vs 接收 [%s, %s]V，摆幅 [%s, %s]V vs [%s, %s]V；%s' % (
            check.get('net') or check.get('ref'), coupling,
            hotmath.fmt(common_mode[0]), hotmath.fmt(common_mode[1]),
            hotmath.fmt(receiver_cm[0]), hotmath.fmt(receiver_cm[1]),
            hotmath.fmt(swing[0]), hotmath.fmt(swing[1]),
            hotmath.fmt(receiver_diff[0]), hotmath.fmt(receiver_diff[1]), lint._citation(check))
        if problems:
            lint.add('DL-10', '差分电平不兼容', detail + '；' + '；'.join(problems),
                     check.get('ref'), check_id=check['id'], citation=check['citation'],
                     calculation=calculation)
        else:
            lint.record_pass('DL-10', check, detail,
                             scope='所给保证范围下的共模/摆幅兼容；抖动、低频截止、阻抗与回流未判定',
                             calculation=calculation)

    def evidence_errors(self, check, label):
        return hotmath.structure_errors(
            check, label,
            spans=('driver_common_mode_v', 'driver_swing_v', 'bias_common_mode_v',
                   'receiver_common_mode_v', 'receiver_input_diff_v'),
            choices=(('coupling', {'ac', 'dc'}),))

    def model_gaps(self, check):
        spans = ['driver_swing_v', 'receiver_common_mode_v', 'receiver_input_diff_v']
        coupling = check.get('coupling')
        if coupling == 'ac':
            spans.append('bias_common_mode_v')
        elif coupling == 'dc':
            spans.append('driver_common_mode_v')
        return hotmath.missing_gaps(check, spans=spans, texts=('coupling',))

    def rule_instances(self, rule, inventory):
        return sorted({pair['id'] for _, pair in inv.walk(inventory, 'pairs')
                       if pair['coupling'] == 'ac'})

    def binds(self, item):
        obj = item.get('object')
        return isinstance(obj, dict) and bool(obj.get('diff_pair'))

    def object_errors(self, key, obj, inventory):
        known = {pair['id'] for _, pair in inv.walk(inventory, 'pairs')}
        if (obj.get('diff_pair') not in known
                or obj.get('diff_levels_digest') != inventory['digest']):
            return [key + ': stale diff level object binding']
        return []

    def pass_blockers(self, key, planned, generated, inventory):
        if generated and generated.get('inventory_gaps'):
            return [key + ': diff level gaps must be resolved in a regenerated plan before PASS']
        return []
