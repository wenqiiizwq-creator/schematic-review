#!/usr/bin/env python3
"""State-bound decoupling inventory and review inputs, never a PDN solver.

Only direct supply/return pairs are matched. No series component is shorted,
no package/ESR/DC-bias estimate is invented, and no electrical verdict is made.
"""
import argparse
from collections import defaultdict
from copy import deepcopy
from decimal import Decimal, DecimalException
import json
import re

from electrical_contract import db_fingerprint, finite, load_json
from i2c_topology import digest, validate_db_shape

KINDS = {'capacitor', 'resistor', 'ferrite', 'inductor', 'jumper', 'switch', 'other'}
ROLES = {'power', 'return', 'other'}
REQUIREMENT_KINDS = {'connection', 'capacitance', 'rating'}
POWER = re.compile(r'(^|[_/.-])(VDD\w*|VCC\w*|AVDD\w*|AVCC\w*|DVDD\w*|DVCC\w*|VIN|VOUT|VBAT|VBUS)(?=$|[_/.-])', re.I)
GROUND = re.compile(r'(^|[_/.-])(?:GND\w*|[APD]?VSS\w*|[APD]GND\w*)(?=$|[_/.-])', re.I)
PASSIVE_PREFIX = re.compile(r'^(?:R|C|L|FB|F|JP|TP|J|P|CN|Y)\d', re.I)
CAP_TOKEN = re.compile(r'^(?:(\d+(?:\.\d*)?|\.\d+)([eE][+-]?\d+)?\s*([pPnNuUµμm]?)[fF]|'
                       r'(\d+(?:\.\d*)?|\.\d+)\s*([pPnNuUµμm])|'
                       r'(\d+)([pPnNuUµμm])(\d+))(?:$|(?=[/;,\s]))')


def _text(value):
    return isinstance(value, str) and bool(value.strip())


def _strings(value):
    return (isinstance(value, list) and bool(value) and all(_text(x) for x in value)
            and len(set(value)) == len(value))


def parse_capacitance(value):
    """Parse a leading explicit capacitance token; never infer a unit/code.

    Examples: 100nF, 4.7u/10%/16V, 4n7, 1e-6F. Bare 104 is ambiguous.
    Remaining BOM fields are not interpreted as tolerance, rating or DC bias.
    """
    if not isinstance(value, str):
        return None
    match = CAP_TOKEN.match(value.strip())
    if not match:
        return None
    a, exponent, unit, b, unit_b, c, unit_c, fraction = match.groups()
    number = a + (exponent or '') if a is not None else b if b is not None else c + '.' + fraction
    unit = unit if a is not None else unit_b if b is not None else unit_c
    scales = {'': '1', 'p': '1e-12', 'n': '1e-9', 'u': '1e-6', 'µ': '1e-6', 'μ': '1e-6', 'm': '1e-3'}
    try:
        result = float(Decimal(number) * Decimal(scales[unit.lower()]))
        return result if finite(result) and result > 0 else None
    except (DecimalException, OverflowError, ValueError):
        return None


def _db_errors(db):
    errors = validate_db_shape(db)
    if errors:
        return errors
    for field in ('declared_pinname', 'declared_pintype'):
        values = db.get(field, {})
        if not isinstance(values, dict) or not all(_text(n) and isinstance(v, str) for n, v in values.items()):
            errors.append('db.' + field + ' must map physical nodes to strings')
    return errors


def input_fingerprint(db):
    """Include symbol-only pins and pseudo/integrity data absent from db_fingerprint."""
    return digest({'db_sha256': db_fingerprint(db), **{k: db.get(k) for k in (
        'declared_pinname', 'declared_pintype', 'pseudo_nets', 'integrity', 'export_errors')}})


def validate_decoupling_intent(intent, db=None):
    if db is not None:
        errors = _db_errors(db)
        if errors:
            return errors
    if intent is None or isinstance(intent, dict) and 'decoupling' not in intent:
        return []
    if not isinstance(intent, dict) or not isinstance(intent.get('decoupling'), dict):
        return ['decoupling must be an object']
    cfg, errors = intent['decoupling'], []

    def require(ok, message):
        if not ok:
            errors.append('decoupling: ' + message)

    def fields(obj, allowed, label):
        require(not set(obj) - set(allowed), label + ' has unsupported fields')

    def ref_ok(ref):
        return _text(ref) and (db is None or ref in db.get('parts', {}))

    fields(cfg, ('schema_version', 'input_sha256', 'states', 'devices', 'groups', 'components'), 'root')
    require(type(cfg.get('schema_version')) is int and cfg['schema_version'] == 1, 'schema_version must be 1')
    require(isinstance(cfg.get('input_sha256'), str) and bool(re.fullmatch('[0-9a-f]{64}', cfg['input_sha256'])), 'input_sha256 is required')
    if db is not None:
        require(cfg.get('input_sha256') == input_fingerprint(db), 'stale input_sha256')
    states = cfg.get('states')
    require(isinstance(states, list) and 0 < len(states) <= 32, 'states must contain 1..32 states')
    seen = set()
    for state in states if isinstance(states, list) else []:
        if not isinstance(state, dict):
            require(False, 'state must be an object')
            continue
        fields(state, ('id', 'citation', 'population'), 'state')
        sid = state.get('id')
        require(_text(sid) and sid not in seen, 'state id missing/duplicate')
        if _text(sid):
            seen.add(sid)
        require(_text(state.get('citation')), 'state needs assembly/configuration citation')
        population = state.get('population', {})
        require(isinstance(population, dict), 'population must be an object')
        for ref, fitted in population.items() if isinstance(population, dict) else []:
            require(ref_ok(ref) and type(fitted) is bool, 'population needs known ref and boolean')
    devices = cfg.get('devices', {})
    require(isinstance(devices, dict), 'devices must be an object')
    for ref, device in devices.items() if isinstance(devices, dict) else []:
        require(ref_ok(ref), 'unknown device ' + str(ref))
        if not isinstance(device, dict):
            require(False, 'device must be an object')
            continue
        fields(device, ('mpn', 'package', 'identity_citation', 'citation', 'pinout_complete', 'pins'), 'device')
        for key in ('mpn', 'package', 'identity_citation', 'citation'):
            require(_text(device.get(key)), 'device ' + str(ref) + ' needs ' + key)
        require(type(device.get('pinout_complete')) is bool, 'pinout_complete must be boolean')
        pins = device.get('pins')
        require(isinstance(pins, dict) and bool(pins), 'device needs full physical pin map')
        for pin, spec in pins.items() if isinstance(pins, dict) else []:
            require(_text(pin) and '.' not in pin and not any(c.isspace() for c in pin), 'invalid physical pin number')
            if not isinstance(spec, dict):
                require(False, 'pin must be an object')
                continue
            fields(spec, ('name', 'role'), 'pin')
            require(_text(spec.get('name')), 'pin name missing')
            require(isinstance(spec.get('role'), str) and spec['role'] in ROLES, 'unsupported pin role')
    components = cfg.get('components', {})
    require(isinstance(components, dict), 'components must be an object')
    for ref, comp in components.items() if isinstance(components, dict) else []:
        require(ref_ok(ref), 'unknown component ' + str(ref))
        require(not isinstance(devices, dict) or ref not in devices, 'device cannot also be a passive/excluded component')
        if not isinstance(comp, dict):
            require(False, 'component must be an object')
            continue
        fields(comp, ('kind', 'citation'), 'component')
        require(isinstance(comp.get('kind'), str) and comp['kind'] in KINDS, 'unsupported component kind')
        require(_text(comp.get('citation')), 'component kind needs citation')
    groups = cfg.get('groups', [])
    require(isinstance(groups, list), 'groups must be an array')
    seen, used = set(), set()
    for group in groups if isinstance(groups, list) else []:
        if not isinstance(group, dict):
            require(False, 'group must be an object')
            continue
        fields(group, ('id', 'ref', 'supply_nodes', 'return_nodes', 'citation', 'requirements'), 'group')
        gid, ref = group.get('id'), group.get('ref')
        require(_text(gid) and gid not in seen, 'group id missing/duplicate')
        if _text(gid):
            seen.add(gid)
        require(ref_ok(ref), 'group ref missing/unknown')
        device = devices.get(ref) if isinstance(devices, dict) and _text(ref) else None
        require(isinstance(device, dict), 'group needs exact device pin map')
        require(_text(group.get('citation')), 'group needs grouping/return citation')
        pins = device.get('pins', {}) if isinstance(device, dict) else {}
        for field, role in (('supply_nodes', 'power'), ('return_nodes', 'return')):
            nodes = group.get(field)
            require(_strings(nodes), field + ' needs unique physical nodes')
            for node in nodes if _strings(nodes) else []:
                prefix, _, number = node.rpartition('.')
                spec = pins.get(number) if isinstance(pins, dict) else None
                require(prefix == ref and isinstance(spec, dict) and spec.get('role') == role, field + ' must match device physical pin roles: ' + node)
                if role == 'power':
                    require(node not in used, 'supply node belongs to multiple groups: ' + node)
                    used.add(node)
        requirements = group.get('requirements', [])
        require(isinstance(requirements, list), 'requirements must be an array')
        req_ids = set()
        for req in requirements if isinstance(requirements, list) else []:
            if not isinstance(req, dict):
                require(False, 'requirement must be an object')
                continue
            fields(req, ('id', 'kind', 'criterion', 'citation'), 'requirement')
            rid = req.get('id')
            require(_text(rid) and rid not in req_ids, 'requirement id missing/duplicate')
            if _text(rid):
                req_ids.add(rid)
            require(isinstance(req.get('kind'), str) and req['kind'] in REQUIREMENT_KINDS, 'unsupported requirement kind')
            require(_text(req.get('criterion')) and _text(req.get('citation')), 'requirement needs criterion/citation')
    return errors


class Inventory:
    def __init__(self, db, cfg):
        self.db, self.cfg = db, cfg or {}
        self.parts, self.nets = db.get('parts', {}), db.get('nets', {})
        self.pin2net = db.get('pin2net', {})
        self.names = dict(db.get('declared_pinname', {}), **db.get('pinname', {}))
        self.types = dict(db.get('declared_pintype', {}), **db.get('pintype', {}))
        self.devices, self.components = self.cfg.get('devices', {}), self.cfg.get('components', {})
        self.pseudo = set(db.get('pseudo_nets', []))
        self.nodes, self.input_gaps = defaultdict(set), set()
        for node in set(self.names) | set(self.types) | set(self.pin2net):
            ref, dot, number = node.rpartition('.')
            if not dot or not number or ref not in self.parts:
                self.input_gaps.add('invalid-physical-node:' + node)
            self.nodes[ref].add(node)
        seen = set()
        for net, nodes in self.nets.items():
            for node in nodes:
                ref = node.rpartition('.')[0]
                self.nodes[ref].add(node)
                if node in seen or self.pin2net.get(node) != net or ref not in self.parts:
                    self.input_gaps.add('inconsistent-index:' + node)
                seen.add(node)
        for node, net in self.pin2net.items():
            if node not in self.nets.get(net, []):
                self.input_gaps.add('inconsistent-index:' + node)
        if db.get('integrity', {}).get('self_check_passed') is False or db.get('export_errors'):
            self.input_gaps.add('netlist-integrity/export')

    def kind(self, ref):
        if ref in self.components:
            return self.components[ref]['kind']
        for pattern, kind in ((r'^C\d', 'capacitor'), (r'^R\d', 'resistor'), (r'^FB\d', 'ferrite'),
                              (r'^L\d', 'inductor'), (r'^JP\d', 'jumper')):
            if re.match(pattern, ref, re.I):
                return kind
        return 'unmodeled'

    def role(self, node):
        ref, _, pin = node.rpartition('.')
        spec = self.devices.get(ref, {}).get('pins', {}).get(pin)
        if spec:
            return spec['role']
        name, kind = self.names.get(node, ''), self.types.get(node, '').upper()
        if GROUND.search(name) or kind in ('GROUND', 'GND'):
            return 'return'
        if POWER.search(name) or kind in ('POWER', 'POWER_IN', 'POWER_OUT'):
            return 'power'
        return 'other'

    def groups(self):
        groups = [dict(deepcopy(g), origin='declared') for g in self.cfg.get('groups', [])]
        assigned = {n for g in groups for n in g['supply_nodes']}
        for ref, device in self.devices.items():
            self.nodes[ref].update(ref + '.' + pin for pin in device['pins'])
        for ref, nodes in sorted(self.nodes.items()):
            if self.kind(ref) != 'unmodeled':
                continue
            by_net = defaultdict(list)
            for node in sorted(nodes - assigned):
                if self.role(node) == 'power':
                    by_net[self.pin2net.get(node) or 'UNCONNECTED:' + node].append(node)
            returns = sorted(n for n in nodes if self.role(n) == 'return')
            for net, supplies in sorted(by_net.items()):
                groups.append({'id': 'candidate:' + ref + ':' + net, 'ref': ref,
                               'supply_nodes': supplies, 'return_nodes': returns,
                               'origin': 'candidate', 'requirements': []})
        return groups

    def device_inventory(self):
        output = []
        for ref, device in sorted(self.devices.items()):
            official = {ref + '.' + pin for pin in device['pins']}
            observed = {n for n in set(self.pin2net) | set(self.names) | set(self.types) if n.rpartition('.')[0] == ref}
            output.append({'ref': ref, **deepcopy(device), 'official_only_nodes': sorted(official - observed),
                           'symbol_only_nodes': sorted(observed - official)})
        return output

    def state_inventory(self, state, groups, device_records):
        population, sid = state.get('population', {}), state['id']
        caps, by_net = {}, defaultdict(set)
        for ref in sorted(self.parts):
            if self.kind(ref) != 'capacitor':
                continue
            nodes = sorted(self.nodes[ref])
            nets = [self.pin2net.get(n) for n in nodes]
            gaps = []
            if ref not in self.components:
                gaps.append('component-kind:' + ref)
            if len(nodes) != 2 or any(n is None or n in self.pseudo for n in nets):
                gaps.append('capacitor-two-pin-connection:' + ref)
            if len(nodes) == 2 and nets[0] == nets[1]:
                gaps.append('capacitor-same-net:' + ref)
            if ref not in population:
                gaps.append('population:' + ref)
            caps[ref] = {'ref': ref, 'nodes': nodes, 'nets': nets, 'value': self.parts[ref].get('value'),
                         'nominal_f': parse_capacitance(self.parts[ref].get('value')),
                         'populated': population.get(ref), 'parsed_nc': self.parts[ref].get('nc'),
                         'kind_basis': 'declared' if ref in self.components else 'refdes-hint',
                         'gaps': gaps, 'matched_groups': []}
            for net in set(nets) - {None}:
                by_net[net].add(ref)
        output = []
        devices = {d['ref']: d for d in device_records}
        for group in sorted(groups, key=lambda g: (g['ref'], g['id'], g['origin'])):
            ref = group['ref']
            gaps, value_gaps = set(self.input_gaps), set()
            if ref not in population:
                gaps.add('population:' + ref)
            device = devices.get(ref)
            if not device or not device['pinout_complete']:
                gaps.add('official-full-pinout:' + ref)
            if device:
                gaps.update('official-pin-absent:' + n for n in device['official_only_nodes'])
                gaps.update('pin-not-in-official-map:' + n for n in device['symbol_only_nodes'])
            if group['origin'] != 'declared':
                gaps.add('confirm-group-and-return:' + group['id'])
            supply_nets, return_nets = set(), set()
            for field, target in (('supply_nodes', supply_nets), ('return_nodes', return_nets)):
                for node in group[field]:
                    net = self.pin2net.get(node)
                    if net is None or net in self.pseudo:
                        gaps.add('unconnected-or-pseudo-pin:' + node)
                    elif node not in self.nets.get(net, []):
                        gaps.add('inconsistent-index:' + node)
                    else:
                        target.add(net)
            if len(supply_nets) != 1:
                gaps.add('need-one-direct-supply-net')
            if len(return_nets) != 1:
                gaps.add('need-one-explicit-return-net')
            if supply_nets & return_nets:
                gaps.add('supply-return-same-net')
            gid = 'DECAP-' + digest([sid, group['origin'], group['id'], ref])[:20]
            matches, rejected, fitted, boundaries = [], [], [], []
            touched = sorted(set().union(*(by_net[n] for n in supply_nets)))
            for cap_ref in touched:
                cap = caps[cap_ref]
                pair = len(cap['nodes']) == 2 and len(supply_nets) == len(return_nets) == 1 and (
                    supply_nets != return_nets and set(cap['nets']) == supply_nets | return_nets)
                if not pair:
                    rejected.append({'ref': cap_ref, 'nets': cap['nets'], 'reason': 'not-direct-supply-return-pair'})
                    # A malformed/unknown component cannot silently disappear.
                    if cap['populated'] is not False and (len(cap['nodes']) != 2 or None in cap['nets']):
                        gaps.update(cap['gaps'])
                    continue
                matches.append(cap_ref)
                cap['matched_groups'].append(gid)
                if cap['populated'] is not False:
                    gaps.update(cap['gaps'])
                if cap['populated'] is True and not cap['gaps']:
                    fitted.append(cap_ref)
                    if cap['nominal_f'] is None:
                        value_gaps.add('capacitance-unparsed:' + cap_ref)
            # Boundaries are listed, never crossed -- including 0R and closed jumpers.
            refs = {n.rpartition('.')[0] for net in supply_nets for n in self.nets.get(net, [])}
            for other_ref in sorted(refs - {ref} - set(caps)):
                nodes = sorted(self.nodes[other_ref])
                kind = self.kind(other_ref)
                if kind in ('resistor', 'ferrite', 'inductor', 'jumper', 'switch'):
                    boundaries.append({'ref': other_ref, 'kind': kind, 'nodes': nodes,
                                       'nets': [self.pin2net.get(n) for n in nodes],
                                       'populated': population.get(other_ref), 'crossed': False})
            known = [caps[c]['nominal_f'] for c in fitted if caps[c]['nominal_f'] is not None]
            subtotal = float(sum((Decimal(str(v)) for v in known), Decimal(0)))
            if not finite(subtotal):
                value_gaps.add('nominal-sum-overflow')
                subtotal = None
            output.append({'id': gid, 'declared_id': group['id'], 'origin': group['origin'],
                'ref': ref, 'state': sid, 'populated': population.get(ref),
                'supply_nodes': sorted(group['supply_nodes']), 'return_nodes': sorted(group['return_nodes']),
                'supply_nets': sorted(supply_nets), 'return_nets': sorted(return_nets),
                'capacitors': matches, 'fitted_capacitors': fitted, 'fitted_count': len(fitted),
                'known_nominal_subtotal_f': subtotal,
                'nominal_total_f': subtotal if not gaps and not value_gaps else None,
                'rejected_capacitors': rejected, 'boundaries': boundaries,
                'gaps': sorted(gaps), 'capacitance_gaps': sorted(value_gaps),
                'coverage': 'INCOMPLETE' if gaps else 'DISCOVERED',
                'observations': [] if fitted else ['no-confirmed-fitted-matching-capacitor'],
                'requirements': deepcopy(group.get('requirements', [])), 'citation': group.get('citation')})
        return {'id': sid, 'citation': state.get('citation'), 'groups': output,
                'capacitors': list(caps.values())}


def build_decoupling_inventory(db, intent=None):
    errors = _db_errors(db) or validate_decoupling_intent(intent, db)
    if errors:
        raise ValueError('; '.join(errors))
    cfg = (intent or {}).get('decoupling')
    inv = Inventory(db, cfg)
    groups, device_records = inv.groups(), inv.device_inventory()
    unknown = sorted(ref for ref in inv.parts if ref not in inv.devices and
                     ref not in inv.components and not PASSIVE_PREFIX.match(ref))
    discovery_gaps = set(inv.input_gaps) | {'unverified-device-pinout:' + r for r in unknown}
    for device in device_records:
        if not device['pinout_complete']:
            discovery_gaps.add('official-full-pinout:' + device['ref'])
        discovery_gaps.update('official-pin-absent:' + n for n in device['official_only_nodes'])
        discovery_gaps.update('pin-not-in-official-map:' + n for n in device['symbol_only_nodes'])
    states = (cfg or {}).get('states', [{'id': 'UNSPECIFIED', 'population': {}}])
    result = {'schema_version': 1, 'db_sha256': db_fingerprint(db), 'input_sha256': input_fingerprint(db),
              'context': deepcopy(cfg), 'scope': 'direct-net schematic inventory only; nominal is not effective capacitance; no electrical or PCB PASS',
              'devices': device_records, 'unverified_device_refs': unknown, 'discovery_gaps': sorted(discovery_gaps),
              'states': [inv.state_inventory(s, groups, device_records) for s in sorted(states, key=lambda s: s['id'])]}
    result['digest'] = digest(result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('db')
    parser.add_argument('--intent')
    parser.add_argument('--json', required=True)
    args = parser.parse_args()
    try:
        intent = load_json(args.intent) if args.intent else None
        if args.intent and not isinstance(intent, dict):
            raise ValueError('explicit intent.json root must be an object')
        result = build_decoupling_inventory(load_json(args.db), intent)
    except (OSError, ValueError) as error:
        parser.error(str(error))
    with open(args.json, 'w', encoding='utf-8') as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2, allow_nan=False)
    groups = [g for s in result['states'] for g in s['groups']]
    print(f'Decoupling inventory: {len(groups)} groups; no electrical verdict; input_sha256={result["input_sha256"]}')


if __name__ == '__main__':
    main()
