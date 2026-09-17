import pathlib
import sys
import tempfile
import unittest

SCRIPTS = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))

from checkers.power_switch import PowerSwitchChecker, build_inventory, validate_power_switch_intent
from electrical_contract import db_fingerprint, validate_evidence
from electrical_fixtures import bind_evidence
from lint import Lint
from plan_review import build_review_plan
from test_inductive_load import add


def switch_board(driver='ic', pull=True, snubber=None, load='inductor'):
    """U2 驱动 Q1 低边开关，负载与吸收网络按参数变化。"""
    db = {'nets': {}, 'parts': {}, 'pin2net': {}, 'pinname': {}, 'pintype': {},
          'pseudo_nets': [], 'ref2page': {}}
    add(db, 'Q1', 'AO3400', [('1', 'G', 'GATE'), ('2', 'D', 'SW_OUT'), ('3', 'S', 'GND')])
    if driver == 'ic':
        add(db, 'U2', 'GATE-DRIVER', [('1', 'LO', 'DRV_OUT'), ('2', 'VDD', 'V12'),
                                      ('3', 'GND', 'GND')])
        add(db, 'R1', '10R', [('1', '1', 'DRV_OUT'), ('2', '2', 'GATE')])
    elif driver == 'connector':
        add(db, 'J3', 'HEADER-2', [('1', '1', 'GATE'), ('2', '2', 'GND')])
    if pull:
        add(db, 'R2', '100K', [('1', '1', 'GATE'), ('2', '2', 'GND')])
    if load == 'inductor':
        add(db, 'L2', 'IND-100UH', [('1', '1', 'SW_OUT'), ('2', '2', 'V12')])
    if snubber == 'rc':
        add(db, 'R3', '10R', [('1', '1', 'SW_OUT'), ('2', '2', 'SNUB')])
        add(db, 'C3', '1nF', [('1', '1', 'SNUB'), ('2', '2', 'GND')])
    elif snubber == 'tvs':
        add(db, 'D3', 'SMBJ30A', [('1', 'K', 'SW_OUT'), ('2', 'A', 'GND')])
    return db


def switches_of(inventory, state='as-built'):
    entry = next(x for x in inventory['states'] if x['id'] == state)
    return {item['ref']: item for item in entry['switches']}


def findings(db, intent=None):
    return [f for f in Lint(db, '', intent).run() if f['rule'].startswith('PS-')]


def gate_check(**overrides):
    check = {'id': 'GATE-DRIVE', 'rule': 'PS-10', 'kind': 'gate_drive', 'node': 'Q1.1',
             'channel': 'n', 'vgs_drive_v': {'min': 9.0, 'max': 12.6},
             'vgs_rds_on_v': 4.5, 'vgs_abs_v': {'min': -20.0, 'max': 20.0},
             'citation': 'Synthetic driver supply window and MOSFET guaranteed conditions'}
    check.update(overrides)
    return check


class RecognitionTest(unittest.TestCase):
    def test_driven_switch_with_pull_and_snubber_has_no_findings(self):
        db = switch_board(snubber='rc')
        switch = switches_of(build_inventory(db))['Q1']
        self.assertEqual(switch['topology'], 'low-side')
        self.assertEqual(switch['gate_net'], 'GATE')
        self.assertEqual([d['via'] for d in switch['drivers']], ['series:R1'])
        self.assertEqual([p['to'] for p in switch['gate_pulls']], ['GND'])
        self.assertEqual([s['type'] for s in switch['snubbers']], ['rc-snubber'])
        self.assertEqual(switch['switch_node_evidence'], ['inductive:L2'])
        self.assertEqual(findings(db), [])

    def test_floating_gate_reports_ps01(self):
        db = switch_board(driver=None, pull=False, snubber='rc')
        switch = switches_of(build_inventory(db))['Q1']
        self.assertEqual(switch['drivers'], [])
        hits = findings(db)
        self.assertEqual([f['rule'] for f in hits], ['PS-01'])
        self.assertIn('GATE', hits[0]['detail'])

    def test_gate_pull_alone_is_not_a_finding(self):
        self.assertEqual(findings(switch_board(driver=None, snubber='rc')), [])

    def test_connector_driven_gate_counts_as_a_driver(self):
        switch = switches_of(build_inventory(switch_board(driver='connector', pull=False,
                                                          snubber='rc')))['Q1']
        self.assertEqual([d['source'] for d in switch['drivers']], ['external'])

    def test_switch_node_without_damping_reports_ps02_candidate(self):
        hits = findings(switch_board())
        self.assertEqual([f['rule'] for f in hits], ['PS-02'])
        self.assertEqual(hits[0]['kind'], 'CANDIDATE')
        self.assertIn('inductive:L2', hits[0]['detail'])

    def test_clamp_counts_as_damping(self):
        self.assertEqual(findings(switch_board(snubber='tvs')), [])

    def test_freewheel_diode_across_the_load_counts_as_a_clamp(self):
        db = switch_board()
        add(db, 'D9', '1N4148', [('1', 'A', 'SW_OUT'), ('2', 'K', 'V12')])
        switch = switches_of(build_inventory(db))['Q1']
        self.assertEqual([s['ref'] for s in switch['snubbers']], ['D9'])
        self.assertEqual(findings(db), [])

    def test_resistive_load_without_damping_is_silent(self):
        self.assertEqual(findings(switch_board(load=None)), [])

    def test_half_bridge_midpoint_is_recognised(self):
        db = switch_board(load=None)
        add(db, 'Q2', 'AO3400', [('1', 'G', 'GATE_H'), ('2', 'D', 'V12'), ('3', 'S', 'SW_OUT')])
        add(db, 'R4', '100K', [('1', '1', 'GATE_H'), ('2', '2', 'SW_OUT')])
        switch = switches_of(build_inventory(db))['Q1']
        self.assertEqual(switch['switch_node_evidence'], ['half-bridge:Q2'])
        self.assertIn('PS-02', [f['rule'] for f in findings(db)])

    def test_unpopulated_switch_is_skipped(self):
        db = switch_board(snubber='rc')
        db['parts']['Q1']['nc'] = True
        self.assertEqual(switches_of(build_inventory(db)), {})

    def test_unknown_pin_roles_keep_a_gap_without_findings(self):
        db = switch_board(snubber='rc')
        for pin in ('1', '2', '3'):
            db['pinname'].pop('Q1.' + pin)
        switch = switches_of(build_inventory(db))['Q1']
        self.assertIn('pin-roles:Q1', switch['gaps'])
        self.assertIsNone(switch['gate_net'])
        self.assertEqual(findings(db), [])


class PlanTest(unittest.TestCase):
    def test_plan_items_bind_to_the_inventory_digest(self):
        db = switch_board()
        plan = build_review_plan(db)
        items = [x for x in plan['checks'] if x['object'].get('power_switch')]
        self.assertEqual({x['check'] for x in items},
                         {'power-switch-gate-drive-Q1-GATE', 'power-switch-soa-Q1-GATE',
                          'power-switch-node-damping-Q1-GATE'})
        for item in items:
            self.assertEqual(item['object']['power_switch_digest'], plan['power_switch']['digest'])
        self.assertEqual(plan['power_switch_version'], 1)
        rules = {row['rule']: row for row in plan['rule_plan']}
        self.assertEqual(rules['PS-01']['instances'], ['Q1-GATE'])
        self.assertEqual(rules['PS-10']['applicability'], 'APPLICABLE')
        self.assertEqual(rules['PS-10']['readiness'], 'WAITING_EVIDENCE')

    def test_damping_item_only_exists_with_a_switch_node(self):
        plan = build_review_plan(switch_board(load=None))
        self.assertNotIn('power-switch-node-damping-Q1-GATE',
                         {x['check'] for x in plan['checks']})

    def test_stale_object_binding_is_reported(self):
        db = switch_board()
        plan = build_review_plan(db)
        item = next(x for x in plan['checks'] if x['object'].get('power_switch'))
        stale = dict(item['object'], power_switch_digest='0' * 64)
        self.assertEqual(PowerSwitchChecker().object_errors('X', stale, plan['power_switch']),
                         ['X: stale power switch object binding'])

    def test_intent_validation_rejects_unsound_configuration(self):
        db = switch_board()
        base = {'schema_version': 1, 'db_sha256': db_fingerprint(db),
                'states': [{'id': 'run', 'citation': 'BOM Rev.A', 'population': {}}],
                'switches': [{'id': 'q1', 'ref': 'Q1', 'citation': 'driver schematic'}]}
        cases = {
            'schema_version must be 1': {'schema_version': 2},
            'switch ref unknown: QX': {'switches': [{'id': 'x', 'ref': 'QX', 'citation': 'y'}]},
            'switch needs a citation': {'switches': [{'id': 'x', 'ref': 'Q1'}]},
            'gate_net unknown: NOPE': {'switches': [{'id': 'x', 'ref': 'Q1', 'citation': 'y',
                                                     'gate_net': 'NOPE'}]},
        }
        self.assertEqual(validate_power_switch_intent({'power_switches': base}, db), [])
        for message, override in cases.items():
            errors = validate_power_switch_intent({'power_switches': dict(base, **override)}, db)
            self.assertTrue(any(message in error for error in errors), (message, errors))


class HotRuleTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)

    def run_check(self, check, db=None):
        db = db or switch_board(snubber='rc')
        evidence = {'schema_version': 1, 'checks': [check]}
        audit = bind_evidence(db, evidence, self.directory.name)
        self.assertEqual(validate_evidence(evidence), [])
        lint = Lint(db, evidence=evidence, datasheet_audit=audit)
        lint.run()
        results = [x for x in lint.results if x['check_id'] == check['id']]
        self.assertEqual(len(results), 1)
        return results[0]

    def test_sufficient_drive_passes(self):
        result = self.run_check(gate_check())
        self.assertEqual(result['review_result'], 'PASS')
        self.assertEqual(result['rule'], 'PS-10')
        self.assertIn('SOA', result['scope'])

    def test_drive_below_rds_on_condition_fails(self):
        result = self.run_check(gate_check(vgs_drive_v={'min': 3.0, 'max': 5.0}))
        self.assertEqual(result['review_result'], 'FAIL')
        self.assertIn('低于 RDS(on) 保证条件', result['detail'])

    def test_drive_above_absolute_maximum_fails(self):
        result = self.run_check(gate_check(vgs_drive_v={'min': 9.0, 'max': 25.0}))
        self.assertEqual(result['review_result'], 'FAIL')
        self.assertIn('超出绝对最大范围', result['detail'])

    def test_p_channel_is_compared_on_magnitude(self):
        result = self.run_check(gate_check(channel='p', vgs_drive_v={'min': -12.6, 'max': -9.0},
                                           vgs_rds_on_v=-4.5))
        self.assertEqual(result['review_result'], 'PASS')
        result = self.run_check(gate_check(channel='p', vgs_drive_v={'min': -3.0, 'max': -1.0},
                                           vgs_rds_on_v=-4.5))
        self.assertEqual(result['review_result'], 'FAIL')

    def test_missing_guaranteed_values_stay_insufficient(self):
        check = gate_check()
        check.pop('vgs_abs_v')
        result = self.run_check(check)
        self.assertEqual(result['review_result'], 'INSUFFICIENT')
        self.assertIn('vgs_abs_v 缺少保证 min/max', result['detail'])

    def test_malformed_evidence_is_rejected_by_the_contract(self):
        evidence = {'schema_version': 1, 'checks': [gate_check(channel='x')]}
        self.assertTrue(any('channel' in error for error in validate_evidence(evidence)))
        evidence = {'schema_version': 1, 'checks': [gate_check(kind='divider')]}
        self.assertTrue(any('kind' in error for error in validate_evidence(evidence)))


if __name__ == '__main__':
    unittest.main()
