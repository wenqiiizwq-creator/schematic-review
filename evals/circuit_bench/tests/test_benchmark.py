"""Tests for the evaluator and independent oracle, including deliberate mutants."""
import copy
import importlib.util
import json
import math
import pathlib
import sys
import tempfile
import unittest

HERE = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
from generate import canonical, digest, make_cases, write_dataset
from oracle import feedback_value, judge, nodal, rc_value, solve_linear
from run import acceptance, compare, load_dataset, metrics, score_record
from stimulus import prepare


class OracleTests(unittest.TestCase):
    def test_analytic_feedback_and_bias_sign(self):
        case = make_cases()[0]
        self.assertAlmostEqual(feedback_value(case, case['resistors'], .8, 0), 2.4)
        self.assertAlmostEqual(feedback_value(case, case['resistors'], .8, 1e-6), 2.42)
        self.assertAlmostEqual(feedback_value(case, case['resistors'], .8, -1e-6), 2.38)

    def test_bridge_is_solvable_by_independent_kcl(self):
        case = next(c for c in make_cases() if c['family'] == 'feedback_bridge' and c['variant'] == 'normal')
        self.assertAlmostEqual(feedback_value(case, case['resistors'], .8, 0), 2.4)
        self.assertEqual(judge(case)['status'], 'PASS')

    def test_rc_time_constant_both_directions(self):
        self.assertAlmostEqual(rc_value(1000, 1e-6, 1, 0, .001), 1 - math.exp(-1))
        self.assertAlmostEqual(rc_value(1000, 1e-6, 0, 1, .001), math.exp(-1))

    def test_singular_network_rejected(self):
        with self.assertRaises(ValueError):
            solve_linear([[0]], [1])

    def test_public_example_and_selected_resistors(self):
        cases = [c for c in make_cases() if c['source'] == 'TI-SLVA689-2015']
        self.assertAlmostEqual(cases[0]['limits'][0], 966.6666666666666)
        self.assertAlmostEqual(cases[0]['limits'][1], 1770.3292812463117)
        self.assertEqual([judge(c)['status'] for c in cases], ['PASS', 'FAIL', 'FAIL', 'INSUFFICIENT'])

    def test_missing_or_stale_evidence_is_not_physical_pass(self):
        for case in make_cases():
            if case['evidence_state'] != 'current':
                self.assertEqual(judge(case)['status'], 'INSUFFICIENT')


    def test_corner_violations_are_detected(self):
        cases = [c for c in make_cases() if c['variant'] == 'corner_violation']
        self.assertEqual(len(cases), 18)
        self.assertTrue(all(judge(c)['status'] == 'FAIL' for c in cases))


class DatasetTests(unittest.TestCase):
    def test_reproducible_and_group_disjoint(self):
        cases = make_cases()
        self.assertEqual(canonical(cases), canonical(make_cases()))
        self.assertEqual(len(cases), 166)
        for family in {c['family'] for c in cases}:
            self.assertEqual(len({c['split'] for c in cases if c['family'] == family}), 1)
        self.assertEqual({judge(c)['status'] for c in cases}, {'PASS', 'FAIL', 'INSUFFICIENT'})

    def test_incomplete_dataset_and_tampered_label_rejected(self):
        with tempfile.TemporaryDirectory() as work:
            path = pathlib.Path(work) / 'data'
            write_dataset(path)
            load_dataset(path)
            rows = (path / 'answers.jsonl').read_text().splitlines()
            row = json.loads(rows[0])
            row['status'] = 'FAIL'
            rows[0] = canonical(row)
            (path / 'answers.jsonl').write_text('\n'.join(rows) + '\n')
            with self.assertRaises(ValueError):
                load_dataset(path)

    def test_no_labels_in_worker_input(self):
        for case in make_cases():
            payload = prepare(case)
            serialized = canonical(payload)
            self.assertNotIn('"status"', serialized)
            self.assertNotIn('"variant"', serialized)
            self.assertNotIn('"split"', serialized)
            self.assertNotIn('"oracle"', serialized)
            if case['kind'] != 'rc':
                self.assertNotIn('voltage_analysis', serialized)

    def test_reference_generator_does_not_import_checker(self):
        for name in ('oracle.py', 'generate.py', 'stimulus.py'):
            source = (HERE / name).read_text()
            for forbidden in ('from lint import', 'from solve_dividers import', 'from electrical_contract import'):
                self.assertNotIn(forbidden, source)
        self.assertNotIn('from oracle import', (HERE / 'worker.py').read_text())


class ScoringTests(unittest.TestCase):
    def records(self, mode):
        rows = []
        for case in make_cases():
            answer = judge(case)
            status = answer['status'] if mode == 'correct' else mode
            calculation = answer['window'] or {}
            if case['kind'] == 'rc':
                calculation = {'voltage_v': calculation}
            observed = {'status': status, 'parser_match': True, 'result': {'calculation': calculation}}
            rows.append(score_record(case, answer, observed))
        return rows

    def test_always_pass_mutant_rejected(self):
        rows = self.records('PASS')
        self.assertFalse(acceptance(rows)['declared_scope_pass'])
        self.assertGreater(metrics(rows)['false_pass'], 0)

    def test_always_insufficient_mutant_rejected(self):
        rows = self.records('INSUFFICIENT')
        self.assertFalse(acceptance(rows)['declared_scope_pass'])
        self.assertEqual(metrics(rows)['defect_recall'], 0)

    def test_always_fail_mutant_rejected(self):
        rows = self.records('FAIL')
        self.assertFalse(acceptance(rows)['declared_scope_pass'])
        self.assertGreater(metrics(rows)['false_alarm'], 0)

    def test_correct_labels_wrong_numbers_rejected(self):
        case = make_cases()[0]
        row = score_record(case, judge(case), {'status': 'PASS', 'parser_match': True,
                           'result': {'calculation': {'min': 0, 'max': 99}}})
        self.assertFalse(row['passed'])
        self.assertFalse(row['numerical_match'])

    def test_parser_error_cannot_be_relabelled_insufficient(self):
        case = next(c for c in make_cases() if judge(c)['status'] == 'INSUFFICIENT')
        row = score_record(case, judge(case), {'status': 'ERROR', 'error': 'parser failure'})
        self.assertFalse(row['passed'])
        self.assertEqual(metrics([row])['errors'], 1)

    def test_challenge_errors_or_false_certainty_block_gate(self):
        for status in ('ERROR', 'FAIL'):
            rows = self.records('correct')
            row = next(r for r in rows if r['split'] == 'challenge' and r['expected'] == 'INSUFFICIENT')
            row['observed'] = status
            row['passed'] = False
            self.assertFalse(acceptance(rows)['declared_scope_pass'])

    def test_comparison_rejects_protocol_or_dataset_change(self):
        a = dict(dataset_digest='a', answer_digest='b', protocol_digest='c',
                 records=self.records('correct'), acceptance={'declared_scope_pass': True})
        for key in ('dataset_digest', 'answer_digest', 'protocol_digest'):
            b = copy.deepcopy(a)
            b[key] = 'changed'
            self.assertFalse(compare(a, b)['comparable'])
        self.assertFalse(compare(a, a)['promote'])

    def test_comparison_detects_regression(self):
        a = dict(dataset_digest='a', answer_digest='b', protocol_digest='c',
                 records=self.records('correct'), acceptance={'declared_scope_pass': True})
        b = copy.deepcopy(a)
        b['records'][0]['passed'] = False
        result = compare(a, b)
        self.assertFalse(result['promote'])
        self.assertEqual(result['regressed_ids'], ['CB-0001'])


if __name__ == '__main__':
    unittest.main()
