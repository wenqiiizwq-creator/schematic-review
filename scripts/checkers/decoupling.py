#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""去耦覆盖检查器（清单引擎仍在 scripts/decoupling.py）。"""
from copy import deepcopy

from decoupling import build_decoupling_inventory, validate_decoupling_intent

from .base import Checker
from .planutil import handoff, slug

DEFAULT_CRITERIA = {
    'connection': '按准确器件条款核对本组各电源脚与指定返回节点的去耦接法；同网存在电容不证明布局充分',
    'capacitance': '按本状态器件条款分别核对数量、容量组合及适用的有效容量要求；标称总量不能替代偏压/温度/公差后的保证容量',
    'rating': '按具体电容料号与项目工况核对耐压/降额及适用 ESR 要求；不从封装或轨名猜参数',
}


class DecouplingChecker(Checker):
    id = 'decoupling'
    title = '去耦覆盖'
    plan_key = 'decoupling'
    version_key = 'decoupling_version'
    version = 1
    intent_key = 'decoupling'

    missing_inventory_message = 'decoupling checks require their inventory'
    inventory_type_message = 'decoupling inventory must be an object'
    requires_db_message = 'decoupling inventory validation requires --db'
    stale_message = 'decoupling inventory/binding is stale or modified'
    invalid_message = 'invalid decoupling inventory: '
    allow_manual_bound_objects = False
    generated_fields = ('check', 'rule', 'object', 'criterion', 'stage', 'executor',
                        'readiness', 'required_inputs', 'trigger', 'inventory_gaps',
                        'required_material_refs', 'handoff')

    def incomplete_message(self, key):
        return key + ': incomplete decoupling planned coverage'

    def changed_message(self, key):
        return key + ': decoupling generated criterion/object changed'

    def manual_addition_message(self, key):
        return key + ': use an independent object for manual decoupling additions'

    def validate_intent(self, intent, db):
        return validate_decoupling_intent(intent, db)

    def build(self, db, intent):
        return build_decoupling_inventory(db, intent)

    def plan(self, planner, inventory):
        """Direct-net inventory and separate, source-bound engineering criteria."""
        obj = {'feature': 'DECOUPLING-INVENTORY', 'decoupling_scope': 'inventory',
               'decoupling_inventory_digest': inventory['digest']}
        item = planner.add_check('decoupling-discovery', obj,
            '结合完整器件/官方物理脚清单核对供电脚与去耦分组覆盖；无名称命中不代表不适用',
            'ER1', 'Expert Review',
            readiness='WAITING_EVIDENCE' if inventory['discovery_gaps'] else 'READY',
            required_inputs=inventory['discovery_gaps'], trigger=['decoupling-inventory'])
        item['inventory_gaps'] = inventory['discovery_gaps']
        for state in inventory['states']:
            for group in state['groups']:
                obj = {'ref': group['ref'], 'nodes': group['supply_nodes'], 'return_nodes': group['return_nodes'],
                       'nets': group['supply_nets'], 'return_nets': group['return_nets'], 'state': state['id'],
                       'decoupling_group': group['id'], 'decoupling_inventory_digest': inventory['digest']}
                if len(group['supply_nets']) == 1:
                    obj['net'] = group['supply_nets'][0]
                item = planner.add_check('decoupling-coverage-' + group['id'], deepcopy(obj),
                    '逐物理脚核对本状态分组、返回节点、直接连接电容和装配；保留零电容/不贴/未知项，不跨串联边界，不判断电气合格',
                    'ER1', 'Expert Review', readiness='WAITING_EVIDENCE' if group['gaps'] else 'READY',
                    required_inputs=group['gaps'], trigger=['decoupling-group:' + group['id']])
                item['inventory_gaps'] = group['gaps']
                for kind, default in DEFAULT_CRITERIA.items():
                    requirements = [r for r in group['requirements'] if r['kind'] == kind]
                    for req in requirements or [None]:
                        gaps = group['gaps'] + (group['capacitance_gaps'] if kind == 'capacitance' else [])
                        if req is None:
                            gaps = gaps + ['datasheet-requirement:' + kind]
                        key = group['id'] + '-' + kind + ('-' + slug(req['id']) if req else '')
                        item = planner.add_check('decoupling-' + key, deepcopy(obj),
                            req['criterion'] if req else default,
                            'ER2' if kind == 'connection' else 'ER4', 'Expert Review',
                            readiness='WAITING_EVIDENCE',
                            required_inputs=gaps + ['state-specific engineering evidence: ' + kind],
                            trigger=['decoupling-group:' + group['id']] + ([req['citation']] if req else []),
                            handoff=handoff({'required': kind == 'connection', 'receivers': ['PCB Layout'],
                                'constraint': '按本组实际器件条款落实去耦位置、回流与环路；同网共享电容不证明各器件本地去耦充分',
                                'verification': '核对本组各供电脚、实际电容与返回路径的 PCB 摆放和回路'}, 'APPLICABLE'))
                        item['inventory_gaps'] = sorted(set(gaps))
                        item['analysis_required'] = True
                        item['domain'] = 'DECOUPLING'
                        item['required_material_refs'] = sorted(set([group['ref']] + group['fitted_capacitors']))

    def binds(self, item):
        obj = item.get('object')
        if isinstance(item.get('check'), str) and item['check'].startswith('decoupling-'):
            return True
        return isinstance(obj, dict) and ('decoupling_group' in obj or 'decoupling_scope' in obj)

    def extra_presence_trigger(self, plan):
        return self.version_key in plan

    def object_errors(self, key, obj, inventory):
        if 'decoupling_group' not in obj and 'decoupling_scope' not in obj:
            return []
        if obj.get('decoupling_inventory_digest') != inventory['digest']:
            return [key + ': stale decoupling object binding']
        return []

    def pass_blockers(self, key, planned, generated, inventory):
        if generated and generated.get('inventory_gaps'):
            return [key + ': decoupling gaps must be resolved in a regenerated plan before PASS']
        return []
