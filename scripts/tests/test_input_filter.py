import pathlib
import sys
import tempfile
import unittest

SCRIPTS = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))

from checkers.input_filter import InputFilterChecker, build_inventory, validate_input_filter_intent
from electrical_contract import db_fingerprint, validate_evidence
from electrical_fixtures import bind_evidence
from lint import Lint
from plan_review import build_review_plan
from test_inductive_load import add


def converter(series='ferrite', damper=None, bulk=False):
    """U1 是降压变换器，输入网 VIN_12 的上游滤波按参数变化。"""
    db = {'nets': {}, 'parts': {}, 'pin2net': {}, 'pinname': {}, 'pintype': {},
          'pseudo_nets': [], 'ref2page': {}}
    add(db, 'U1', 'BUCK-3A', [('1', 'VIN', 'VIN_12'), ('2', 'SW', 'SW_NODE'),
                              ('3', 'GND', 'GND'), ('4', 'FB', 'FB')])
    add(db, 'C1', '10uF', [('1', '1', 'VIN_12'), ('2', '2', 'GND')])
    add(db, 'L1', 'IND-4U7', [('1', '1', 'SW_NODE'), ('2', '2', 'VOUT_5V')])
    if series == 'ferrite':
        add(db, 'FB1', 'BLM21', [('1', '1', 'VIN_12'), ('2', '2', 'V12_RAW')])
        add(db, 'C2', '10uF', [('1', '1', 'V12_RAW'), ('2', '2', 'GND')])
    elif series == 'choke':
        db['parts']['CM1'] = {'part': 'COMMON-MODE-CHOKE', 'value': 'CHOKE', 'prim': 'L', 'nc': False}
        for pin, net in (('1', 'VIN_12'), ('2', 'V12_RAW'), ('3', 'VIN_RTN'), ('4', 'GND')):
            node = 'CM1.' + pin
            db['nets'].setdefault(net, []).append(node)
            db['pin2net'][node] = net
    if damper == 'rc':
        add(db, 'R5', '1R', [('1', '1', 'VIN_12'), ('2', '2', 'DAMP_MID')])
        add(db, 'C5', '47uF', [('1', '1', 'DAMP_MID'), ('2', '2', 'GND')])
    if bulk:
        add(db, 'C6', '100uF/ALUM', [('1', '1', 'VIN_12'), ('2', '2', 'GND')])
    return db


def converters_of(inventory, state='as-built'):
    entry = next(x for x in inventory['states'] if x['id'] == state)
    return {item['ref']: item for item in entry['converters']}


def findings(db, intent=None):
    return [f for f in Lint(db, '', intent).run() if f['rule'].startswith('IF-')]


def damping_check(**overrides):
    check = {'id': 'IF', 'rule': 'IF-10', 'kind': 'input_filter_damping', 'ref': 'U1',
             'net': 'VIN_12', 'vin_min_v': 10.8, 'pin_max_w': 12.0,
             'esr_bulk_ohm': {'min': 0.15, 'max': 0.6},
             'c_bulk_f': {'min': 80e-6, 'max': 120e-6},
             'c_in_f': {'min': 6e-6, 'max': 10e-6},
             'l_filter_h': {'min': 0.8e-6, 'max': 1.2e-6},
             'c_bulk_ratio_min': 4.0,
             'citation': 'Synthetic project damping rule and guaranteed component data'}
    check.update(overrides)
    return check


class RecognitionTest(unittest.TestCase):
    def test_converter_input_with_series_filter_is_recognised(self):
        item = converters_of(build_inventory(converter()))['U1']
        self.assertEqual(item['input_net'], 'VIN_12')
        self.assertEqual([x['ref'] for x in item['series_elements']], ['FB1'])
        self.assertEqual([x['ref'] for x in item['input_caps']], ['C1'])
        self.assertEqual([x['ref'] for x in item['upstream_caps']], ['C2'])
        self.assertAlmostEqual(item['input_caps'][0]['farads'], 1e-5)

    def test_series_filter_without_damping_reports_if01(self):
        hits = findings(converter())
        self.assertEqual([f['rule'] for f in hits], ['IF-01'])
        self.assertEqual(hits[0]['kind'], 'CANDIDATE')
        self.assertIn('FB1', hits[0]['detail'])

    def test_rc_damper_clears_if01(self):
        db = converter(damper='rc')
        item = converters_of(build_inventory(db))['U1']
        self.assertEqual([x['resistor'] for x in item['dampers']], ['R5'])
        self.assertEqual(findings(db), [])

    def test_electrolytic_bulk_clears_if01(self):
        db = converter(bulk=True)
        self.assertEqual(converters_of(build_inventory(db))['U1']['bulk_candidates'], ['C6'])
        self.assertEqual(findings(db), [])

    def test_no_series_element_means_no_finding(self):
        db = converter(series=None)
        self.assertEqual(converters_of(build_inventory(db))['U1']['series_elements'], [])
        self.assertEqual(findings(db), [])

    def test_common_mode_choke_keeps_a_topology_gap(self):
        item = converters_of(build_inventory(converter(series='choke')))['U1']
        self.assertIn('series-element-topology:CM1', item['gaps'])
        self.assertEqual(item['series_elements'], [])

    def test_linear_regulator_is_not_a_converter(self):
        db = converter(series=None)
        db['pinname']['U1.2'] = 'VOUT'
        self.assertEqual(converters_of(build_inventory(db)), {})


class PlanTest(unittest.TestCase):
    def test_plan_items_bind_to_the_inventory_digest(self):
        plan = build_review_plan(converter())
        items = [x for x in plan['checks'] if x['object'].get('input_filter')]
        self.assertEqual({x['check'] for x in items},
                         {'input-filter-damping-U1-VIN-12', 'input-filter-attenuation-U1-VIN-12'})
        for item in items:
            self.assertEqual(item['object']['input_filter_digest'], plan['input_filter']['digest'])
        rules = {row['rule']: row for row in plan['rule_plan']}
        self.assertEqual(rules['IF-01']['instances'], ['U1-VIN-12'])
        self.assertEqual(rules['IF-10']['readiness'], 'WAITING_EVIDENCE')

    def test_intent_validation_rejects_unsound_configuration(self):
        db = converter()
        base = {'schema_version': 1, 'db_sha256': db_fingerprint(db),
                'states': [{'id': 'run', 'citation': 'BOM Rev.A', 'population': {}}],
                'converters': [{'id': 'u1', 'ref': 'U1', 'citation': 'power tree'}]}
        self.assertEqual(validate_input_filter_intent({'input_filters': base}, db), [])
        errors = validate_input_filter_intent(
            {'input_filters': dict(base, converters=[{'id': 'u1', 'ref': 'U9', 'citation': 'x'}])}, db)
        self.assertTrue(any('converter ref unknown: U9' in error for error in errors), errors)


class HotRuleTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)

    def run_check(self, check, db=None):
        db = db or converter(damper='rc')
        evidence = {'schema_version': 1, 'checks': [check]}
        audit = bind_evidence(db, evidence, self.directory.name)
        self.assertEqual(validate_evidence(evidence), [])
        lint = Lint(db, evidence=evidence, datasheet_audit=audit)
        lint.run()
        results = [x for x in lint.results if x['check_id'] == check['id']]
        self.assertEqual(len(results), 1)
        return results[0]

    def test_damped_filter_passes(self):
        result = self.run_check(damping_check())
        self.assertEqual(result['review_result'], 'PASS')
        self.assertAlmostEqual(result['calculation']['r_in_ohm'], 10.8 ** 2 / 12.0)
        self.assertIn('一阶', result['scope'])

    def test_esr_above_negative_input_impedance_fails(self):
        result = self.run_check(damping_check(esr_bulk_ohm={'min': 9.0, 'max': 12.0}))
        self.assertEqual(result['review_result'], 'FAIL')
        self.assertIn('不低于负输入阻抗', result['detail'])

    def test_esr_below_damping_floor_fails(self):
        result = self.run_check(damping_check(esr_bulk_ohm={'min': 1e-4, 'max': 2e-4}))
        self.assertEqual(result['review_result'], 'FAIL')
        self.assertIn('阻尼下界', result['detail'])

    def test_capacitance_ratio_below_project_rule_fails(self):
        result = self.run_check(damping_check(c_bulk_f={'min': 20e-6, 'max': 24e-6}))
        self.assertEqual(result['review_result'], 'FAIL')
        self.assertIn('比值', result['detail'])

    def test_missing_project_ratio_stays_insufficient(self):
        check = damping_check()
        check.pop('c_bulk_ratio_min')
        result = self.run_check(check)
        self.assertEqual(result['review_result'], 'INSUFFICIENT')
        self.assertIn('c_bulk_ratio_min 缺少保证值', result['detail'])

    def test_nonpositive_inputs_are_rejected_by_the_contract(self):
        evidence = {'schema_version': 1, 'checks': [damping_check(pin_max_w=0)]}
        self.assertTrue(any('pin_max_w 必须为正数' in error for error in validate_evidence(evidence)))


if __name__ == '__main__':
    unittest.main()
