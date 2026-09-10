import pathlib
import sys
import unittest
import tempfile

SCRIPTS = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))

from lint import Lint, _pin_class, validate_evidence
from electrical_fixtures import bind_evidence, pin_analysis, divider_model


def sample_db():
    nets = {
        'BUS': ['U1.1', 'U2.1'],
        'EN_NET': ['U3.1', 'R1.1'],
        'VCC_12V': ['R1.2'],
        'I2C_SCL': ['U4.1'],
        'BOOT0': ['U5.1', 'R2.1'],
        'VCC_3V3': ['R2.2'],
        'JMAP': ['J1.1'],
    }
    parts = {
        'U1': {'value': 'A', 'part': 'A', 'prim': 'A', 'nc': False},
        'U2': {'value': 'B', 'part': 'B', 'prim': 'B', 'nc': False},
        'U3': {'value': 'C', 'part': 'C', 'prim': 'C', 'nc': False},
        'U4': {'value': 'D', 'part': 'D', 'prim': 'D', 'nc': False},
        'U5': {'value': 'E', 'part': 'E', 'prim': 'E', 'nc': False},
        'R1': {'value': '10K/1%', 'part': 'R', 'prim': 'R', 'nc': False},
        'R2': {'value': '10K/1%', 'part': 'R', 'prim': 'R', 'nc': False},
        'J1': {'value': 'CONN', 'part': 'CONN', 'prim': 'CONN', 'nc': False},
    }
    pin2net = {node: net for net, nodes in nets.items() for node in nodes}
    return {
        'nets': nets,
        'parts': parts,
        'pin2net': pin2net,
        'pinname': {
            'U1.1': 'OUTA', 'U2.1': 'OUTB', 'U3.1': 'EN',
            'U4.1': 'SCL', 'U5.1': 'BOOT0', 'J1.1': 'VBUS_WRONG',
        },
        'pintype': {'U1.1': 'OUT', 'U2.1': 'OUT'},
        'ref2page': {},
        'pseudo_nets': [],
    }


def hot_evidence():
    return {
        'schema_version': 1,
        'checks': [
            {
                'id': 'EN-ABS',
                'rule': 'Rule-12',
                'kind': 'pin_bias',
                'node': 'U3.1',
                'required_default': 'high',
                'abs_max_v': 5.5,
                'citation': 'U3 datasheet Rev.A p.4',
            },
            {
                'id': 'SCL-PULL',
                'rule': 'Rule-09',
                'kind': 'required_pull',
                'net': 'I2C_SCL',
                'direction': 'up',
                'to': 'VCC_3V3',
                'resistance_ohm': {'min': 1000, 'max': 4700},
                'citation': 'U4 HDG v1 p.8',
            },
            {
                'id': 'J1-MAP',
                'rule': 'Rule-14',
                'kind': 'pin_map',
                'ref': 'J1',
                'expected': {'1': 'VBUS'},
                'citation': 'J1 drawing Rev.B p.2',
            },
            {
                'id': 'BOOT-LOW',
                'rule': 'Rule-16',
                'kind': 'strap',
                'node': 'U5.1',
                'required': 'low',
                'citation': 'U5 datasheet Rev.C Table 3',
            },
        ],
    }


def divider_db():
    nets = {
        'VOUT_2V4': ['R10.1'],
        'FB': ['U10.1', 'R10.2', 'R11.1'],
        'GND': ['R11.2'],
    }
    parts = {
        'U10': {'value': 'REG', 'part': 'REG', 'prim': 'REG', 'nc': False},
        'R10': {'value': '20K/1%', 'part': 'R', 'prim': 'R', 'nc': False},
        'R11': {'value': '10K/1%', 'part': 'R', 'prim': 'R', 'nc': False},
    }
    return {
        'nets': nets,
        'parts': parts,
        'pin2net': {node: net for net, nodes in nets.items() for node in nodes},
        'pinname': {'U10.1': 'FB'},
        'pintype': {},
        'ref2page': {},
        'pseudo_nets': [],
    }


class LintTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)

    def bound_lint(self, database, evidence):
        audit = bind_evidence(database, evidence, self.directory.name)
        return Lint(database, intent={'expect': {}}, evidence=evidence, datasheet_audit=audit)

    def test_cadence_bi_pinuse_maps_to_bidirectional(self):
        self.assertEqual(_pin_class('BI'), 'BIDI')

    def test_evidence_validation_requires_citation(self):
        evidence = hot_evidence()
        del evidence['checks'][0]['citation']
        self.assertTrue(validate_evidence(evidence))

    def test_evidence_validation_rejects_duplicate_and_bad_ranges(self):
        evidence = {
            'schema_version': 1,
            'checks': [
                {
                    'id': 'DUP', 'rule': 'Rule-08', 'kind': 'divider',
                    'net': 'FB',
                    'vref': {'min': 0.81, 'typ': 0.8, 'max': 0.79},
                    'resistor_tolerance': 1.2,
                    'expected': {'min': 3.4, 'max': 3.3},
                    'citation': 'REG datasheet Rev.A p.9',
                },
                {
                    'id': 'DUP', 'rule': 'Rule-12', 'kind': 'pin_bias',
                    'node': 'U1.1', 'required_default': 'high',
                    'citation': 'REG datasheet Rev.A p.4',
                },
            ],
        }
        errors = validate_evidence(evidence)
        self.assertTrue(any('重复' in error for error in errors))
        self.assertTrue(any('min <= typ <= max' in error for error in errors))
        self.assertTrue(any('resistor_tolerance' in error for error in errors))
        self.assertTrue(any('expected.min 不得大于 max' in error
                            for error in errors))

    def test_evidence_validation_handles_non_string_schema_values(self):
        evidence = {
            'schema_version': 1,
            'checks': [{
                'id': ['bad'], 'rule': ['Rule-12'], 'kind': ['pin_bias'],
                'node': ['U1.1'], 'required_default': ['high'],
                'citation': ['not text'],
            }],
        }
        errors = validate_evidence(evidence)
        self.assertGreaterEqual(len(errors), 4)

    def test_pinuse_and_hot_rules_execute(self):
        evidence = hot_evidence()
        self.assertEqual(validate_evidence(evidence), [])
        evidence['checks'][0].update(vih_min_v=2.0, abs_min_v=-.3, voltage_analysis=pin_analysis(12, 12))
        evidence['checks'][3].update(vil_max_v=0.8, voltage_analysis=pin_analysis(3.3, 3.3))
        lint = self.bound_lint(sample_db(), evidence)
        findings = lint.run()
        rules = {item['rule'] for item in findings
                 if item['kind'] == 'FINDING'}
        self.assertIn('Rule-19', rules)
        self.assertIn('Rule-12', rules)
        self.assertIn('Rule-09', rules)
        self.assertIn('Rule-14', rules)
        self.assertIn('Rule-16', rules)
        self.assertEqual(
            lint.hot_executed, {'Rule-09', 'Rule-12', 'Rule-14', 'Rule-16'})
        self.assertTrue(any(item[0] == 'Rule-19' for item in lint.skipped))

    def test_hot_divider_uses_worst_case_window(self):
        evidence = {
            'schema_version': 1,
            'checks': [
                {
                    'id': 'FB-WCA',
                    'rule': 'Rule-08',
                    'kind': 'divider',
                    'net': 'FB',
                    'vref': {'typ': 0.8, 'min': 0.792, 'max': 0.808},
                    'expected': {'min': 2.39, 'max': 2.41},
                    'citation': 'REG datasheet Rev.A p.9',
                }
            ],
        }
        self.assertEqual(validate_evidence(evidence), [])
        model = divider_model('VOUT_2V4')
        model['ignored_nodes'] = {'U10.1': 'Synthetic FB input current included in model'}
        evidence['checks'][0]['divider_model'] = model
        lint = self.bound_lint(divider_db(), evidence)
        findings = lint.run()
        self.assertTrue(any(x['rule'] == 'Rule-08' for x in findings))
        self.assertIn('Rule-08', lint.hot_executed)

    def test_conflicting_pulls_remain_candidate(self):
        database = sample_db()
        database['nets']['GND'] = ['R3.2']
        database['nets']['EN_NET'].append('R3.1')
        database['parts']['R3'] = {
            'value': '10K/1%', 'part': 'R', 'prim': 'R', 'nc': False}
        database['pin2net']['R3.1'] = 'EN_NET'
        database['pin2net']['R3.2'] = 'GND'
        evidence = {
            'schema_version': 1,
            'checks': [{
                'id': 'EN-BIAS', 'rule': 'Rule-12', 'kind': 'pin_bias',
                'node': 'U3.1', 'required_default': 'high',
                'citation': 'U3 datasheet Rev.A p.4',
            }],
        }
        lint = self.bound_lint(database, evidence)
        findings = lint.run()
        self.assertTrue(any(
            item['rule'] == 'Rule-12' and item['kind'] == 'CANDIDATE'
            and item.get('review_result') == 'INSUFFICIENT'
            for item in findings))
        self.assertFalse(any(
            item['rule'] == 'Rule-12' and item['check_id'] == 'EN-BIAS'
            for item in lint.passes))

    def test_required_series_honors_explicit_rail_target(self):
        database = sample_db()
        evidence = {
            'schema_version': 1,
            'checks': [{
                'id': 'EN-SERIES', 'rule': 'Rule-09',
                'kind': 'required_series', 'net': 'EN_NET',
                'to': 'VCC_12V',
                'resistance_ohm': {'min': 9000, 'max': 11000},
                'citation': 'U3 datasheet Rev.A p.4',
            }],
        }
        self.assertEqual(validate_evidence(evidence), [])
        lint = self.bound_lint(database, evidence)
        lint.run()
        self.assertTrue(any(
            item['rule'] == 'Rule-09' and item['check_id'] == 'EN-SERIES'
            for item in lint.passes))

    def test_rule20_checks_all_bom_identity_fields(self):
        database = sample_db()
        database['parts']['U1']['jedec'] = ' QFN32'
        lint = Lint(database, intent={'expect': {}})
        findings = lint.run()
        self.assertTrue(any(
            item['rule'] == 'Rule-20' and 'U1.jedec' in item['detail']
            for item in findings))


if __name__ == '__main__':
    unittest.main()
