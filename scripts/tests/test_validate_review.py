import copy
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from validate_review import validate_review, fingerprint, SCOPE

E = [{'source': 'synthetic-fixture.json', 'locator': 'all values stipulated for tests'}]


def fixture():
    keys = ['C1'] + sorted(SCOPE)
    plan = {'checks': [{'id': k, 'applicability': 'APPLICABLE', 'object': {}} for k in keys]}
    db = {'parts': {'R1': {}}, 'pin2net': {'R1.1': 'A'}, 'nets': {'A': ['R1.1']},
          'ref2page': {'R1': 1}, 'declared_pinname': {'R1.1': '1', 'R1.2': '2'}}
    report = {'schema_version': 2, 'plan_digest': fingerprint(plan), 'db_digest': fingerprint(db),
        'checks': [{'id': k, 'applicability': 'APPLICABLE', 'review_result': 'PASS',
                    'evidence_confidence': 'A', 'evidence': E, 'rationale': 'stipulated compliant fixture',
                    'blocking': False, 'handoff': {'required': False}} for k in keys],
        'findings': [], 'scope_checks': {k: k for k in SCOPE},
        'coverage': {'components': {'R1': ['C1']}, 'pins': {'R1.1': ['C1'], 'R1.2': ['C1']},
                     'nets': {'A': ['C1']}, 'pages': {'1': ['C1']}, 'requirements': {}}}
    return plan, report, db


def fail(report, severity='P1'):
    c = report['checks'][0]
    c.update(review_result='FAIL', severity=severity, finding_id='F1')
    report['findings'] = [{'id': 'F1', 'kind': 'DEFECT', 'severity': severity, 'check_ids': ['C1'],
        'location': {'pages': ['1'], 'refs': ['R1'], 'nets': ['A']},
        **{key: 'synthetic evidence' for key in ('title', 'observed', 'criterion', 'impact', 'scenario',
              'root_cause', 'recommendation', 'verification', 'severity_reason')}}]
    return c


class ReviewGateTests(unittest.TestCase):
    def test_complete_ledger_can_release(self):
        p, r, db = fixture()
        result = validate_review(p, r, db)
        self.assertTrue(result['valid'], result)
        self.assertEqual(result['release'], 'GO')

    def test_omitted_check_rejected(self):
        p, r, db = fixture(); r['checks'].pop()
        self.assertFalse(validate_review(p, r, db)['valid'])

    def test_declared_but_absent_pin_cannot_disappear_from_coverage(self):
        p, r, db = fixture(); del r['coverage']['pins']['R1.2']
        self.assertFalse(validate_review(p, r, db)['valid'])

    def test_one_part_missing_from_coverage_rejected(self):
        p, r, db = fixture(); r['coverage']['components'] = {}
        self.assertFalse(validate_review(p, r, db)['valid'])

    def test_c_confidence_cannot_pass(self):
        p, r, db = fixture(); r['checks'][0]['evidence_confidence'] = 'C'
        self.assertFalse(validate_review(p, r, db)['valid'])

    def test_na_requires_applicability_change_evidence(self):
        p, r, db = fixture(); r['checks'][0]['review_result'] = 'NA'
        self.assertFalse(validate_review(p, r, db)['valid'])
        r['checks'][0].update(applicability='NOT_APPLICABLE', applicability_evidence=E)
        self.assertTrue(validate_review(p, r, db)['valid'])

    def test_unknown_is_separate_and_blocks_for_potential_p0(self):
        p, r, db = fixture()
        r['checks'][0].update(review_result='INSUFFICIENT', evidence_confidence='C',
                              missing_inputs=['assembly BOM'], potential_severity='P0')
        result = validate_review(p, r, db)
        self.assertTrue(result['valid'], result)
        self.assertEqual(result['release'], 'NO_GO')
        self.assertEqual(result['summary']['confirmed_defects'], 0)

    def test_p1_blocks_even_if_agent_sets_nonblocking(self):
        p, r, db = fixture(); fail(r)
        result = validate_review(p, r, db)
        self.assertTrue(result['valid'], result)
        self.assertEqual(result['release'], 'NO_GO')

    def test_acceptance_preserves_fail_and_conditional_release(self):
        p, r, db = fixture(); c = fail(r)
        c.update(disposition='ACCEPTED', acceptance={key: 'synthetic approval only' for key in
                 ('by', 'date', 'scope', 'reason', 'record')})
        self.assertEqual(validate_review(p, r, db)['release'], 'CONDITIONAL_GO')
        c['severity'] = r['findings'][0]['severity'] = 'P0'
        self.assertEqual(validate_review(p, r, db)['release'], 'NO_GO')

    def test_fake_acceptance_rejected(self):
        p, r, db = fixture(); fail(r).update(disposition='ACCEPTED')
        self.assertFalse(validate_review(p, r, db)['valid'])

    def test_handoff_open_does_not_change_pass_but_blocks_release(self):
        p, r, db = fixture()
        r['checks'][0]['handoff'] = {'required': True, 'state': 'OPEN', 'receivers': ['Layout'],
                                    'constraint': 'specified impedance', 'verification': 'layout check'}
        result = validate_review(p, r, db)
        self.assertTrue(result['valid'], result)
        self.assertEqual(result['release'], 'NO_GO')

    def test_same_defect_multiple_checks_counted_once(self):
        p, r, db = fixture(); fail(r)
        second = copy.deepcopy(r['checks'][0]); second['id'] = 'C2'
        p['checks'].append({'id': 'C2', 'object': {}, 'applicability': 'APPLICABLE'})
        r['plan_digest'] = fingerprint(p); r['checks'].append(second)
        r['findings'][0]['check_ids'].append('C2')
        result = validate_review(p, r, db)
        self.assertTrue(result['valid'], result)
        self.assertEqual(result['summary']['results']['FAIL'], 2)
        self.assertEqual(result['summary']['confirmed_defects'], 1)

    def test_summary_and_claimed_release_cannot_hide_failure(self):
        p, r, db = fixture(); fail(r); r['release'] = 'GO'
        self.assertFalse(validate_review(p, r, db)['valid'])
        del r['release']; r['summary'] = {'confirmed_defects': 0}
        self.assertFalse(validate_review(p, r, db)['valid'])

    def test_changed_baseline_invalidates_ledger(self):
        p, r, db = fixture(); db['parts']['R1']['value'] = 'NEW'
        self.assertFalse(validate_review(p, r, db)['valid'])

    def test_export_failure_cannot_release(self):
        p, r, db = fixture(); db['export_errors'] = ['Aborting Netlisting']
        r['db_digest'] = fingerprint(db)
        self.assertEqual(validate_review(p, r, db)['release'], 'NO_GO')

    def test_unreviewed_lint_candidate_rejected(self):
        p, r, db = fixture()
        run = {'findings': [{'rule': 'Rule-04', 'detail': 'candidate 1'},
                            {'rule': 'Rule-04', 'detail': 'candidate 2'}]}
        r['lint_reviews'] = [{'run_digest': fingerprint(run), 'items': {'0': ['C1']}}]
        self.assertFalse(validate_review(p, r, db, [run])['valid'])
        r['lint_reviews'][0]['items']['1'] = ['C1']
        self.assertTrue(validate_review(p, r, db, [run])['valid'])

    def test_malformed_result_cannot_release(self):
        p, r, db = fixture()
        r['checks'][0]['review_result'] = ['PASS']
        self.assertFalse(validate_review(p, r, db)['valid'])

    def test_required_planned_handoff_cannot_be_silently_removed(self):
        p, r, db = fixture()
        p['checks'][0]['handoff'] = {'required': True}
        r['plan_digest'] = fingerprint(p)
        self.assertFalse(validate_review(p, r, db)['valid'])
        r['checks'][0]['handoff_evidence'] = E
        self.assertTrue(validate_review(p, r, db)['valid'])


if __name__ == '__main__':
    unittest.main()
