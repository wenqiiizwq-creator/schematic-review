import contextlib
import copy
import io
import pathlib
import sys
import unittest
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from parse_netlist import parse_pstxnet, self_check, is_not_populated, build
from plan_review import build_review_plan, validate_intent
from electrical_contract import check_matches
from electrical_fixtures import bind_evidence, divider_model
from lint import Lint
from solve_dividers import Solver
from diff_netlists import connected, evaluate_claims, validate_claims
from test_solve_dividers import divider_db
from test_lint import sample_db


def network(parts, nets, pinname=None):
    return {'parts': parts, 'nets': nets, 'pinname': pinname or {},
            'pin2net': {n: net for net, nodes in nets.items() for n in nodes},
            'pseudo_nets': [], 'ref2page': {}, 'pintype': {}}


class V2Regressions(unittest.TestCase):
    def test_normal_flat_net_is_not_suppressed_as_pseudo(self):
        nets, pins, pseudo = parse_pstxnet("NET_NAME\n'VCC'\nC_SIGNAL='VCC';\nNODE_NAME U1 1\n'VDD':;\n")
        self.assertEqual(nets, {'VCC': ['U1.1']})
        self.assertEqual(pins, {'U1.1': 'VDD'})
        self.assertEqual(pseudo, [])

    def test_dnp_markers_do_not_match_inside_part_number(self):
        for marker in ('DNP', 'DNI', 'DNF', 'NC'):
            self.assertTrue(is_not_populated('R_10K_' + marker, '10K'))
        self.assertFalse(is_not_populated('NCP1117', 'NCP1117'))

    def test_duplicate_pin_networks_fail_integrity(self):
        db = network({'R1': {}}, {'A': ['R1.1'], 'B': ['R1.1']}, {'R1.1': '1'})
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertFalse(self_check(db, strict=False))

    def test_zero_ohm_between_two_floating_rails_is_not_source(self):
        db = network({'R1': {'value': '0R'}, 'U1': {}},
                     {'VDD_A': ['R1.1', 'U1.1'], 'VDD_B': ['R1.2']}, {'U1.1': 'VDD'})
        lint = Lint(db)
        self.assertFalse(lint.driven('VDD_A'))
        self.assertTrue(any(x['rule'] == 'Rule-05' for x in lint.run()))

    def test_trace_through_bead_and_fitted_resistor_to_source(self):
        db = network({'R1': {'value': '0.1R'}, 'FB1': {'value': 'FERRITE'}, 'U1': {'value': 'REG'}},
                     {'VDD_A': ['R1.1'], 'MID': ['R1.2', 'FB1.1'],
                      'SOURCE': ['FB1.2', 'U1.1']}, {'U1.1': 'VOUT'})
        self.assertFalse(Lint(db).driven('VDD_A'))
        intent = {'active_state': 'RUN', 'power_paths': [
            {'ref': 'R1', 'from': 'MID', 'to': 'VDD_A', 'state': 'RUN',
             'citation': 'Synthetic fitted 0.1 ohm sense resistor; conducting in RUN'}]}
        self.assertTrue(Lint(db, intent=intent).driven('VDD_A'))
        db['parts']['R1']['nc'] = True
        self.assertFalse(Lint(db, intent=intent).driven('VDD_A'))

    def test_pseudo_net_cannot_bridge_to_a_source(self):
        db = network({'U1': {'value': 'REG'}}, {'NC': ['U1.1']}, {'U1.1': 'VOUT'})
        db['pseudo_nets'] = ['NC']
        self.assertFalse(Lint(db).driven('NC'))

    def test_lint_preserves_export_abort_as_input_finding(self):
        findings = Lint(sample_db(), log_text='ERROR(ORCAP-1): invalid\nAborting Netlisting').run()
        self.assertEqual(len([x for x in findings if x['rule'] == 'INPUT-EXPORT']), 2)

    def test_requirement_validation_and_state_expansion_coexist(self):
        intent = {
            'requirements': [{'id': 'REQ-01', 'text': 'Startup guaranteed',
                              'criterion': 'Defined state throughout sampling', 'citation': 'SYNTHETIC REQ'}],
            'circuits': [{'id': 'BOOT', 'domain': 'STARTUP', 'refs': ['U1'],
                          'states': ['cold-start', 'brownout'], 'citation': 'SYNTHETIC states'}]}
        self.assertEqual(validate_intent(intent), [])
        plan = build_review_plan(sample_db(), intent)
        states = {x['object']['state'] for x in plan['checks'] if x['object'].get('circuit') == 'BOOT'}
        self.assertEqual(states, {'cold-start', 'brownout'})
        self.assertTrue(any(x['object'].get('requirement_id') == 'REQ-01' for x in plan['checks']))
        del intent['requirements'][0]['criterion']
        self.assertTrue(validate_intent(intent))

    def test_tvs_is_not_power_source(self):
        db = network({'D1': {'value': 'TVS'}}, {'VDD_A': ['D1.1'], 'GND': ['D1.2']})
        self.assertFalse(Lint(db).driven('VDD_A'))

    def test_evidence_for_one_en_does_not_cover_another(self):
        evidence = {'checks': [{'rule': 'Rule-12', 'node': 'U1.1', 'ref': 'U1'}]}
        self.assertFalse(check_matches(sample_db(), evidence['checks'][0], 'Rule-12', {'node': 'U1.2', 'ref': 'U1'}))
        self.assertTrue(check_matches(sample_db(), evidence['checks'][0], 'Rule-12', {'node': 'U1.1', 'ref': 'U1'}))

    def test_requirement_and_full_pin_audits_are_planned(self):
        intent = {'requirements': [{'id': 'REQ-USB', 'text': 'USB port required',
                   'criterion': 'end-to-end connectivity', 'citation': 'REQ Rev.A section 1'}]}
        self.assertEqual(validate_intent(intent), [])
        plan = build_review_plan(sample_db(), intent)
        self.assertTrue(any(x['object'].get('requirement_id') == 'REQ-USB' for x in plan['checks']))
        self.assertTrue(any(x['check'] == 'physical-pin-inventory' and x['object']['ref'] == 'U1'
                            for x in plan['checks']))

    def test_source_rail_is_not_divided_pin_voltage(self):
        db = network({'U1': {}, 'R1': {'value': '100K'}, 'R2': {'value': '10K'}},
                     {'VCC_24V': ['R1.1'], 'EN': ['R1.2', 'R2.1', 'U1.1'], 'GND': ['R2.2']},
                     {'U1.1': 'EN'})
        check = {'id': 'EN', 'rule': 'Rule-12', 'kind': 'pin_bias', 'node': 'U1.1',
                 'required_default': 'high', 'abs_max_v': 5.5, 'citation': 'SYNTHETIC Rev.A p.1'}
        lint = Lint(db, evidence={'checks': [check]})
        findings = [x for x in lint.run() if x.get('check_id') == 'EN']
        self.assertTrue(findings)
        self.assertTrue(all(x['kind'] == 'CANDIDATE' for x in findings))
        self.assertFalse(lint.passes)

    def test_missing_explicit_float_net_never_passes(self):
        check = {'id': 'F', 'rule': 'Rule-16', 'kind': 'strap', 'net': 'MISSING',
                 'required': 'float', 'citation': 'SYNTHETIC Rev.A p.1'}
        lint = Lint(sample_db(), evidence={'checks': [check]})
        lint.run()
        self.assertFalse(lint.passes)

    def test_shared_upper_lower_resistor_requires_node_analysis(self):
        db = network({'R1': {'value': '1K'}, 'R2': {'value': '10K'}, 'R3': {'value': '20K'}},
                     {'FB': ['R1.1'], 'MID': ['R1.2', 'R2.1', 'R3.1'],
                      'GND': ['R2.2'], 'VOUT': ['R3.2']})
        self.assertEqual(Solver(db).solve_net('FB')['status'], 'ambiguous')

    def test_unknown_parallel_resistor_does_not_silently_disappear(self):
        db = divider_db(parallel=True)
        db['parts']['R3']['value'] = 'UNKNOWN'
        self.assertEqual(Solver(db).solve_net('FB_NET')['status'], 'ambiguous')

    def test_truncated_branch_does_not_produce_partial_solution(self):
        db = divider_db()
        for i in range(3, 16):
            db['parts'][f'R{i}'] = {'value': '10K'}
            a = 'FB_NET' if i == 3 else f'MID{i-1}'
            b = 'VOUT_OTHER' if i == 15 else f'MID{i}'
            db['nets'].setdefault(a, []).append(f'R{i}.1')
            db['nets'].setdefault(b, []).append(f'R{i}.2')
            db['pin2net'][f'R{i}.1'], db['pin2net'][f'R{i}.2'] = a, b
        self.assertEqual(Solver(db).solve_net('FB_NET')['status'], 'ambiguous')

    def test_changed_only_claim_does_not_close_repair(self):
        old, new = sample_db(), sample_db()
        new['pin2net']['U1.1'] = 'STILL_WRONG'
        claims = {'schema_version': 1, 'claims': [{'id': 'F1', 'expect': [
                  {'kind': 'pin_net_changed', 'node': 'U1.1'}]}]}
        result, findings = evaluate_claims(old, new, claims)
        self.assertEqual(result[0]['status'], 'INSUFFICIENT')
        self.assertTrue(findings)

    def test_connectivity_assertion_respects_dnp_and_pseudo(self):
        db = network({'R1': {'value': '0R'}}, {'A': ['U1.1', 'R1.1'], 'B': ['R1.2', 'U2.1']})
        self.assertTrue(connected(db, 'U1.1', 'U2.1'))
        db['parts']['R1']['nc'] = True
        self.assertFalse(connected(db, 'U1.1', 'U2.1'))
        db['pseudo_nets'] = ['A']
        self.assertIsNone(connected(db, 'U1.1', 'U2.1'))
        self.assertTrue(validate_claims({'schema_version': 1, 'claims': [
            {'id': 'F1', 'expect': [{'kind': 'pins_connected', 'nodes': ['U1.1']}]}]}))

    def test_nominal_vref_cannot_be_reported_as_wca_pass(self):
        db = divider_db()
        check = {'id': 'FB', 'rule': 'Rule-08', 'kind': 'divider', 'net': 'FB_NET',
                 'vref': 0.8, 'expected': {'min': 2.0, 'max': 3.0},
                 'citation': 'SYNTHETIC Rev.A p.1'}
        lint = Lint(db, evidence={'checks': [check]})
        findings = [x for x in lint.run() if x.get('check_id') == 'FB']
        self.assertTrue(findings)
        self.assertTrue(all(x['kind'] == 'CANDIDATE' for x in findings))
        self.assertFalse(lint.passes)

    def test_default_resistor_tolerance_cannot_pass_wca(self):
        db = divider_db()
        db['parts']['R1']['value'] = '20K'
        check = {'id': 'FB', 'rule': 'Rule-08', 'kind': 'divider', 'net': 'FB_NET',
                 'vref': {'min': 0.792, 'typ': 0.8, 'max': 0.808},
                 'divider_model': divider_model(), 'depends_on': ['R1', 'R2'],
                 'expected': {'min': 2.0, 'max': 3.0}, 'citation': 'SYNTHETIC Rev.A p.1'}
        evidence = {'schema_version': 1, 'checks': [check]}
        with tempfile.TemporaryDirectory() as directory:
            audit = bind_evidence(db, evidence, directory)
            lint = Lint(db, evidence=evidence, datasheet_audit=audit)
            lint.run()
            self.assertFalse(lint.passes)
            self.assertTrue(any(x.get('check_id') == 'FB' and x['kind'] == 'CANDIDATE'
                                for x in lint.F))
            check['resistor_tolerance'] = 0.01
            lint = Lint(db, evidence=evidence, datasheet_audit=audit)
            lint.run()
            self.assertTrue(any(x['check_id'] == 'FB' for x in lint.passes))

    def test_parser_keeps_symbol_pin_omitted_from_netlist(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            (root/'pstxnet.dat').write_text("NET_NAME\n'VCC'\nC_SIGNAL='@D:VCC';\nNODE_NAME U1 1\n'VDD':;\n")
            (root/'pstxprt.dat').write_text(" U1 'IC':;\nP_PATH='@D:page1_main';\n")
            (root/'pstchip.dat').write_text("primitive 'IC';\nPART_NAME='SYNTHETIC';\nVALUE='IC';\n"
                " 'VDD':\nPIN_NUMBER='(1)';\nPINUSE='POWER';\n"
                " 'RESERVED':\nPIN_NUMBER='(2)';\nPINUSE='IN';\nend_primitive;\n")
            db = build(directory)
            self.assertEqual(db['declared_pinname']['U1.2'], 'RESERVED')
            self.assertNotIn('U1.2', db['pin2net'])
            self.assertEqual(db['missing_primitives'], [])
            (root/'netlist.log').write_text('ERROR(ORCAP-36041): invalid export\nAborting Netlisting')
            db = build(directory)
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                self.assertFalse(self_check(db, strict=False))

    def test_missing_part_and_null_value_cannot_close_claim(self):
        claim = {'schema_version': 1, 'claims': [{'id': 'F', 'expect': [
            {'kind': 'part_field_equals', 'ref': 'MISSING', 'field': 'value', 'value': None}]}]}
        self.assertTrue(validate_claims(claim))
        result, _ = evaluate_claims(sample_db(), sample_db(), claim)
        self.assertEqual(result[0]['status'], 'FAIL')


if __name__ == '__main__':
    unittest.main()
