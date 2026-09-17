"""Synthetic revision routing/ledger tests, not electrical acceptance evidence."""
from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
from electrical_contract import db_fingerprint
from plan_review import build_review_plan, validate_intent
from revision_impact import (attach_metadata, check_spec, COVERAGE_ID, digest,
                             validate_metadata, validate_reverification)
from validate_review import validate_review
from test_i2c_topology import add, fixture as i2c_fixture, pending_report


E = [{'source': 'synthetic-review.md', 'locator': 'current revision exercise; no physical result claimed'}]


def database():
    db = {'parts': {}, 'nets': {}, 'pin2net': {}, 'pinname': {}, 'pintype': {}, 'pseudo_nets': []}
    for ref in ('R1', 'R10'):
        add(db, ref, '10K/1%', [('1', '1', ref + '_A'), ('2', '2', ref + '_B')])
    return db


def check(key, ref):
    return {'id': key, 'check': 'synthetic-resistor-check', 'object': {'ref': ref},
            'criterion': 'Evaluate this independent synthetic resistor', 'rule': None,
            'applicability': 'APPLICABLE', 'readiness': 'READY', 'stage': 'ER4',
            'executor': 'Expert Review', 'review_result': None, 'evidence_confidence': None,
            'required_inputs': [], 'trigger': [], 'handoff': {'required': False}}


def focused(db, checks=None, old_db=None, old_plan=None, complete=True, intent=None):
    checks = deepcopy(checks if checks is not None else [check('CHECK-R1', 'R1'), check('CHECK-R10', 'R10')])
    intent = deepcopy(intent or {})
    declarations = intent.setdefault('review_dependencies', {})
    if complete:
        for item in checks:
            declarations.setdefault(item['id'], {'complete': True, 'citation': 'Synthetic isolated scope reviewed'})
            declarations[item['id']].update(db_digest=digest(db), check_digest=digest(check_spec(item)))
    plan = {'schema_version': 1, 'db_sha256': db_fingerprint(db), 'checks': checks,
            'review_mode': 'revision' if old_db is not None or old_plan is not None else 'first'}
    return attach_metadata(plan, db, intent, old_db=old_db, old_plan=old_plan)


def entries(plan):
    return {e['check_id']: e for e in plan['revision_impact']['entries']}


def reviewed_records(plan, report):
    # Explicitly stipulated records test the binding mechanics, not engineering
    # truth. Their method/evidence never claims that an actual board was tested.
    impact = plan['revision_impact']
    report.update(revision_impact_version=1, revision_digest=impact['digest'])
    for row in report['checks']:
        entry = entries(plan)[row['id']]
        if entry['required']:
            row['reverification'] = {'revision_digest': impact['digest'],
                'inputs_digest': plan['review_inputs']['digest'], 'check_digest': entry['check_digest'],
                'method': 'Record present synthetic input and remaining evidence gaps', 'evidence': deepcopy(E)}
    return report


class RevisionRoutingTests(unittest.TestCase):
    def setUp(self):
        self.old = database()
        self.base = focused(self.old)
        self.new = deepcopy(self.old)

    def build(self, **kwargs):
        return focused(self.new, old_db=self.old, old_plan=self.base, **kwargs)

    def test_exact_ref_not_substring_and_small_change_retained(self):
        self.new['parts']['R1']['value'] = '10.00001K/1%'
        plan = self.build()
        self.assertEqual(plan['revision_impact']['strategy'], 'EXACT_DEPENDENCIES')
        self.assertTrue(entries(plan)['CHECK-R1']['required'])
        self.assertFalse(entries(plan)['CHECK-R10']['required'])
        self.assertEqual(entries(plan)['CHECK-R10']['change_ids'], [])
        event = next(x for x in plan['revision_impact']['changes'] if x['kind'] == 'db.parts')
        self.assertEqual(event['new']['value'], '10.00001K/1%')
        self.assertEqual(event['refs'], ['R1'])

    def test_same_inputs_produce_no_recorded_change_not_pass(self):
        plan = self.build()
        self.assertEqual(entries(plan)['CHECK-R1']['status'], 'NO_RECORDED_CHANGE')
        self.assertTrue(all(c['review_result'] is None for c in plan['checks']))

    def test_part_population_change_rechecks_dependents(self):
        self.new['parts']['R1']['nc'] = True
        self.assertTrue(entries(self.build())['CHECK-R1']['required'])

    def test_pin_move_uses_both_sides(self):
        self.new['nets']['R1_A'].remove('R1.1')
        self.new['nets']['R10_A'].append('R1.1')
        self.new['pin2net']['R1.1'] = 'R10_A'
        self.assertTrue(all(entries(self.build())[k]['required'] for k in ('CHECK-R1', 'CHECK-R10')))

    def test_symbol_only_pin_change_retained(self):
        self.new['declared_pinname'] = {'R1.9': 'EP'}
        self.assertTrue(any(x['kind'] == 'db.declared_pinname' for x in self.build()['revision_impact']['changes']))

    def test_pin_function_type_and_extra_part_metadata(self):
        for field, key, value in [('pinname', 'R1.1', 'SENSE'), ('pintype', 'R1.1', 'IN')]:
            with self.subTest(field=field):
                self.new = deepcopy(self.old)
                self.new[field][key] = value
                self.assertTrue(entries(self.build())['CHECK-R1']['required'])
        self.new['parts']['R1']['manufacturer'] = 'SYNTHETIC-B'
        self.assertTrue(any(x['kind'] == 'db.parts' for x in self.build()['revision_impact']['changes']))

    def test_export_error_and_pseudo_net_change_require_full_review(self):
        for field, value in [('export_errors', ['synthetic interrupted export']), ('pseudo_nets', ['R1_A'])]:
            with self.subTest(field=field):
                self.new = deepcopy(self.old)
                self.new[field] = value
                self.assertEqual(self.build()['revision_impact']['strategy'], 'FULL_REVIEW')

    def test_incomplete_scope_falls_back_to_all_checks(self):
        self.base = focused(self.old, complete=False)
        self.new['parts']['R1']['value'] = '11K/1%'
        plan = self.build()
        self.assertEqual(plan['revision_impact']['strategy'], 'FULL_REVIEW')
        self.assertTrue(all(e['required'] for e in entries(plan).values()))
        self.assertTrue(plan['revision_impact']['partial_dependencies'])

    def test_stale_scope_binding_cannot_exclude_other_checks(self):
        self.new['parts']['R1']['value'] = '11K/1%'
        stale = deepcopy(self.base['review_inputs']['intent'])
        plan = self.build(intent=stale, complete=False)
        self.assertIn('stale-dependency-scope-declaration', plan['check_dependencies']['CHECK-R10']['gaps'])
        self.assertTrue(entries(plan)['CHECK-R10']['required'])

    def test_new_criterion_and_state_trigger_reverification(self):
        for field, value in [('criterion', 'A new exact criterion'), ('object', {'ref': 'R1', 'state': 'startup'})]:
            with self.subTest(field=field):
                checks = [check('CHECK-R1', 'R1'), check('CHECK-R10', 'R10')]
                checks[0][field] = value
                self.assertIn('check-definition-changed', entries(self.build(checks=checks))['CHECK-R1']['reasons'])

    def test_intent_only_change_is_visible(self):
        plan = self.build(intent={'power_rails': {'CUSTOM': {'load_a': {'min': 0, 'max': 0.4}}}})
        self.assertEqual(plan['revision_impact']['structural_diff']['summary']['parts_changed'], 0)
        self.assertTrue(any(c['kind'] == 'intent' for c in plan['revision_impact']['changes']))
        self.assertEqual(plan['revision_impact']['strategy'], 'FULL_REVIEW')

    def test_explicit_check_dependencies_propagate_exactly_through_cycle(self):
        checks = [check(k, ref) for k, ref in [('A', 'R1'), ('B', 'R10'), ('C', 'R10')]]
        intent = {'review_dependencies': {k: {'complete': True, 'citation': 'Synthetic dependency',
                  'check_ids': other} for k, other in [('A', ['C']), ('B', ['A']), ('C', ['B'])]}}
        self.base = focused(self.old, checks, intent=intent)
        self.new['parts']['R1']['value'] = '11K/1%'
        plan = self.build(checks=checks, intent=intent)
        self.assertIn('dependent-check:A', entries(plan)['B']['reasons'])
        self.assertIn('dependent-check:B', entries(plan)['C']['reasons'])

    def test_unknown_dependency_and_unmatched_check_declaration_remain_gaps(self):
        intent = {'review_dependencies': {'TYPO': {'complete': False, 'citation': 'Synthetic typo'},
            'CHECK-R1': {'complete': True, 'citation': 'Synthetic bad target', 'check_ids': ['ABSENT']}}}
        self.new['parts']['R1']['value'] = '11K/1%'
        plan = self.build(intent=intent)
        self.assertIn('unknown-or-self-check:ABSENT', plan['check_dependencies']['CHECK-R1']['gaps'])
        self.assertIn('unmatched-dependency-declaration:TYPO', plan['revision_impact']['blocking_gaps'])

    def test_removed_check_requires_disposition_not_automatic_resolution(self):
        plan = self.build(checks=[check('CHECK-R10', 'R10')])
        removed = next(c for c in plan['checks'] if c['check'] == 'revision-removed-check')
        self.assertEqual(removed['prior_check'], check_spec(self.base['checks'][0]))
        self.assertIsNone(removed['review_result'])
        self.assertTrue(entries(plan)[removed['id']]['required'])
        later = focused(self.new, [check('CHECK-R10', 'R10')], old_db=self.new, old_plan=plan)
        self.assertIn(removed['id'], entries(later))

    def test_new_check_is_required_without_db_change(self):
        plan = self.build(checks=[check('CHECK-R1', 'R1'), check('CHECK-R10', 'R10'), check('NEW', 'R1')])
        self.assertIn('new-check', entries(plan)['NEW']['reasons'])

    def test_legacy_baseline_without_context_is_not_invented(self):
        self.base = {k: v for k, v in self.base.items() if k not in
                     ('dependency_version', 'check_dependencies', 'review_inputs', 'dependency_unmatched')}
        plan = self.build()
        self.assertIn('legacy-old-plan-has-no-input-snapshot', plan['revision_impact']['blocking_gaps'])
        self.assertEqual(plan['revision_impact']['strategy'], 'FULL_REVIEW')

    def test_wrong_old_db_and_tampered_old_dependencies_rejected(self):
        wrong = deepcopy(self.old)
        wrong['parts']['R1']['value'] = 'OTHER'
        with self.assertRaisesRegex(ValueError, 'old plan'):
            focused(self.new, old_db=wrong, old_plan=self.base)
        self.base['check_dependencies']['CHECK-R1']['refs'] = []
        with self.assertRaisesRegex(ValueError, 'dependency catalog'):
            self.build()

    def test_no_input_mutation_and_deterministic_repeat(self):
        old, new, base = deepcopy(self.old), deepcopy(self.new), deepcopy(self.base)
        first = self.build()
        self.assertEqual(first, self.build())
        self.assertEqual((self.old, self.new, self.base), (old, new, base))

    def test_metadata_tampering_and_missing_baseline_are_rejected(self):
        plan = self.build()
        self.assertEqual(validate_metadata(plan, self.new, self.old, self.base)[0], [])
        self.assertTrue(validate_metadata(plan, self.new)[0])
        for key in ('revision_impact', 'check_dependencies', 'review_inputs'):
            bad = deepcopy(plan)
            bad[key] = {}
            self.assertTrue(validate_metadata(bad, self.new, self.old, self.base)[0])

    def test_deleted_impact_entry_and_disposition_check_are_rejected(self):
        plan = self.build(checks=[check('CHECK-R10', 'R10')])
        bad = deepcopy(plan)
        bad['revision_impact']['entries'].pop()
        self.assertTrue(validate_metadata(bad, self.new, self.old, self.base)[0])
        plan['checks'] = [c for c in plan['checks'] if c['check'] != 'revision-removed-check']
        self.assertTrue(validate_metadata(plan, self.new, self.old, self.base)[0])


class RevisionSourceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.db = database()
        self.source = self.root/'synthetic-spec.txt'
        self.source.write_text('SYNTHETIC LIMIT A')
        self.intent = {'review_sources': [{'id': 'REQ', 'path': str(self.source),
                       'citation': 'Synthetic requirements document', 'refs': ['R1']}]}
        self.base = focused(self.db, intent=self.intent)

    def test_source_bytes_change_without_netlist_or_filename_change(self):
        stat = self.source.stat()
        self.source.write_text('SYNTHETIC LIMIT B')
        os.utime(self.source, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        plan = focused(self.db, intent=self.intent, old_db=self.db, old_plan=self.base)
        self.assertTrue(entries(plan)['CHECK-R1']['required'])
        self.assertFalse(entries(plan)['CHECK-R10']['required'])
        self.assertTrue(any(c['kind'] == 'document' for c in plan['revision_impact']['changes']))

    def test_document_disappears_and_old_snapshot_is_preserved(self):
        self.source.unlink()
        plan = focused(self.db, intent=self.intent, old_db=self.db, old_plan=self.base)
        self.assertTrue(plan['revision_impact']['blocking_gaps'])
        self.assertEqual(self.base['review_inputs']['documents'][0]['status'], 'READABLE')

    def test_live_document_change_after_plan_invalidates_validation(self):
        plan = focused(self.db, intent=self.intent, old_db=self.db, old_plan=self.base)
        self.source.write_text('SYNTHETIC LIMIT B')
        self.assertTrue(validate_metadata(plan, self.db, self.db, self.base)[0])

    def test_source_manifest_and_declaration_validation(self):
        for intent in ({'review_sources': [{}]}, {'review_sources': self.intent['review_sources'] * 2},
                       {'review_dependencies': {'X': {'complete': 1, 'citation': 'test'}}},
                       {'review_dependencies': {'X': {'complete': False, 'citation': 'test', 'refs': ['R1', 'R1']}}},
                       {'review_dependencies': {'X': {'complete': True, 'citation': 'test'}}}):
            with self.subTest(intent=intent):
                self.assertTrue(validate_intent(intent))


class RevisionWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.old = database()
        self.base = build_review_plan(self.old)
        self.new = deepcopy(self.old)
        self.new['parts']['R1']['value'] = '4.7K/1%'
        self.plan = build_review_plan(self.new, old_db=self.old, old_plan=self.base)

    def report(self, plan=None):
        plan = plan or self.plan
        return reviewed_records(plan, pending_report(plan, self.new))

    def validate(self, plan, report, **kwargs):
        return validate_review(plan, report, self.new, old_db=self.old, old_plan=self.base,
                               require_revision=True, **kwargs)

    def test_pending_ledger_valid_but_no_go(self):
        result = self.validate(self.plan, self.report())
        self.assertTrue(result['valid'], result)
        self.assertEqual(result['release'], 'NO_GO')
        self.assertTrue(result['revision_validation']['enforced'])

    def test_fresh_scope_review_can_pass_without_passing_electrical_checks(self):
        report = self.report()
        scope = next(row for row in report['checks'] if row['id'] == COVERAGE_ID)
        scope.update(review_result='PASS', evidence_confidence='B',
                     rationale='Synthetic exercise reviewed the expanded scope; electrical evidence gaps remain below')
        scope.pop('missing_inputs')
        scope.pop('potential_severity')
        result = self.validate(self.plan, report)
        self.assertTrue(result['valid'], result)
        self.assertEqual(result['summary']['results']['PASS'], 1)
        self.assertEqual(result['release'], 'NO_GO')

    def test_missing_impact_check_and_old_review_digest_are_rejected(self):
        report = self.report()
        report['checks'] = [r for r in report['checks'] if r['id'] != COVERAGE_ID]
        self.assertFalse(self.validate(self.plan, report)['valid'])
        report = self.report()
        report['revision_digest'] = 'previous-revision'
        self.assertFalse(self.validate(self.plan, report)['valid'])

    def test_additional_lint_snapshot_prevents_omitting_manual_check(self):
        cold = deepcopy(self.plan)
        cold['checks'].append(check('MANUAL-IN-COLD', 'R1'))
        final = build_review_plan(self.new, old_db=self.old, old_plan=self.base, previous_plan=cold)
        run = {'review_plan': cold, 'findings': []}
        report = self.report(final)
        report['lint_reviews'] = [{'run_digest': digest(run), 'items': {}}]
        result = self.validate(final, report, lint_runs=[run])
        self.assertTrue(result['valid'], result)
        bad = deepcopy(final)
        bad['checks'] = [c for c in bad['checks'] if c['id'] != 'MANUAL-IN-COLD']
        attach_metadata(bad, self.new, old_db=self.old, old_plan=self.base)
        missing = self.report(bad)
        missing['lint_reviews'] = report['lint_reviews']
        self.assertFalse(self.validate(bad, missing, lint_runs=[run])['valid'])

    def test_old_verdict_without_fresh_record_fails_even_after_digest_update(self):
        report = self.report()
        row = next(r for r in report['checks'] if r['id'] != COVERAGE_ID and entries(self.plan)[r['id']]['required'])
        row.update(review_result='PASS', evidence_confidence='A')
        row.pop('reverification')
        self.assertTrue(any('re-verification record required' in e for e in self.validate(self.plan, report)['errors']))

    def test_stale_reverification_binding_and_missing_method_fail(self):
        for key, value in [('revision_digest', 'old'), ('inputs_digest', 'old'), ('check_digest', 'old'), ('method', '')]:
            report = self.report()
            report['checks'][0]['reverification'][key] = value
            self.assertFalse(self.validate(self.plan, report)['valid'])

    def test_revision_mode_cannot_disable_object_bindings(self):
        report = self.report()
        report.pop('binding_version')
        for row in report['checks']:
            row.pop('binding')
        self.assertFalse(self.validate(self.plan, report)['valid'])

    def test_cannot_delete_automatic_check_and_rehash_metadata(self):
        bad = deepcopy(self.plan)
        removed = next(c for c in bad['checks'] if c['check'] == 'physical-pin-inventory') if any(
            c['check'] == 'physical-pin-inventory' for c in bad['checks']) else next(
                c for c in bad['checks'] if c['check'].startswith('feature-'))
        bad['checks'].remove(removed)
        attach_metadata(bad, self.new, old_db=self.old, old_plan=self.base)
        result = self.validate(bad, self.report(bad))
        self.assertTrue(any('automatic check missing or changed' in e for e in result['errors']))

    def test_missing_old_plan_cannot_become_pass_coverage(self):
        plan = build_review_plan(self.new, review_mode='revision', old_db=self.old)
        report = self.report(plan)
        row = next(r for r in report['checks'] if r['id'] == COVERAGE_ID)
        row.update(review_result='PASS', evidence_confidence='A')
        result = validate_review(plan, report, self.new, old_db=self.old)
        self.assertFalse(result['valid'])
        self.assertTrue(any('must remain INSUFFICIENT' in e for e in result['errors']))

    def test_markers_cannot_be_stripped_in_strict_revision_gate(self):
        plan = {k: v for k, v in self.plan.items() if k not in
                ('dependency_version', 'review_inputs', 'check_dependencies', 'revision_impact_version', 'revision_impact')}
        plan['checks'] = [c for c in plan['checks'] if not c['check'].startswith('revision-')]
        self.assertTrue(validate_metadata(plan, self.new, require_revision=True)[0])

    def test_malformed_result_container_returns_invalid_not_exception(self):
        for value in (None, {}, [{'id': []}]):
            report = self.report()
            report['checks'] = value
            self.assertFalse(self.validate(self.plan, report)['valid'])

    def test_malformed_old_indexes_fail_cleanly(self):
        for field in ('parts', 'pin2net', 'nets', 'declared_pinname', 'ref2page'):
            wrong = deepcopy(self.old)
            wrong[field] = []
            with self.assertRaisesRegex(ValueError, 'old_db'):
                build_review_plan(self.new, old_db=wrong, old_plan=self.base)

    def test_same_revision_merge_retains_manual_checks_not_results(self):
        cold = deepcopy(self.plan)
        manual = check('MANUAL', 'R1')
        manual['review_result'] = 'PASS'
        cold['checks'].append(manual)
        final = build_review_plan(self.new, previous_plan=cold, old_db=self.old, old_plan=self.base)
        self.assertIsNone(next(c for c in final['checks'] if c['id'] == 'MANUAL')['review_result'])
        self.assertTrue(entries(final)['MANUAL']['required'])
        with self.assertRaisesRegex(ValueError, 'revision baseline changed'):
            build_review_plan(self.new, previous_plan=cold)

    def test_hot_restored_old_check_keeps_cold_provisional_disposition(self):
        self.base['checks'].append(check('PRIOR-STATE', 'R1'))
        attach_metadata(self.base, self.old)
        cold = build_review_plan(self.new, old_db=self.old, old_plan=self.base)
        restored = deepcopy(cold)
        restored['checks'].append(check('PRIOR-STATE', 'R1'))
        final = build_review_plan(self.new, old_db=self.old, old_plan=self.base, previous_plan=restored)
        self.assertTrue({c['id'] for c in cold['checks']} <= {c['id'] for c in final['checks']})
        self.assertIn('PRIOR-STATE', entries(final))
        run = {'review_plan': cold, 'findings': []}
        report = self.report(final)
        report['lint_reviews'] = [{'run_digest': digest(run), 'items': {}}]
        result = self.validate(final, report, lint_runs=[run])
        self.assertTrue(result['valid'], result)

    def test_i2c_population_and_jumper_context_changes_trigger_reverification(self):
        db, intent = i2c_fixture(['jumper'])
        base = build_review_plan(db, intent)
        for key, value in [('population', {'R1': False}), ('jumpers', {'JP10': 'open'})]:
            with self.subTest(field=key):
                new_intent = deepcopy(intent)
                new_intent['i2c_topology']['states'][0][key].update(value)
                plan = build_review_plan(db, new_intent, old_db=db, old_plan=base)
                self.assertTrue(any(c['kind'] == 'intent' for c in plan['revision_impact']['changes']))
                self.assertEqual(plan['revision_impact']['strategy'], 'FULL_REVIEW')

    def write(self, name, data):
        path = self.root/name
        path.write_text(json.dumps(data))
        return path

    def cli(self, script, *args):
        return subprocess.run([sys.executable, '-B', str(SCRIPTS/script), *map(str, args)],
                              capture_output=True, text=True)

    def test_plan_lint_and_gate_cli_roundtrip(self):
        db, old, base = self.write('new.json', self.new), self.write('old.json', self.old), self.write('old-plan.json', self.base)
        output, impact, lint = self.root/'plan.json', self.root/'impact.json', self.root/'lint.json'
        result = self.cli('lint.py', db, '--old-db', old, '--old-plan', base, '--plan-json', output,
                          '--revision-impact-json', impact, '--json', lint)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        plan = json.loads(output.read_text())
        self.assertEqual(plan['revision_impact'], json.loads(impact.read_text()))
        self.assertEqual(plan, json.loads(lint.read_text())['review_plan'])
        result = self.cli('validate_review.py', output, self.write('report.json', self.report(plan)),
                          '--db', db, '--old-db', old, '--old-plan', base, '--require-revision-impact')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        result = self.cli('plan_review.py', db, '--old-db', old, '--old-plan', base, '--json', self.root/'standalone.json')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads((self.root/'standalone.json').read_text()), plan)

    def test_invalid_old_json_duplicate_keys_and_nonfinite_rejected_before_output(self):
        db = self.write('new.json', self.new)
        old = self.root/'invalid.json'
        for raw in ('{"parts":{},"parts":{}}', '{"value":NaN}', '[]', 'null'):
            old.write_text(raw)
            result = self.cli('plan_review.py', db, '--old-db', old, '--json', self.root/'no-output.json')
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((self.root/'no-output.json').exists())


if __name__ == '__main__':
    unittest.main()
