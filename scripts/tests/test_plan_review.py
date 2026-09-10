import pathlib
import sys
import unittest

SCRIPTS = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))

from plan_review import build_review_plan, validate_intent


def sample_db():
    nets = {
        'VCC_3V3': ['R1.1', 'U1.5'],
        'REG_FB': ['U1.1', 'R1.2', 'R2.1'],
        'GND': ['R2.2', 'R3.2', 'R4.2'],
        'REG_EN': ['U1.2', 'R3.1'],
        'BOOT0': ['U2.1', 'R4.1'],
        'I2C_SCL': ['U2.2', 'J1.1'],
        'I2C_SDA': ['U2.3', 'J1.2'],
        'USB_TX_P': ['U2.4', 'J1.3'],
        'USB_TX_N': ['U2.5', 'J1.4'],
    }
    parts = {
        'U1': {'part': 'REG-X', 'value': 'REG-X', 'prim': 'REG-X',
               'jedec': 'QFN', 'nc': False},
        'U2': {'part': 'SOC-X', 'value': 'SOC-X', 'prim': 'SOC-X',
               'jedec': 'BGA', 'nc': False},
        'J1': {'part': 'CONN', 'value': 'CONN', 'prim': 'CONN',
               'jedec': 'CONN', 'nc': False},
        'R1': {'part': 'R', 'value': '20K', 'prim': 'R',
               'jedec': '0402', 'nc': False},
        'R2': {'part': 'R', 'value': '10K', 'prim': 'R',
               'jedec': '0402', 'nc': False},
        'R3': {'part': 'R', 'value': '10K', 'prim': 'R',
               'jedec': '0402', 'nc': False},
        'R4': {'part': 'R', 'value': '10K', 'prim': 'R',
               'jedec': '0402', 'nc': False},
    }
    return {
        'nets': nets,
        'parts': parts,
        'pin2net': {node: net for net, nodes in nets.items() for node in nodes},
        'pinname': {
            'U1.1': 'FB', 'U1.2': 'EN', 'U1.5': 'VOUT',
            'U2.1': 'BOOT0', 'U2.2': 'SCL', 'U2.3': 'SDA',
            'U2.4': 'USB_TX_P', 'U2.5': 'USB_TX_N',
            'J1.1': 'SCL', 'J1.2': 'SDA',
            'J1.3': 'USB_TX_P', 'J1.4': 'USB_TX_N',
        },
        'pintype': {'U1.1': 'IN', 'U1.2': 'IN', 'U1.5': 'OUT'},
        'ref2page': {'U1': 1, 'U2': 2, 'J1': 2},
        'pseudo_nets': [],
    }


def get_feature(plan, name):
    return next(item for item in plan['checks']
                if item['object'].get('feature') == name
                and item['check'] == f'feature-{name.lower()}')


class ReviewPlanTests(unittest.TestCase):
    def test_first_pass_discovers_instances_without_inventing_na(self):
        plan = build_review_plan(sample_db())
        self.assertEqual(get_feature(plan, 'DDR')['applicability'],
                         'UNDETERMINED')
        self.assertIsNone(get_feature(plan, 'DDR')['review_result'])
        self.assertEqual(get_feature(plan, 'USB')['applicability'],
                         'APPLICABLE')
        pair = next(item for item in plan['checks']
                    if item['check'] == 'differential-pair-connectivity')
        self.assertTrue(pair['handoff']['required'])
        self.assertIsNone(pair['review_result'])
        self.assertNotIn('HANDOFF', plan['result_model']['review_result'])
        self.assertNotIn('C', plan['result_model']['review_result'])
        self.assertEqual(
            plan['result_model']['review_result'],
            ['PASS', 'FAIL', 'INSUFFICIENT', 'NA'])
        self.assertTrue(
            plan['aggregate_release_gate'][
                'evaluate_after_per_check_review'])
        self.assertEqual(
            len({item['id'] for item in plan['checks']}),
            len(plan['checks']))
        rule17 = next(item for item in plan['rule_plan']
                      if item['rule'] == 'Rule-17')
        self.assertEqual(rule17['applicability'], 'NOT_APPLICABLE')
        rule11 = next(item for item in plan['rule_plan']
                      if item['rule'] == 'Rule-11')
        self.assertEqual(rule11['applicability'], 'APPLICABLE')
        self.assertEqual(rule11['readiness'], 'READY')

    def test_explicit_na_requires_intent_and_citation(self):
        intent = {
            'schema_version': 1,
            'features': {
                'DDR': {
                    'applicability': 'NOT_APPLICABLE',
                    'citation': 'Requirements v1 section 2',
                }
            },
        }
        self.assertEqual(validate_intent(intent), [])
        item = get_feature(build_review_plan(sample_db(), intent), 'DDR')
        self.assertEqual(item['applicability'], 'NOT_APPLICABLE')
        self.assertEqual(item['review_result'], 'NA')
        self.assertFalse(item['handoff']['required'])

    def test_required_but_missing_feature_creates_ready_presence_check(self):
        intent = {
            'schema_version': 1,
            'features': {
                'BLUETOOTH': {
                    'applicability': 'APPLICABLE',
                    'citation': 'Requirements v2 section 5',
                }
            },
        }
        plan = build_review_plan(sample_db(), intent)
        presence = next(item for item in plan['checks']
                        if item['check'] == 'required-feature-presence'
                        and item['object']['feature'] == 'BLUETOOTH')
        self.assertEqual(presence['readiness'], 'READY')
        self.assertTrue(any(x['code'] == 'REQUIRED_FEATURE_NOT_DETECTED'
                            for x in plan['diagnostics']))

    def test_intent_netlist_conflict_is_not_na(self):
        intent = {
            'schema_version': 1,
            'features': {
                'USB': {
                    'applicability': 'NOT_APPLICABLE',
                    'citation': 'Requirements v2 section 7',
                }
            },
        }
        plan = build_review_plan(sample_db(), intent)
        self.assertEqual(get_feature(plan, 'USB')['applicability'],
                         'UNDETERMINED')
        self.assertTrue(any(x['code'] == 'INTENT_NETLIST_CONFLICT'
                            for x in plan['diagnostics']))

    def test_unbound_structured_evidence_does_not_make_hot_instance_ready(self):
        evidence = {
            'schema_version': 1,
            'checks': [{
                'id': 'U1-FB', 'rule': 'Rule-08', 'kind': 'divider',
                'net': 'REG_FB', 'vref': 0.8,
                'expected': {'min': 2.9, 'max': 3.4},
                'citation': 'REG-X datasheet Rev.A p.10',
            }],
        }
        plan = build_review_plan(sample_db(), evidence=evidence)
        item = next(x for x in plan['checks']
                    if x['check'] == 'feedback-divider-wca')
        self.assertEqual(item['readiness'], 'WAITING_EVIDENCE')
        self.assertTrue(item['required_inputs'])

    def test_enable_net_does_not_turn_passive_pins_into_checks(self):
        db = sample_db()
        db['pinname'].update({'R3.1': '1', 'R3.2': '2'})
        checks = [item for item in build_review_plan(db)['checks']
                  if item['check'] == 'enable-default-absmax']
        self.assertEqual([item['object']['node'] for item in checks],
                         ['U1.2'])

    def test_intent_validation_rejects_unproven_na(self):
        errors = validate_intent({
            'schema_version': 1,
            'features': {'DDR': {'applicability': 'NOT_APPLICABLE'}},
        })
        self.assertTrue(any('citation' in error for error in errors))

    def test_spi_clock_does_not_create_i2c_pull_check(self):
        db = sample_db()
        db['nets'] = {'SCLK': ['U2.6', 'J1.6']}
        db['pin2net'] = {'U2.6': 'SCLK', 'J1.6': 'SCLK'}
        db['pinname'] = {'U2.6': 'SCLK', 'J1.6': 'SCLK'}
        db['pintype'] = {'U2.6': 'OUT', 'J1.6': 'IN'}
        plan = build_review_plan(db)
        self.assertEqual(get_feature(plan, 'SPI')['applicability'],
                         'APPLICABLE')
        self.assertEqual(get_feature(plan, 'I2C')['applicability'],
                         'UNDETERMINED')
        self.assertFalse(any(item['check'] == 'i2c-required-pull'
                             for item in plan['checks']))

    def test_revision_rule17_waits_for_both_inputs(self):
        waiting = build_review_plan(sample_db(), review_mode='revision')
        rule17 = next(item for item in waiting['rule_plan']
                      if item['rule'] == 'Rule-17')
        self.assertEqual(rule17['applicability'], 'APPLICABLE')
        self.assertEqual(rule17['readiness'], 'WAITING_EVIDENCE')
        self.assertEqual(rule17['required_inputs'],
                         ['old_db', 'review_claims'])

        ready = build_review_plan(
            sample_db(), review_mode='revision',
            old_db_available=True, claims_available=True)
        rule17 = next(item for item in ready['rule_plan']
                      if item['rule'] == 'Rule-17')
        self.assertEqual(rule17['readiness'], 'READY')

    def test_datasheet_audit_controls_each_component_readiness(self):
        message = ('找不到这颗物料的 datasheet：SOC-X（位号：U2）。'
                   '请提供该物料的原厂 datasheet。')
        audit = {
            'schema_version': 1,
            'summary': {
                'required_materials': 2,
                'available': 1,
                'needs_verification': 0,
                'missing': 0,
                'not_found': 1,
                'unresolved': 1,
                'all_required_available': False,
            },
            'materials': [
                {
                    'material_id': 'DS-REG-X',
                    'identity': 'REG-X',
                    'refdes': ['U1'],
                    'status': 'AVAILABLE',
                    'document': {
                        'identity_verified': True,
                        'source_kind': 'package',
                        'path': 'REG-X.pdf',
                        'document_model': 'REG-X',
                        'document_version': 'Rev.A',
                    },
                },
                {
                    'material_id': 'DS-SOC-X',
                    'identity': 'SOC-X',
                    'refdes': ['U2'],
                    'status': 'NOT_FOUND',
                    'message': message,
                    'searched_sources': [
                        'LCSC query SOC-X',
                        'manufacturer official website query SOC-X',
                    ],
                    'searched_at': '2026-09-02',
                },
            ],
            'agent_requests': [{
                'id': 'REQUEST-SOC-X',
                'action': 'REQUEST_USER_DATASHEET',
                'identity': 'SOC-X',
                'refdes': ['U2'],
                'message': message,
            }],
            'user_messages': [message],
            'diagnostics': [],
        }
        plan = build_review_plan(sample_db(), datasheet_audit=audit)
        checks = {
            item['object']['ref']: item
            for item in plan['checks']
            if item['check'] == 'component-identity-package'
        }
        self.assertEqual(checks['U1']['readiness'], 'READY')
        self.assertEqual(checks['U2']['readiness'], 'WAITING_EVIDENCE')
        self.assertEqual(checks['U2']['required_inputs'], ['datasheet:SOC-X'])
        self.assertEqual(plan['summary']['datasheet_unresolved'], 1)
        self.assertEqual(plan['datasheet_audit']['user_messages'], [message])
        self.assertTrue(any(item['code'] == 'DATASHEET_NOT_FOUND'
                            for item in plan['diagnostics']))


if __name__ == '__main__':
    unittest.main()
