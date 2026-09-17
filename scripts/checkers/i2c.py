#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""I²C 连接覆盖检查器（清单引擎仍在 scripts/i2c_topology.py）。"""
from copy import deepcopy

from i2c_topology import build_i2c_topology, validate_i2c_intent

from .base import Checker


class I2CTopologyChecker(Checker):
    id = 'i2c_topology'
    title = 'I²C 连接覆盖'
    plan_key = 'i2c_topology'
    intent_key = 'i2c_topology'

    missing_inventory_message = 'I2C checks require their topology inventory'
    inventory_type_message = 'I2C topology must be an object'
    requires_db_message = 'I2C topology validation requires --db'
    stale_message = 'I2C topology inventory/binding is stale or modified'
    invalid_message = 'invalid I2C topology: '

    def incomplete_message(self, key):
        return key + ': incomplete I2C planned coverage'

    def changed_message(self, key):
        return key + ': I2C generated criterion/object changed'

    def validate_intent(self, intent, db):
        return validate_i2c_intent(intent, db)

    def build(self, db, intent):
        return build_i2c_topology(db, intent)

    def plan(self, planner, inventory):
        """Inventory coverage is independent of the existing direct Rule-09 check."""
        for state in inventory['states']:
            for region in state['regions']:
                obj = {'net': region['nets'][0], 'nets': region['nets'], 'state': state['id'],
                       'i2c_region': region['id'], 'i2c_topology_digest': inventory['digest']}
                key = region['id']
                item = planner.add_check(
                    'i2c-topology-' + key, obj,
                    '核对本状态 SDA/SCL 物理端点、装配/跳线、全部上拉与电源域、串阻路径及隔离/外接边界；仅连接覆盖',
                    'ER3', 'Expert Review',
                    readiness='WAITING_EVIDENCE' if region['gaps'] else 'READY',
                    required_inputs=region['gaps'], trigger=['i2c-topology:' + key])
                item['domain'] = 'I2C'
                item['analysis_required'] = True
                for check, criterion in planner.circuit_checks['I2C']:
                    item = planner.add_check(
                        'i2c-region-' + key + '-' + check, deepcopy(obj),
                        criterion + '；按拓扑清单保留串阻节点及跨段耦合，不把远端上拉直接并联或跨有源器件合并',
                        'ER4', 'Expert Review', readiness='WAITING_EVIDENCE',
                        required_inputs=region['gaps'] + ['I2C:' + check + ': applicable specifications and state-specific analysis'],
                        trigger=['i2c-topology:' + key])
                    item['domain'] = 'I2C'
                    item['analysis_required'] = True

    def binds(self, item):
        obj = item.get('object')
        return isinstance(obj, dict) and bool(obj.get('i2c_region'))

    @staticmethod
    def _regions(inventory):
        return {region['id']: region for state in inventory['states'] for region in state['regions']}

    def object_errors(self, key, obj, inventory):
        regions = self._regions(inventory)
        region = regions.get(obj.get('i2c_region'))
        if region is None or obj.get('i2c_topology_digest') != inventory['digest']:
            return [key + ': stale I2C object binding']
        if not (obj.get('state') == region['state'] and obj.get('nets') == region['nets']
                and obj.get('net') == region['nets'][0]):
            return [key + ': I2C region coordinates differ']
        return []

    def pass_blockers(self, key, planned, generated, inventory):
        obj = planned.get('object') if isinstance(planned.get('object'), dict) else {}
        region = self._regions(inventory).get(obj.get('i2c_region'))
        if region and region['gaps']:
            return [key + ': I2C topology gaps must be resolved in a regenerated plan before PASS']
        return []
