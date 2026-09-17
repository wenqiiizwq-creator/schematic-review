"""Pin-first ESD coverage. No protector is required to trigger this scanner.

Connectivity candidates are deliberately not an ESD immunity verdict. Even a
declared channel requires manual polarity, clamp, loading and return-path review.
"""
import re
from collections import Counter
from .coverage_base import CoverageChecker, envelope, root_errors, text
from .inventory import walk, digest
from .netgraph import NetGraph, CONNECTOR, TVS, UNKNOWN, PASSIVE_LINKS, is_ground, is_rail

MAX_PATH_NETS = 128
MAX_HOPS = 8


def validate_intent(intent, db=None):
    key = 'connector_esd'
    errors = root_errors(intent, db, key, ('connectors', 'protectors', 'discovery_citation'))
    cfg = (intent or {}).get(key) if isinstance(intent, dict) else None
    if not isinstance(cfg, dict):
        return errors
    graph = NetGraph(db) if db is not None else None
    for group in ('connectors', 'protectors'):
        rows = cfg.get(group, {})
        if not isinstance(rows, dict):
            errors.append(key + '.' + group + ' must be an object')
            continue
        for ref, row in rows.items():
            label = key + '.' + group + '.' + str(ref)
            if not isinstance(row, dict) or not text(row.get('citation')) or (db is not None and ref not in db['parts']):
                errors.append(label + ': unknown ref or missing citation')
                continue
            allowed = (('citation', 'exposure', 'pinout_complete', 'pins') if group == 'connectors'
                       else ('citation', 'channels'))
            if set(row) - set(allowed):
                errors.append(label + ': unsupported fields')
            if group == 'connectors':
                if row.get('exposure') not in ('external', 'internal', 'unknown'):
                    errors.append(label + ': exposure must be external/internal/unknown')
                if type(row.get('pinout_complete')) is not bool or not isinstance(row.get('pins'), dict):
                    errors.append(label + ': pinout_complete boolean and physical pins required')
                    continue
                for pin, spec in row['pins'].items():
                    if (not text(pin) or not isinstance(spec, dict) or
                            set(spec) - {'role', 'requirement', 'citation'} or
                            spec.get('role') not in ('signal', 'power', 'return', 'shield', 'nc', 'unknown') or
                            spec.get('requirement') not in ('required', 'exempt', 'unknown') or not text(spec.get('citation'))):
                        errors.append(label + ': each pin needs role, requirement and citation')
            else:
                channels = row.get('channels')
                if not isinstance(channels, list) or not channels:
                    errors.append(label + ': nonempty channels required')
                    continue
                seen = set()
                for ch in channels:
                    if (not isinstance(ch, dict) or set(ch) - {'signal_pin', 'return_pins', 'citation'} or
                            not text(ch.get('signal_pin')) or not text(ch.get('citation')) or
                            not isinstance(ch.get('return_pins'), list) or not ch['return_pins'] or
                            not all(text(p) for p in ch['return_pins'])):
                        errors.append(label + ': invalid channel mapping')
                        continue
                    pins = [ch['signal_pin']] + ch['return_pins']
                    if len(set(pins)) != len(pins) or ch['signal_pin'] in seen:
                        errors.append(label + ': duplicate channel/pin')
                    seen.add(ch['signal_pin'])
                    if graph is not None and any(p not in graph.pins_of(ref) for p in pins):
                        errors.append(label + ': channel pin absent from current netlist')
    if 'discovery_citation' in cfg and not text(cfg['discovery_citation']):
        errors.append(key + ': discovery_citation must be nonempty')
    return errors


def paths_from(graph, net, jumpers=None):
    """Finite series reachability with visible limits, not electrical equivalence."""
    if not net or net in graph.pseudo:
        return {}, []
    paths, queue, gaps = {net: []}, [net], []
    for current in queue:
        # Never traverse through shared power/return rails to unrelated ports.
        if is_ground(current) or is_rail(current):
            continue
        for ref, other in graph.neighbors(current, PASSIVE_LINKS):
            if len(graph.pins_of(ref)) != 2 or other in paths or other in graph.pseudo:
                continue
            if graph.kind(ref) == 'jumper' and (jumpers or {}).get(ref) != 'closed':
                gaps.append('unconfirmed-jumper-position:' + ref)
                continue
            if is_ground(other) or is_rail(other):
                continue
            if len(paths[current]) >= MAX_HOPS or len(paths) >= MAX_PATH_NETS:
                gaps.append('path-limit:' + ref + ':' + other)
                continue
            paths[other] = paths[current] + [ref]
            queue.append(other)
    return paths, sorted(set(gaps))


def scan(db, cfg, state):
    graph = NetGraph(db, state['fitted'])
    declared, mappings = cfg.get('connectors', {}), cfg.get('protectors', {})
    connectors = set(declared) | {r for r, p in graph.parts.items() if graph.kind(r) == CONNECTOR
        or re.search(r'CONNECTOR|CONN[_ -]|连接器', ' '.join(str(p.get(k, '')) for k in ('part', 'prim')), re.I)}
    protectors = set(mappings) | {r for r in graph.parts if graph.kind(r) == TVS}
    rows = []
    for ref in sorted(connectors):
        spec = declared.get(ref, {})
        official = spec.get('pins', {})
        symbol = {n.split('.', 1)[1] for n in db.get('declared_pinname', {}) if n.startswith(ref + '.')}
        connected = set(graph.pins_of(ref))
        pins = sorted(set(official) | symbol | connected)
        shared = []
        if not spec.get('pinout_complete'):
            shared.append('official-full-pinout:' + ref)
        if spec.get('exposure', 'unknown') == 'unknown':
            shared.append('connector-exposure:' + ref)
        if spec.get('pinout_complete') and (symbol | connected) - set(official):
            shared.append('pins-outside-official-pinout:' + ref)
        if not pins:
            pins = ['?']  # Empty connector inventories are still a pending object.
        for pin in pins:
            node, role = ref + '.' + pin, official.get(pin, {})
            net = db.get('pin2net', {}).get(node)
            gaps = list(shared)
            if role.get('requirement', 'unknown') == 'unknown' or role.get('role', 'unknown') == 'unknown':
                gaps.append('pin-role/esd-requirement:' + node)
            exempt = role.get('requirement') == 'exempt' and text(role.get('citation'))
            if not net or net in graph.pseudo or node in db.get('no_connect_nodes', []):
                if not exempt:
                    gaps.append('unconnected/pseudo/NC-pin-needs-exposure-decision:' + node)
            paths, path_gaps = paths_from(graph, net, state['jumpers'])
            gaps += path_gaps
            candidates, excluded = [], []
            for device in sorted(protectors):
                dpins = graph.pins_of(device)
                touched = sorted(p for p, n in dpins.items() if n in paths)
                if not touched:
                    continue
                if not graph.is_fitted(device):
                    excluded.append(device)
                    continue
                for p in touched:
                    channel = next((c for c in mappings.get(device, {}).get('channels', []) if c['signal_pin'] == p), None)
                    return_pins = channel['return_pins'] if channel else []
                    returns = {rp: dpins.get(rp) for rp in return_pins}
                    mapped = bool(channel and returns and all(n and n not in graph.pseudo and n != dpins[p] for n in returns.values()))
                    candidates.append({'ref': device, 'signal_node': device + '.' + p,
                        'signal_net': dpins[p], 'series_path': paths[dpins[p]],
                        'return_nodes': {device + '.' + rp: n for rp, n in returns.items()},
                        'all_device_pins': dpins, 'channel_mapped': mapped,
                        'citation': channel.get('citation') if channel else None})
            if exempt:
                coverage = 'exemption-needs-review'
            elif not graph.is_fitted(ref):
                coverage = 'connector-not-fitted'
                gaps.append('unfitted-connector/pads-exposure:' + node)
            elif not candidates:
                coverage = 'no-protector-found'
                gaps.append('no-protector-found:' + node)
            elif not any(c['channel_mapped'] for c in candidates):
                coverage = 'unmapped-protector-candidate'
                gaps.append('protection-channel/return-map:' + node)
            else:
                coverage = 'mapped-topology-candidate'
            rows.append({'id': node, 'ref': ref, 'node': node, 'net': net,
                'role': role.get('role', 'unknown'), 'requirement': role.get('requirement', 'unknown'),
                'requirement_citation': role.get('citation'), 'exposure': spec.get('exposure', 'unknown'),
                'fitted': graph.is_fitted(ref), 'coverage': coverage, 'protectors': candidates,
                'excluded_protectors': excluded,
                'reachable_endpoints': sorted(n for reach in paths for n in graph.nodes_on(reach)
                    if n.partition('.')[0] not in protectors and n.partition('.')[0] != ref),
                'gaps': sorted(set(gaps))})
    return rows


def build_inventory(db, intent=None):
    errors = validate_intent(intent, db)
    if errors:
        raise ValueError('; '.join(errors))
    cfg = (intent or {}).get('connector_esd')
    gaps = [] if (cfg or {}).get('discovery_citation') else ['connector-discovery: reconcile BOM/pages/external and internal ports']
    result = envelope(db, cfg, 'pins', lambda state: scan(db, cfg or {}, state), gaps)
    graph = NetGraph(db)
    result['unclassified_refs'] = sorted(r for r in graph.parts if graph.kind(r) == UNKNOWN)
    result['summary'] = {state['id']: {
        'connectors': len({p['ref'] for p in state['pins']}),
        'pins': len(state['pins']), 'coverage': dict(sorted(Counter(p['coverage'] for p in state['pins']).items()))}
        for state in result['states']}
    result.pop('digest')
    result['digest'] = digest(result)
    return result


class ConnectorESDChecker(CoverageChecker):
    id = plan_key = intent_key = 'connector_esd'
    title = '逐连接器逐引脚 ESD 覆盖'
    version_key = 'connector_esd_version'
    item_key = 'pins'
    cold_rules = {'ES-01': '连接器引脚防护缺口'}

    def validate_intent(self, intent, db):
        return validate_intent(intent, db)

    def build(self, db, intent):
        return build_inventory(db, intent)

    def plan(self, planner, inventory):
        self.discovery_plan(planner, inventory, '核对全部连接器/外露端口与物理脚；零保护器件或零候选不表示已覆盖')
        for state, pin in walk(inventory, 'pins'):
            gaps = state['gaps'] + pin['gaps']
            for suffix, criterion in (
                ('coverage', '逐针核暴露/需求/豁免、贴装、连接器脚→网络→保护通道物理脚→返回网→受保护脚；不得由NC/地名/有TVS判通过'),
                ('electrical', '逐针核保护极性、双向摆幅、VRWM/漏电、适用波形下VC与下游承受能力、注入电流、结电容/带宽、返回/掉电；豁免须有适用条款')):
                item = planner.add_check('esd-' + suffix + '-' + pin['id'] + '-' + state['id'],
                    self.object(inventory, state, pin), criterion, 'ER3' if suffix == 'coverage' else 'ER4',
                    'Expert Review', readiness='WAITING_EVIDENCE', required_inputs=gaps + ['pin-specific ESD requirement and datasheet evidence'])
                item['inventory_gaps'] = gaps

    def cold_findings(self, lint, inventory):
        for state, pin in walk(inventory, 'pins'):
            gaps = state['gaps'] + pin['gaps']
            if gaps:
                lint.add('ES-01', '连接器引脚防护覆盖待核',
                    '%s [%s] %s；%s' % (pin['id'], state['id'], pin['coverage'], '; '.join(gaps)),
                    pin['ref'], kind='CANDIDATE', node=pin['node'], net=pin['net'],
                    state=state['id'], coverage=pin['coverage'])
