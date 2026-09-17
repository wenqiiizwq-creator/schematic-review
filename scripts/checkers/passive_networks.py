"""Discover RC/LC candidates, account for the remainder, and verify explicit RLC models."""
import math
import re
from copy import deepcopy
from .coverage_base import CoverageChecker, envelope, root_errors, text
from .inventory import digest, walk
from .netgraph import NetGraph, RESISTOR, CAPACITOR, INDUCTOR, is_ground
from electrical_contract import bounded, finite, readiness_gaps
from decoupling import parse_capacitance
from solve_dividers import parse_resistor
from passive_ac import analytic_metrics, response

KINDS = {RESISTOR, CAPACITOR, INDUCTOR}


def nominal(kind, value):
    if kind == RESISTOR:
        parsed = parse_resistor(value)
        return parsed['kohm'] * 1000 if parsed else None
    if kind == CAPACITOR:
        return parse_capacitance(value)
    # Explicit units only; do not treat a ferrite's impedance as inductance.
    if not isinstance(value, str):
        return None
    match = re.fullmatch(r'\s*(\d+(?:\.\d*)?|\.\d+)([eE][+-]?\d+)?\s*([pnumµμ]?)H(?:[/;,\s].*)?', value, re.I)
    embedded = re.fullmatch(r'\s*(\d+)([pnumµμ])(\d+)(?:H)?(?:[/;,\s].*)?', value, re.I)
    if match:
        a, exponent, unit = match.groups()
        number = float(a + (exponent or ''))
    elif embedded:
        a, unit, b = embedded.groups()
        number = float(a + '.' + b)
    else:
        return None
    value = number * {'': 1, 'p': 1e-12, 'n': 1e-9, 'u': 1e-6, 'µ': 1e-6, 'μ': 1e-6, 'm': 1e-3}[unit.lower()]
    return value if math.isfinite(value) and value > 0 else None


def validate_intent(intent, db=None):
    errors = root_errors(intent, db, 'passive_networks', ('networks', 'exclusions', 'discovery_citation'))
    cfg = (intent or {}).get('passive_networks') if isinstance(intent, dict) else None
    if not isinstance(cfg, dict):
        return errors
    rows = cfg.get('networks', [])
    if not isinstance(rows, list):
        return errors + ['passive_networks.networks must be an array']
    seen = set()
    for row in rows:
        if not isinstance(row, dict):
            errors.append('passive network must be an object')
            continue
        if set(row) - {'id', 'refs', 'input_net', 'output_net', 'reference_net', 'citation'}:
            errors.append('passive network: unsupported fields')
        key = row.get('id')
        if not text(key) or key in seen or str(key).startswith(('auto-', 'unresolved-')):
            errors.append('passive network: unique id required (auto-/unresolved- reserved)')
        if text(key):
            seen.add(key)
        if not text(row.get('citation')):
            errors.append('passive network: port/model citation required')
        refs = row.get('refs')
        if (not isinstance(refs, list) or not refs or not all(text(r) for r in refs)
                or len(set(refs)) != len(refs) or db is not None and any(r not in db['parts'] for r in refs)):
            errors.append('passive network: unique current refs required')
        ports = [row.get(k) for k in ('input_net', 'output_net', 'reference_net')]
        if not all(text(n) for n in ports) or len(set(n for n in ports if text(n))) != 3 or db is not None and any(n not in db['nets'] for n in ports if text(n)):
            errors.append('passive network: three distinct current port nets required')
    if 'discovery_citation' in cfg and not text(cfg['discovery_citation']):
        errors.append('passive_networks: discovery_citation must be nonempty')
    exclusions = cfg.get('exclusions', {})
    if not isinstance(exclusions, dict):
        errors.append('passive_networks.exclusions must be an object')
    else:
        for ref, spec in exclusions.items():
            if (not isinstance(spec, dict) or set(spec) != {'citation', 'reason'} or
                    not text(spec.get('citation')) or not text(spec.get('reason')) or
                    db is not None and ref not in db['parts']):
                errors.append('passive exclusion needs current ref, reason and citation')
        for row in rows:
            if isinstance(row, dict) and isinstance(row.get('refs'), list) and any(r in exclusions for r in row['refs'] if text(r)):
                errors.append('passive ref cannot be both modelled and excluded')
    return errors


def scan(db, cfg, state):
    graph = NetGraph(db, state['fitted'])
    refs = {r for r in graph.parts if graph.kind(r) in KINDS}
    edges = {r: {'ref': r, 'kind': graph.kind(r), 'nets': graph.terminals(r),
                 'nominal': nominal(graph.kind(r), graph.parts[r].get('value')),
                 'physical_pins': graph.pins_of(r), 'fitted': graph.is_fitted(r)} for r in sorted(refs)}
    rows, covered = [], set()
    excluded = cfg.get('exclusions', {})
    declared = cfg.get('networks', [])
    for spec in declared:
        members = [edges.get(r, {'ref': r, 'kind': graph.kind(r), 'nets': graph.terminals(r),
                   'physical_pins': graph.pins_of(r), 'nominal': None, 'fitted': graph.is_fitted(r)}) for r in spec['refs']]
        gaps = []
        for e in members:
            if len(e['physical_pins']) != 2 or len(e['nets']) != 2 or e['kind'] not in KINDS:
                gaps.append('unsupported-element:' + e['ref'])
            if not e['fitted']:
                gaps.append('element-not-fitted:' + e['ref'])
            if e['nominal'] is None or e['nominal'] <= 0:
                gaps.append('unknown/zero-element-value:' + e['ref'])
        item = dict(spec, ref=spec['refs'][0], net=spec['output_net'], topology='declared-rlc',
                    edges=members, declared=True, gaps=gaps)
        rows.append(item)
        covered.update(spec['refs'])
    adjacent = {}
    for e in edges.values():
        if e['ref'] not in excluded and e['fitted'] and len(e['physical_pins']) == 2 and len(e['nets']) == 2:
            for net in e['nets']:
                adjacent.setdefault(net, []).append(e)
    patterns = {(RESISTOR, CAPACITOR): 'RC-lowpass', (CAPACITOR, RESISTOR): 'RC-highpass',
                (INDUCTOR, CAPACITOR): 'LC-lowpass', (CAPACITOR, INDUCTOR): 'LC-highpass'}
    groups = {}
    for shunt in edges.values():
        if shunt['ref'] in excluded or not shunt['fitted'] or len(shunt['physical_pins']) != 2 or len(shunt['nets']) != 2:
            continue
        grounds = [n for n in shunt['nets'] if is_ground(n)]
        if len(grounds) != 1:
            continue
        ground = grounds[0]
        out = next(n for n in shunt['nets'] if n != ground)
        for series in adjacent.get(out, []):
            kind = patterns.get((series['kind'], shunt['kind']))
            inp = next(n for n in series['nets'] if n != out)
            if not kind or inp == ground or series['ref'] == shunt['ref']:
                continue
            members = [series, shunt]
            pair = {e['ref'] for e in members}
            # A declared model containing this stage supersedes its heuristic candidate.
            if any(pair <= set(d['refs']) for d in declared):
                continue
            # Parallel caps/resistors on a shared rail are one candidate region,
            # not the Cartesian product of every possible two-component pairing.
            group = groups.setdefault((kind, out, ground), {'refs': set(), 'inputs': set()})
            group['refs'].update(pair)
            group['inputs'].add(inp)
    for (kind, out, ground), group in sorted(groups.items()):
        pair, inputs = sorted(group['refs']), sorted(group['inputs'])
        key = 'auto-' + digest([kind, inputs, out, ground, pair])[:16]
        members = [edges[r] for r in pair]
        gaps = ['confirm-ports/model-boundary:' + key]
        if len(inputs) != 1:
            gaps.append('multiple-source-ports: declare a complete multi-branch model')
        gaps += ['unknown/zero-element-value:' + e['ref'] for e in members if e['nominal'] is None or e['nominal'] <= 0]
        rows.append({'id': key, 'ref': pair[0], 'refs': pair, 'net': out,
            'input_net': inputs[0] if len(inputs) == 1 else None, 'input_nets': inputs,
            'output_net': out, 'reference_net': ground, 'topology': kind,
            'edges': members, 'declared': False, 'gaps': gaps})
        covered.update(pair)
    for ref in sorted(refs - covered):
        edge = edges[ref]
        rows.append({'id': 'unresolved-' + ref, 'ref': ref, 'refs': [ref], 'nets': edge['nets'],
            'topology': 'excluded-from-ac' if ref in excluded else 'unresolved-passive',
            'exclusion': excluded.get(ref), 'edges': [edge], 'declared': False,
            'gaps': [] if ref in excluded else ['assign-passive-function/model-or-evidenced-exclusion:' + ref]})
    return sorted(rows, key=lambda x: x['id'])


def build_inventory(db, intent=None):
    errors = validate_intent(intent, db)
    if errors:
        raise ValueError('; '.join(errors))
    cfg = (intent or {}).get('passive_networks')
    gaps = [] if (cfg or {}).get('discovery_citation') else ['passive-discovery: reconcile R/C/L, arrays, coupled/ferrite and unknown types']
    result = envelope(db, cfg, 'networks', lambda state: scan(db, cfg or {}, state), gaps)
    graph = NetGraph(db)
    result['special_or_unclassified_refs'] = sorted(r for r in graph.parts
        if graph.kind(r) in ('ferrite', 'transformer', 'unknown') or
        graph.kind(r) in KINDS and len(graph.pins_of(r)) != 2)
    result['summary'] = {state['id']: {
        'networks': len(state['networks']),
        'accounted_refs': sorted({r for n in state['networks'] for r in n['refs']}),
        'unresolved_refs': sorted({r for n in state['networks'] if n['topology'] == 'unresolved-passive' for r in n['refs']})}
        for state in result['states']}
    result.pop('digest')
    result['digest'] = digest(result)
    return result


def range_ok(value, positive=True):
    return (bounded(value, positive=positive) and finite(value.get('nominal'))
            and value['min'] <= value['nominal'] <= value['max']
            and (positive or value['min'] >= 0))


def model_errors(check):
    model = check.get('model')
    if not isinstance(model, dict):
        return ['model missing']
    gaps = []
    allowed = {'input_net', 'output_net', 'reference_net', 'components', 'source_resistance_ohm',
               'load_resistance_ohm', 'load_capacitance_f', 'load_open', 'boundaries', 'citation', 'validity_citation',
               'frequency_hz', 'metric_requirements', 'gain_requirements'}
    if set(model) - allowed:
        gaps.append('unsupported model fields')
    for field in ('input_net', 'output_net', 'reference_net', 'citation', 'validity_citation'):
        if not text(model.get(field)):
            gaps.append(field + ' missing')
    if not range_ok(model.get('source_resistance_ohm'), positive=False):
        gaps.append('source_resistance_ohm requires nonnegative guaranteed min/nominal/max')
    elif model['source_resistance_ohm']['min'] == 0 < model['source_resistance_ohm']['max']:
        gaps.append('source resistance range crosses ideal-source boundary; split model states')
    if type(model.get('load_open')) is not bool:
        gaps.append('load_open must be explicitly true/false')
    if model.get('load_open') is True and 'load_resistance_ohm' in model:
        gaps.append('load_open conflicts with load_resistance_ohm')
    if model.get('load_open') is not True and not range_ok(model.get('load_resistance_ohm')):
        gaps.append('load_resistance_ohm guaranteed range missing')
    if not range_ok(model.get('load_capacitance_f'), positive=False):
        gaps.append('load_capacitance_f needs explicit guaranteed bounds, including zero if justified')
    pars = model.get('components')
    if not isinstance(pars, dict) or not pars:
        gaps.append('component guarantee ranges missing')
    else:
        for ref, spec in pars.items():
            if not range_ok(spec) or not text(spec.get('citation')):
                gaps.append(ref + ': component range/citation missing')
            if isinstance(spec, dict) and set(spec) - {'min', 'max', 'nominal', 'bom_nominal', 'citation', 'series_resistance_ohm'}:
                gaps.append(ref + ': unsupported component fields')
            if isinstance(spec, dict) and 'bom_nominal' in spec and (not finite(spec['bom_nominal']) or spec['bom_nominal'] <= 0):
                gaps.append(ref + ': bom_nominal must be a finite positive value')
    boundaries = model.get('boundaries')
    if not isinstance(boundaries, dict):
        gaps.append('boundaries must explicitly map every external physical node')
    else:
        for node, boundary in boundaries.items():
            if (not isinstance(boundary, dict) or set(boundary) - {'role', 'citation'} or
                    boundary.get('role') not in ('source', 'load', 'open') or not text(boundary.get('citation'))):
                gaps.append(node + ': invalid boundary role/citation')
    frequencies = model.get('frequency_hz')
    if (not isinstance(frequencies, list) or not 1 <= len(frequencies) <= 256 or
            not all(finite(f) and f > 0 for f in frequencies) or len(set(frequencies)) != len(frequencies)):
        gaps.append('frequency_hz needs 1..256 distinct finite positive frequencies')
    metric = model.get('metric_requirements', {})
    gain = model.get('gain_requirements', [])
    if not isinstance(metric, dict) or any(not bounded(x) or x['min'] < 0 for x in metric.values()):
        gaps.append('metric_requirements needs nonnegative min/max bounds')
    if not isinstance(gain, list) or any(not isinstance(x, dict) or set(x) != {'frequency_hz', 'min', 'max'} or
            not bounded(x) or x['min'] < 0 or x.get('frequency_hz') not in (frequencies if isinstance(frequencies, list) else []) for x in gain):
        gaps.append('gain_requirements needs min/max and an evaluated frequency_hz')
    if not metric and not gain:
        gaps.append('quantified acceptance requirements missing')
    return gaps


def evaluate(db, item, check, fitted=None):
    """Actual graph and values are mandatory; evidence cannot replace connectivity."""
    model, edges = check['model'], item['edges']
    gaps = model_errors(check)
    if gaps:
        return {'status': 'INSUFFICIENT', 'gaps': gaps}
    graph = NetGraph(db, fitted)
    refs = {e['ref'] for e in edges}
    if set(model['components']) != refs:
        gaps.append('model component set differs from inventoried network')
    for key in ('input_net', 'output_net', 'reference_net'):
        if model[key] != item.get(key):
            gaps.append(key + ' differs from inventoried port')
    for edge in edges:
        spec = model['components'].get(edge['ref'], {})
        if not range_ok(spec):
            continue
        if edge['nominal'] is None or not math.isclose(spec.get('bom_nominal', spec['nominal']), edge['nominal'], rel_tol=1e-9, abs_tol=0):
            gaps.append(edge['ref'] + ': nominal differs from actual BOM value')
        if edge['kind'] in (CAPACITOR, INDUCTOR) and not range_ok(spec.get('series_resistance_ohm'), positive=False):
            gaps.append(edge['ref'] + ': explicit ESR/DCR range required (zero needs model-validity evidence)')
    # A branch cannot disappear just because the two-element filter was recognised.
    active_nets = {n for e in edges for n in e['nets']} - {model['reference_net']}
    external = {n for net in active_nets for n in graph.nodes_on(net)
                if n.partition('.')[0] not in refs and graph.is_fitted(n.partition('.')[0])}
    if set(model['boundaries']) != external:
        gaps.append('boundary inventory mismatch: missing=%s extra=%s' %
                    (sorted(external - set(model['boundaries'])), sorted(set(model['boundaries']) - external)))
    for node in sorted(external):
        ref = node.partition('.')[0]
        boundary = model['boundaries'].get(node, {})
        net = db['pin2net'][node]
        role = boundary.get('role')
        if graph.kind(ref) in KINDS:
            gaps.append('unmodelled passive branch:' + ref)
        if role == 'source' and net != model['input_net'] or role == 'load' and net != model['output_net']:
            gaps.append('boundary port mismatch:' + node)
    if gaps:
        return {'status': 'INSUFFICIENT', 'gaps': sorted(set(gaps))}
    samples, proof_gaps, failures = [], [], []
    try:
        metrics, notes = analytic_metrics(edges, model)
    except (ValueError, ZeroDivisionError, OverflowError) as err:
        metrics, notes = {}, [str(err)]
    for frequency in model['frequency_hz']:
        sample = {'frequency_hz': frequency}
        try:
            sample.update(response(edges, model, frequency))
            sample['gain_bounds'] = response(edges, model, frequency, boxed=True)
        except (ValueError, ZeroDivisionError, OverflowError) as err:
            sample['gap'] = str(err)
        samples.append(sample)

    def assess(actual, wanted, label):
        if actual is None:
            proof_gaps.append('metric/bound unavailable:' + label)
        elif actual['min'] >= wanted['min'] and actual['max'] <= wanted['max']:
            pass
        elif actual['max'] < wanted['min'] or actual['min'] > wanted['max']:
            failures.append('guaranteed range outside requirement:' + label)
        else:
            # Interval overestimation/overlap is not a proof of failure or success.
            proof_gaps.append('range overlaps requirement boundary; refine proof:' + label)
    for name, wanted in model.get('metric_requirements', {}).items():
        assess(metrics.get(name), wanted, name)
    for wanted in model.get('gain_requirements', []):
        sample = next(x for x in samples if x['frequency_hz'] == wanted['frequency_hz'])
        assess(sample.get('gain_bounds'), wanted, 'gain@' + str(wanted['frequency_hz']))
        # Validate a nominal counterexample with a point-interval solve as well;
        # floating ill-conditioning near a limit must not invent a failure.
        if 'gain' in sample and not wanted['min'] <= sample['gain'] <= wanted['max']:
            point = deepcopy(model)
            specs = list(point['components'].values()) + [point['source_resistance_ohm'], point['load_capacitance_f']]
            if not point['load_open']:
                specs.append(point['load_resistance_ohm'])
            for spec in list(specs):
                if 'series_resistance_ohm' in spec:
                    specs.append(spec['series_resistance_ohm'])
            for spec in specs:
                spec['min'] = spec['max'] = spec['nominal']
            try:
                bound = response(edges, point, wanted['frequency_hz'], boxed=True)
                sample['nominal_point_gain_bounds'] = bound
                if bound['max'] < wanted['min'] or bound['min'] > wanted['max']:
                    failures.append('validated nominal counterexample at ' + str(wanted['frequency_hz']) + ' Hz')
            except (ValueError, ZeroDivisionError, OverflowError):
                proof_gaps.append('nominal counterexample needs a numerically resolved bound')
    return {'status': 'FAIL' if failures else 'INSUFFICIENT' if proof_gaps else 'PASS',
            'metrics': metrics, 'samples': samples, 'gaps': proof_gaps, 'failures': failures, 'notes': notes,
            'scope': '线性小信号、声明源/负载及有效参数区间；频点只证明所列频点，不能代表全频带、瞬态、饱和、稳定性或实测性能'}


class PassiveNetworksChecker(CoverageChecker):
    id = plan_key = intent_key = 'passive_networks'
    title = '通用无源网络识别与验算'
    version_key = 'passive_networks_version'
    item_key = 'networks'
    cold_rules = {'PN-01': '无源网络模型/覆盖缺口'}
    hot_rules = {'PN-10': '无源网络加载响应与参数验算'}
    evidence_kinds = {'PN-10': {'passive_ac'}}

    def validate_intent(self, intent, db):
        return validate_intent(intent, db)

    def build(self, db, intent):
        return build_inventory(db, intent)

    def plan(self, planner, inventory):
        self.discovery_plan(planner, inventory, '对账全部R/C/L及未识别/多脚/磁珠/耦合元件；每件归属电路或有据排除，不以零RC/LC候选判通过')
        for state, network in walk(inventory, 'networks'):
            gaps = state['gaps'] + network['gaps']
            if network.get('exclusion'):
                item = planner.add_check('passive-disposition-' + network['id'] + '-' + state['id'],
                    self.object(inventory, state, network),
                    '核对此器件不进入通用AC模型的理由、适用状态及另行审查归属：' + network['exclusion']['reason'],
                    'ER4', 'Expert Review', readiness='WAITING_EVIDENCE', required_inputs=gaps)
                item['inventory_gaps'] = gaps
                continue
            for suffix, criterion, executor, rule in (
                ('model', '核定功能、输入/输出/参考端、全部支路、源阻抗、真实负载、装配/工况与线性模型适用频带', 'Expert Review', None),
                ('ac', '验算有负载的截止/时间常数、LC自然频率/峰值/Q/阻尼及所要求频点的传输区间，不能套用空载RC或把f0等同截止', 'AC0-HOT', 'PN-10'),
                ('ratings', '独立核R功耗/脉冲、C有效容量/偏压/耐压/ESR、L DCR/额定/饱和/SRF，以及时域/采样/启动条件；超模型范围另补工程验算', 'Expert Review', None)):
                item = planner.add_check('passive-' + suffix + '-' + network['id'] + '-' + state['id'],
                    self.object(inventory, state, network), criterion, 'ER4', executor, rule=rule,
                    readiness='WAITING_EVIDENCE', required_inputs=gaps + ['guaranteed component/source/load ranges and acceptance requirements'])
                item['inventory_gaps'] = gaps

    def cold_findings(self, lint, inventory):
        for state, network in walk(inventory, 'networks'):
            gaps = state['gaps'] + network['gaps']
            if gaps:
                lint.add('PN-01', '无源网络待确认/建模', '%s [%s] %s；%s' % (
                    network['id'], state['id'], network['topology'], '; '.join(gaps)),
                    network['ref'], kind='CANDIDATE', state=state['id'], refs=network['refs'])

    def model_gaps(self, check):
        gaps = model_errors(check)
        if not text(check.get('network_id')) or not text(check.get('inventory_digest')):
            gaps.append('network_id/inventory_digest missing')
        return gaps

    def hot_check(self, lint, check):
        inventory = lint.inventories[self.id]
        state_id = (check.get('basis') or {}).get('state')
        pair = next(((s, n) for s, n in walk(inventory, 'networks') if s['id'] == state_id and n['id'] == check['network_id']), None)
        gaps = []
        if check['inventory_digest'] != inventory['digest'] or pair is None:
            gaps.append('stale passive inventory/state/network binding')
        if pair is not None:
            state, item = pair
            gaps += state['gaps'] + item['gaps']
            if check.get('ref') != item['ref'] or check.get('net') != item.get('output_net'):
                gaps.append('hot-check coordinates differ from inventoried network')
            dependencies = set(item['refs']) | {n.split('.')[0] for n in check['model'].get('boundaries', {})}
            gaps += readiness_gaps(lint.db, dict(check, depends_on=sorted(dependencies)), lint.datasheet_audit)
        calculation = {'status': 'INSUFFICIENT', 'gaps': gaps} if gaps else evaluate(lint.db, item, check, state['fitted'])
        if calculation['status'] == 'PASS':
            lint.record_pass('PN-10', check, '有负载模型在声明判据范围内；' + lint._citation(check),
                             scope=calculation['scope'], calculation=calculation)
        else:
            lint.add('PN-10', '无源网络验算' + calculation['status'],
                     '; '.join(calculation.get('gaps', []) + calculation.get('failures', [])),
                     check.get('ref'), kind='CANDIDATE' if calculation['status'] == 'INSUFFICIENT' else 'FINDING',
                     check_id=check['id'], citation=check['citation'], calculation=calculation)
