"""Native regression tests for the bounded linear-feedback fallback."""
import copy
import pathlib
import sys
import unittest
from fractions import Fraction

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from solve_dividers import Solver, linear_feedback_window
from test_solve_dividers import divider_db
from electrical_fixtures import divider_model
from electrical_contract import dependency_refs, model_gaps


def shared_stem():
    # R1 + (R3 || R4) = 5k + 5k = 10k; R2 = 10k.
    # Gain=1/2 and Rth=5k, independently reducible by hand.
    db = divider_db()
    db['parts']['R1']['value'] = '5K/0%'
    db['parts']['R2']['value'] = '10K/0%'
    db['parts'].update({'R3': {'value': '10K/0%'}, 'R4': {'value': '10K/0%'}})
    db['nets']['FB_NET'] = ['U1.1', 'R3.2', 'R4.2', 'R2.1']
    db['nets']['MID'] = ['R1.2', 'R3.1', 'R4.1']
    db['pin2net'] = {p: n for n, pins in db['nets'].items() for p in pins}
    return db


class LinearFeedbackTests(unittest.TestCase):
    def network(self, db=None):
        return Solver(db or shared_stem(), model=divider_model()).linear_network('FB_NET')

    def test_shared_stem_closed_form_and_bias_sign(self):
        network = self.network()
        voltage = {'min': .8, 'typ': .8, 'max': .8}
        result = linear_feedback_window(network, voltage, {'min': -1e-6, 'max': 1e-6})
        self.assertAlmostEqual(result['typ'], 1.6)
        self.assertAlmostEqual(result['min'], 1.59)
        self.assertAlmostEqual(result['max'], 1.61)
        self.assertEqual(result['corner_count'], 2)
        self.assertEqual(len(result['resistors']), 4)
        self.assertEqual(set(result['extreme_corners']['max']['resistance_ohm']), {'R1', 'R2', 'R3', 'R4'})

    def test_uniform_resistance_scaling_only_scales_bias_effect(self):
        original = self.network()
        scaled = copy.deepcopy(original)
        for r in scaled['resistors']:
            r['ohm'] *= 3
            r['ohm_exact'] = str(Fraction(r['ohm_exact']) * 3)
        result = linear_feedback_window(scaled, {'min': .8, 'typ': .8, 'max': .8},
                                        {'min': 1e-6, 'max': 1e-6})
        self.assertAlmostEqual(result['typ'], 1.6)
        self.assertAlmostEqual(result['min'], 1.63)
        self.assertAlmostEqual(result['max'], 1.63)

    def test_all_combinations_and_corner_evidence(self):
        network = self.network()
        for r in network['resistors']:
            r['tol'] = .01
            r['tol_exact'] = '1/100'
        result = linear_feedback_window(network, {'min': .792, 'typ': .8, 'max': .808},
                                        {'min': -1e-6, 'max': 1e-6})
        self.assertEqual(result['corner_count'], 64)
        self.assertAlmostEqual(result['min'], .792 * (1 + 9900 / 10100) - .0099)
        self.assertAlmostEqual(result['max'], .808 * (1 + 10100 / 9900) + .0101)
        self.assertEqual(result['extreme_corners']['min']['value_exact'], result['bounds_exact']['min'])
        self.assertLessEqual(Fraction.from_float(result['min']), Fraction(result['bounds_exact']['min']))
        self.assertGreaterEqual(Fraction.from_float(result['max']), Fraction(result['bounds_exact']['max']))

    def test_missing_tolerance_and_limits_reject(self):
        network = self.network()
        network['resistors'][0]['tol'] = None
        with self.assertRaises(ValueError):
            linear_feedback_window(network, {'min': .8, 'typ': .8, 'max': .8},
                                   {'min': 0, 'max': 0})
        for ref in (None, {'min': .8, 'typ': .8}, {'min': .8, 'typ': .7, 'max': .9}):
            with self.assertRaises(ValueError):
                linear_feedback_window(self.network(), ref, {'min': 0, 'max': 0})

    def test_endpoints_must_exist_and_be_distinct(self):
        for source, reference in (('FB_NET', 'GND'), ('GND', 'GND'), ('MISSING', 'GND')):
            with self.assertRaises(ValueError):
                Solver(shared_stem(), model=divider_model(source, reference)).linear_network('FB_NET')

    def test_pseudo_net_and_extra_pin_reject(self):
        db = shared_stem()
        db['pseudo_nets'] = ['MID']
        with self.assertRaises(ValueError):
            self.network(db)
        db = shared_stem()
        db['nets']['MID'].append('R1.3')
        db['pin2net']['R1.3'] = 'MID'
        with self.assertRaises(ValueError):
            self.network(db)

    def test_zero_resistance_and_resource_limit_reject(self):
        db = shared_stem()
        db['parts']['R1']['value'] = '0R'
        with self.assertRaises(ValueError):
            self.network(db)
        network = self.network()
        network['resistors'] *= 6
        with self.assertRaises(ValueError):
            linear_feedback_window(network, {'min': .8, 'typ': .8, 'max': .8}, {'min': 0, 'max': 0})

    def test_variable_resistor_budget_separate_from_edge_budget(self):
        network = self.network()
        seed = network['resistors'][0]
        network['resistors'] = [dict(seed, ref='R%d' % i, tol=.01, tol_exact='1/100') for i in range(11)]
        with self.assertRaisesRegex(ValueError, '非零公差'):
            linear_feedback_window(network, {'min': .8, 'typ': .8, 'max': .8}, {'min': 0, 'max': 0})

    def test_malformed_endpoint_stays_a_model_gap(self):
        for source in (['VOUT_3V3'], {'net': 'VOUT_3V3'}):
            check = {'rule': 'Rule-08', 'net': 'FB_NET', 'divider_model': divider_model(source)}
            self.assertTrue(model_gaps(check))
            self.assertIn('U1', dependency_refs(shared_stem(), check))
            with self.assertRaises(ValueError):
                Solver(shared_stem(), model=check['divider_model']).linear_network('FB_NET')

    def test_numerically_unreliable_network_rejects(self):
        network = self.network()
        network['resistors'][0]['ohm'] = 1e-12
        network['resistors'][0]['ohm_exact'] = '1/1000000000000'
        with self.assertRaises(ValueError):
            linear_feedback_window(network, {'min': .8, 'typ': .8, 'max': .8}, {'min': 0, 'max': 0})

    def test_original_decimal_survives_kohm_conversion(self):
        db = shared_stem()
        db['parts']['R1']['value'] = '0.9R/0.7%'
        resistor = next(r for r in self.network(db)['resistors'] if r['ref'] == 'R1')
        self.assertEqual(resistor['ohm_exact'], '9/10')
        self.assertEqual(resistor['tol_exact'], '7/1000')

    def test_near_conditioning_boundary_uses_exact_arithmetic(self):
        network = {'source_net': 'SOURCE', 'reference_net': 'GND', 'fbnet': 'FB',
                   'nets': ['SOURCE', 'MID', 'FB', 'GND'],
                   'resistors': [
                       {'ref': 'R1', 'nets': ['SOURCE', 'MID'], 'ohm': 1e10, 'tol': 0},
                       {'ref': 'R2', 'nets': ['MID', 'FB'], 'ohm': 1, 'tol': 0},
                       {'ref': 'R3', 'nets': ['MID', 'GND'], 'ohm': 1e10, 'tol': 0},
                       {'ref': 'R4', 'nets': ['FB', 'GND'], 'ohm': 1e10, 'tol': 0}]}
        result = linear_feedback_window(network, {'min': .8, 'typ': .8, 'max': .8}, {'min': 0, 'max': 0})
        self.assertEqual(Fraction(result['bounds_exact']['min']), Fraction(15000000001, 6250000000))
        self.assertEqual(result['bounds_exact']['min'], result['bounds_exact']['max'])

    def test_cli_exploration_does_not_gain_release_authority(self):
        self.assertEqual(Solver(shared_stem()).solve_net('FB_NET')['status'], 'ambiguous')


if __name__ == '__main__':
    unittest.main()
