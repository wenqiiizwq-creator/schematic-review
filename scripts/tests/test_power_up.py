import pathlib
import sys
import tempfile
import unittest

SCRIPTS = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))

from checkers.power_up import PowerUpChecker, build_inventory, validate_power_up_intent
from electrical_contract import db_fingerprint, validate_evidence
from electrical_fixtures import bind_evidence
from lint import Lint
from plan_review import build_review_plan
from test_inductive_load import add


def rail_board(enable='divider', kind='linear', load_enable=None):
    """U1 是带使能的稳压器，使能来源与负载使能接法按参数变化。"""
    db = {'nets': {}, 'parts': {}, 'pin2net': {}, 'pinname': {}, 'pintype': {},
          'pseudo_nets': [], 'ref2page': {}}
    pins = [('1', 'VIN', 'V12'), ('2', 'EN', 'EN_3V3'), ('3', 'GND', 'GND')]
    pins.append(('4', 'SW', 'SW_NODE') if kind == 'switching' else ('4', 'VOUT', 'VCC_3V3'))
    add(db, 'U1', 'REG-X', pins)
    if kind == 'switching':
        add(db, 'L1', 'IND-2U2', [('1', '1', 'SW_NODE'), ('2', '2', 'VCC_3V3')])
    if enable == 'divider':
        add(db, 'R1', '100K', [('1', '1', 'V12'), ('2', '2', 'EN_3V3')])
        add(db, 'R2', '22K', [('1', '1', 'EN_3V3'), ('2', '2', 'GND')])
    elif enable == 'rc':
        add(db, 'R1', '100K', [('1', '1', 'V12'), ('2', '2', 'EN_3V3')])
        add(db, 'C1', '100nF', [('1', '1', 'EN_3V3'), ('2', '2', 'GND')])
    elif enable == 'tied':
        db['nets']['V12'].append('U1.2')
        db['pin2net']['U1.2'] = 'V12'
        db['nets']['EN_3V3'].remove('U1.2')
    elif enable == 'gpio':
        add(db, 'U9', 'SOC', [('1', 'GPIO7', 'EN_3V3'), ('2', 'VDD', 'VCC_3V3')])
        db['pintype']['U9.1'] = 'OUT'
    if load_enable:
        net = 'VCC_3V3' if load_enable == 'same-rail' else 'EN_DLY'
        add(db, 'U2', 'SENSOR', [('1', 'VDD', 'VCC_3V3'), ('2', 'EN', net), ('3', 'GND', 'GND')])
    return db


def regulators_of(inventory, state='as-built'):
    entry = next(x for x in inventory['states'] if x['id'] == state)
    return {item['ref']: item for item in entry['regulators']}


def findings(db, intent=None):
    return [f for f in Lint(db, '', intent).run() if f['rule'].startswith('PU-')]


def dropout_check(**overrides):
    check = {'id': 'DROPOUT', 'rule': 'PU-10', 'kind': 'dropout', 'ref': 'U1',
             'vin_min_v': 3.6, 'dropout_max_v': 0.25, 'vout_required_min_v': 3.2,
             'citation': 'Synthetic LDO dropout at minimum temperature and maximum load'}
    check.update(overrides)
    return check


class RecognitionTest(unittest.TestCase):
    def test_uvlo_divider_is_recognised(self):
        item = regulators_of(build_inventory(rail_board()))['U1']
        self.assertEqual(item['enable_source'], 'uvlo-divider')
        self.assertEqual(item['kind'], 'linear')
        self.assertEqual(item['input_nets'], ['V12'])
        self.assertEqual(item['output_nets'], ['VCC_3V3'])
        self.assertEqual(findings(rail_board()), [])

    def test_rc_delay_is_recognised(self):
        self.assertEqual(regulators_of(build_inventory(rail_board(enable='rc')))['U1'
                                                                                 ]['enable_source'],
                         'rc-delay')

    def test_controller_output_is_recognised(self):
        item = regulators_of(build_inventory(rail_board(enable='gpio')))['U1']
        self.assertEqual(item['enable_source'], 'controlled')
        self.assertEqual([x['source'] for x in item['enable_evidence']], ['controller-output'])

    def test_floating_enable_reports_pu03(self):
        db = rail_board(enable=None)
        item = regulators_of(build_inventory(db))['U1']
        self.assertEqual(item['enable_source'], 'unknown')
        hits = findings(db)
        self.assertEqual([f['rule'] for f in hits], ['PU-03'])
        self.assertEqual(hits[0]['kind'], 'CANDIDATE')
        self.assertIn('U1.2', hits[0]['detail'])

    def test_feedback_only_regulator_is_recognised_without_a_feedback_rail(self):
        db = rail_board(enable='divider', kind='switching')
        db['pinname']['U1.4'] = 'FB'
        db['nets']['FB_NET'] = ['U1.4']
        db['nets']['SW_NODE'].remove('U1.4')
        db['pin2net']['U1.4'] = 'FB_NET'
        item = regulators_of(build_inventory(db))['U1']
        self.assertEqual(item['output_nets'], [])
        self.assertEqual(item['kind'], 'linear')

    def test_enable_tied_to_input_reports_pu02(self):
        db = rail_board(enable='tied')
        item = regulators_of(build_inventory(db))['U1']
        self.assertEqual(item['enable_source'], 'tied-to-input')
        hits = findings(db)
        self.assertEqual([f['rule'] for f in hits], ['PU-02'])
        self.assertEqual(hits[0]['kind'], 'CANDIDATE')

    def test_load_enable_on_its_own_rail_reports_pu01(self):
        db = rail_board(load_enable='same-rail')
        hits = findings(db)
        self.assertEqual([f['rule'] for f in hits], ['PU-01'])
        self.assertIn('U2.2', hits[0]['detail'])

    def test_separate_enable_net_is_silent(self):
        self.assertEqual(findings(rail_board(load_enable='delayed')), [])

    def test_switching_regulator_is_classified(self):
        item = regulators_of(build_inventory(rail_board(kind='switching')))['U1']
        self.assertEqual(item['kind'], 'switching')

    def test_unpopulated_regulator_is_skipped(self):
        db = rail_board()
        db['parts']['U1']['nc'] = True
        self.assertEqual(regulators_of(build_inventory(db)), {})


class PlanTest(unittest.TestCase):
    def test_linear_rail_plans_a_dropout_check(self):
        plan = build_review_plan(rail_board())
        checks = {x['check'] for x in plan['checks'] if x['object'].get('power_up')}
        self.assertEqual(checks, {'power-up-enable-source-U1-EN-3V3', 'power-up-dropout-U1-EN-3V3'})
        self.assertEqual(plan['power_up_version'], 1)

    def test_switching_rail_plans_a_prebias_check(self):
        plan = build_review_plan(rail_board(kind='switching'))
        checks = {x['check'] for x in plan['checks'] if x['object'].get('power_up')}
        self.assertEqual(checks, {'power-up-enable-source-U1-EN-3V3', 'power-up-prebias-U1-EN-3V3'})

    def test_rule_instances_follow_the_triggering_objects(self):
        plan = build_review_plan(rail_board(enable='tied', load_enable='same-rail'))
        rules = {row['rule']: row for row in plan['rule_plan']}
        self.assertEqual(rules['PU-02']['instances'], ['U1-V12'])
        self.assertEqual(rules['PU-01']['instances'], ['U2-2'])

    def test_stale_object_binding_is_reported(self):
        plan = build_review_plan(rail_board())
        item = next(x for x in plan['checks'] if x['object'].get('power_up'))
        stale = dict(item['object'], power_up_digest='0' * 64)
        self.assertEqual(PowerUpChecker().object_errors('X', stale, plan['power_up']),
                         ['X: stale power up object binding'])

    def test_intent_validation_accepts_a_sound_section(self):
        db = rail_board()
        section = {'schema_version': 1, 'db_sha256': db_fingerprint(db),
                   'states': [{'id': 'run', 'citation': 'BOM Rev.A', 'population': {}}],
                   'regulators': [{'id': 'u1', 'ref': 'U1', 'citation': 'power tree',
                                   'enable_net': 'EN_3V3'}]}
        self.assertEqual(validate_power_up_intent({'power_up': section}, db), [])
        errors = validate_power_up_intent(
            {'power_up': dict(section, regulators=[{'id': 'u1', 'ref': 'U1', 'citation': 'x',
                                                    'enable_net': 'NOPE'}])}, db)
        self.assertTrue(any('enable_net unknown: NOPE' in error for error in errors), errors)


class HotRuleTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)

    def run_check(self, check):
        db = rail_board()
        evidence = {'schema_version': 1, 'checks': [check]}
        audit = bind_evidence(db, evidence, self.directory.name)
        self.assertEqual(validate_evidence(evidence), [])
        lint = Lint(db, evidence=evidence, datasheet_audit=audit)
        lint.run()
        results = [x for x in lint.results if x['check_id'] == check['id']]
        self.assertEqual(len(results), 1)
        return results[0]

    def test_sufficient_headroom_passes(self):
        result = self.run_check(dropout_check())
        self.assertEqual(result['review_result'], 'PASS')
        self.assertAlmostEqual(result['calculation']['margin_v'], 0.15)

    def test_insufficient_headroom_fails(self):
        result = self.run_check(dropout_check(dropout_max_v=0.6))
        self.assertEqual(result['review_result'], 'FAIL')
        self.assertIn('最坏压差', result['detail'])

    def test_missing_dropout_stays_insufficient(self):
        check = dropout_check()
        check.pop('dropout_max_v')
        result = self.run_check(check)
        self.assertEqual(result['review_result'], 'INSUFFICIENT')
        self.assertIn('dropout_max_v 缺少保证值', result['detail'])

    def test_negative_dropout_is_rejected_by_the_contract(self):
        evidence = {'schema_version': 1, 'checks': [dropout_check(dropout_max_v=-1)]}
        self.assertTrue(any('dropout_max_v 不得为负' in error for error in validate_evidence(evidence)))


if __name__ == '__main__':
    unittest.main()
