"""Cold/hot CLI handoff regressions using explicitly synthetic circuit conditions."""
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
from electrical_fixtures import bind_evidence, pin_analysis
from decoupling import input_fingerprint
from plan_review import build_review_plan
from validate_review import fingerprint, validate_review, SCOPE
from test_validate_review import E
from test_remediation import ready


class PlanHandoffTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.db = {'parts': {'U1': {'value': 'SYNTHETIC-IC', 'nc': False}},
            'nets': {'CONTROL': ['U1.1']}, 'pin2net': {'U1.1': 'CONTROL'},
            'pinname': {'U1.1': 'EN'}, 'pintype': {}, 'ref2page': {'U1': 1}, 'pseudo_nets': []}
        self.cold = build_review_plan(self.db, self.inventory_context())
        self.parent = next(c['id'] for c in self.cold['checks'] if c.get('rule') == 'Rule-12')
        base = {'rule': 'Rule-12', 'kind': 'pin_bias', 'node': 'U1.1',
            'required_default': 'high', 'vih_min_v': 2.0, 'abs_min_v': -0.3,
            'abs_max_v': 5.5, 'citation': 'Synthetic IC Rev.A p.1'}
        self.evidence = {'schema_version': 1, 'checks': [
            dict(base, id='EN-RUN', voltage_analysis=pin_analysis(3.3, 3.3)),
            dict(base, id='EN-BOOT', voltage_analysis=pin_analysis(0.0, 0.0))]}
        self.audit = bind_evidence(self.db, self.evidence, self.root)
        for e in self.evidence['checks']:
            e['basis']['state'] = e['id']

    def inventory_context(self):
        # These fixtures deliberately model only a signal input, not IC supply pins.
        # Declare that limited synthetic model instead of automatically passing a
        # new unresolved physical-device inventory. Frozen circuit data is untouched.
        return {'decoupling': {'schema_version': 1, 'input_sha256': input_fingerprint(self.db),
            'states': [{'id': 'synthetic-signal-only', 'citation': 'Stipulated input-only test model',
                        'population': {r: True for r in self.db['parts']}}],
            'components': {'U1': {'kind': 'other',
                'citation': 'Synthetic one-pin input stub; no supply terminal modeled in this ledger-handoff test'}}}}

    def merged(self, previous=None):
        return build_review_plan(self.db, self.inventory_context(), evidence=self.evidence, datasheet_audit=self.audit,
                                 previous_plan=previous if previous is not None else self.cold)

    def write(self, name, value):
        path = self.root/name
        path.write_text(json.dumps(value))
        return path

    def cli(self, script, *args):
        result = subprocess.run([sys.executable, '-B', str(SCRIPTS/script), *map(str, args)],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def runs(self, previous=None):
        db = self.write('db.json', self.db)
        intent = self.write('intent.json', self.inventory_context())
        self.cli('lint.py', db, '--intent', intent, '--plan-json', self.root/'cold.json', '--json', self.root/'lint-cold.json')
        if previous is not None:
            self.write('cold.json', previous)
        self.cli('lint.py', db, '--intent', intent, '--evidence', self.write('evidence.json', self.evidence),
                 '--datasheet-audit', self.write('audit.json', self.audit),
                 '--merge-plan', self.root/'cold.json', '--plan-json', self.root/'final.json',
                 '--json', self.root/'lint-hot.json')
        read = lambda name: json.loads((self.root/name).read_text())
        return read('final.json'), [read('lint-cold.json'), read('lint-hot.json')]

    def report(self, plan, runs, defect=False, defect_evidence='EN-BOOT'):
        # Stipulated ledger fixture only; these records are not actual engineering conclusions.
        checks = []
        for p in plan['checks']:
            app = p['applicability']
            item = {'id': p['id'], 'applicability': app, 'review_result': 'PASS',
                'evidence_confidence': 'A', 'evidence': E, 'rationale': 'Stipulated synthetic review',
                'blocking': False, 'handoff': deepcopy(p['handoff'])}
            if app != 'APPLICABLE':
                item.update(applicability='NOT_APPLICABLE', review_result='NA', applicability_evidence=E)
            if item['handoff']['required']:
                item['handoff'].update(state='ACCEPTED', evidence=E)
            checks.append(item)
        report = {'schema_version': 2, 'remediation_version': 1,
            'plan_digest': fingerprint(plan), 'db_digest': fingerprint(self.db),
            'checks': checks, 'findings': [],
            'scope_checks': {d: next(p['id'] for p in plan['checks'] if p['check'] == 'coverage-' + d) for d in SCOPE},
            'coverage': {'requirements': {},
                'components': {k: [self.parent] for k in self.db['parts']},
                'pins': {k: [self.parent] for k in self.db['pin2net']},
                'nets': {k: [self.parent] for k in self.db['nets']},
                'pages': {str(k): [self.parent] for k in self.db['ref2page'].values()}},
            'lint_reviews': []}
        if defect:
            target = next(p['id'] for p in plan['checks'] if p.get('evidence_check_id') == defect_evidence)
            next(x for x in checks if x['id'] == target).update(review_result='FAIL', severity='P1', finding_id='F1')
            report['findings'] = [{'id': 'F1', 'kind': 'DEFECT', 'severity': 'P1', 'check_ids': [target],
                'location': {'pages': ['1'], 'refs': ['U1'], 'nets': ['CONTROL']},
                **{k: 'Synthetic condition' for k in ('title', 'observed', 'criterion', 'impact', 'scenario',
                      'root_cause', 'recommendation', 'verification', 'severity_reason')}, 'remediation': ready()}]
        for run in runs:
            items = {}
            for i, finding in enumerate(run['findings']):
                targets = [p['id'] for p in plan['checks']
                           if finding.get('check_id') and p.get('evidence_check_id') == finding['check_id']]
                items[str(i)] = targets or [self.parent]
            report['lint_reviews'].append({'run_digest': fingerprint(run), 'items': items})
        return report

    def test_combined_cold_cli_preserves_standalone_plan(self):
        _, runs = self.runs()
        self.assertEqual(runs[0]['review_plan'], self.cold)

    def test_old_cold_ledger_cannot_hide_hot_state_checks(self):
        _, runs = self.runs()
        report = self.report(self.cold, runs)
        result = validate_review(self.cold, report, self.db, runs, require_actionable=True)
        self.assertFalse(result['valid'])
        self.assertEqual(result['release'], 'NO_GO')
        self.assertTrue(any('final plan omits lint check' in e for e in result['errors']))

    def test_complete_state_ledger_is_valid_but_fail_blocks_release(self):
        final, runs = self.runs()
        actual = {x['check_id']: x['review_result'] for x in runs[1]['check_results']}
        self.assertEqual(actual, {'EN-BOOT': 'FAIL', 'EN-RUN': 'PASS'})
        report = self.report(final, runs, defect=True)
        result = validate_review(final, report, self.db, runs, require_actionable=True)
        self.assertTrue(result['valid'], result)
        self.assertEqual(result['release'], 'NO_GO')
        self.assertEqual(result['summary']['confirmed_defects'], 1)

    def test_complete_passing_state_ledger_can_release(self):
        self.evidence['checks'][1]['voltage_analysis'] = pin_analysis(3.3, 3.3)
        final, runs = self.runs()
        report = self.report(final, runs)
        result = validate_review(final, report, self.db, runs, require_actionable=True)
        self.assertTrue(result['valid'], result)
        self.assertEqual(result['release'], 'GO')

    def test_candidate_cannot_link_only_parent(self):
        final, runs = self.runs()
        report = self.report(final, runs, defect=True)
        report['lint_reviews'][1]['items'] = {str(i): [self.parent] for i in range(len(runs[1]['findings']))}
        result = validate_review(final, report, self.db, runs, require_actionable=True)
        self.assertTrue(any('hot candidate must link its state checks' in e for e in result['errors']))

    def test_manual_check_retained_and_old_plan_not_mutated(self):
        manual = deepcopy(self.cold['checks'][0])
        manual.update(id='MANUAL-RETURN-PATH', check='manual-return-path', criterion='Review return path',
                      review_result='PASS', evidence_confidence='A')
        previous = deepcopy(self.cold)
        previous['checks'].append(manual)
        snapshot = deepcopy(previous)
        final, runs = self.runs(previous)
        retained = next(c for c in final['checks'] if c['id'] == manual['id'])
        self.assertIsNone(retained['review_result'])
        self.assertEqual(previous, snapshot)
        report = self.report(final, runs, defect=True)
        report['checks'] = [c for c in report['checks'] if c['id'] != manual['id']]
        self.assertFalse(validate_review(final, report, self.db, runs)['valid'])

    def test_changed_netlist_or_criterion_cannot_merge_silently(self):
        previous = deepcopy(self.cold)
        previous['db_sha256'] = 'old-revision'
        with self.assertRaisesRegex(ValueError, 'db_sha256'):
            self.merged(previous)
        previous = deepcopy(self.cold)
        previous['checks'][0]['criterion'] = 'Changed requirement'
        with self.assertRaisesRegex(ValueError, 'criterion changed'):
            self.merged(previous)

    def test_duplicate_or_incomplete_manual_check_rejected(self):
        previous = deepcopy(self.cold)
        previous['checks'].append(deepcopy(previous['checks'][0]))
        with self.assertRaisesRegex(ValueError, 'duplicate'):
            self.merged(previous)
        previous['checks'][-1] = {'id': 'MANUAL'}
        with self.assertRaisesRegex(ValueError, 'incomplete'):
            self.merged(previous)

    def test_manual_handoff_refinement_is_not_silently_overwritten(self):
        previous = deepcopy(self.cold)
        previous['checks'][0]['handoff'].update(required=True, receivers=['Power owner'],
            constraint='Synthetic voltage drop below 20 mV', verification='Synthetic four-wire measurement')
        with self.assertRaisesRegex(ValueError, 'handoff details changed'):
            self.merged(previous)

    def test_explicit_series_check_outside_naming_heuristics_is_reviewable(self):
        self.db = {'parts': {'U1': {'value': 'SYNTHETIC-IC', 'nc': False},
                            'R1': {'value': '1K/1%', 'nc': False}},
            'nets': {'SIGNAL_IN': ['U1.1', 'R1.1'], 'SIGNAL_OUT': ['R1.2']},
            'pin2net': {'U1.1': 'SIGNAL_IN', 'R1.1': 'SIGNAL_IN', 'R1.2': 'SIGNAL_OUT'},
            'pinname': {'U1.1': 'GPIO', 'R1.1': '1', 'R1.2': '2'},
            'pintype': {}, 'ref2page': {'U1': 1, 'R1': 1}, 'pseudo_nets': []}
        self.cold = build_review_plan(self.db, self.inventory_context())
        self.parent = self.cold['checks'][0]['id']
        self.evidence = {'schema_version': 1, 'checks': [{'id': 'SERIES-ALERT', 'rule': 'Rule-09',
            'kind': 'required_series', 'net': 'SIGNAL_IN', 'to': 'SIGNAL_OUT',
            'resistance_ohm': {'min': 2000, 'max': 3000}, 'depends_on': ['R1'],
            'citation': 'Synthetic interface Rev.A p.1'}]}
        self.audit = bind_evidence(self.db, self.evidence, self.root)
        final, runs = self.runs()
        self.assertTrue(any(c.get('evidence_check_id') == 'SERIES-ALERT' for c in final['checks']))
        self.assertEqual(runs[1]['check_results'][0]['review_result'], 'FAIL')
        report = self.report(final, runs, defect=True, defect_evidence='SERIES-ALERT')
        result = validate_review(final, report, self.db, runs, require_actionable=True)
        self.assertTrue(result['valid'], result)
        self.assertEqual(result['release'], 'NO_GO')

    def test_lint_from_another_baseline_rejected(self):
        final, runs = self.runs()
        runs[0]['review_plan']['db_sha256'] = 'old-revision'
        report = self.report(final, runs, defect=True)
        self.assertFalse(validate_review(final, report, self.db, runs)['valid'])


if __name__ == '__main__':
    unittest.main()
