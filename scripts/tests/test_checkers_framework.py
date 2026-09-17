import copy
import pathlib
import re
import sys
import unittest

SCRIPTS = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))

import test_inductive_load as relay_fixture
from checkers import REGISTRY, REGISTRY_BY_ID, registry_cold_rules, validate_inventories
from checkers import netgraph as ng
from checkers import powertree
from checkers.supervision import build_inventory as supervision_inventory
from lint import Lint
from checkers import states as state_lib
from plan_review import ReviewPlanner, build_review_plan


def whole_board():
    """一块同时命中全部检查器的合成板，用于框架级契约测试。"""
    db = {'nets': {}, 'parts': {}, 'pin2net': {}, 'pinname': {}, 'pintype': {},
          'pseudo_nets': [], 'ref2page': {}}
    add = relay_fixture.add
    # 继电器 + 低边开关 + 续流二极管：inductive_load、power_switch
    add(db, 'K1', 'RELAY-24V', [('1', 'COIL1', 'RELAY_DRV'), ('2', 'COIL2', 'V24')])
    add(db, 'D1', '1N4148', [('1', 'A', 'RELAY_DRV'), ('2', 'K', 'V24')])
    add(db, 'Q1', 'AO3400', [('1', 'G', 'RELAY_EN'), ('2', 'D', 'RELAY_DRV'), ('3', 'S', 'GND')])
    add(db, 'R1', '100K', [('1', '1', 'RELAY_EN'), ('2', '2', 'GND')])
    # 降压变换器 + 输入磁珠：input_filter
    add(db, 'U1', 'BUCK-3A', [('1', 'VIN', 'VIN_12'), ('2', 'SW', 'SW_NODE'),
                              ('3', 'GND', 'GND'), ('4', 'EN', 'EN_12V')])
    add(db, 'L1', 'IND-4U7', [('1', '1', 'SW_NODE'), ('2', '2', 'VCC_5V')])
    add(db, 'FB1', 'BLM21', [('1', '1', 'VIN_12'), ('2', '2', 'V12_RAW')])
    add(db, 'C1', '10uF', [('1', '1', 'VIN_12'), ('2', '2', 'GND')])
    add(db, 'R2', '100K', [('1', '1', 'V12_RAW'), ('2', '2', 'EN_12V')])
    add(db, 'R3', '22K', [('1', '1', 'EN_12V'), ('2', '2', 'GND')])
    # LDO + 使能分压：power_up
    add(db, 'U2', 'LDO-3V3', [('1', 'VIN', 'VCC_5V'), ('2', 'EN', 'EN_3V3'),
                              ('3', 'VOUT', 'VCC_3V3'), ('4', 'GND', 'GND')])
    add(db, 'R4', '100K', [('1', '1', 'VCC_5V'), ('2', '2', 'EN_3V3')])
    add(db, 'R5', '22K', [('1', '1', 'EN_3V3'), ('2', '2', 'GND')])
    # 监控器 + 复位链：supervision
    add(db, 'U3', 'SUPERVISOR', [('1', 'VCC', 'VCC_3V3'), ('2', 'SENSE', 'SENSE_3V3'),
                                 ('3', 'WDI', 'WDI_NET'), ('4', 'RESET', 'SYS_RST_N'),
                                 ('5', 'GND', 'GND')])
    add(db, 'R6', '100K', [('1', '1', 'VCC_3V3'), ('2', '2', 'SENSE_3V3')])
    add(db, 'R7', '47K', [('1', '1', 'SENSE_3V3'), ('2', '2', 'GND')])
    add(db, 'R8', '10K', [('1', '1', 'SYS_RST_N'), ('2', '2', 'VCC_3V3')])
    # 主控：I²C、去耦、复位输入、差分发送
    add(db, 'U4', 'SOC', [('1', 'VDD', 'VCC_3V3'), ('2', 'VSS', 'GND'),
                          ('3', 'SDA', 'I2C_SDA'), ('4', 'SCL', 'I2C_SCL'),
                          ('5', 'NRST', 'SYS_RST_N'), ('6', 'GPIO1', 'WDI_NET'),
                          ('7', 'OUTP', 'LVDS_TX_P'), ('8', 'OUTN', 'LVDS_TX_N')])
    db['pintype'].update({'U4.6': 'OUT', 'U4.7': 'OUT', 'U4.8': 'OUT'})
    add(db, 'C2', '100nF', [('1', '1', 'VCC_3V3'), ('2', '2', 'GND')])
    add(db, 'R9', '4.7K', [('1', '1', 'I2C_SDA'), ('2', '2', 'VCC_3V3')])
    add(db, 'R10', '4.7K', [('1', '1', 'I2C_SCL'), ('2', '2', 'VCC_3V3')])
    add(db, 'U5', 'EEPROM', [('1', 'SDA', 'I2C_SDA'), ('2', 'SCL', 'I2C_SCL'),
                             ('3', 'VCC', 'VCC_3V3'), ('4', 'VSS', 'GND')])
    # LVDS 差分接收：diff_levels
    add(db, 'U6', 'LVDS-RX', [('1', 'INP', 'LVDS_TX_P'), ('2', 'INN', 'LVDS_TX_N'),
                              ('3', 'VDD', 'VCC_3V3'), ('4', 'GND', 'GND')])
    db['pintype'].update({'U6.1': 'IN', 'U6.2': 'IN'})
    add(db, 'R11', '100R', [('1', '1', 'LVDS_TX_P'), ('2', '2', 'LVDS_TX_N')])
    # 光耦：optocoupler
    add(db, 'OK1', 'PC817', [('1', 'ANODE', 'LED_A'), ('2', 'CATHODE', 'OPTO_DRV'),
                             ('3', 'EMITTER', 'GND'), ('4', 'COLLECTOR', 'OPTO_OUT')])
    add(db, 'R12', '1K', [('1', '1', 'VCC_5V'), ('2', '2', 'LED_A')])
    add(db, 'R13', '10K', [('1', '1', 'OPTO_OUT'), ('2', '2', 'VCC_3V3')])
    return db


def run_validation(plan, db, items=None):
    errors = []
    expected = {item['id']: item for item in (items if items is not None else plan['checks'])}
    gates = validate_inventories(REGISTRY, plan, expected, db, lambda ok, msg: None if ok else errors.append(msg),
                                 lambda context: ReviewPlanner(db, context))
    return errors, gates


class RegistryTest(unittest.TestCase):
    def test_ids_and_plan_keys_are_unique(self):
        ids = [checker.id for checker in REGISTRY]
        keys = [checker.plan_key for checker in REGISTRY if checker.plan_key]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(len(keys), len(set(keys)))
        self.assertEqual(set(ids), set(REGISTRY_BY_ID))

    def test_cold_rule_numbers_do_not_collide_with_legacy_rules(self):
        for rule in registry_cold_rules():
            self.assertFalse(rule.startswith('Rule-'), rule)

    def test_every_checker_writes_its_inventory_into_the_plan(self):
        plan = build_review_plan(relay_fixture.relay_board())
        for checker in REGISTRY:
            self.assertIn(checker.plan_key, plan)
            if checker.version_key:
                self.assertEqual(plan[checker.version_key], checker.version)


class GenericValidationTest(unittest.TestCase):
    def setUp(self):
        self.db = relay_fixture.relay_board()
        self.plan = build_review_plan(self.db)
        self.bound = [x for x in self.plan['checks'] if x['object'].get('inductive_load')]

    def test_clean_plan_validates(self):
        errors, _ = run_validation(self.plan, self.db)
        self.assertEqual(errors, [])

    def test_missing_inventory_is_rejected_when_items_are_bound(self):
        plan = copy.deepcopy(self.plan)
        del plan['inductive_load']
        errors, _ = run_validation(plan, self.db)
        self.assertIn('inductive load checks require their inventory', errors)

    def test_modified_inventory_is_detected(self):
        plan = copy.deepcopy(self.plan)
        plan['inductive_load']['digest'] = '0' * 64
        errors, _ = run_validation(plan, self.db)
        self.assertIn('inductive load inventory/binding is stale or modified', errors)

    def test_edited_criterion_is_detected(self):
        plan = copy.deepcopy(self.plan)
        target = next(x for x in plan['checks'] if x['object'].get('inductive_load'))
        target['criterion'] = '改写过的判据'
        errors, _ = run_validation(plan, self.db)
        self.assertIn(target['id'] + ': inductive load generated criterion/object changed', errors)

    def test_dropped_generated_check_is_detected(self):
        plan = copy.deepcopy(self.plan)
        dropped = next(x for x in plan['checks'] if x['object'].get('inductive_load'))
        remaining = [x for x in plan['checks'] if x['id'] != dropped['id']]
        errors, _ = run_validation(plan, self.db, items=remaining)
        self.assertIn(dropped['id'] + ': incomplete inductive load planned coverage', errors)

    def test_open_gaps_block_pass(self):
        _, gates = run_validation(self.plan, self.db)
        blocked = gates[self.bound[0]['id']]
        self.assertEqual(blocked, [self.bound[0]['id'] +
                                   ': inductive load gaps must be resolved in a regenerated plan before PASS'])

    def test_version_mismatch_is_rejected(self):
        plan = copy.deepcopy(self.plan)
        plan['decoupling_version'] = 2
        errors, _ = run_validation(plan, self.db)
        self.assertIn('decoupling_version must be 1', errors)


class DecouplingBindingTest(unittest.TestCase):
    def test_manual_item_may_not_reuse_a_generated_decoupling_object(self):
        import test_decoupling
        db, intent = test_decoupling.fixture()
        plan = build_review_plan(db, intent)
        generated = next(x for x in plan['checks'] if x['object'].get('decoupling_group'))
        manual = copy.deepcopy(generated)
        manual['id'] = generated['id'] + '.manual'
        errors, _ = run_validation(plan, db, items=plan['checks'] + [manual])
        self.assertIn(manual['id'] + ': use an independent object for manual decoupling additions', errors)


class NetGraphTest(unittest.TestCase):
    def setUp(self):
        self.graph = ng.NetGraph(relay_fixture.relay_board())

    def test_classification_prefers_part_keyword_over_prefix(self):
        self.assertEqual(self.graph.kind('Q1'), ng.MOSFET)
        self.assertEqual(self.graph.basis('Q1'), 'part-keyword')
        self.assertEqual(self.graph.kind('K1'), ng.RELAY)

    def test_two_pin_part_is_not_classified_as_a_transistor(self):
        kind, basis = ng.classify('Q9', {'part': 'AO3400', 'value': '', 'prim': '', 'jedec': ''}, 2)
        self.assertEqual(kind, ng.UNKNOWN)
        self.assertIn('pin count', basis)

    def test_roles_come_from_pin_names_only(self):
        self.assertEqual(self.graph.role('Q1.2'), 'drain')
        self.assertEqual(self.graph.role('D1.1'), 'anode')
        graph = ng.NetGraph(relay_fixture.relay_board(clamp='unknown-roles'))
        self.assertIsNone(graph.role('D1.1'))

    def test_between_and_neighbors_respect_population(self):
        self.assertEqual(self.graph.between('RELAY_DRV', 'V24', {ng.DIODE}), ['D1'])
        unfitted = ng.NetGraph(relay_fixture.relay_board(clamp_nc=True))
        self.assertEqual(unfitted.between('RELAY_DRV', 'V24', {ng.DIODE}), [])
        self.assertEqual([ref for ref, _ in self.graph.neighbors('RELAY_DRV', {ng.DIODE})], ['D1'])

    def test_rail_and_ground_naming(self):
        self.assertTrue(ng.is_rail('VCC_3V3'))
        self.assertTrue(ng.is_ground('GND'))
        self.assertFalse(ng.is_rail('GND'))


class StatesTest(unittest.TestCase):
    def test_as_built_keeps_an_explicit_gap(self):
        db = relay_fixture.relay_board()
        state = state_lib.as_built(db)[0]
        self.assertEqual(state['gaps'], ['assembly state unverified: netlist nc only'])
        self.assertNotIn('D1', state['fitted'] if 'D1' not in db['parts'] else [])

    def test_declared_population_and_open_jumper(self):
        db = relay_fixture.relay_board()
        db['parts']['JP1'] = {'part': 'JUMPER', 'value': '', 'prim': '', 'jedec': '', 'nc': False}
        cfg = {'states': [{'id': 'run', 'citation': 'x',
                           'population': {ref: True for ref in db['parts']},
                           'jumpers': {'JP1': 'open'}}]}
        state = state_lib.resolve(db, cfg)[0]
        self.assertNotIn('JP1', state['fitted'])
        self.assertEqual(state['gaps'], [])

    def test_unknown_jumper_and_undeclared_part_are_gaps(self):
        db = relay_fixture.relay_board()
        cfg = {'states': [{'id': 'run', 'citation': 'x',
                           'population': {'K1': True}, 'jumpers': {'Q1': 'unknown'}}]}
        state = state_lib.resolve(db, cfg)[0]
        self.assertIn('jumper:Q1', state['gaps'])
        self.assertIn('population:D1', state['gaps'])

    def test_state_errors_reject_malformed_declarations(self):
        db = relay_fixture.relay_board()
        errors = state_lib.state_errors({'states': [{'id': 'run', 'citation': 'x',
                                                     'population': {'NOPE': True}}]}, db, 'demo')
        self.assertIn('demo: population: unknown ref NOPE', errors)
        self.assertIn('demo: states must contain 1..32 states',
                      state_lib.state_errors({'states': []}, db, 'demo'))


if __name__ == '__main__':
    unittest.main()


class PinNameAliasTest(unittest.TestCase):
    """写法差异不该让识别失效；极性标记不能被抹掉。"""

    def test_exporter_and_index_decorations_are_recovered(self):
        self.assertIn('EN', ng.pin_aliases('EN_12', '12'))
        self.assertIn('G', ng.pin_aliases('G1', '3'))
        self.assertIn('VDD', ng.pin_aliases('VDD_1', '1'))

    def test_overbar_and_compound_names_are_recovered(self):
        self.assertIn('RESET', ng.pin_aliases('~{RESET}', '5'))
        self.assertIn('SCL', ng.pin_aliases('PB6/SCL', '6'))

    def test_active_low_markers_are_preserved(self):
        self.assertEqual(ng.pin_aliases('NRST', '4'), ('NRST',))
        self.assertNotIn('RESET', ng.pin_aliases('RESET_N', '4'))

    def test_raw_name_wins_over_a_stripped_alias(self):
        db = relay_fixture.relay_board()
        graph = ng.NetGraph(db)
        self.assertEqual(graph.role('K1.1'), 'coil')     # A1/COIL1 类原文优先

    def test_decorated_pin_names_still_resolve_roles_and_queries(self):
        db = relay_fixture.relay_board()
        for pin, name in (('1', 'G1'), ('2', 'D_2'), ('3', '~{S}')):
            db['pinname']['Q1.' + pin] = name
        graph = ng.NetGraph(db)
        self.assertEqual(graph.role('Q1.1'), 'gate')
        self.assertEqual(graph.role('Q1.2'), 'drain')
        self.assertEqual(graph.role('Q1.3'), 'source')
        self.assertEqual(sorted(graph.named_pins('Q1', re.compile(r'^G$'))), ['Q1.1'])


class PowerTreeTest(unittest.TestCase):
    """轨身份按来源推导，轨名只作最弱一级依据。"""

    def board(self):
        db = {'nets': {}, 'parts': {}, 'pin2net': {}, 'pinname': {}, 'pintype': {},
              'pseudo_nets': ['NC'], 'ref2page': {}}
        add = relay_fixture.add
        add(db, 'U1', 'BUCK', [('1', 'VIN', '+12V'), ('2', 'SW', 'SW_NODE'),
                               ('3', 'GND', 'GND'), ('4', 'VOUT', '+3V3')])
        add(db, 'L1', 'IND-4U7', [('1', '1', 'SW_NODE'), ('2', '2', '+3V3')])
        add(db, 'U2', 'SOC', [('1', 'VDD', '+3V3'), ('2', 'GND', 'GND'),
                              ('3', 'IO', 'VCC_UNFED')])
        add(db, 'C9', '100nF', [('1', '1', 'NC'), ('2', '2', 'GND')])
        return db, powertree.PowerTree(ng.NetGraph(db))

    def test_output_pin_makes_a_rail_the_name_regex_misses(self):
        _, tree = self.board()
        self.assertFalse(ng.is_rail('+3V3'))             # 轨名正则认不出
        self.assertEqual(tree.rail_basis('+3V3'), powertree.DRIVER)

    def test_switch_node_is_not_a_rail_despite_reaching_the_output(self):
        _, tree = self.board()
        self.assertTrue(tree.driven('SW_NODE'))          # 经电感能回溯到输出脚
        self.assertTrue(tree.is_switch_node('SW_NODE'))
        self.assertIsNone(tree.rail_basis('SW_NODE'))

    def test_familiar_name_without_a_source_stays_name_hint(self):
        _, tree = self.board()
        self.assertEqual(tree.rail_basis('VCC_UNFED'), powertree.NAME_HINT)

    def test_ground_and_pseudo_nets_are_never_rails(self):
        _, tree = self.board()
        self.assertIsNone(tree.rail_basis('GND'))
        self.assertIsNone(tree.rail_basis('NC'))

    def test_lint_and_checkers_share_one_derivation(self):
        db, tree = self.board()
        engine = Lint(db)
        for net in db['nets']:
            self.assertEqual(engine.power_path(net), tree.source_of(net), net)

    def test_name_only_rail_identity_is_recorded_as_a_gap(self):
        db, _ = self.board()
        add = relay_fixture.add
        add(db, 'U3', 'SUPERVISOR', [('1', 'VCC', '+3V3'), ('2', 'SENSE', 'SENSE_3V3'),
                                     ('3', 'WDI', 'WDI'), ('4', 'RESET', 'RST_N'),
                                     ('5', 'GND', 'GND')])
        db['pintype']['U3.4'] = 'OUT'
        add(db, 'U4', 'SENSOR', [('1', 'VDD', 'VCC_UNFED'), ('2', 'GND', 'GND')])
        rails = {rail['net']: rail for state in supervision_inventory(db)['states']
                 for rail in state['rails']}
        self.assertEqual(rails['+3V3']['basis'], powertree.DRIVER)
        self.assertEqual(rails['+3V3']['gaps'], [])
        self.assertEqual(rails['VCC_UNFED']['basis'], powertree.NAME_HINT)
        self.assertEqual(rails['VCC_UNFED']['gaps'], ['rail-identity:VCC_UNFED'])


class WholeBoardContractTest(unittest.TestCase):
    """全部检查器在同一块板上的框架级契约：识别、绑定、过期、缺口门。"""

    @classmethod
    def setUpClass(cls):
        cls.db = whole_board()
        cls.plan = build_review_plan(cls.db)

    def bound_items(self, checker):
        return [item for item in self.plan['checks'] if checker.binds(item)]

    def test_every_checker_recognises_something_on_the_board(self):
        for checker in REGISTRY:
            with self.subTest(checker.id):
                self.assertTrue(self.bound_items(checker),
                                '%s recognised nothing' % checker.id)

    def test_clean_plan_validates_for_all_checkers(self):
        errors, _ = run_validation(self.plan, self.db)
        self.assertEqual(errors, [])

    def test_tampered_inventory_is_detected_for_every_checker(self):
        for checker in REGISTRY:
            with self.subTest(checker.id):
                plan = copy.deepcopy(self.plan)
                plan[checker.plan_key]['digest'] = '0' * 64
                errors, _ = run_validation(plan, self.db)
                self.assertIn(checker.stale_message, errors)

    def test_dropped_generated_item_is_detected_for_every_checker(self):
        for checker in REGISTRY:
            with self.subTest(checker.id):
                items = [x for x in self.plan['checks'] if not checker.binds(x)]
                errors, _ = run_validation(self.plan, self.db, items)
                self.assertTrue(any('incomplete' in message for message in errors), errors)

    def test_unverified_assembly_state_blocks_pass_for_every_checker(self):
        _, gates = run_validation(self.plan, self.db)
        for checker in REGISTRY:
            with self.subTest(checker.id):
                keys = [item['id'] for item in self.bound_items(checker)]
                self.assertTrue(any(gates.get(key) for key in keys),
                                '%s never blocks PASS on an unverified state' % checker.id)
