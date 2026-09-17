import copy
import pathlib
import sys
import unittest

SCRIPTS = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))

from checkers.inductive_load import InductiveLoadChecker, build_inventory, validate_inductive_intent
from electrical_contract import db_fingerprint
from lint import Lint
from plan_review import build_review_plan


def add(db, ref, part, pins, nc=False):
    db['parts'][ref] = {'part': part, 'value': part, 'prim': part, 'jedec': 'X', 'nc': nc}
    for pin, name, net in pins:
        node = ref + '.' + pin
        db['nets'].setdefault(net, []).append(node)
        db['pin2net'][node] = net
        if name:
            db['pinname'][node] = name


def relay_board(clamp='forward', clamp_nc=False):
    """K1 线圈由 Q1 低边驱动，按参数决定钳位形态。"""
    db = {'nets': {}, 'parts': {}, 'pin2net': {}, 'pinname': {}, 'pseudo_nets': []}
    add(db, 'K1', 'RELAY-5V', [('1', 'COIL1', 'RELAY_DRV'), ('2', 'COIL2', 'V24')])
    add(db, 'Q1', 'AO3400', [('1', 'G', 'RELAY_EN'), ('2', 'D', 'RELAY_DRV'), ('3', 'S', 'GND')])
    if clamp == 'forward':
        add(db, 'D1', '1N4148', [('1', 'A', 'RELAY_DRV'), ('2', 'K', 'V24')], nc=clamp_nc)
    elif clamp == 'reversed':
        add(db, 'D1', '1N4148', [('1', 'K', 'RELAY_DRV'), ('2', 'A', 'V24')], nc=clamp_nc)
    elif clamp == 'rc':
        add(db, 'R9', 'RES', [('1', '1', 'RELAY_DRV'), ('2', '2', 'SNUB_MID')])
        add(db, 'C9', 'CAP', [('1', '1', 'SNUB_MID'), ('2', '2', 'V24')])
    elif clamp == 'unknown-roles':
        add(db, 'D1', '1N4148', [('1', '1', 'RELAY_DRV'), ('2', '2', 'V24')])
    return db


def converter_board():
    """开关电源储能电感：SW 引脚命中，不属于感性负载。"""
    db = {'nets': {}, 'parts': {}, 'pin2net': {}, 'pinname': {}, 'pseudo_nets': []}
    add(db, 'U5', 'BUCK-X', [('1', 'SW', 'SW_NODE'), ('2', 'VIN', 'V12'), ('3', 'GND', 'GND')])
    add(db, 'L1', 'IND-10UH', [('1', '1', 'SW_NODE'), ('2', '2', 'VCC_3V3')])
    add(db, 'Q2', 'AO3400', [('1', 'G', 'PWM'), ('2', 'D', 'SW_NODE'), ('3', 'S', 'GND')])
    return db


def loads_of(inventory, state='as-built'):
    entry = next(x for x in inventory['states'] if x['id'] == state)
    return {load['ref']: load for load in entry['loads']}


def findings(db, intent=None):
    return [f for f in Lint(db, '', intent).run() if f['rule'].startswith('IL-')]


class RecognitionTest(unittest.TestCase):
    def test_relay_with_flyback_diode_is_recognised_without_finding(self):
        db = relay_board()
        load = loads_of(build_inventory(db))['K1']
        self.assertEqual(load['basis'], 'topology')
        self.assertEqual(load['switch_net'], 'RELAY_DRV')
        self.assertEqual(load['rail_net'], 'V24')
        self.assertEqual(load['switch_ref'], 'Q1')
        self.assertEqual([c['type'] for c in load['clamps']], ['freewheel-diode'])
        self.assertEqual(load['clamps'][0]['orientation'], 'forward')
        self.assertEqual(findings(db), [])

    def test_missing_clamp_reports_il01(self):
        db = relay_board(clamp=None)
        load = loads_of(build_inventory(db))['K1']
        self.assertEqual(load['clamps'], [])
        self.assertIn('clamp:none-found', load['gaps'])
        hits = findings(db)
        self.assertEqual([f['rule'] for f in hits], ['IL-01'])
        self.assertIn('RELAY_DRV', hits[0]['detail'])

    def test_reversed_diode_reports_il02(self):
        hits = findings(relay_board(clamp='reversed'))
        self.assertEqual([f['rule'] for f in hits], ['IL-02'])

    def test_rc_snubber_counts_as_clamp(self):
        db = relay_board(clamp='rc')
        load = loads_of(build_inventory(db))['K1']
        self.assertEqual([c['type'] for c in load['clamps']], ['rc-snubber'])
        self.assertEqual(findings(db), [])

    def test_unplaced_clamp_is_reported_for_that_state(self):
        db = relay_board(clamp_nc=True)
        load = loads_of(build_inventory(db))['K1']
        self.assertEqual(load['clamps'], [])
        self.assertEqual([f['rule'] for f in findings(db)], ['IL-01'])

    def test_unknown_pin_roles_do_not_manufacture_orientation(self):
        db = relay_board(clamp='unknown-roles')
        load = loads_of(build_inventory(db))['K1']
        self.assertEqual(load['clamps'][0]['orientation'], 'unknown')
        self.assertIn('clamp-orientation:pin roles unknown', load['gaps'])
        self.assertEqual(findings(db), [])

    def test_converter_inductor_is_not_an_inductive_load(self):
        db = converter_board()
        self.assertEqual(loads_of(build_inventory(db)), {})
        self.assertEqual(findings(db), [])

    def test_external_load_stays_a_candidate_without_intent(self):
        db = relay_board(clamp=None)
        db['parts'].pop('K1')
        for node in ('K1.1', 'K1.2'):
            net = db['pin2net'].pop(node)
            db['nets'][net].remove(node)
            db['pinname'].pop(node, None)
        add(db, 'J7', 'CONN-2P', [('1', 'MOTOR+', 'MOTOR_DRV'), ('2', 'RTN', 'V24')])
        db['nets']['MOTOR_DRV'] = db['nets'].pop('RELAY_DRV') + db['nets']['MOTOR_DRV']
        for node in list(db['pin2net']):
            if db['pin2net'][node] == 'RELAY_DRV':
                db['pin2net'][node] = 'MOTOR_DRV'
        inventory = build_inventory(db)
        load = loads_of(inventory)['J7']
        self.assertEqual(load['basis'], 'name-hint')
        self.assertEqual(findings(db), [])
        plan = build_review_plan(db)
        item = next(x for x in plan['checks'] if x['check'].startswith('inductive-load-clamp-topology'))
        self.assertEqual(item['applicability'], 'UNDETERMINED')


class IntentTest(unittest.TestCase):
    def intent(self, db, **overrides):
        cfg = {'schema_version': 1, 'db_sha256': db_fingerprint(db),
               'states': [{'id': 'run', 'citation': 'synthetic BOM option A',
                           'population': {ref: True for ref in db['parts']}}],
               'loads': [{'id': 'K1-COIL', 'ref': 'K1', 'kind': 'relay',
                          'citation': 'synthetic relay coil spec'}]}
        cfg.update(overrides)
        return {'inductive_loads': cfg}

    def test_declared_state_and_load_upgrade_basis(self):
        db = relay_board()
        inventory = build_inventory(db, self.intent(db))
        load = loads_of(inventory, 'run')['K1']
        self.assertEqual(load['basis'], 'declared')
        self.assertEqual(load['gaps'], [])

    def test_undeclared_population_stays_a_gap(self):
        db = relay_board()
        intent = self.intent(db)
        del intent['inductive_loads']['states'][0]['population']['D1']
        load = loads_of(build_inventory(db, intent), 'run')['K1']
        self.assertIn('population:D1', load['gaps'])
        self.assertIn('clamp:none-found', load['gaps'])

    def test_exclusion_removes_the_candidate(self):
        db = relay_board(clamp=None)
        intent = self.intent(db)
        intent['inductive_loads']['exclusions'] = [
            {'ref': 'K1', 'citation': 'synthetic: coil driven externally, reviewed separately'}]
        self.assertEqual(loads_of(build_inventory(db, intent), 'run'), {})

    def test_intent_validation_rejects_unsound_configuration(self):
        db = relay_board()
        cases = {
            'schema_version must be 1': {'schema_version': 2},
            'state needs assembly/configuration citation':
                {'states': [{'id': 'run', 'citation': '', 'population': {}}]},
            'load ref unknown: QX': {'loads': [{'id': 'x', 'ref': 'QX', 'citation': 'y'}]},
            'load needs a citation': {'loads': [{'id': 'x', 'ref': 'K1'}]},
        }
        for message, override in cases.items():
            errors = validate_inductive_intent(self.intent(db, **override), db)
            self.assertTrue(any(message in error for error in errors), (message, errors))

    def test_stale_fingerprint_is_rejected(self):
        db = relay_board()
        intent = self.intent(db)
        intent['inductive_loads']['db_sha256'] = '0' * 64
        self.assertIn('inductive_loads: stale db_sha256', validate_inductive_intent(intent, db))


class PlanBindingTest(unittest.TestCase):
    def test_plan_items_bind_to_the_inventory_digest(self):
        db = relay_board()
        plan = build_review_plan(db)
        items = [x for x in plan['checks'] if x['object'].get('inductive_load')]
        self.assertEqual(len(items), 2)
        for item in items:
            self.assertEqual(item['object']['inductive_load_digest'], plan['inductive_load']['digest'])
        self.assertEqual(plan['inductive_load_version'], 1)

    def test_checker_reports_stale_object_binding(self):
        db = relay_board()
        plan = build_review_plan(db)
        checker = InductiveLoadChecker()
        item = next(x for x in plan['checks'] if x['object'].get('inductive_load'))
        stale = copy.deepcopy(item['object'])
        stale['inductive_load_digest'] = '0' * 64
        self.assertEqual(checker.object_errors('X', stale, plan['inductive_load']),
                         ['X: stale inductive load object binding'])

    def test_rule_plan_lists_both_cold_rules(self):
        plan = build_review_plan(relay_board())
        rules = {row['rule']: row for row in plan['rule_plan']}
        self.assertIn('IL-01', rules)
        self.assertIn('IL-02', rules)
        self.assertEqual(rules['IL-01']['instances'], ['K1-RELAY-DRV'])


if __name__ == '__main__':
    unittest.main()
