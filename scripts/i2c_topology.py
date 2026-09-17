#!/usr/bin/env python3
"""State-bound I2C connectivity inventory, never an electrical solver or verdict.

Traverse verified fitted two-pin resistors/closed jumpers for discovery. Keep
finite series resistance and translator boundaries; never flatten them into a
Rule-09 equivalent. Unverified population, names and external ports stay gaps.
"""
import argparse
from collections import defaultdict, deque
from copy import deepcopy
import hashlib
import json
import re

from electrical_contract import db_fingerprint, finite, load_json
from solve_dividers import parse_resistor

MAX_TRACE_NETS = 1024
KINDS = {'resistor', 'jumper', 'endpoint', 'connector', 'level_shifter', 'isolator', 'switch'}
BARRIERS = {'level_shifter', 'isolator', 'switch'}
RAIL = re.compile(r'^(?:\+?\d+(?:V\d*|\.\d+V)|VCC|VDD|VDDA|VCCA|VOUT|VBAT|AVDD|DVDD|VIN|VBUS)', re.I)
GROUND = re.compile(r'^(?:GND|PGND|AGND|DGND|EGND|VSS)(?:$|[_\d])', re.I)
SIGNAL = re.compile(r'(^|[_/.-])(SDA\d*|SCL\d*)(?=$|[_/.-])', re.I)
I2C_HINT = re.compile(r'(^|[_/.-])I2C\w*(?=$|[_/.-])', re.I)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def _text(value):
    return isinstance(value, str) and bool(value.strip())


def _strings(value):
    return isinstance(value, list) and bool(value) and all(_text(x) for x in value) and len(set(value)) == len(value)


def validate_db_shape(db):
    """Reject malformed index containers before hashing or graph iteration."""
    if not isinstance(db, dict):
        return ['db must be an object']
    errors = []
    for field in ('parts', 'nets', 'pin2net', 'pinname', 'pintype', 'integrity'):
        if field in db and not isinstance(db[field], dict):
            errors.append('db.' + field + ' must be an object')
    if errors:
        return errors
    if not all(_text(ref) and isinstance(part, dict) for ref, part in db.get('parts', {}).items()):
        errors.append('db.parts requires named part objects')
    if not all(_text(net) and isinstance(nodes, list) and all(_text(n) for n in nodes)
               for net, nodes in db.get('nets', {}).items()):
        errors.append('db.nets requires named arrays of physical nodes')
    for field in ('pin2net', 'pinname', 'pintype'):
        if not all(_text(node) and isinstance(value, str) for node, value in db.get(field, {}).items()):
            errors.append('db.' + field + ' requires string node/value pairs')
    if 'pseudo_nets' in db and not (isinstance(db['pseudo_nets'], list) and all(_text(n) for n in db['pseudo_nets'])):
        errors.append('db.pseudo_nets must be a string array')
    return errors


def validate_i2c_intent(intent, db=None):
    """Validate the optional, explicit topology context. No best-effort typo repair."""
    if db is not None:
        errors = validate_db_shape(db)
        if errors:
            return errors
    if intent is None or isinstance(intent, dict) and 'i2c_topology' not in intent:
        return []
    if not isinstance(intent, dict) or not isinstance(intent.get('i2c_topology'), dict):
        return ['i2c_topology must be an object']
    cfg, errors = intent['i2c_topology'], []
    def require(ok, message):
        if not ok:
            errors.append('i2c_topology: ' + message)
    def fields(obj, allowed, label):
        require(not set(obj) - set(allowed), label + ' has unsupported fields')
    def node_ok(node):
        return _text(node) and '.' in node and (db is None or (
            node.rsplit('.', 1)[0] in db.get('parts', {}) and
            node in db.get('nets', {}).get(db.get('pin2net', {}).get(node), []) and
            db.get('pin2net', {}).get(node) not in db.get('pseudo_nets', [])))
    fields(cfg, ('schema_version', 'db_sha256', 'states', 'buses', 'components', 'rails'), 'root')
    require(type(cfg.get('schema_version')) is int and cfg['schema_version'] == 1, 'schema_version must be 1')
    require(isinstance(cfg.get('db_sha256'), str) and bool(re.fullmatch('[0-9a-f]{64}', cfg['db_sha256'])), 'db_sha256 is required')
    if db is not None:
        require(cfg.get('db_sha256') == db_fingerprint(db), 'stale db_sha256')
    states = cfg.get('states')
    require(isinstance(states, list) and 0 < len(states) <= 32, 'states must contain 1..32 states')
    seen = set()
    for state in states if isinstance(states, list) else []:
        if not isinstance(state, dict):
            require(False, 'state must be an object')
            continue
        fields(state, ('id', 'citation', 'population', 'jumpers'), 'state')
        sid = state.get('id')
        require(_text(sid) and sid not in seen, 'state id missing/duplicate')
        if _text(sid):
            seen.add(sid)
        require(_text(state.get('citation')), 'state needs assembly/configuration citation')
        for key in ('population', 'jumpers'):
            entries = state.get(key, {})
            require(isinstance(entries, dict), key + ' must be an object')
            for ref, value in entries.items() if isinstance(entries, dict) else []:
                require(_text(ref) and (db is None or ref in db.get('parts', {})), key + ': unknown ref ' + str(ref))
                require(type(value) is bool if key == 'population' else isinstance(value, str) and value in ('closed', 'open', 'unknown'), key + ': invalid value for ' + str(ref))
    buses = cfg.get('buses', [])
    require(isinstance(buses, list), 'buses must be an array')
    seen = set()
    for bus in buses if isinstance(buses, list) else []:
        if not isinstance(bus, dict):
            require(False, 'bus must be an object')
            continue
        fields(bus, ('id', 'sda', 'scl', 'citation'), 'bus')
        bid = bus.get('id')
        require(_text(bid) and bid not in seen, 'bus id missing/duplicate')
        if _text(bid):
            seen.add(bid)
        require(_text(bus.get('citation')), 'bus needs pin-role citation')
        for signal in ('sda', 'scl'):
            require(_strings(bus.get(signal)), 'bus ' + signal + ' needs unique physical nodes')
            if _strings(bus.get(signal)):
                require(all(node_ok(node) for node in bus[signal]), 'bus ' + signal + ' contains unknown/pseudo node')
        if _strings(bus.get('sda')) and _strings(bus.get('scl')):
            require(not set(bus['sda']) & set(bus['scl']), 'same physical pin declared SDA and SCL')
    components = cfg.get('components', {})
    require(isinstance(components, dict), 'components must be an object')
    for ref, comp in components.items() if isinstance(components, dict) else []:
        require(_text(ref) and (db is None or ref in db.get('parts', {})), 'unknown component ' + str(ref))
        if not isinstance(comp, dict):
            require(False, 'component must be an object')
            continue
        fields(comp, ('kind', 'citation', 'ports'), 'component')
        require(isinstance(comp.get('kind'), str) and comp['kind'] in KINDS, 'unsupported component kind')
        require(_text(comp.get('citation')), 'component needs type/model citation')
        if db is not None and comp.get('kind') in ('resistor', 'jumper'):
            require(len([n for n in db.get('pin2net', {}) if n.rsplit('.', 1)[0] == ref]) == 2, 'pass-through must have exactly two physical pins: ' + ref)
        ports = comp.get('ports', [])
        require(isinstance(ports, list), 'ports must be an array')
        if ports:
            require(isinstance(comp.get('kind'), str) and comp['kind'] in BARRIERS, 'only a boundary component can declare ports')
        port_nodes = []
        for port in ports if isinstance(ports, list) else []:
            if not isinstance(port, dict):
                require(False, 'port must be an object')
                continue
            fields(port, ('sda', 'scl'), 'port')
            for signal in ('sda', 'scl'):
                node = port.get(signal)
                require(node_ok(node) and node.rsplit('.', 1)[0] == ref, 'port needs physical node on ' + ref)
                if _text(node):
                    port_nodes.append(node)
        require(len(port_nodes) == len(set(port_nodes)), 'duplicate boundary port pin')
    rails = cfg.get('rails', {})
    require(isinstance(rails, dict), 'rails must map net names to citations')
    for net, citation in rails.items() if isinstance(rails, dict) else []:
        require(_text(net) and _text(citation), 'rail name/citation missing')
        if db is not None:
            require(net in db.get('nets', {}) and net not in db.get('pseudo_nets', []), 'unknown/pseudo rail ' + str(net))
    return errors


class Inventory:
    def __init__(self, db, cfg):
        self.db, self.cfg = db, cfg or {}
        self.nets, self.parts = db.get('nets', {}), db.get('parts', {})
        self.pin2net, self.pinname = db.get('pin2net', {}), db.get('pinname', {})
        self.pseudo = set(db.get('pseudo_nets', []))
        self.components, self.rails = self.cfg.get('components', {}), self.cfg.get('rails', {})
        self.nodes = defaultdict(list)
        self.input_gaps = set()
        for node, net in sorted(self.pin2net.items()):
            ref = node.rsplit('.', 1)[0]
            if ref not in self.parts or node not in self.nets.get(net, []):
                self.input_gaps.add('inconsistent-index:' + node)
            else:
                self.nodes[ref].append(node)
        seen = set()
        for net, nodes in self.nets.items():
            for node in nodes:
                if node in seen or self.pin2net.get(node) != net:
                    self.input_gaps.add('inconsistent-index:' + node)
                seen.add(node)
        if db.get('integrity', {}).get('self_check_passed') is False or db.get('export_errors'):
            self.input_gaps.add('netlist-integrity/export')

    def kind(self, ref):
        if ref in self.components:
            return self.components[ref]['kind']
        if re.match(r'^R\d', ref, re.I):
            return 'resistor'
        if re.match(r'^JP\d', ref, re.I):
            return 'jumper'
        if re.match(r'^(J|P|CN)\d', ref, re.I):
            return 'connector'
        return 'unmodeled'

    def rail(self, net):
        return bool(net in self.rails or RAIL.search(net) or GROUND.search(net))

    def seeds(self):
        buses = [dict(deepcopy(b), origin='declared') for b in self.cfg.get('buses', [])]
        confirmed = {n for b in buses for signal in ('sda', 'scl') for n in b[signal]}
        for ref, comp in sorted(self.components.items()):
            for i, port in enumerate(comp.get('ports', [])):
                buses.append({'id': 'port:' + ref + ':' + str(i), 'sda': [port['sda']], 'scl': [port['scl']],
                              'origin': 'declared-port', 'citation': comp['citation']})
                confirmed.update(port.values())
        hints = defaultdict(lambda: {'sda': set(), 'scl': set()})
        loose = set()
        for net, nodes in sorted(self.nets.items()):
            if net in self.pseudo:
                continue
            for label, anchor in [(net, 'net:')] + [(str(self.pinname.get(n, '')), 'pin:' + n.rsplit('.', 1)[0] + ':') for n in nodes]:
                match = SIGNAL.search(label)
                if match:
                    role = match[2][:3].lower()
                    key = anchor + (label[:match.start(2)] + 'LINE' + match[2][3:] + label[match.end(2):]).upper()
                    hints[key][role].add(net)
                elif I2C_HINT.search(label):
                    loose.add(net)
        for key, roles in sorted(hints.items()):
            buses.append({'id': 'hint:' + key, 'sda': sorted(roles['sda']), 'scl': sorted(roles['scl']),
                          'origin': 'name-hint', 'citation': key})
        for net in sorted(loose):
            buses.append({'id': 'hint:' + net, 'sda': [net], 'scl': [], 'origin': 'name-hint', 'citation': 'unclassified I2C name'})
        return buses, confirmed

    def state_inventory(self, state, buses, confirmed):
        sid, population, jumpers = state['id'], state.get('population', {}), state.get('jumpers', {})
        edges, adjacency, potential = {}, defaultdict(list), defaultdict(set)
        def present(ref):
            return population.get(ref)  # nc=False is deliberately not assembly proof.
        for ref, nodes in sorted(self.nodes.items()):
            kind = self.kind(ref)
            if kind not in ('resistor', 'jumper') or len(nodes) != 2:
                continue
            n1, n2 = (self.pin2net[n] for n in nodes)
            parsed = parse_resistor(self.parts[ref].get('value')) if kind == 'resistor' else None
            ohms = parsed['kohm'] * 1000 if parsed else None
            if ohms is not None and not finite(ohms):
                ohms = None
            reason = None
            if present(ref) is not True:
                reason = 'not-fitted' if present(ref) is False else 'population-unverified'
            elif kind == 'jumper' and jumpers.get(ref) != 'closed':
                reason = 'open' if jumpers.get(ref) == 'open' else 'jumper-state-unverified'
            elif kind == 'resistor' and ohms is None:
                reason = 'resistance-unparsed'
            edge = {'ref': ref, 'nodes': nodes, 'nets': [n1, n2], 'kind': kind, 'ohms': ohms,
                    'tolerance': parsed['tol'] if parsed else None, 'conductive': reason is None,
                    'stop_reason': reason, 'assembly_citation': state.get('citation'),
                    'model_citation': self.components.get(ref, {}).get('citation'),
                    'parsed_nc': self.parts[ref].get('nc')}
            edges[ref] = edge
            if n1 != n2 and not any(self.rail(n) or n in self.pseudo for n in (n1, n2)):
                # Potential coverage reaches both sides even when an option is open;
                # it NEVER joins those nets in the conductive graph.
                potential[n1].add(n2)
                potential[n2].add(n1)
                if edge['conductive']:
                    adjacency[n1].append((n2, ref))
                    adjacency[n2].append((n1, ref))
        starts = set()
        declared_starts = set()
        for bus in buses:
            for role in ('sda', 'scl'):
                mapped = bus[role] if bus['origin'] == 'name-hint' else [self.pin2net[n] for n in bus[role]]
                starts.update(mapped)
                if bus['origin'] != 'name-hint':
                    declared_starts.update(mapped)
        wanted, queue, frontier = set(), deque(sorted(starts)), set()
        gaps = set(self.input_gaps)
        while queue:
            net = queue.popleft()
            if net in wanted or net in self.pseudo:
                continue
            if len(wanted) >= MAX_TRACE_NETS:
                gaps.add('trace-net-limit:' + str(MAX_TRACE_NETS))
                frontier.update(queue)
                frontier.add(net)
                break
            wanted.add(net)
            if not self.rail(net):
                queue.extend(sorted(potential[net] - wanted))
        regions, net_region = [], {}
        for start in sorted(wanted):
            if start in net_region:
                continue
            members, reached, queue = set(), {start}, deque([start])
            while queue:
                net = queue.popleft()
                if net in members:
                    continue
                members.add(net)
                for other, ref in sorted(adjacency[net]):
                    if other in wanted and other not in reached:
                        reached.add(other)
                        queue.append(other)
            origin = min(members & declared_starts or members & starts or members)
            paths, queue = {origin: []}, deque([origin])
            while queue:
                net = queue.popleft()
                for other, ref in sorted(adjacency[net]):
                    if other in members and other not in paths:
                        paths[other] = paths[net] + [ref]
                        queue.append(other)
            rid = 'I2C-' + digest([sid, sorted(members)])[:20]
            for net in members:
                net_region[net] = rid
            region = {'id': rid, 'state': sid, 'nets': sorted(members), 'path_origin_net': origin,
                      'edges': [], 'segments': [], 'pullups': [], 'endpoints': [], 'boundaries': [],
                      'gaps': set(gaps), 'buses': []}
            if any(self.rail(n) for n in members):
                region['gaps'].add('signal-on-supply-or-ground')
            touched = sorted({n.rsplit('.', 1)[0] for net in members for n in self.nets.get(net, [])})
            for ref in touched:
                nodes = self.nodes.get(ref, [])
                here = [n for n in nodes if self.pin2net[n] in members]
                kind, edge = self.kind(ref), edges.get(ref)
                if present(ref) is None:
                    region['gaps'].add('population:' + ref)
                if edge:
                    other = [n for n in edge['nets'] if n not in members]
                    if not other and edge['conductive']:
                        region['edges'].append(edge)
                    elif edge['conductive'] and kind == 'resistor' and len(other) == 1 and self.rail(other[0]) and not GROUND.search(other[0]):
                        rail = other[0]
                        net = self.pin2net[here[0]]
                        region['pullups'].append({'ref': ref, 'nodes': edge['nodes'], 'signal_net': net, 'rail': rail,
                            'ohms': edge['ohms'], 'tolerance': edge['tolerance'], 'path': paths[net],
                            'rail_basis': 'declared' if rail in self.rails else 'name-hint'})
                        if rail not in self.rails:
                            region['gaps'].add('rail-identity:' + rail)
                        if edge['ohms'] == 0:
                            region['gaps'].add('zero-ohm-to-rail:' + ref)
                    else:
                        region['boundaries'].append(dict(edge, boundary_kind='passive-stop'))
                        if edge['stop_reason'] not in (None, 'not-fitted', 'open'):
                            region['gaps'].add(edge['stop_reason'] + ':' + ref)
                    continue
                for node in here:
                    region['endpoints'].append({'node': node, 'net': self.pin2net[node], 'pin_name': self.pinname.get(node),
                                                'kind': kind, 'populated': present(ref)})
                if present(ref) is False:
                    continue
                if kind == 'connector':
                    region['gaps'].add('external-port:' + ref)
                elif kind in BARRIERS:
                    if not self.components[ref].get('ports'):
                        region['gaps'].add('boundary-port-map:' + ref)
                elif kind in ('resistor', 'jumper') or any(node not in confirmed for node in here):
                    region['gaps'].add('pin-role-or-model:' + ref)
                if kind != 'endpoint' or any(node not in confirmed for node in here):
                    region['boundaries'].append({'ref': ref, 'kind': kind, 'nodes': here,
                        'other_nodes': [n for n in nodes if n not in here], 'boundary_kind': 'component-stop'})
            # Only topology links (0R / closed jumper) share a segment. Finite
            # resistors remain edges between segments, even within one region.
            ungrouped = set(members)
            while ungrouped:
                segment, queue = set(), deque([min(ungrouped)])
                while queue:
                    net = queue.popleft()
                    if net in segment:
                        continue
                    segment.add(net)
                    for other, ref in adjacency[net]:
                        if other in members and (edges[ref]['kind'] == 'jumper' or edges[ref]['ohms'] == 0):
                            if other not in segment:
                                queue.append(other)
                ungrouped -= segment
                region['segments'].append({'id': 'SEG-' + digest([sid, sorted(segment)])[:20], 'nets': sorted(segment)})
            regions.append(region)
        output_buses, declared_signatures = [], set()
        for bus in sorted(buses, key=lambda b: (b['origin'] == 'name-hint', b['id'])):
            mapped = {role: sorted({net_region[n] for n in (bus[role] if bus['origin'] == 'name-hint' else
                       [self.pin2net[node] for node in bus[role]]) if n in net_region}) for role in ('sda', 'scl')}
            signature = (tuple(mapped['sda']), tuple(mapped['scl']))
            if bus['origin'] == 'name-hint' and signature in declared_signatures:
                continue
            if bus['origin'] != 'name-hint':
                declared_signatures.add(signature)
            item = dict(deepcopy(bus), regions=mapped)
            output_buses.append(item)
            for region in regions:
                roles = [role for role in ('sda', 'scl') if region['id'] in mapped[role]]
                if roles:
                    region['buses'].append({'id': bus['id'], 'signals': roles, 'origin': bus['origin']})
                    if len(roles) == 2:
                        region['gaps'].add('sda-scl-connected:' + bus['id'])
        for region in regions:
            if not any(b['origin'] != 'name-hint' for b in region['buses']):
                region['gaps'].add('confirm-bus-pins-and-pairing')
            region['gaps'] = sorted(region['gaps'])
            region['coverage'] = 'INCOMPLETE' if region['gaps'] else 'DISCOVERED'
        return {'id': sid, 'citation': state.get('citation'), 'buses': output_buses,
                'regions': regions, 'gaps': sorted(gaps),
                'unvisited_seed_nets': sorted(starts - wanted), 'unvisited_frontier_nets': sorted(frontier - wanted)}


def build_i2c_topology(db, intent=None):
    errors = validate_db_shape(db) or validate_i2c_intent(intent, db)
    if errors:
        raise ValueError('; '.join(errors))
    cfg = (intent or {}).get('i2c_topology')
    inv = Inventory(db, cfg)
    buses, confirmed = inv.seeds()
    states = (cfg or {}).get('states', [{'id': 'UNSPECIFIED', 'population': {}, 'jumpers': {}}])
    result = {'schema_version': 1, 'db_sha256': db_fingerprint(db), 'context': deepcopy(cfg),
              'scope': 'connectivity inventory only; no voltage/timing/Rule-09 equivalent or PASS',
              'states': [inv.state_inventory(s, buses, confirmed) for s in sorted(states, key=lambda x: x['id'])]}
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
        result = build_i2c_topology(load_json(args.db), intent)
    except (OSError, ValueError) as error:
        parser.error(str(error))
    with open(args.json, 'w', encoding='utf-8') as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2, allow_nan=False)
    regions = [r for s in result['states'] for r in s['regions']]
    print(f'I2C inventory: {len(regions)} regions, {sum(bool(r["gaps"]) for r in regions)} incomplete; no electrical verdict')


if __name__ == '__main__':
    main()
