"""Shared binding gates for the connector and passive-network inventories."""
from .base import Checker
from .inventory import digest, walk
from . import states
from electrical_contract import db_fingerprint


def input_fingerprint(db):
    return digest({'electrical': db_fingerprint(db), **{k: db.get(k) for k in (
        'declared_pinname', 'declared_pintype', 'no_connect_nodes', 'pseudo_nets',
        'integrity', 'export_errors')}})


def text(value):
    return isinstance(value, str) and bool(value.strip())


def root_errors(intent, db, key, fields):
    if intent is None or isinstance(intent, dict) and key not in intent:
        return []
    if not isinstance(intent, dict) or not isinstance(intent.get(key), dict):
        return [key + ' must be an object']
    cfg, errors = intent[key], []
    if set(cfg) - set(fields) - {'schema_version', 'input_sha256', 'states'}:
        errors.append(key + ': unsupported fields')
    if type(cfg.get('schema_version')) is not int or cfg['schema_version'] != 1:
        errors.append(key + ': schema_version must be 1')
    if not text(cfg.get('input_sha256')) or (db is not None and cfg['input_sha256'] != input_fingerprint(db)):
        errors.append(key + ': missing/stale input_sha256')
    errors.extend(states.state_errors(cfg, db, key))
    return errors


def envelope(db, cfg, key, scanner, discovery_gaps=()):
    result = {'schema_version': 1, 'input_sha256': input_fingerprint(db),
              'context': cfg, 'discovery_gaps': list(discovery_gaps), 'states': []}
    for state in states.resolve(db, cfg):
        result['states'].append({'id': state['id'], 'citation': state['citation'],
                                 'gaps': state['gaps'], 'fitted': state['fitted'], key: scanner(state)})
    result['digest'] = digest(result)
    return result


class CoverageChecker(Checker):
    version = 1
    allow_manual_bound_objects = False
    item_key = ''

    def binds(self, item):
        return isinstance(item.get('object'), dict) and self.id in item['object']

    def object_errors(self, key, obj, inventory):
        known = {(s['id'], x['id']) for s, x in walk(inventory, self.item_key)} | {('all', 'discovery')}
        if ((obj.get('state'), obj.get(self.id)) not in known
                or obj.get(self.id + '_digest') != inventory['digest']):
            return [key + ': stale ' + self.id + ' object binding']
        return []

    def pass_blockers(self, key, planned, generated, inventory):
        if generated and generated.get('inventory_gaps'):
            return [key + ': ' + self.id + ' gaps must be resolved in a regenerated plan before PASS']
        return []

    def rule_instances(self, rule, inventory):
        # State is part of identity; two assembly variants are never one instance.
        return sorted(s['id'] + ':' + x['id'] for s, x in walk(inventory, self.item_key))

    def object(self, inventory, state, item):
        obj = {self.id: item['id'], self.id + '_digest': inventory['digest'], 'state': state['id']}
        for key in ('ref', 'node', 'net', 'refs', 'nets'):
            if item.get(key):
                obj[key] = item[key]
        return obj

    def discovery_plan(self, planner, inventory, criterion):
        item = planner.add_check(self.id + '-discovery',
            {self.id: 'discovery', self.id + '_digest': inventory['digest'],
             'state': 'all', 'feature': self.id}, criterion, 'ER3', 'Expert Review',
            readiness='WAITING_EVIDENCE', required_inputs=inventory['discovery_gaps'])
        item['inventory_gaps'] = inventory['discovery_gaps']
