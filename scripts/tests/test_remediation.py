import copy
import json
import pathlib
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from validate_review import validate_review
from test_validate_review import fixture, fail, E


def ready():
    return {
        'readiness': 'READY', 'purpose': 'Restore the specified fitted pull-up.',
        'prerequisites': [],
        'steps': [{'kind': 'ASSEMBLY', 'target': 'p1 R1', 'before': 'DNP',
                   'after': 'fitted, quantity 1', 'instruction': 'Enable R1 in configuration A and its BOM.'}],
        'parameters': [{'target': 'R1', 'specification': '10k, 1%, 0402, fitted',
                        'status': 'SELECTED', 'basis': E}],
        'related_findings': [], 'impact_review': 'Confirm the stipulated reset load remains within its limit.',
        'verification': [{'stage': 'NETLIST', 'method': 'Re-export configuration A and trace R1.',
                          'expected': 'R1 is fitted between A and U1.2.'}]}


def actionable():
    p, r, db = fixture(); fail(r)
    r['remediation_version'] = 1
    r['findings'][0]['remediation'] = ready()
    return p, r, db


class RemediationTests(unittest.TestCase):
    def test_legacy_is_compatible_but_does_not_pass_new_gate(self):
        p, r, db = fixture(); fail(r)
        self.assertTrue(validate_review(p, r, db)['valid'])
        self.assertFalse(validate_review(p, r, db, require_actionable=True)['valid'])

    def test_detailed_plan_does_not_close_electrical_defect(self):
        result = validate_review(*actionable(), require_actionable=True)
        self.assertTrue(result['valid'], result)
        self.assertEqual(result['release'], 'NO_GO')
        self.assertEqual(result['summary']['confirmed_defects'], 1)
        self.assertEqual(result['remediation_validation']['by_readiness']['READY'], 1)

    def test_version_declaration_enforces_instructions_without_cli_flag(self):
        p, r, db = actionable(); del r['findings'][0]['remediation']
        self.assertFalse(validate_review(p, r, db)['valid'])

    def test_connect_needs_explicit_old_and_new_endpoints(self):
        p, r, db = actionable(); m = r['findings'][0]['remediation']
        s = m['steps'][0]; s.update(kind='CONNECT', remove_connections=[],
                                  add_connections=[{'from': 'R1.2'}])
        self.assertFalse(validate_review(p, r, db)['valid'])
        s['add_connections'][0]['to'] = 'U1.2'
        self.assertTrue(validate_review(p, r, db)['valid'])

    def test_empty_connection_list_does_not_describe_a_fix(self):
        p, r, db = actionable()
        r['findings'][0]['remediation']['steps'][0].update(kind='CONNECT', remove_connections=[], add_connections=[])
        self.assertFalse(validate_review(p, r, db)['valid'])

    def test_component_change_needs_specification_and_source(self):
        p, r, db = actionable(); m = r['findings'][0]['remediation']
        m['parameters'][0]['basis'] = []
        self.assertFalse(validate_review(p, r, db)['valid'])
        m['parameters'] = []
        self.assertFalse(validate_review(p, r, db)['valid'])

    def test_ready_cannot_hide_candidate_or_missing_inputs(self):
        p, r, db = actionable(); m = r['findings'][0]['remediation']
        m['parameters'][0].update(status='CANDIDATE', needed_input='Maximum input voltage',
                                  selection_method='Calculate Vmax squared divided by Rmin.')
        m['prerequisites'] = [{'input': 'Vmax', 'reason': 'Continuous dissipation',
                              'how_to_obtain': 'Read the controlled supply requirement.',
                              'acceptance': 'A bounded maximum voltage is recorded.'}]
        self.assertFalse(validate_review(p, r, db)['valid'])
        m['readiness'] = 'CONDITIONAL'
        self.assertTrue(validate_review(p, r, db)['valid'])

    def test_conditional_requires_how_to_resolve_the_input(self):
        p, r, db = actionable(); m = r['findings'][0]['remediation']
        m['readiness'] = 'CONDITIONAL'
        m['prerequisites'] = [{'input': 'Vmax', 'reason': 'Power', 'acceptance': 'Known'}]
        self.assertFalse(validate_review(p, r, db)['valid'])

    def test_ready_cannot_still_need_design_choice(self):
        p, r, db = actionable(); r['findings'][0]['remediation']['steps'][0]['kind'] = 'DESIGN'
        self.assertFalse(validate_review(p, r, db)['valid'])

    def test_coupled_finding_must_exist_and_not_be_self(self):
        for value in ['F404', 'F1']:
            p, r, db = actionable(); r['findings'][0]['remediation']['related_findings'] = [value]
            self.assertFalse(validate_review(p, r, db)['valid'])

    def test_future_bench_test_alone_is_not_editing_acceptance(self):
        p, r, db = actionable(); r['findings'][0]['remediation']['verification'][0]['stage'] = 'BENCH'
        self.assertFalse(validate_review(p, r, db)['valid'])

    def test_document_edit_can_be_short_without_electrical_specs(self):
        p, r, db = actionable(); m = r['findings'][0]['remediation']
        m['steps'][0].update(kind='DOCUMENT', target='Cover title', before='Old revision',
                             after='Current revision', instruction='Replace the stale title field.')
        m['parameters'] = []; m['verification'][0]['stage'] = 'DOCUMENT'
        self.assertTrue(validate_review(p, r, db)['valid'])

    def test_malformed_instruction_arrays_do_not_crash_or_pass(self):
        for field, value in [('steps', [None]), ('prerequisites', {}), ('parameters', None),
                             ('related_findings', [['bad']]), ('verification', ['test'])]:
            with self.subTest(field=field):
                p, r, db = actionable(); r['findings'][0]['remediation'][field] = value
                self.assertFalse(validate_review(p, r, db)['valid'])

    def test_cli_requires_details_and_keeps_no_go_semantics(self):
        p, r, db = actionable()
        script = pathlib.Path(__file__).resolve().parents[1] / 'validate_review.py'
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            for name, value in [('plan', p), ('report', r), ('db', db)]:
                (root/name).write_text(json.dumps(value))
            args = [sys.executable, str(script), str(root/'plan'), str(root/'report'),
                    '--db', str(root/'db'), '--require-actionable']
            result = subprocess.run(args, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout+result.stderr)
            self.assertEqual(json.loads(result.stdout)['release'], 'NO_GO')
            self.assertEqual(subprocess.run(args+['--require-release'], capture_output=True).returncode, 2)
            del r['findings'][0]['remediation']
            (root/'report').write_text(json.dumps(r))
            self.assertEqual(subprocess.run(args, capture_output=True).returncode, 2)


if __name__ == '__main__':
    unittest.main()
