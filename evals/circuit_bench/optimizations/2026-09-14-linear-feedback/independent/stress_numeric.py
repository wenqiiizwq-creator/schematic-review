import json
import random
import sys

import check_candidate as independent
from solve_dividers import Solver, linear_feedback_window
from plan_review import build_review_plan
from electrical_fixtures import bind_evidence
import tempfile
from fractions import Fraction

cases = []
rng = random.Random(81823123)
graphs = [independent.shared_edges(), independent.bridge_edges()]
for graph in graphs:
    for _ in range(256):
        edges = [(ref, a, b, 10 ** rng.randint(0, 10), 0) for ref, a, b, ohm, tol in graph]
        cases.append(edges)
cases.append([('R1', 'SOURCE', 'MID', 10**10, 0), ('R2', 'MID', 'FB', 1, 0),
              ('R3', 'MID', 'GND', 10**10, 0), ('R4', 'FB', 'GND', 10**10, 0)])
worst, accepted, rejected, exact_matches = None, 0, 0, 0
for edges in cases:
    db = independent.make_db(edges)
    try:
        network = Solver(db, model=independent.model()).linear_network('FB')
        window = linear_feedback_window(network, {'min': .8, 'typ': .8, 'max': .8}, {'min': 0, 'max': 0})
    except ValueError:
        rejected += 1
        continue
    accepted += 1
    exact = independent.rational_source(edges, {e[0]: e[3] for e in edges}, .8, 0)
    actual = float(exact)
    if 'bounds_exact' in window:
        assert Fraction(window['bounds_exact']['min']) == exact
        assert Fraction(window['bounds_exact']['max']) == exact
        assert Fraction.from_float(window['min']) <= exact <= Fraction.from_float(window['max'])
        exact_matches += 1
    error = window['max'] - actual
    record = {'edges': edges, 'observed': window['max'], 'exact_float': actual,
              'absolute_error': error, 'relative_error': error / actual}
    if worst is None or abs(record['relative_error']) > abs(worst['relative_error']):
        worst = record
print(json.dumps({'numeric_stress': {'accepted': accepted, 'rejected': rejected,
                                   'exact_matches': exact_matches, 'worst': worst}}, indent=2))

db = independent.make_db(independent.shared_edges())
check = {'id': 'INDEPENDENT-PLAN', 'rule': 'Rule-08', 'kind': 'divider', 'net': 'FB',
         'vref': {'min': .77, 'typ': .8, 'max': .83}, 'divider_model': independent.model(),
         'depends_on': ['U1'], 'expected': {'min': 0, 'max': 10}, 'citation': 'Synthetic missing resistor source'}
evidence = {'schema_version': 1, 'checks': [check]}
with tempfile.TemporaryDirectory(dir=independent.REVIEW) as directory:
    audit = bind_evidence(db, evidence, directory)
    check['basis']['sources'] = [source for source in check['basis']['sources'] if source['ref'] == 'U1']
    plan = build_review_plan(db, evidence=evidence, datasheet_audit=audit)
    targets = [{k: item.get(k) for k in ('id', 'check', 'readiness', 'role', 'evidence_check_id', 'missing_inputs')}
               for item in plan['checks'] if item['check'] == 'feedback-divider-wca']
    print(json.dumps({'planner_missing_resistor_sources': targets}, indent=2))
