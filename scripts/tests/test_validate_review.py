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


def bound_fixture():
    p, r, _ = fixture()
    for check in p['checks']:
        check.update(object={'audit': check['id']}, criterion='enumerate the declared audit scope')
    p['checks'][0].update(object={'ref': 'U10', 'node': 'U10.4', 'net': 'EN',
                                  'state': 'RUN', 'configuration': 'A'}, criterion='EN >= 2.0 V')
    r['plan_digest'] = fingerprint(p)
    r['binding_version'] = 1
    for planned, result in zip(p['checks'], r['checks']):
        result['binding'] = {k: copy.deepcopy(planned[k]) for k in ('object', 'criterion')}
    return p, r


def bound_fail(report, severity='P1'):
    item = fail(report, severity)
    report['findings'][0]['location'].update(refs=['R10', 'U10'], nets=['VIN', 'EN'])
    return item


class ReviewBindingTests(unittest.TestCase):
    def test_complete_bound_ledger_can_release(self):
        p, r = bound_fixture()
        out = validate_review(p, r)
        self.assertTrue(out['valid'], out)
        self.assertEqual(out['release'], 'GO')
        self.assertTrue(out['binding_validation']['enforced'])
        self.assertEqual(out['binding_validation']['bound_checks'], len(p['checks']))

    def test_legacy_report_remains_explicitly_unbound(self):
        p, r, db = fixture()
        out = validate_review(p, r, db)
        self.assertTrue(out['valid'], out)
        self.assertFalse(out['binding_validation']['enforced'])

    def test_required_binding_cannot_be_disabled_by_omitting_version(self):
        p, r, _ = fixture()
        out = validate_review(p, r, require_bindings=True)
        self.assertFalse(out['valid'])
        self.assertEqual(out['release'], 'NO_GO')

    def test_partial_binding_without_version_is_rejected(self):
        p, r = bound_fixture(); del r['binding_version']
        self.assertFalse(validate_review(p, r)['valid'])

    def test_invalid_binding_versions_are_rejected(self):
        for version in (True, False, 0, 2, '1', None, []):
            with self.subTest(version=version):
                p, r = bound_fixture(); r['binding_version'] = version
                self.assertFalse(validate_review(p, r)['valid'])

    def test_missing_binding_is_rejected(self):
        p, r = bound_fixture(); del r['checks'][0]['binding']
        self.assertFalse(validate_review(p, r)['valid'])

    def test_object_ref_pin_net_state_and_configuration_must_match(self):
        for field, value in [('ref', 'U11'), ('node', 'U10.5'), ('net', 'STRAP'),
                             ('state', 'OFF'), ('configuration', 'B')]:
            with self.subTest(field=field):
                p, r = bound_fixture(); r['checks'][0]['binding']['object'][field] = value
                self.assertFalse(validate_review(p, r)['valid'])

    def test_criterion_mismatch_is_rejected_even_if_result_is_pass(self):
        p, r = bound_fixture(); r['checks'][0]['binding']['criterion'] = 'STRAP <= 0.8 V'
        self.assertFalse(validate_review(p, r)['valid'])

    def test_malformed_binding_fails_closed(self):
        for binding in (None, [], 'EN', {}, {'object': [], 'criterion': 'EN >= 2.0 V'},
                        {'object': {}, 'criterion': None}):
            with self.subTest(binding=binding):
                p, r = bound_fixture(); r['checks'][0]['binding'] = binding
                self.assertFalse(validate_review(p, r)['valid'])

    def test_malformed_primary_coordinates_cannot_disable_anchors(self):
        for field in ('ref', 'node', 'net'):
            for value in ([], {}, 1, None, ''):
                with self.subTest(field=field, value=value):
                    p, r = bound_fixture(); bound_fail(r)
                    p['checks'][0]['object'][field] = value
                    r['checks'][0]['binding']['object'] = copy.deepcopy(p['checks'][0]['object'])
                    r['plan_digest'] = fingerprint(p)
                    self.assertFalse(validate_review(p, r)['valid'])

    def test_changed_plan_cannot_reuse_old_binding_after_rehash(self):
        p, r = bound_fixture(); p['checks'][0]['criterion'] = 'EN >= 2.2 V'
        r['plan_digest'] = fingerprint(p)
        self.assertFalse(validate_review(p, r)['valid'])

    def test_unbound_plan_criterion_cannot_be_invented_by_result(self):
        p, r = bound_fixture(); del p['checks'][0]['criterion']
        r['plan_digest'] = fingerprint(p)
        self.assertFalse(validate_review(p, r)['valid'])

    def test_en_cannot_be_failed_by_strap_finding(self):
        p, r = bound_fixture(); bound_fail(r)
        r['findings'][0]['location'].update(refs=['U11', 'R12', 'R13'], nets=['VDD', 'STRAP'])
        r['findings'][0]['criterion'] = 'STRAP <= 0.8 V'
        r['checks'][0]['rationale'] = 'STRAP 0.845..0.910 V violates guaranteed LOW'
        out = validate_review(p, r)
        self.assertFalse(out['valid'])
        self.assertTrue(any('U10' in x for x in out['errors']), out)
        self.assertEqual(out['release'], 'NO_GO')

    def test_primary_net_must_be_in_finding_location(self):
        p, r = bound_fixture(); bound_fail(r)
        r['findings'][0]['location']['nets'] = ['STRAP']
        self.assertFalse(validate_review(p, r)['valid'])

    def test_primary_node_owner_is_checked_without_ref(self):
        p, r = bound_fixture(); del p['checks'][0]['object']['ref']
        r['checks'][0]['binding']['object'] = copy.deepcopy(p['checks'][0]['object'])
        r['plan_digest'] = fingerprint(p); bound_fail(r)
        r['findings'][0]['location']['refs'] = ['U11']
        self.assertFalse(validate_review(p, r)['valid'])

    def test_upstream_cause_with_affected_target_is_allowed(self):
        p, r = bound_fixture(); bound_fail(r)
        p['checks'][0]['object']['refs'] = ['U10', 'U11', 'R10', 'J1']
        r['checks'][0]['binding']['object'] = copy.deepcopy(p['checks'][0]['object'])
        r['plan_digest'] = fingerprint(p)
        out = validate_review(p, r)
        self.assertTrue(out['valid'], out)
        self.assertEqual(out['release'], 'NO_GO')

    def test_requirement_aggregate_has_no_invented_physical_anchor(self):
        p, r = bound_fixture(); bound_fail(r)
        p['checks'][0]['object'] = {'requirement_id': 'REQ-EN'}
        r['checks'][0]['binding']['object'] = copy.deepcopy(p['checks'][0]['object'])
        r['plan_digest'] = fingerprint(p); r['coverage']['requirements'] = {'REQ-EN': ['C1']}
        self.assertTrue(validate_review(p, r)['valid'])

    def test_defect_cannot_link_a_pass_check(self):
        p, r = bound_fixture(); bound_fail(r)
        r['findings'][0]['check_ids'].append(r['checks'][1]['id'])
        self.assertFalse(validate_review(p, r)['valid'])

    def test_true_p0_is_not_removed_by_binding_validation(self):
        p, r = bound_fixture(); bound_fail(r, 'P0')
        out = validate_review(p, r)
        self.assertTrue(out['valid'], out)
        self.assertEqual(out['release'], 'NO_GO')
        self.assertEqual(out['summary']['by_severity']['P0'], 1)

    def test_insufficient_and_na_keep_existing_semantics(self):
        p, r = bound_fixture()
        r['checks'][0].update(review_result='INSUFFICIENT', evidence_confidence='C',
                              missing_inputs=['guaranteed startup peak'], potential_severity='P1')
        out = validate_review(p, r)
        self.assertTrue(out['valid'], out); self.assertEqual(out['release'], 'NO_GO')
        p, r = bound_fixture()
        r['checks'][0].update(review_result='NA', applicability='NOT_APPLICABLE', applicability_evidence=E)
        self.assertTrue(validate_review(p, r)['valid'])


if __name__ == '__main__':
    unittest.main()
