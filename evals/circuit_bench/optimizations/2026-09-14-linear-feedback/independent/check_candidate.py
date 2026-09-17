#!/usr/bin/env python3
"""Independent synthetic safety checks; no frozen benchmark imports or data."""
import copy
import hashlib
import itertools
import json
import math
from fractions import Fraction as F
from pathlib import Path
import random
import sys
import tempfile
import unittest

REVIEW = Path('/tmp/codex-work/schematic-optimize.2Xfcdr/independent-safety')
ROOT = Path(sys.argv.pop(1))
sys.path[:0] = [str(ROOT / 'scripts'), str(ROOT / 'scripts/tests')]
from solve_dividers import Solver, linear_feedback_window, _linear_coefficients
from electrical_fixtures import bind_evidence
from electrical_contract import readiness_gaps, validate_evidence
from lint import Lint
from plan_review import build_review_plan


def make_db(edges, extra=()):
    nets, parts = {}, {'U1': {'value': 'SYNTHETIC-FB', 'nc': False}}
    for ref, a, b, ohm, tol in edges:
        parts[ref] = {'value': str(ohm) + ('/' + str(tol * 100) + '%' if tol is not None else ''), 'nc': False}
        nets.setdefault(a, []).append(ref + '.1')
        nets.setdefault(b, []).append(ref + '.2')
    nets.setdefault('FB', []).append('U1.1')
    for ref, value, connections in extra:
        parts[ref] = {'value': value, 'nc': False}
        for pin, net in connections:
            nets.setdefault(net, []).append(ref + '.' + pin)
    return {'nets': nets, 'parts': parts,
            'pin2net': {node: net for net, nodes in nets.items() for node in nodes},
            'pinname': {'U1.1': 'FB'}, 'pintype': {}, 'ref2page': {}, 'pseudo_nets': []}


def model():
    return {'source_net': 'SOURCE', 'reference_net': 'GND',
            'bias_current_a': {'min': -0.000003, 'max': 0.000007},
            'ignored_nodes': {'U1.1': 'Synthetic current at FB is included in bias_current_a'}}


def shared_edges():
    return [('R1', 'SOURCE', 'MID', 1000, .02),
            ('R2', 'MID', 'FB', 2000, .01),
            ('R3', 'MID', 'GND', 3000, .05),
            ('R4', 'FB', 'GND', 4000, .03)]


def bridge_edges():
    return [('R1', 'SOURCE', 'A', 1100, .01),
            ('R2', 'A', 'GND', 2200, .02),
            ('R3', 'SOURCE', 'FB', 3300, .03),
            ('R4', 'FB', 'GND', 4700, .04),
            ('R5', 'A', 'FB', 5600, .05)]


def rational_source(edges, resistances, vref, bias):
    """Independent inverse KCL: FB voltage fixed, solve source and internal V.

    Include KCL for all free passive nodes and FB, but not the ideal source.
    This uses exact fractions and Gauss-Jordan elimination, no production math.
    """
    nodes = sorted({n for _, a, b, _, _ in edges for n in (a, b)} - {'GND', 'FB'})
    rows = [n for n in nodes if n != 'SOURCE'] + ['FB']
    idx = {n: i for i, n in enumerate(nodes)}
    matrix = []
    vref, bias = F(str(vref)), F(str(bias))
    for net in rows:
        coeff, rhs = [F(0)] * len(nodes), -bias if net == 'FB' else F(0)
        for ref, a, b, _, _ in edges:
            if net not in (a, b):
                continue
            other = b if net == a else a
            g = 1 / F(str(resistances[ref]))
            for term, sign in ((net, 1), (other, -1)):
                if term == 'FB':
                    rhs -= sign * g * vref
                elif term != 'GND':
                    coeff[idx[term]] += sign * g
        matrix.append(coeff + [rhs])
    n = len(nodes)
    if len(matrix) != n:
        raise ValueError('oracle dimension mismatch')
    for col in range(n):
        pivot = next((row for row in range(col, n) if matrix[row][col]), None)
        if pivot is None:
            raise ValueError('oracle singular')
        matrix[col], matrix[pivot] = matrix[pivot], matrix[col]
        factor = matrix[col][col]
        matrix[col] = [x / factor for x in matrix[col]]
        for row in range(n):
            if row == col:
                continue
            factor = matrix[row][col]
            matrix[row] = [x - factor * y for x, y in zip(matrix[row], matrix[col])]
    return matrix[idx['SOURCE']][-1]


def oracle_window(edges, vref, bias, exact=False):
    options = [(F(str(ohm)) * (1-F(str(tol))), F(str(ohm)) * (1+F(str(tol))))
               for _, _, _, ohm, tol in edges]
    values = []
    for resistance_corner in itertools.product(*options):
        resistances = {edge[0]: value for edge, value in zip(edges, resistance_corner)}
        for v, i in itertools.product((vref['min'], vref['max']), (bias['min'], bias['max'])):
            values.append(rational_source(edges, resistances, v, i))
    result = min(values), max(values)
    return result if exact else tuple(map(float, result))


class IndependentSafetyChecks(unittest.TestCase):
    vref = {'min': .77, 'typ': .8, 'max': .83}

    def network(self, edges=None, extra=(), chosen_model=None):
        db = make_db(edges or shared_edges(), extra)
        chosen_model = chosen_model or model()
        return db, Solver(db, model=chosen_model).linear_network('FB')

    def hot(self, db, chosen_model=None, all_sources=True, alter=None):
        chosen_model = chosen_model or model()
        refs = sorted(db['parts']) if all_sources else ['U1']
        check = {'id': 'INDEPENDENT-FB', 'rule': 'Rule-08', 'kind': 'divider', 'net': 'FB',
                 'vref': self.vref, 'divider_model': chosen_model, 'depends_on': refs,
                 'expected': {'min': -1000, 'max': 1000}, 'citation': 'Synthetic independent KCL specification'}
        evidence = {'schema_version': 1, 'checks': [check]}
        with tempfile.TemporaryDirectory(dir=REVIEW) as directory:
            audit = bind_evidence(db, evidence, directory)
            if not all_sources:
                # Freeze the missing-source input independently of the production
                # fixture's changing dependency discovery logic.
                check['basis']['sources'] = [source for source in check['basis']['sources']
                                            if source['ref'] in refs]
            if alter:
                alter(db, check, audit)
            self.assertEqual(validate_evidence(evidence), [])
            lint = Lint(db, evidence=evidence, datasheet_audit=audit)
            lint.run()
            self.assertEqual(len(lint.results), 1)
            return lint.results[0]

    def test_shared_and_bridge_match_exact_rational_oracle(self):
        for edges in (shared_edges(), bridge_edges()):
            with self.subTest(edges=edges):
                db, network = self.network(edges)
                self.assertEqual(len(network['resistors']), len(edges))
                self.assertEqual(Solver(db, model=model()).solve_net('FB')['status'], 'ambiguous')
                window = linear_feedback_window(network, self.vref, model()['bias_current_a'])
                low, high = oracle_window(edges, self.vref, model()['bias_current_a'])
                self.assertAlmostEqual(window['min'], low, delta=1e-11)
                self.assertAlmostEqual(window['max'], high, delta=1e-11)
                result = self.hot(db)
                self.assertEqual(result['review_result'], 'PASS')
                self.assertAlmostEqual(result['calculation']['min'], low, delta=1e-11)

    def test_dense_interior_samples_stay_inside_exact_corner_envelope(self):
        rng = random.Random(48572061)
        for edges in (shared_edges(), bridge_edges()):
            low, high = oracle_window(edges, self.vref, model()['bias_current_a'])
            for _ in range(128):
                resistances = {ref: ohm * (1 + tol * rng.uniform(-1, 1)) for ref, _, _, ohm, tol in edges}
                value = float(rational_source(edges, resistances, rng.uniform(.77, .83), rng.uniform(-3e-6, 7e-6)))
                self.assertLessEqual(low, value)
                self.assertLessEqual(value, high)

    def test_dead_end_resistor_retained_and_does_not_change_static_result(self):
        edges = shared_edges()
        _, base = self.network(edges)
        _, extended = self.network(edges + [('R5', 'MID', 'DEAD', 8888, .2)])
        self.assertEqual(len(extended['resistors']), 5)
        before = linear_feedback_window(base, self.vref, model()['bias_current_a'])
        after = linear_feedback_window(extended, self.vref, model()['bias_current_a'])
        self.assertAlmostEqual(before['min'], after['min'], delta=1e-11)
        self.assertAlmostEqual(before['max'], after['max'], delta=1e-11)

    def test_unmodelled_semiconductor_and_intermediate_input_rejected(self):
        for ref, value in (('Q1', 'MOS'), ('D1', 'LED'), ('J1', 'CONN'), ('U2', 'INPUT'), ('M2', 'MODULE')):
            with self.subTest(ref=ref):
                db = make_db(shared_edges(), [(ref, value, [('1', 'MID')])])
                chosen = model()
                chosen['ignored_nodes'][ref+'.1'] = 'Must not convert hidden load into FB bias'
                with self.assertRaises(ValueError):
                    Solver(db, model=chosen).linear_network('FB')
                self.assertEqual(self.hot(db, chosen)['review_result'], 'INSUFFICIENT')

    def test_ignored_capacitor_requires_bound_evidence(self):
        db = make_db(shared_edges(), [('C1', '100nF', [('1', 'MID'), ('2', 'GND')])])
        chosen = model()
        chosen['ignored_nodes']['C1.1'] = 'Synthetic ideal DC-open capacitor, leakage specified as zero'
        self.assertEqual(self.hot(db, chosen)['review_result'], 'PASS')
        def omit(db, check, audit):
            check['depends_on'].remove('C1')
            check['basis']['sources'] = [x for x in check['basis']['sources'] if x['ref'] != 'C1']
        result = self.hot(db, chosen, alter=omit)
        self.assertEqual(result['review_result'], 'INSUFFICIENT')
        self.assertIn('C1', result['detail'])

    def test_missing_resistor_evidence_or_tolerance_rejected(self):
        db = make_db(shared_edges())
        self.assertEqual(self.hot(db, all_sources=False)['review_result'], 'INSUFFICIENT')
        db['parts']['R3']['value'] = '3000'
        self.assertEqual(self.hot(db)['review_result'], 'INSUFFICIENT')

    def test_stale_dependency_and_document_rejected(self):
        def change_material(db, check, audit):
            db['parts']['R3']['value'] = '3100/5%'
        def change_document(db, check, audit):
            material = next(x for x in audit['materials'] if 'R3' in x['refdes'])
            Path(material['document']['path']).write_bytes(b'SYNTHETIC revised source')
        def remove_identity(db, check, audit):
            next(x for x in check['basis']['sources'] if x['ref'] == 'R3')['identity'] = 'WRONG'
        for alter in (change_material, change_document, remove_identity):
            with self.subTest(alter=alter.__name__):
                self.assertEqual(self.hot(make_db(shared_edges()), alter=alter)['review_result'], 'INSUFFICIENT')

    def test_declared_boundaries_must_exist_and_be_distinct(self):
        for source, ground in (('GND', 'GND'), ('FB', 'GND'), ('SOURCE', 'FB'), ('MISSING', 'GND')):
            chosen = model()
            chosen.update(source_net=source, reference_net=ground)
            with self.assertRaises(ValueError):
                self.network(chosen_model=chosen)
        edges = shared_edges() + [('R5', 'MID', 'AGND', 1000, .01)]
        with self.assertRaises(ValueError):
            self.network(edges)

    def test_source_reachable_only_through_clamped_reference_is_not_control(self):
        edges = [('R1', 'FB', 'GND', 1000, .01), ('R2', 'GND', 'SOURCE', 2000, .01)]
        with self.assertRaises(ValueError):
            self.network(edges)
        self.assertEqual(self.hot(make_db(edges))['review_result'], 'INSUFFICIENT')

    def test_invalid_or_multi_pin_resistors_and_pseudo_nodes_rejected(self):
        for value in ('0R/1%', '-1000/1%', 'garbage', '1000/100%', '1000/NaN%'):
            db = make_db(shared_edges())
            db['parts']['R3']['value'] = value
            with self.subTest(value=value), self.assertRaises(ValueError):
                net = Solver(db, model=model()).linear_network('FB')
                linear_feedback_window(net, self.vref, model()['bias_current_a'])
        db = make_db(shared_edges())
        db['nets']['MID'].append('R3.3')
        db['pin2net']['R3.3'] = 'MID'
        with self.assertRaises(ValueError):
            Solver(db, model=model()).linear_network('FB')
        db = make_db(shared_edges())
        db['pseudo_nets'] = ['MID']
        with self.assertRaises(ValueError):
            Solver(db, model=model()).linear_network('FB')

    def test_dynamic_range_corner_cap_and_nonfinite_values_rejected(self):
        edges = shared_edges()
        edges[0] = ('R1', 'SOURCE', 'MID', 1e14, .01)
        _, network = self.network(edges)
        with self.assertRaises(ValueError):
            linear_feedback_window(network, self.vref, model()['bias_current_a'])
        edges = shared_edges() + [('R'+str(i), 'FB', 'GND', 1000+i, .01) for i in range(5, 12)]
        with self.assertRaises(ValueError):
            # The invariant is rejection of a guaranteed window with >10
            # variable resistors, whether collection or solving enforces it.
            _, too_many_variables = self.network(edges)
            linear_feedback_window(too_many_variables, self.vref, model()['bias_current_a'])
        _, network = self.network()
        for bad in (float('nan'), float('inf'), True):
            vref = dict(self.vref, min=bad)
            with self.assertRaises(ValueError):
                linear_feedback_window(network, vref, model()['bias_current_a'])

    def test_dnp_hidden_load_is_removed_by_assembly(self):
        db = make_db(shared_edges(), [('Q1', 'MOS', [('1', 'MID')])])
        db['parts']['Q1']['nc'] = True
        net = Solver(db, model=model()).linear_network('FB')
        self.assertNotIn('Q1', net['required_refs'])

    def test_accepted_ill_conditioned_network_cannot_false_pass_narrow_requirement(self):
        edges = [('R1', 'SOURCE', 'MID', 10**10, 0), ('R2', 'MID', 'FB', 1, 0),
                 ('R3', 'MID', 'GND', 10**10, 0), ('R4', 'FB', 'GND', 10**10, 0)]
        exact = rational_source(edges, {edge[0]: edge[3] for edge in edges}, .8, 0)
        self.assertEqual(exact, F('2.40000000016'))
        lower_limit = 2.4000001
        self.assertLess(exact, F(str(lower_limit)))
        chosen = model()
        chosen['bias_current_a'] = {'min': 0, 'max': 0}
        def narrow(db, check, audit):
            check['vref'] = {'min': .8, 'typ': .8, 'max': .8}
            check['expected'] = {'min': lower_limit, 'max': 3}
        result = self.hot(make_db(edges), chosen, alter=narrow)
        self.assertNotEqual(result['review_result'], 'PASS', result)

    def test_planner_cannot_ready_new_network_without_resistor_sources(self):
        db = make_db(shared_edges())
        check = {'id': 'INDEPENDENT-PLAN', 'rule': 'Rule-08', 'kind': 'divider', 'net': 'FB',
                 'vref': self.vref, 'divider_model': model(), 'depends_on': ['U1'],
                 'expected': {'min': 0, 'max': 10}, 'citation': 'Synthetic missing resistor source'}
        evidence = {'schema_version': 1, 'checks': [check]}
        with tempfile.TemporaryDirectory(dir=REVIEW) as directory:
            audit = bind_evidence(db, evidence, directory)
            check['basis']['sources'] = [source for source in check['basis']['sources']
                                        if source['ref'] == 'U1']
            plan = build_review_plan(db, evidence=evidence, datasheet_audit=audit)
            targets = [item for item in plan['checks'] if item.get('evidence_check_id') == check['id']]
            self.assertTrue(targets)
            self.assertTrue(all(item['readiness'] == 'WAITING_EVIDENCE' for item in targets), targets)

    def test_decimal_resistance_and_signed_bias_keep_exact_envelope(self):
        edges = [('R1', 'SOURCE', 'MID', .9, .01), ('R2', 'MID', 'FB', 1.3, .02),
                 ('R3', 'MID', 'GND', 2.7, .03), ('R4', 'FB', 'GND', 4.1, .05)]
        _, network = self.network(edges)
        for bias in ({'min': -.005, 'max': -.001}, {'min': -3e-6, 'max': 7e-6}):
            with self.subTest(bias=bias):
                low, high = oracle_window(edges, self.vref, bias, exact=True)
                window = linear_feedback_window(network, self.vref, bias)
                self.assertEqual(F(window['bounds_exact']['min']), low)
                self.assertEqual(F(window['bounds_exact']['max']), high)
                self.assertLessEqual(F.from_float(window['min']), low)
                self.assertGreaterEqual(F.from_float(window['max']), high)

    def test_malformed_source_reference_types_are_rejected_without_crash(self):
        for key, value in (('source_net', ['SOURCE']), ('reference_net', {'net': 'GND'})):
            chosen = model()
            chosen[key] = value
            check = {'id': 'MALFORMED-BOUNDARY', 'rule': 'Rule-08', 'kind': 'divider', 'net': 'FB',
                     'vref': self.vref, 'divider_model': chosen, 'expected': {'min': 0, 'max': 10},
                     'citation': 'Synthetic malformed boundary'}
            evidence = {'schema_version': 1, 'checks': [check]}
            with self.subTest(key=key):
                if validate_evidence(evidence):
                    continue
                plan = build_review_plan(make_db(shared_edges()), evidence=evidence)
                targets = [item for item in plan['checks'] if item.get('evidence_check_id') == check['id']]
                self.assertTrue(targets)
                self.assertTrue(all(item['readiness'] == 'WAITING_EVIDENCE' for item in targets))

    def test_expanded_fixed_resistor_grid_matches_independent_exact_kcl(self):
        ends = [('SOURCE', 'N00'), ('SOURCE', 'N10'), ('N00', 'N01'), ('N01', 'FB'),
                ('N10', 'N11'), ('N11', 'N12'), ('N00', 'N10'), ('N01', 'N11'),
                ('FB', 'N12'), ('FB', 'GND'), ('N12', 'GND')]
        edges = [('R'+str(i), a, b, 1000+100*i, 0) for i, (a, b) in enumerate(ends, 1)]
        db, network = self.network(edges)
        self.assertEqual(len(network['resistors']), 11)
        low, high = oracle_window(edges, self.vref, model()['bias_current_a'], exact=True)
        window = linear_feedback_window(network, self.vref, model()['bias_current_a'])
        self.assertEqual(F(window['bounds_exact']['min']), low)
        self.assertEqual(F(window['bounds_exact']['max']), high)
        self.assertEqual(self.hot(db)['review_result'], 'PASS')


if __name__ == '__main__':
    print(json.dumps({'candidate_sha256': {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
                                          for path in (ROOT/'scripts/solve_dividers.py', ROOT/'scripts/lint.py', ROOT/'scripts/electrical_contract.py')}}))
    unittest.main(verbosity=2)
