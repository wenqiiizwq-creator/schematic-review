import pathlib
import sys
import tempfile
import unittest

SCRIPTS = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))

from checkers.optocoupler import OptocouplerChecker, build_inventory, validate_optocoupler_intent
from electrical_contract import db_fingerprint, validate_evidence
from electrical_fixtures import bind_evidence
from lint import Lint
from plan_review import build_review_plan
from test_inductive_load import add


def opto_board(led_resistor=True, pullup=True):
    """U1 驱动 PC817 的 LED，输出侧上拉到 VCC_3V3。"""
    db = {'nets': {}, 'parts': {}, 'pin2net': {}, 'pinname': {}, 'pintype': {},
          'pseudo_nets': [], 'ref2page': {}}
    anode_net = 'LED_A' if led_resistor else 'V5_ISO'
    add(db, 'OK1', 'PC817', [('1', 'ANODE', anode_net), ('2', 'CATHODE', 'OPTO_DRV'),
                             ('3', 'EMITTER', 'GND'), ('4', 'COLLECTOR', 'OPTO_OUT')])
    add(db, 'U1', 'MCU', [('1', 'GPIO2', 'OPTO_DRV'), ('2', 'VDD', 'V5_ISO'),
                          ('3', 'GND', 'GND_ISO')])
    db['pintype']['U1.1'] = 'OUT'
    if led_resistor:
        add(db, 'R1', '1K', [('1', '1', 'V5_ISO'), ('2', '2', 'LED_A')])
    if pullup:
        add(db, 'R2', '10K', [('1', '1', 'OPTO_OUT'), ('2', '2', 'VCC_3V3')])
    add(db, 'U2', 'HOST', [('1', 'IN', 'OPTO_OUT'), ('2', 'VDD', 'VCC_3V3'),
                           ('3', 'GND', 'GND')])
    return db


def optos_of(inventory, state='as-built'):
    entry = next(x for x in inventory['states'] if x['id'] == state)
    return {item['ref']: item for item in entry['optocouplers']}


def findings(db, intent=None):
    return [f for f in Lint(db, '', intent).run() if f['rule'].startswith('OC-')]


def ctr_check(**overrides):
    check = {'id': 'OPTO', 'rule': 'OC-10', 'kind': 'opto_ctr', 'ref': 'OK1',
             'net': 'OPTO_OUT', 'drive_v': {'min': 4.75, 'max': 5.25},
             'vf_v': {'min': 1.0, 'max': 1.4}, 'driver_drop_v': {'min': 0.0, 'max': 0.4},
             'r_led_ohm': {'min': 970.0, 'max': 1030.0},
             'r_pullup_ohm': {'min': 9700.0, 'max': 10300.0},
             'v_pullup_v': {'min': 3.15, 'max': 3.45}, 'vol_required_v': 0.4,
             'ctr_min': 0.5, 'ctr_derating': 0.5, 'if_abs_max_a': 0.05,
             'citation': 'Synthetic optocoupler CTR and project lifetime derating rule'}
    check.update(overrides)
    return check


class RecognitionTest(unittest.TestCase):
    def test_complete_optocoupler_has_no_findings(self):
        db = opto_board()
        item = optos_of(build_inventory(db))['OK1']
        self.assertEqual(item['led_nets'], ['LED_A', 'OPTO_DRV'])
        self.assertEqual([x['ref'] for x in item['led_resistors']], ['R1'])
        self.assertEqual([x['node'] for x in item['led_drivers']], ['U1.1'])
        self.assertEqual(item['collector_net'], 'OPTO_OUT')
        self.assertEqual(item['output_pullups'], ['R2'])
        self.assertEqual(findings(db), [])

    def test_missing_series_resistor_reports_oc01(self):
        db = opto_board(led_resistor=False)
        hits = findings(db)
        self.assertEqual([f['rule'] for f in hits], ['OC-01'])
        self.assertEqual(hits[0]['kind'], 'FINDING')

    def test_declared_constant_current_drive_clears_oc01(self):
        db = opto_board(led_resistor=False)
        intent = {'optocouplers': {
            'schema_version': 1, 'db_sha256': db_fingerprint(db),
            'states': [{'id': 'run', 'citation': 'BOM Rev.A',
                        'population': {ref: True for ref in db['parts']}}],
            'optocouplers': [{'id': 'ok1', 'ref': 'OK1', 'drive': 'constant-current',
                              'citation': 'constant current driver datasheet'}]}}
        self.assertEqual(findings(db, intent), [])

    def test_missing_pullup_reports_oc02(self):
        hits = findings(opto_board(pullup=False))
        self.assertEqual([f['rule'] for f in hits], ['OC-02'])
        self.assertEqual(hits[0]['kind'], 'CANDIDATE')

    def test_unknown_pin_roles_keep_a_gap(self):
        db = opto_board()
        for pin in ('1', '2', '3', '4'):
            db['pinname'].pop('OK1.' + pin)
        item = optos_of(build_inventory(db))['OK1']
        self.assertIn('pin-roles:OK1', item['gaps'])
        self.assertEqual(item['led_nets'], [])
        self.assertEqual(findings(db), [])

    def test_unpopulated_optocoupler_is_skipped(self):
        db = opto_board()
        db['parts']['OK1']['nc'] = True
        self.assertEqual(optos_of(build_inventory(db)), {})


class PlanTest(unittest.TestCase):
    def test_plan_covers_transfer_and_isolation(self):
        plan = build_review_plan(opto_board())
        checks = {x['check'] for x in plan['checks'] if x['object'].get('optocoupler')}
        self.assertEqual(checks, {'optocoupler-transfer-OK1', 'optocoupler-isolation-OK1'})
        self.assertEqual(plan['optocoupler_version'], 1)
        rules = {row['rule']: row for row in plan['rule_plan']}
        self.assertEqual(rules['OC-01']['instances'], ['OK1'])

    def test_stale_object_binding_is_reported(self):
        plan = build_review_plan(opto_board())
        item = next(x for x in plan['checks'] if x['object'].get('optocoupler'))
        stale = dict(item['object'], optocoupler_digest='0' * 64)
        self.assertEqual(OptocouplerChecker().object_errors('X', stale, plan['optocoupler']),
                         ['X: stale optocoupler object binding'])

    def test_intent_validation_rejects_unknown_refs(self):
        db = opto_board()
        section = {'schema_version': 1, 'db_sha256': db_fingerprint(db),
                   'states': [{'id': 'run', 'citation': 'BOM Rev.A', 'population': {}}],
                   'optocouplers': [{'id': 'x', 'ref': 'OK9', 'citation': 'y'}]}
        errors = validate_optocoupler_intent({'optocouplers': section}, db)
        self.assertTrue(any('optocoupler ref unknown: OK9' in error for error in errors), errors)


class HotRuleTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)

    def run_check(self, check):
        db = opto_board()
        evidence = {'schema_version': 1, 'checks': [check]}
        audit = bind_evidence(db, evidence, self.directory.name)
        self.assertEqual(validate_evidence(evidence), [])
        lint = Lint(db, evidence=evidence, datasheet_audit=audit)
        lint.run()
        results = [x for x in lint.results if x['check_id'] == check['id']]
        self.assertEqual(len(results), 1)
        return results[0]

    def test_sufficient_transfer_passes(self):
        result = self.run_check(ctr_check())
        self.assertEqual(result['review_result'], 'PASS')
        self.assertGreater(result['calculation']['ic_available_a'],
                           result['calculation']['ic_required_a'])

    def test_derated_ctr_below_requirement_fails(self):
        result = self.run_check(ctr_check(ctr_derating=0.05))
        self.assertEqual(result['review_result'], 'FAIL')
        self.assertIn('低于拉低上拉所需', result['detail'])

    def test_forward_current_above_rating_fails(self):
        result = self.run_check(ctr_check(r_led_ohm={'min': 50.0, 'max': 55.0}))
        self.assertEqual(result['review_result'], 'FAIL')
        self.assertIn('超过额定', result['detail'])

    def test_led_not_conducting_fails(self):
        result = self.run_check(ctr_check(drive_v={'min': 1.0, 'max': 1.2}))
        self.assertEqual(result['review_result'], 'FAIL')
        self.assertIn('LED 不导通', result['detail'])

    def test_missing_project_derating_stays_insufficient(self):
        check = ctr_check()
        check.pop('ctr_derating')
        result = self.run_check(check)
        self.assertEqual(result['review_result'], 'INSUFFICIENT')
        self.assertIn('ctr_derating 缺少保证值', result['detail'])

    def test_out_of_range_derating_is_rejected_by_the_contract(self):
        evidence = {'schema_version': 1, 'checks': [ctr_check(ctr_derating=1.5)]}
        self.assertTrue(any('ctr_derating 必须在 (0, 1] 内' in error
                            for error in validate_evidence(evidence)))


if __name__ == '__main__':
    unittest.main()
