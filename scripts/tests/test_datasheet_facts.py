"""Synthetic source/condition/identity tests; no real component specifications."""
import copy
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest

SCRIPTS = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))

from datasheet_facts import (check_bindings, materialize_evidence, resolve_vref,
                             validate_facts, vref_binding_gaps)
from electrical_contract import db_fingerprint, document_fingerprint, readiness_gaps, validate_evidence
from electrical_fixtures import bind_evidence
from lint import Lint
from plan_review import build_review_plan
from test_electrical_safety import fb_check
from test_solve_dividers import divider_db


def conditions(low=-40, high=85):
    return {'temperature_c': {'min': low, 'max': high}, 'temperature_basis': 'junction',
            'vin_v': {'min': 3, 'max': 5.5}, 'load_a': {'min': 0, 'max': 1},
            'mode': 'regulating', 'qualifiers': {}}


class DatasheetFactsTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = pathlib.Path(self.directory.name)
        self.db = divider_db()
        self.db['parts']['U1'].update(value='REG-I-QFN', jedec='QFN-16')
        self.request = {'schema_version': 1, 'checks': [fb_check()]}
        self.audit = bind_evidence(self.db, self.request, self.directory.name)
        check = self.request['checks'][0]
        check.pop('vref')
        check['node'] = 'U1.1'
        check['vref_request'] = {'ref': 'U1', 'conditions': conditions()}
        source = check['basis']['sources'][0]
        source.update(mpn='REG-I-QFN', package='QFN-16')
        document = self.audit['materials'][0]['document']
        self.pdf = pathlib.Path(document['path'])
        self.fact = {
            'id': 'REG-I-VREF-FULL', 'parameter': 'vref', 'mpn': 'REG-I-QFN', 'package': 'QFN-16',
            'unit': 'V', 'values': {'min': .792, 'typ': .8, 'max': .808}, 'guaranteed': True,
            'conditions': conditions(), 'raw_conditions': 'Synthetic full-range guarantee only for fixture.',
            'source': {'path': self.pdf.name, 'sha256': document_fingerprint(self.pdf),
                       'document_model': document['document_model'], 'document_version': document['document_version'],
                       'locator': 'Synthetic test definition, Vref table, full-range row', 'footnotes': []},
            'verification': {'status': 'VERIFIED', 'conditions_complete': True,
                             'note': 'Synthetic fixture values explicitly declared by the test, not real specifications.'},
        }
        self.store = {'schema_version': 1, 'facts': [self.fact]}
        self.facts_path = self.root / 'facts.json'
        self.write_store()

    def write_store(self):
        self.facts_path.write_text(json.dumps(self.store), encoding='utf-8')

    def materialize(self):
        self.write_store()
        return materialize_evidence(self.db, self.request, self.audit, str(self.facts_path))

    def result(self, evidence, db=None, audit=None):
        self.assertEqual(validate_evidence(evidence), [])
        lint = Lint(db or self.db, evidence=evidence, datasheet_audit=audit or self.audit)
        lint.run()
        return lint.results[0]

    def assert_unresolved(self):
        output, report = self.materialize()
        self.assertEqual(report['affected_check_ids'], ['FB'])
        self.assertNotIn('vref', output['checks'][0])
        self.assertNotIn('vref_binding', output['checks'][0])
        self.assertEqual(self.result(output)['review_result'], 'INSUFFICIENT')
        return report

    def test_roundtrip_preserves_sources_and_legacy_numeric_result(self):
        before = copy.deepcopy(self.request)
        output, report = self.materialize()
        self.assertEqual(self.request, before)
        self.assertEqual(report['affected_check_ids'], [])
        check = output['checks'][0]
        self.assertEqual(check['vref'], self.fact['values'])
        self.assertEqual(vref_binding_gaps(self.db, check, self.audit), [])
        self.assertEqual(check['basis']['sources'][0]['locator'], before['checks'][0]['basis']['sources'][0]['locator'])
        self.assertEqual(check['basis']['sources'][0]['parameter_locators']['vref'], self.fact['source']['locator'])
        legacy = copy.deepcopy(before)
        legacy['checks'][0].pop('vref_request')
        legacy['checks'][0]['vref'] = copy.deepcopy(self.fact['values'])
        actual = self.result(output)
        expected = self.result(legacy)
        self.assertEqual(actual['review_result'], 'PASS')
        self.assertEqual(actual['calculation'], expected['calculation'])
        self.assertEqual(actual['vref_binding']['fact_id'], self.fact['id'])

    def test_request_alone_never_uses_manually_leftover_vref(self):
        self.request['checks'][0]['vref'] = copy.deepcopy(self.fact['values'])
        self.assertEqual(self.result(self.request)['review_result'], 'INSUFFICIENT')

    def test_legacy_manual_evidence_keeps_existing_metadata_compatibility(self):
        legacy = copy.deepcopy(self.request)
        check = legacy['checks'][0]
        check.pop('vref_request')
        check['vref'] = copy.deepcopy(self.fact['values'])
        check['basis']['sources'][0].update(mpn=None, package=None, parameter_locators=[])
        self.assertEqual(self.result(legacy)['review_result'], 'PASS')

    def test_reuses_one_fact_across_two_checks_with_separate_contexts(self):
        second = copy.deepcopy(self.request['checks'][0])
        second['id'] = 'FB-OTHER-STATE'
        second['basis']['state'] = 'Second synthetic assembly state, same operating bounds'
        self.request['checks'].append(second)
        output, report = self.materialize()
        self.assertEqual(report['affected_check_ids'], [])
        a, b = output['checks']
        self.assertEqual(a['vref'], b['vref'])
        self.assertEqual(a['vref_binding']['fact_id'], b['vref_binding']['fact_id'])
        self.assertNotEqual(a['vref_binding']['context_sha256'], b['vref_binding']['context_sha256'])

    def test_room_temperature_first_does_not_hide_full_range_row(self):
        room = copy.deepcopy(self.fact)
        room.update(id='ROOM', values={'min': .799, 'typ': .8, 'max': .801}, conditions=conditions(25, 25))
        self.store['facts'].insert(0, room)
        output, report = self.materialize()
        self.assertFalse(report['affected_check_ids'])
        self.assertEqual(output['checks'][0]['vref_binding']['fact_id'], 'REG-I-VREF-FULL')
        self.assertEqual(output['checks'][0]['vref']['min'], .792)

    def test_room_temperature_only_is_not_full_range_guarantee(self):
        self.fact['conditions'] = conditions(25, 25)
        self.assert_unresolved()

    def test_typical_only_is_not_a_guarantee(self):
        self.fact['values'] = {'typ': .8, 'min': None, 'max': None}
        self.fact['guaranteed'] = False
        self.assert_unresolved()

    def test_min_max_without_guarantee_is_not_usable(self):
        self.fact['guaranteed'] = False
        self.assert_unresolved()

    def test_unverified_or_incomplete_footnotes_are_not_usable(self):
        for change in ({'status': 'UNVERIFIED'}, {'conditions_complete': False}):
            with self.subTest(change=change):
                old = copy.deepcopy(self.fact['verification'])
                self.fact['verification'].update(change)
                self.assert_unresolved()
                self.fact['verification'] = old

    def test_wrong_suffix_and_package_never_match(self):
        for field, value in (('mpn', 'REG-C-QFN'), ('package', 'SOIC-8')):
            with self.subTest(field=field):
                old = self.fact[field]
                self.fact[field] = value
                self.assert_unresolved()
                self.fact[field] = old

    def test_exact_identity_is_case_sensitive(self):
        self.fact['mpn'] = 'reg-i-qfn'
        self.assert_unresolved()

    def test_missing_explicit_identity_does_not_guess_from_title(self):
        self.request['checks'][0]['basis']['sources'][0].pop('package')
        self.assert_unresolved()

    def test_copied_fact_identity_cannot_override_actual_bom(self):
        source = self.request['checks'][0]['basis']['sources'][0]
        for field, value in (('mpn', 'REG-C-QFN'), ('package', 'SOIC-8')):
            with self.subTest(field=field):
                old = source[field]
                source[field] = self.fact[field] = value
                self.assert_unresolved()
                source[field] = self.fact[field] = old

    def test_different_version_or_pdf_is_not_reused(self):
        for field, value in (('document_version', 'OTHER'), ('document_model', 'OTHER'), ('sha256', 'a' * 64)):
            with self.subTest(field=field):
                old = self.fact['source'][field]
                self.fact['source'][field] = value
                self.assert_unresolved()
                self.fact['source'][field] = old

    def test_missing_request_dimension_and_unknown_condition_are_gaps(self):
        request = self.request['checks'][0]['vref_request']['conditions']
        for dimension in ('temperature_c', 'vin_v', 'load_a', 'temperature_basis', 'mode', 'qualifiers'):
            with self.subTest(dimension=dimension):
                old = request.pop(dimension)
                self.assert_unresolved()
                request[dimension] = old
        request['temperature_typo'] = 25
        self.assert_unresolved()

    def test_temperature_kind_mode_and_qualifiers_are_not_interchangeable(self):
        for key, value in (('temperature_basis', 'ambient'), ('mode', 'startup'), ('qualifiers', {'frequency': '1MHz'})):
            with self.subTest(key=key):
                old = self.fact['conditions'][key]
                self.fact['conditions'][key] = value
                self.assert_unresolved()
                self.fact['conditions'][key] = old

    def test_equal_qualifiers_and_inclusive_subranges_match(self):
        request = self.request['checks'][0]['vref_request']['conditions']
        request['vin_v'] = {'min': 3.1, 'max': 5.5}
        request['load_a'] = {'min': 0, 'max': .7}
        request['qualifiers'] = self.fact['conditions']['qualifiers'] = {'test_setup': 'closed-loop'}
        output, report = self.materialize()
        self.assertFalse(report['affected_check_ids'])
        self.assertEqual(self.result(output)['review_result'], 'PASS')

    def test_input_and_load_extrapolation_rejected(self):
        request = self.request['checks'][0]['vref_request']['conditions']
        for key, value in (('vin_v', {'min': 2.9, 'max': 5}), ('load_a', {'min': 0, 'max': 1.01})):
            with self.subTest(key=key):
                old = request[key]
                request[key] = value
                self.assert_unresolved()
                request[key] = old

    def test_two_applicable_rows_are_ambiguous_in_any_order(self):
        second = copy.deepcopy(self.fact)
        second.update(id='CONFLICT', values={'min': .78, 'typ': .8, 'max': .82})
        self.store['facts'].append(second)
        for _ in range(2):
            report = self.assert_unresolved()
            self.assertIn('CONFLICT', ' '.join(report['checks'][0]['gaps']))
            self.store['facts'].reverse()

    def test_disjoint_rows_are_not_stitched_into_full_range(self):
        second = copy.deepcopy(self.fact)
        self.fact['conditions'] = conditions(-40, 25)
        second.update(id='HIGH', conditions=conditions(25, 85))
        self.store['facts'].append(second)
        self.assert_unresolved()

    def test_pdf_change_marks_every_consumer_and_blocks_existing_evidence(self):
        self.request['checks'].append(copy.deepcopy(self.request['checks'][0]))
        self.request['checks'][1]['id'] = 'SECOND'
        output, _ = self.materialize()
        self.pdf.write_bytes(self.pdf.read_bytes() + b'changed')
        report = check_bindings(self.db, output, self.audit)
        self.assertEqual(report['affected_check_ids'], ['FB', 'SECOND'])
        self.assertEqual(self.result(output)['review_result'], 'INSUFFICIENT')

    def test_missing_fact_file_or_pdf_never_falls_back(self):
        for target in (self.facts_path, self.pdf):
            with self.subTest(target=target):
                output, _ = self.materialize()
                data = target.read_bytes()
                target.unlink()
                self.assertEqual(self.result(output)['review_result'], 'INSUFFICIENT')
                target.write_bytes(data)

    def test_fact_correction_requires_explicit_rematerialization(self):
        output, _ = self.materialize()
        self.fact['values']['max'] = .81
        self.write_store()
        self.assertEqual(self.result(output)['review_result'], 'INSUFFICIENT')
        updated, _ = self.materialize()
        self.assertEqual(updated['checks'][0]['vref']['max'], .81)
        self.assertEqual(self.result(updated)['review_result'], 'PASS')

    def test_new_conflicting_record_invalidates_previously_bound_fact(self):
        output, _ = self.materialize()
        conflict = copy.deepcopy(self.fact)
        conflict['id'] = 'NEW-CONFLICT'
        self.store['facts'].append(conflict)
        self.write_store()
        self.assertEqual(self.result(output)['review_result'], 'INSUFFICIENT')

    def test_unrelated_valid_fact_does_not_invalidate_selected_record(self):
        output, _ = self.materialize()
        unrelated = copy.deepcopy(self.fact)
        unrelated.update(id='UNRELATED', mpn='OTHER')
        self.store['facts'].append(unrelated)
        self.write_store()
        self.assertEqual(self.result(output)['review_result'], 'PASS')

    def test_source_change_with_same_stat_is_detected(self):
        output, _ = self.materialize()
        previous_stat = self.pdf.stat()
        self.pdf.write_bytes(b'X' * previous_stat.st_size)
        os.utime(self.pdf, ns=(previous_stat.st_atime_ns, previous_stat.st_mtime_ns))
        self.assertEqual(self.result(output)['review_result'], 'INSUFFICIENT')

    def test_changed_state_or_requested_conditions_requires_rematerialization(self):
        output, _ = self.materialize()
        for changed in ('state', 'condition', 'id', 'source'):
            with self.subTest(changed=changed):
                other = copy.deepcopy(output)
                check = other['checks'][0]
                if changed == 'state':
                    check['basis']['state'] = 'different assembly'
                elif changed == 'condition':
                    check['vref_request']['conditions']['temperature_c']['min'] = 0
                elif changed == 'source':
                    check['basis']['sources'][0]['identity_resolution'] += ' changed'
                else:
                    check['id'] = 'DIFFERENT'
                self.assertEqual(self.result(other)['review_result'], 'INSUFFICIENT')

    def test_wrong_target_and_populated_option_not_accepted(self):
        self.request['checks'][0]['vref_request']['ref'] = 'R1'
        self.assert_unresolved()
        self.request['checks'][0]['vref_request']['ref'] = 'U1'
        self.db['parts']['U1']['nc'] = True
        self.assert_unresolved()

    def test_wrong_physical_pin_is_rejected_before_materialization(self):
        self.db['pin2net']['U1.2'] = 'GND'
        self.db['nets']['GND'].append('U1.2')
        check = self.request['checks'][0]
        check['basis']['db_sha256'] = db_fingerprint(self.db)
        check['node'] = 'U1.2'
        report = self.assert_unresolved()
        self.assertIn('node/net', ' '.join(report['checks'][0]['gaps']))

    def test_current_bom_snapshot_required_even_after_db_hash_refresh(self):
        self.db['parts']['U1']['jedec'] = 'OTHER'
        self.request['checks'][0]['basis']['db_sha256'] = db_fingerprint(self.db)
        report = self.assert_unresolved()
        self.assertIn('快照', ' '.join(report['checks'][0]['gaps']))

    def test_original_dependency_and_model_gates_remain(self):
        output, _ = self.materialize()
        for change in ('bias', 'db', 'audit', 'source'):
            with self.subTest(change=change):
                evidence, audit = copy.deepcopy(output), copy.deepcopy(self.audit)
                check = evidence['checks'][0]
                if change == 'bias':
                    check['divider_model'].pop('bias_current_a')
                elif change == 'db':
                    check['basis']['db_sha256'] = 'stale'
                elif change == 'audit':
                    audit['materials'][0]['status'] = 'MISSING'
                else:
                    check['basis']['sources'][0]['identity'] = 'OTHER'
                self.assertEqual(self.result(evidence, audit=audit)['review_result'], 'INSUFFICIENT')

    def test_manual_vref_edit_and_removed_request_do_not_bypass_binding(self):
        output, _ = self.materialize()
        output['checks'][0]['vref']['max'] = .8
        self.assertEqual(self.result(output)['review_result'], 'INSUFFICIENT')
        output['checks'][0].pop('vref_request')
        self.assertTrue(validate_evidence(output))

    def test_failure_clears_previously_materialized_values(self):
        output, _ = self.materialize()
        self.request = output
        self.fact['guaranteed'] = False
        self.assert_unresolved()

    def test_planner_and_lint_share_live_readiness(self):
        output, _ = self.materialize()
        for stale in (False, True):
            with self.subTest(stale=stale):
                if stale:
                    self.pdf.write_bytes(self.pdf.read_bytes() + b'new revision')
                plan = build_review_plan(self.db, evidence=output, datasheet_audit=self.audit)
                items = [x for x in plan['checks'] if x.get('evidence_check_id') == 'FB']
                self.assertTrue(items)
                self.assertTrue(all(x['readiness'] == ('WAITING_EVIDENCE' if stale else 'READY') for x in items))
                self.assertEqual(self.result(output)['review_result'], 'INSUFFICIENT' if stale else 'PASS')

    def test_values_units_schema_and_duplicate_ids_are_checked(self):
        cases = [dict(unit='mV'), dict(values={'min': 0, 'typ': .8, 'max': .81}),
                 dict(values={'min': .79, 'typ': True, 'max': .81}),
                 dict(values={'min': .79, 'typ': float('nan'), 'max': .81}),
                 dict(values={'min': .79, 'typ': .8, 'max': float('inf')}),
                 dict(values={'min': .9, 'typ': .8, 'max': .81})]
        for case in cases:
            with self.subTest(case=case):
                store = copy.deepcopy(self.store)
                store['facts'][0].update(case)
                self.assertTrue(validate_facts(store))
        self.store['facts'].append(copy.deepcopy(self.fact))
        self.assertTrue(validate_facts(self.store))
        self.store['schema_version'] = True
        self.assertTrue(validate_facts(self.store))

    def test_malformed_fact_file_returns_gap_not_exception(self):
        for data in ('null', '{', '{"schema_version":1,"schema_version":1,"facts":[]}'):
            with self.subTest(data=data):
                self.facts_path.write_text(data, encoding='utf-8')
                fact, gaps = resolve_vref(self.db, self.request['checks'][0], self.audit, str(self.facts_path))
                self.assertIsNone(fact)
                self.assertTrue(gaps)

    def test_relative_pdf_path_is_resolved_against_fact_file_not_cwd(self):
        output, _ = self.materialize()
        self.assertTrue(pathlib.Path(output['checks'][0]['vref_binding']['facts_path']).is_absolute())
        self.assertEqual(readiness_gaps(self.db, output['checks'][0], self.audit), [])

    def cli(self, action, evidence_path, report, *extra):
        for filename, value in (('db.json', self.db), ('audit.json', self.audit)):
            (self.root / filename).write_text(json.dumps(value), encoding='utf-8')
        return subprocess.run([sys.executable, '-B', str(SCRIPTS / 'datasheet_facts.py'), action,
                               str(self.root / 'db.json'), '--datasheet-audit', str(self.root / 'audit.json'),
                               '--evidence', str(evidence_path), '--report', str(report), *extra],
                              capture_output=True, text=True, cwd=self.root)

    def test_cli_materialization_and_stale_impact(self):
        source, output = self.root / 'request.json', self.root / 'evidence.json'
        source.write_text(json.dumps(self.request), encoding='utf-8')
        result = self.cli('materialize', source, self.root / 'materialize.json',
                          '--facts', str(self.facts_path), '--out', str(output))
        self.assertEqual(result.returncode, 0, result.stderr)
        stored = json.loads(output.read_text())
        self.assertEqual(self.result(stored)['review_result'], 'PASS')
        self.pdf.write_bytes(self.pdf.read_bytes() + b'new revision')
        result = self.cli('check', output, self.root / 'impact.json')
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(json.loads(result.stdout)['affected_check_ids'], ['FB'])

    def test_cli_unresolved_exits_two_but_retains_insufficient_evidence(self):
        source, output = self.root / 'request.json', self.root / 'unresolved.json'
        source.write_text(json.dumps(self.request), encoding='utf-8')
        self.fact['values'] = {'typ': .8}
        self.write_store()
        result = self.cli('materialize', source, self.root / 'gaps.json',
                          '--facts', str(self.facts_path), '--out', str(output))
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(self.result(json.loads(output.read_text()))['review_result'], 'INSUFFICIENT')

    def test_cli_never_overwrites_inputs_or_existing_evidence(self):
        source = self.root / 'request.json'
        source.write_text(json.dumps(self.request), encoding='utf-8')
        before = source.read_bytes()
        result = self.cli('materialize', source, self.root / 'report.json',
                          '--facts', str(self.facts_path), '--out', str(source))
        self.assertEqual(result.returncode, 2)
        self.assertEqual(source.read_bytes(), before)
        self.assertFalse((self.root / 'report.json').exists())

    def workflow_cli(self, entry, source, prefix):
        artifact = self.root / (prefix + '-' + entry + '.json')
        if entry in ('materialize', 'check'):
            extra = ['--facts', str(self.facts_path), '--out', str(artifact)] if entry == 'materialize' else []
            process = self.cli(entry, source, self.root / (prefix + '-' + entry + '-report.json'), *extra)
        else:
            for filename, value in (('db.json', self.db), ('audit.json', self.audit)):
                (self.root / filename).write_text(json.dumps(value), encoding='utf-8')
            process = subprocess.run([sys.executable, '-B', str(SCRIPTS / (entry + '.py')),
                                      str(self.root / 'db.json'), '--evidence', str(source),
                                      '--datasheet-audit', str(self.root / 'audit.json'), '--json', str(artifact)],
                                     capture_output=True, text=True, cwd=self.root)
        self.assertNotIn('Traceback', process.stderr)
        return process, artifact

    def test_duplicate_raw_condition_is_rejected_by_every_entry(self):
        output, _ = self.materialize()
        raw = json.dumps(output)
        original = '"temperature_c": {"min": -40, "max": 85}'
        self.assertEqual(raw.count(original), 1)
        raw = raw.replace(original, '"temperature_c": {"min": -40, "max": 86, "max": 85}')
        source = self.root / 'duplicate.json'
        source.write_text(raw, encoding='utf-8')
        for entry in ('materialize', 'check', 'plan_review', 'lint'):
            with self.subTest(entry=entry):
                result, artifact = self.workflow_cli(entry, source, 'duplicate')
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn('JSON 重复字段', result.stderr)
                self.assertFalse(artifact.exists())
        # The earlier audit must not silently normalize ambiguity either.
        result = subprocess.run([sys.executable, '-B', str(SCRIPTS / 'audit_datasheets.py'),
                                 str(self.root / 'db.json'), '--evidence', str(source),
                                 '--json', str(self.root / 'bad-audit.json')], capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn('JSON 重复字段', result.stderr)
        self.assertNotIn('Traceback', result.stderr)

    def test_huge_fact_integer_is_a_gap_in_every_entry_not_a_crash(self):
        output, _ = self.materialize()
        source = self.root / 'bound.json'
        source.write_text(json.dumps(output), encoding='utf-8')
        self.fact['conditions']['temperature_c']['max'] = 10 ** 400
        self.write_store()
        for entry in ('materialize', 'check', 'plan_review', 'lint'):
            with self.subTest(entry=entry):
                result, artifact = self.workflow_cli(entry, source, 'huge')
                if entry in ('materialize', 'check'):
                    self.assertEqual(result.returncode, 2, result.stderr)
                    self.assertEqual(json.loads(result.stdout)['affected_check_ids'], ['FB'])
                else:
                    self.assertEqual(result.returncode, 0, result.stderr)
                    report = json.loads(artifact.read_text())
                    if entry == 'lint':
                        self.assertEqual(report['check_results'][0]['review_result'], 'INSUFFICIENT')
                    else:
                        self.assertTrue(all(x['readiness'] == 'WAITING_EVIDENCE' for x in report['checks']
                                            if x.get('evidence_check_id') == 'FB'))


if __name__ == '__main__':
    unittest.main()
