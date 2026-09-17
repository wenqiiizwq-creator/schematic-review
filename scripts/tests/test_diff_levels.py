import pathlib
import sys
import tempfile
import unittest

SCRIPTS = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))

from checkers.diff_levels import DiffLevelsChecker, build_inventory, validate_diff_levels_intent
from electrical_contract import db_fingerprint, validate_evidence
from electrical_fixtures import bind_evidence
from lint import Lint
from plan_review import build_review_plan
from test_inductive_load import add


def link_board(coupling='ac', driver_dc=True, receiver_bias=True, part='LVPECL-CLOCK'):
    """U1 驱动 U2 的一对差分线，耦合与偏置按参数变化。"""
    db = {'nets': {}, 'parts': {}, 'pin2net': {}, 'pinname': {}, 'pintype': {},
          'pseudo_nets': [], 'ref2page': {}}
    tx_p, tx_n = ('CLK_TX_P', 'CLK_TX_N') if coupling == 'ac' else ('CLK_P', 'CLK_N')
    add(db, 'U1', part, [('1', 'OUTP', tx_p), ('2', 'OUTN', tx_n),
                         ('3', 'VDD', 'VCC_3V3'), ('4', 'GND', 'GND')])
    db['pintype']['U1.1'] = db['pintype']['U1.2'] = 'OUT'
    add(db, 'U2', 'RECEIVER', [('1', 'INP', 'CLK_P'), ('2', 'INN', 'CLK_N'),
                               ('3', 'VDD', 'VCC_3V3'), ('4', 'GND', 'GND')])
    db['pintype']['U2.1'] = db['pintype']['U2.2'] = 'IN'
    if coupling == 'ac':
        add(db, 'C1', '100nF', [('1', '1', tx_p), ('2', '2', 'CLK_P')])
        add(db, 'C2', '100nF', [('1', '1', tx_n), ('2', '2', 'CLK_N')])
        if driver_dc:
            add(db, 'R1', '150R', [('1', '1', tx_p), ('2', '2', 'GND')])
            add(db, 'R2', '150R', [('1', '1', tx_n), ('2', '2', 'GND')])
        if receiver_bias:
            add(db, 'R3', '130R', [('1', '1', 'CLK_P'), ('2', '2', 'VCC_3V3')])
            add(db, 'R4', '130R', [('1', '1', 'CLK_N'), ('2', '2', 'VCC_3V3')])
    else:
        add(db, 'R5', '100R', [('1', '1', 'CLK_P'), ('2', '2', 'CLK_N')])
    return db


def pairs_of(inventory, state='as-built'):
    entry = next(x for x in inventory['states'] if x['id'] == state)
    return {pair['base']: pair for pair in entry['pairs']}


def findings(db, intent=None):
    return [f for f in Lint(db, '', intent).run() if f['rule'].startswith('DL-')]


def declared_intent(db, standard='LVPECL'):
    """完整装配声明 + 电平标准出处，使该对成为 declared 依据。"""
    return {'diff_levels': {
        'schema_version': 1, 'db_sha256': db_fingerprint(db),
        'states': [{'id': 'run', 'citation': 'BOM Rev.A',
                    'population': {ref: True for ref in db['parts']}}],
        'pairs': [{'id': 'CLK-TX', 'ref': 'U1', 'standard': standard,
                   'citation': 'clock tree level definition'}]}}


def level_check(**overrides):
    check = {'id': 'LEVELS', 'rule': 'DL-10', 'kind': 'diff_level', 'ref': 'U2',
             'net': 'CLK_P', 'coupling': 'dc',
             'driver_common_mode_v': {'min': 1.1, 'max': 1.3},
             'driver_swing_v': {'min': 0.25, 'max': 0.45},
             'receiver_common_mode_v': {'min': 0.3, 'max': 2.2},
             'receiver_input_diff_v': {'min': 0.1, 'max': 1.0},
             'citation': 'Synthetic driver and receiver guaranteed level ranges'}
    check.update(overrides)
    return check


class RecognitionTest(unittest.TestCase):
    def test_ac_coupled_link_is_tracked_as_two_pairs(self):
        pairs = pairs_of(build_inventory(link_board()))
        self.assertEqual(sorted(pairs), ['CLK', 'CLK_TX'])
        driver = pairs['CLK_TX']
        self.assertEqual(driver['coupling'], 'ac')
        self.assertEqual(driver['standard'], 'LVPECL')
        self.assertEqual(driver['basis'], 'name-hint')
        self.assertEqual([leg['direction'] for leg in driver['legs']], ['driver', 'driver'])
        self.assertEqual(driver['legs'][0]['dc_path'], ['GND'])
        self.assertEqual([leg['direction'] for leg in pairs['CLK']['legs']],
                         ['receiver', 'receiver'])

    def test_dc_coupled_pair_with_termination_is_silent(self):
        db = link_board(coupling='dc')
        pair = pairs_of(build_inventory(db))['CLK']
        self.assertEqual(pair['coupling'], 'dc')
        self.assertEqual(pair['legs'][0]['differential_terminations'], ['R5'])
        self.assertEqual(findings(db), [])

    def test_missing_driver_dc_path_reports_dl01(self):
        db = link_board(driver_dc=False)
        hits = [f for f in findings(db) if f['rule'] == 'DL-01']
        self.assertEqual(len(hits), 2)  # 每条腿各一条
        self.assertEqual(hits[0]['kind'], 'CANDIDATE')
        self.assertIn('直流通路', hits[0]['detail'])

    def test_declared_standard_makes_dl01_a_finding(self):
        db = link_board(driver_dc=False)
        hits = [f for f in findings(db, declared_intent(db)) if f['rule'] == 'DL-01']
        self.assertEqual({f['kind'] for f in hits}, {'FINDING'})

    def test_missing_receiver_bias_reports_dl02(self):
        db = link_board(receiver_bias=False)
        hits = [f for f in findings(db) if f['rule'] == 'DL-02']
        self.assertEqual(len(hits), 2)
        self.assertEqual(hits[0]['kind'], 'CANDIDATE')

    def test_unknown_standard_produces_no_dl01(self):
        db = link_board(driver_dc=False, receiver_bias=False, part='GENERIC-BUFFER')
        pair = pairs_of(build_inventory(db))['CLK_TX']
        self.assertIsNone(pair['standard'])
        self.assertIn('level-standard:CLK_TX', pair['gaps'])
        self.assertEqual({f['rule'] for f in findings(db)}, {'DL-02'})

    def test_receiver_side_without_a_standard_keeps_a_gap(self):
        pair = pairs_of(build_inventory(link_board()))['CLK']
        self.assertIn('level-standard:CLK', pair['gaps'])

    def test_nets_without_a_shared_device_are_not_a_diff_pair(self):
        db = link_board(coupling='dc')
        add(db, 'U8', 'SENSOR', [('1', 'OUT', 'TEMP_P'), ('2', 'GND', 'GND')])
        add(db, 'U9', 'SENSOR', [('1', 'OUT', 'TEMP_N'), ('2', 'GND', 'GND')])
        self.assertNotIn('TEMP', pairs_of(build_inventory(db)))


class PlanTest(unittest.TestCase):
    def test_name_hint_pairs_stay_undetermined(self):
        plan = build_review_plan(link_board())
        items = [x for x in plan['checks'] if x['object'].get('diff_pair')]
        self.assertEqual({x['check'] for x in items},
                         {'diff-level-compatibility-CLK-TX', 'diff-level-termination-CLK-TX',
                          'diff-level-compatibility-CLK', 'diff-level-termination-CLK'})
        self.assertEqual({x['applicability'] for x in items}, {'UNDETERMINED'})
        self.assertEqual(plan['diff_levels_version'], 1)

    def test_declared_pairs_are_applicable(self):
        db = link_board()
        plan = build_review_plan(db, declared_intent(db))
        items = [x for x in plan['checks'] if x['object'].get('diff_pair') == 'CLK-TX']
        self.assertEqual({x['applicability'] for x in items}, {'APPLICABLE'})

    def test_stale_object_binding_is_reported(self):
        plan = build_review_plan(link_board())
        item = next(x for x in plan['checks'] if x['object'].get('diff_pair'))
        stale = dict(item['object'], diff_levels_digest='0' * 64)
        self.assertEqual(DiffLevelsChecker().object_errors('X', stale, plan['diff_levels']),
                         ['X: stale diff level object binding'])

    def test_intent_validation_rejects_unknown_nets(self):
        db = link_board()
        section = {'schema_version': 1, 'db_sha256': db_fingerprint(db),
                   'states': [{'id': 'run', 'citation': 'BOM Rev.A', 'population': {}}],
                   'pairs': [{'id': 'clk', 'ref': 'U1', 'citation': 'level definition',
                              'p_net': 'NOPE'}]}
        errors = validate_diff_levels_intent({'diff_levels': section}, db)
        self.assertTrue(any('p_net unknown: NOPE' in error for error in errors), errors)


class HotRuleTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)

    def run_check(self, check, db=None):
        db = db or link_board(coupling='dc')
        evidence = {'schema_version': 1, 'checks': [check]}
        audit = bind_evidence(db, evidence, self.directory.name)
        self.assertEqual(validate_evidence(evidence), [])
        lint = Lint(db, evidence=evidence, datasheet_audit=audit)
        lint.run()
        results = [x for x in lint.results if x['check_id'] == check['id']]
        self.assertEqual(len(results), 1)
        return results[0]

    def test_compatible_dc_link_passes(self):
        result = self.run_check(level_check())
        self.assertEqual(result['review_result'], 'PASS')
        self.assertEqual(result['calculation']['common_mode_source'], 'driver_common_mode_v')

    def test_common_mode_outside_receiver_range_fails(self):
        result = self.run_check(level_check(driver_common_mode_v={'min': 2.4, 'max': 2.6}))
        self.assertEqual(result['review_result'], 'FAIL')
        self.assertIn('共模', result['detail'])

    def test_swing_outside_receiver_range_fails(self):
        result = self.run_check(level_check(driver_swing_v={'min': 1.2, 'max': 1.6}))
        self.assertEqual(result['review_result'], 'FAIL')
        self.assertIn('差分摆幅', result['detail'])

    def test_ac_coupling_uses_the_biased_common_mode(self):
        check = level_check(coupling='ac', bias_common_mode_v={'min': 1.9, 'max': 2.1})
        result = self.run_check(check, db=link_board())
        self.assertEqual(result['review_result'], 'PASS')
        self.assertEqual(result['calculation']['common_mode_source'], 'bias_common_mode_v')

    def test_ac_coupling_without_bias_range_stays_insufficient(self):
        result = self.run_check(level_check(coupling='ac'), db=link_board())
        self.assertEqual(result['review_result'], 'INSUFFICIENT')
        self.assertIn('bias_common_mode_v 缺少保证 min/max', result['detail'])


if __name__ == '__main__':
    unittest.main()
