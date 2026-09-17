import copy
import json
import math
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from audit_datasheets import build_datasheet_audit, validate_datasheet_audit
from electrical_contract import db_fingerprint, readiness_gaps
from electrical_fixtures import bind_evidence, divider_model, pin_analysis
from lint import Lint, validate_evidence
from plan_review import build_review_plan, validate_intent
from solve_dividers import Solver, divider_window, parse_resistor
from test_solve_dividers import divider_db


def database(nets, values, pins=None):
    return {'nets': nets, 'parts': {ref: {'value': val, 'nc': False} for ref, val in values.items()},
            'pin2net': {node: net for net, nodes in nets.items() for node in nodes},
            'pinname': pins or {}, 'pintype': {}, 'ref2page': {}, 'pseudo_nets': []}


def pull_db(value='1K/1%'):
    return database({'SDA': ['U1.1', 'R1.1', 'R2.1'],
                     'VCC_3V3': ['R1.2', 'R2.2']},
                    {'U1': 'SYNTHETIC-IO', 'R1': value, 'R2': value}, {'U1.1': 'SDA'})


def pull_check():
    return {'id': 'PULL', 'rule': 'Rule-09', 'kind': 'required_pull', 'net': 'SDA',
            'direction': 'up', 'to': 'VCC_3V3', 'resistance_ohm': {'min': 1000, 'max': 4700},
            'citation': 'Synthetic bus driver sink/rise requirement'}


def fb_check():
    return {'id': 'FB', 'rule': 'Rule-08', 'kind': 'divider', 'net': 'FB_NET',
            'vref': {'min': .792, 'typ': .8, 'max': .808},
            'expected': {'min': 2.3, 'max': 2.5}, 'divider_model': divider_model(),
            'citation': 'Synthetic FB reference/load guarantee specification'}


class ElectricalSafetyTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)

    def run_check(self, db, check, audit=None, bind=True):
        evidence = {'schema_version': 1, 'checks': [check]}
        if bind:
            audit = bind_evidence(db, evidence, self.directory.name)
        self.assertEqual(validate_evidence(evidence), [])
        lint = Lint(db, evidence=evidence, datasheet_audit=audit)
        lint.run()
        self.assertEqual(len(lint.results), 1)
        return lint.results[0], audit

    def test_p01_tvs_shunt_does_not_power_a_load(self):
        db = database({'VCC_3V3': ['U1.1', 'D1.1', 'C1.1'],
                       'GND': ['U1.2', 'D1.2', 'C1.2']},
                      {'U1': 'LOAD', 'D1': 'SMBJ5.0A', 'C1': '100nF'},
                      {'U1.1': 'VDD', 'U1.2': 'VSS'})
        lint = Lint(db)
        self.assertFalse(lint.driven('VCC_3V3'))
        self.assertTrue(any(x['rule'] in ('Rule-04', 'Rule-05') for x in lint.run()))

    def test_source_path_through_fuse_ferrite_and_zero_ohm(self):
        db = database({'SOURCE': ['U1.1', 'F1.1'], 'MID1': ['F1.2', 'FB1.1'],
                       'MID2': ['FB1.2', 'R1.1'], 'VCC_3V3': ['R1.2', 'U2.1']},
                      {'U1': 'REG', 'U2': 'LOAD', 'F1': 'FUSE', 'FB1': 'FERRITE', 'R1': '0R'},
                      {'U1.1': 'VOUT', 'U2.1': 'VDD'})
        self.assertEqual(Lint(db).power_path('VCC_3V3')['path'], ['F1', 'FB1', 'R1'])
        db['parts']['R1']['nc'] = True
        self.assertFalse(Lint(db).driven('VCC_3V3'))

    def test_connector_is_only_source_in_declared_state(self):
        db = database({'VBUS': ['J1.1', 'U1.1']}, {'J1': 'CONN', 'U1': 'LOAD'}, {'U1.1': 'VDD'})
        intent = {'active_state': 'on', 'power_sources': [
            {'node': 'J1.1', 'states': ['on'], 'citation': 'Synthetic external supply spec'}]}
        self.assertFalse(Lint(db).driven('VBUS'))
        self.assertTrue(Lint(db, intent=intent).driven('VBUS'))
        intent['active_state'] = 'off'
        self.assertFalse(Lint(db, intent=intent).driven('VBUS'))

    def test_mos_requires_state_specific_directed_path(self):
        db = database({'VIN': ['J1.1', 'Q1.1'], 'VOUT': ['Q1.2', 'U1.1'], 'GATE': ['Q1.3']},
                      {'J1': 'CONN', 'Q1': 'MOS', 'U1': 'LOAD'}, {'U1.1': 'VDD'})
        intent = {'active_state': 'on', 'power_sources': [
            {'node': 'J1.1', 'states': ['on', 'off'], 'citation': 'source'}],
            'power_paths': [{'ref': 'Q1', 'from': 'VIN', 'to': 'VOUT', 'state': 'on', 'citation': 'gate condition'}]}
        self.assertTrue(Lint(db, intent=intent).driven('VOUT'))
        intent['active_state'] = 'off'
        self.assertFalse(Lint(db, intent=intent).driven('VOUT'))

    def test_p02_parallel_pulls_compare_equivalent_worst_case(self):
        result, _ = self.run_check(pull_db(), pull_check())
        self.assertEqual(result['review_result'], 'FAIL')
        self.assertAlmostEqual(result['calculation']['typ'], 500)
        self.assertAlmostEqual(result['calculation']['min'], 495)

    def test_valid_parallel_pulls_pass_only_resistance_scope(self):
        result, _ = self.run_check(pull_db('4K7/1%'), pull_check())
        self.assertEqual(result['review_result'], 'PASS')
        self.assertAlmostEqual(result['calculation']['typ'], 2350)
        self.assertIn('不含总线电平', result['scope'])

    def test_missing_pull_tolerance_and_other_branches_are_not_pass(self):
        result, _ = self.run_check(pull_db('4K7'), pull_check())
        self.assertEqual(result['review_result'], 'INSUFFICIENT')
        db = pull_db('4K7/1%')
        db['parts']['R3'] = {'value': '100/1%', 'nc': False}
        db['nets']['SDA'].append('R3.1')
        db['nets']['REMOTE'] = ['R3.2']
        db['pin2net'].update({'R3.1': 'SDA', 'R3.2': 'REMOTE'})
        result, _ = self.run_check(db, pull_check())
        self.assertEqual(result['review_result'], 'INSUFFICIENT')

    def test_p03_capacitive_strap_not_passed_by_pull_presence(self):
        db = database({'BOOT0': ['U1.1', 'R1.1', 'C1.1'], 'VCC_3V3': ['R1.2'],
                       'GND': ['C1.2']}, {'U1': 'MCU', 'R1': '100K/1%', 'C1': '100nF'}, {'U1.1': 'BOOT0'})
        check = {'id': 'BOOT', 'rule': 'Rule-16', 'kind': 'strap', 'node': 'U1.1',
                 'required': 'high', 'citation': 'Synthetic minimum high 2V at 1ms'}
        result, _ = self.run_check(db, check)
        self.assertEqual(result['review_result'], 'INSUFFICIENT')
        corners = [3.3 * (1 - math.exp(-.001 / (r * 100e-9))) for r in (99000, 101000)]
        check.update(vih_min_v=2, voltage_analysis=pin_analysis(min(corners), max(corners), .001, .001))
        check['voltage_analysis']['calculation'] = '3.3*(1-exp(-1ms/(100k*(1±1%)*100nF))); C exact, Vinitial=0'
        result, _ = self.run_check(db, check)
        self.assertEqual(result['review_result'], 'FAIL')
        self.assertLess(result['calculation']['voltage_v']['max'], .32)

    def test_guaranteed_high_window_passes_and_undefined_region_fails(self):
        db = pull_db('4K7/1%')
        check = {'id': 'IO', 'rule': 'Rule-12', 'kind': 'pin_bias', 'node': 'U1.1',
                 'required_default': 'high', 'vih_min_v': 2, 'abs_min_v': -.3, 'abs_max_v': 3.6,
                 'voltage_analysis': pin_analysis(3, 3.4), 'citation': 'synthetic guaranteed levels'}
        result, _ = self.run_check(db, check)
        self.assertEqual(result['review_result'], 'PASS')
        check['voltage_analysis'] = pin_analysis(1.1, 1.9)
        result, _ = self.run_check(db, check)
        self.assertEqual(result['review_result'], 'FAIL')
        check['voltage_analysis'] = pin_analysis(3.5, 3.7)
        result, _ = self.run_check(db, check)
        self.assertEqual(result['review_result'], 'FAIL')

    def test_internal_pull_can_meet_guaranteed_default_without_external_resistor(self):
        db = database({'BOOT': ['U1.1']}, {'U1': 'MCU'}, {'U1.1': 'BOOT0'})
        check = {'id': 'BOOT', 'rule': 'Rule-16', 'kind': 'strap', 'node': 'U1.1',
                 'required': 'low', 'vil_max_v': .8, 'voltage_analysis': pin_analysis(0, .2),
                 'citation': 'Synthetic guaranteed internal pull and leakage model'}
        result, _ = self.run_check(db, check)
        self.assertEqual(result['review_result'], 'PASS')

    def test_p04_missing_resistor_or_reference_bounds_never_default_to_pass(self):
        db, check = divider_db(), fb_check()
        db['parts']['R1']['value'] = '20K'
        result, _ = self.run_check(db, check)
        self.assertEqual(result['review_result'], 'INSUFFICIENT')
        self.assertIsNone(parse_resistor('20K')['tol'])
        solution = Solver(db).solve_net('FB_NET')
        self.assertIsNone(solution['up']['min'])
        with self.assertRaises(ValueError):
            divider_window(solution, .8, .8, .8)
        db = divider_db()
        check['vref'] = .8
        result, _ = self.run_check(db, check)
        self.assertEqual(result['review_result'], 'INSUFFICIENT')

    def test_valid_divider_with_bias_current_and_explicit_tolerances(self):
        check = fb_check()
        check['divider_model']['bias_current_a'] = {'min': -1e-6, 'max': 1e-6}
        result, _ = self.run_check(divider_db(), check)
        self.assertEqual(result['review_result'], 'PASS')
        expected_min = .792 * (1 + 19.8 / 10.1) - .0198
        expected_max = .808 * (1 + 20.2 / 9.9) + .0202
        self.assertAlmostEqual(result['calculation']['min'], expected_min)
        self.assertAlmostEqual(result['calculation']['max'], expected_max)

    def test_p05_tvs_working_voltage_is_not_breakdown_or_burnout(self):
        db = database({'VCC_5V1': ['D1.1', 'J1.1'], 'GND': ['D1.2']},
                      {'D1': 'SMBJ5.0A', 'J1': 'CONN'})
        items = [x for x in Lint(db).run() if x['rule'] == 'Rule-13']
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]['kind'], 'CANDIDATE')
        self.assertNotIn('上电即', items[0]['detail'])

    def test_p06_mos_branch_cannot_disappear_from_divider_model(self):
        db = divider_db()
        db['parts'].update({'R3': {'value': '10K/1%', 'nc': False}, 'Q1': {'value': 'MOS', 'nc': False}})
        db['nets']['FB_NET'].append('R3.1')
        db['nets']['MID'] = ['R3.2', 'Q1.1']
        db['nets']['GND'].append('Q1.2')
        db['nets']['CTRL'] = ['Q1.3']
        db['pin2net'] = {node: net for net, nodes in db['nets'].items() for node in nodes}
        check = fb_check()
        result, _ = self.run_check(db, check)
        self.assertEqual(result['review_result'], 'INSUFFICIENT')
        self.assertIn('Q1.1', result['detail'])
        check['divider_model']['ignored_nodes']['Q1.1'] = 'Cannot bypass nonlinear model with free text'
        result, _ = self.run_check(db, check)
        self.assertEqual(result['review_result'], 'INSUFFICIENT')

    def test_unresolved_resistor_depth_and_separate_grounds(self):
        db = divider_db(series_lower=True)
        self.assertEqual(Solver(db, max_depth=1).solve_net('FB_NET')['status'], 'ambiguous')
        db['parts']['R4']['value'] = 'UNKNOWN'
        self.assertEqual(Solver(db).solve_net('FB_NET')['status'], 'ambiguous')
        db = divider_db()
        db['parts']['R3'] = {'value': '10K/1%', 'nc': False}
        db['nets']['FB_NET'].append('R3.1')
        db['nets']['AGND'] = ['R3.2']
        db['pin2net'].update({'R3.1': 'FB_NET', 'R3.2': 'AGND'})
        self.assertEqual(Solver(db).solve_net('FB_NET')['status'], 'ambiguous')
        self.assertEqual(Solver(db, model=divider_model()).solve_net('FB_NET')['status'], 'ambiguous')

    def test_p07_missing_datasheet_blocks_planner_and_lint(self):
        db, check = divider_db(), fb_check()
        _, available = self.run_check(db, check)
        missing = build_datasheet_audit(db)
        result, _ = self.run_check(db, check, audit=missing, bind=False)
        self.assertEqual(result['review_result'], 'INSUFFICIENT')
        for audit, expected in ((missing, 'WAITING_EVIDENCE'), (available, 'READY')):
            plan = build_review_plan(db, evidence={'schema_version': 1, 'checks': [check]}, datasheet_audit=audit)
            item = next(x for x in plan['checks'] if x['check'] == 'feedback-divider-wca')
            self.assertEqual(item['readiness'], expected)

    def test_stale_netlist_document_and_identity_each_block(self):
        db, check = divider_db(), fb_check()
        _, audit = self.run_check(db, check)
        changed = copy.deepcopy(db)
        changed['parts']['R1']['value'] = '30K/1%'
        self.assertTrue(readiness_gaps(changed, check, audit))
        changed_check = copy.deepcopy(check)
        changed_check['basis']['sources'][0]['identity'] = 'DIFFERENT-MODEL'
        self.assertTrue(readiness_gaps(db, changed_check, audit))
        path = pathlib.Path(audit['materials'][0]['document']['path'])
        path.write_bytes(path.read_bytes() + b'changed revision')
        self.assertTrue(readiness_gaps(db, check, audit))

    def test_unrelated_missing_material_does_not_block_ready_check(self):
        db, check = divider_db(), fb_check()
        db['parts']['U9'] = {'value': 'UNRELATED', 'nc': False}
        evidence = {'schema_version': 1, 'checks': [check]}
        audit = bind_evidence(db, evidence, self.directory.name)
        unresolved = build_datasheet_audit(db)
        u9 = next(x for x in audit['materials'] if 'U9' in x['refdes'])
        missing_u9 = next(x for x in unresolved['materials'] if 'U9' in x['refdes'])
        audit['materials'][audit['materials'].index(u9)] = missing_u9
        audit['agent_requests'] = [x for x in unresolved['agent_requests'] if 'U9' in x['refdes']]
        audit['summary'].update(available=1, missing=1, unresolved=1, all_required_available=False)
        self.assertEqual(validate_datasheet_audit(audit, db), [])
        result, _ = self.run_check(db, check, audit=audit, bind=False)
        self.assertEqual(result['review_result'], 'PASS')

    def test_target_mismatch_cannot_inherit_ready_from_shared_net(self):
        db, check = divider_db(), fb_check()
        check['node'] = 'U1.99'
        _, audit = self.run_check(db, check)
        plan = build_review_plan(db, evidence={'schema_version': 1, 'checks': [check]}, datasheet_audit=audit)
        item = next(x for x in plan['checks'] if x['check'] == 'feedback-divider-wca')
        self.assertEqual(item['readiness'], 'WAITING_EVIDENCE')

    def test_audit_expands_critical_passives_and_rejects_unknown_ref(self):
        db = divider_db()
        audit = build_datasheet_audit(db, required_refs=['R1'])
        self.assertEqual(validate_datasheet_audit(audit, db), [])
        self.assertTrue(any('R1' in x['refdes'] for x in audit['materials']))
        with self.assertRaises(ValueError):
            build_datasheet_audit(db, required_refs=['R404'])

    def test_state_evidence_and_circuit_checks_are_individual(self):
        db = divider_db()
        first, second = fb_check(), fb_check()
        second['id'] = 'FB-HOT'
        evidence = {'schema_version': 1, 'checks': [first, second]}
        audit = bind_evidence(db, evidence, self.directory.name)
        first['basis']['state'], second['basis']['state'] = 'cold', 'hot'
        intent = {'circuits': [{'id': 'REGULATOR', 'domain': 'POWER_CONVERTER',
                               'refs': ['U1'], 'states': ['cold', 'hot'], 'citation': 'synthetic requirement'}]}
        self.assertEqual(validate_intent(intent), [])
        plan = build_review_plan(db, intent, evidence, datasheet_audit=audit)
        group = [x for x in plan['checks'] if x['check'] == 'feedback-divider-wca']
        hot = [x for x in group if x.get('evidence_check_id')]
        parents = [x for x in group if x.get('role') == 'coverage_parent']
        self.assertEqual(len(parents), 1)
        self.assertEqual({x['parent_check_id'] for x in hot}, {parents[0]['id']})
        self.assertEqual({x['object']['state'] for x in hot}, {'cold', 'hot'})
        domain = [x for x in plan['checks'] if x.get('domain') == 'POWER_CONVERTER']
        self.assertEqual(len(domain), 8)
        self.assertTrue(all(x['review_result'] is None for x in domain))
        self.assertEqual(len(plan['checks']), len({x['id'] for x in plan['checks']}))

    def test_negative_pin_rating_is_checked_independently_of_logic_low(self):
        db = pull_db()
        check = {'id': 'IO-LOW', 'rule': 'Rule-12', 'kind': 'pin_bias', 'node': 'U1.1',
                 'required_default': 'low', 'vil_max_v': .8, 'abs_min_v': -.3, 'abs_max_v': 3.6,
                 'voltage_analysis': pin_analysis(-1, -.5), 'citation': 'synthetic input ratings'}
        result, _ = self.run_check(db, check)
        self.assertEqual(result['review_result'], 'FAIL')
        del check['abs_min_v']
        result, _ = self.run_check(db, check)
        self.assertEqual(result['review_result'], 'INSUFFICIENT')

    def test_missing_identity_resolution_blocks_current_document(self):
        db, check = divider_db(), fb_check()
        _, audit = self.run_check(db, check)
        del check['basis']['sources'][0]['identity_resolution']
        result, _ = self.run_check(db, check, audit, bind=False)
        self.assertEqual(result['review_result'], 'INSUFFICIENT')

    def test_inconsistent_coordinates_block_execution(self):
        db, check = divider_db(), fb_check()
        check['node'] = 'U1.1'
        check['net'] = 'VOUT_3V3'
        result, _ = self.run_check(db, check)
        self.assertEqual(result['review_result'], 'INSUFFICIENT')

    def test_malformed_extensions_are_rejected_by_shared_validator(self):
        for extension in ({'divider_model': []}, {'basis': {'sources': [{'ref': ['U1']}]}},
                          {'depends_on': ['U1', {}]}, {'voltage_analysis': []}):
            check = dict(fb_check(), **extension)
            evidence = {'schema_version': 1, 'checks': [check]}
            self.assertTrue(validate_evidence(evidence))
            with self.assertRaises(ValueError):
                build_review_plan(divider_db(), evidence=evidence)

    def test_resistor_garbage_suffix_is_not_a_numeric_guarantee(self):
        self.assertIsNone(parse_resistor('20Kwrong/1%'))
        self.assertIsNone(parse_resistor('20K', float('nan')))
        self.assertAlmostEqual(parse_resistor('20Kohm/1%')['kohm'], 20)

    def test_missing_vref_bounds_cli_preserves_only_nominal_estimate(self):
        from solve_dividers import _row
        result = Solver(divider_db()).solve_net('FB_NET')
        row = _row('U1', 'FB_NET', result, .8, None)
        self.assertEqual(row['review_result'], 'INSUFFICIENT')
        self.assertAlmostEqual(row['voltage_nominal'], 2.4)
        self.assertNotIn('voltage', row)

    def test_power_budget_needs_quantified_rail_inputs(self):
        db, check = divider_db(), fb_check()
        _, audit = self.run_check(db, check)
        intent = {'materials': {'requirements': {'available': True, 'citation': 'requirement'},
                                'datasheets': {'available': True, 'citation': 'datasheet'}}}
        plan = build_review_plan(db, intent, datasheet_audit=audit)
        item = next(x for x in plan['checks'] if x['check'] == 'power-rail-budget')
        self.assertEqual(item['readiness'], 'WAITING_EVIDENCE')
        intent['power_rails'] = {'VOUT_3V3': {
            'voltage_v': {'min': 2.3, 'max': 2.5}, 'load_a': {'min': 0, 'max': .5},
            'available_a_min': 1, 'refs': ['U1'], 'state': 'specified Vin and temperature',
            'citation': 'synthetic load budget and regulator guarantees'}}
        self.assertEqual(validate_intent(intent), [])
        negative_rail = copy.deepcopy(intent)
        negative_rail['power_rails']['VOUT_3V3']['voltage_v'] = {'min': -5.5, 'max': -4.5}
        self.assertEqual(validate_intent(negative_rail), [])
        plan = build_review_plan(db, intent, datasheet_audit=audit)
        item = next(x for x in plan['checks'] if x['check'] == 'power-rail-budget')
        self.assertEqual(item['readiness'], 'READY')
        self.assertIsNone(item['review_result'])

    def test_cli_missing_evidence_output_has_per_check_result(self):
        db, check = divider_db(), fb_check()
        folder = pathlib.Path(self.directory.name)
        (folder / 'db.json').write_text(json.dumps(db))
        (folder / 'evidence.json').write_text(json.dumps({'schema_version': 1, 'checks': [check]}))
        (folder / 'audit.json').write_text(json.dumps(build_datasheet_audit(db)))
        command = [sys.executable, '-B', str(pathlib.Path(__file__).resolve().parents[1] / 'lint.py'),
                   str(folder / 'db.json'), '--evidence', str(folder / 'evidence.json'),
                   '--datasheet-audit', str(folder / 'audit.json'), '--json', str(folder / 'out.json')]
        run = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(run.returncode, 0, run.stderr)
        result = json.loads((folder / 'out.json').read_text())
        self.assertEqual(result['check_results'][0]['review_result'], 'INSUFFICIENT')
        self.assertFalse(result['passes'])


if __name__ == '__main__':
    unittest.main()
