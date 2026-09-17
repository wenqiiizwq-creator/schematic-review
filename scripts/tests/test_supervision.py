import pathlib
import sys
import tempfile
import unittest

SCRIPTS = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))

from checkers.supervision import SupervisionChecker, build_inventory, validate_supervision_intent
from electrical_contract import db_fingerprint, validate_evidence
from electrical_fixtures import bind_evidence
from lint import Lint
from plan_review import build_review_plan
from test_inductive_load import add


def supervised_board(wdi='driven', reset='connected', extra_rail=False, pullup=True):
    """U3 是带看门狗的监控器，复位链与喂狗来源按参数变化。"""
    db = {'nets': {}, 'parts': {}, 'pin2net': {}, 'pinname': {}, 'pintype': {},
          'pseudo_nets': [], 'ref2page': {}}
    wdi_net = {'driven': 'WDI_NET', 'floating': 'WDI_NET', 'tied': 'VCC_3V3'}[wdi]
    add(db, 'U3', 'SUPERVISOR', [('1', 'VCC', 'VCC_3V3'), ('2', 'SENSE', 'SENSE_3V3'),
                                 ('3', 'WDI', wdi_net), ('4', 'RESET', 'SYS_RST_N'),
                                 ('5', 'GND', 'GND')])
    db['pintype']['U3.4'] = 'OUT'
    add(db, 'R7', '100K', [('1', '1', 'VCC_3V3'), ('2', '2', 'SENSE_3V3')])
    add(db, 'R8', '47K', [('1', '1', 'SENSE_3V3'), ('2', '2', 'GND')])
    if pullup:
        add(db, 'R9', '10K', [('1', '1', 'SYS_RST_N'), ('2', '2', 'VCC_3V3')])
    soc = [('1', 'VDD', 'VCC_3V3'), ('3', 'GND', 'GND')]
    soc.append(('2', 'NRST', 'SYS_RST_N') if reset == 'connected' else
               ('2', 'GPIO3', 'SYS_RST_N') if reset == 'gpio-only' else ('2', 'NRST', 'OTHER_RST'))
    add(db, 'U4', 'SOC', soc)
    if wdi == 'driven':
        add(db, 'U5', 'MCU', [('1', 'GPIO1', 'WDI_NET'), ('2', 'VDD', 'VCC_3V3'),
                              ('3', 'GND', 'GND')])
        db['pintype']['U5.1'] = 'OUT'
    if extra_rail:
        add(db, 'U6', 'FPGA', [('1', 'VDD', 'VCC_1V8'), ('2', 'GND', 'GND')])
        add(db, 'U7', 'LDO', [('1', 'VOUT', 'VCC_1V8'), ('2', 'GND', 'GND')])
    return db


def supervisors_of(inventory, state='as-built'):
    entry = next(x for x in inventory['states'] if x['id'] == state)
    return {item['ref']: item for item in entry['supervisors']}


def rails_of(inventory, state='as-built'):
    entry = next(x for x in inventory['states'] if x['id'] == state)
    return {rail['net']: rail for rail in entry['rails']}


def findings(db, intent=None):
    return [f for f in Lint(db, '', intent).run() if f['rule'].startswith('SV-')]


def pulse_check(**overrides):
    check = {'id': 'RESET', 'rule': 'SV-10', 'kind': 'reset_pulse', 'ref': 'U3',
             'net': 'SYS_RST_N', 'output_type': 'open_drain',
             'pulse_width_s': {'min': 0.14, 'max': 0.28},
             'required_width_s': {'min': 0.001, 'max': 0.1},
             'citation': 'Synthetic supervisor pulse width and SoC reset requirement'}
    check.update(overrides)
    return check


class RecognitionTest(unittest.TestCase):
    def test_complete_supervision_has_no_findings(self):
        db = supervised_board()
        item = supervisors_of(build_inventory(db))['U3']
        self.assertEqual(item['monitored_nets'], ['SENSE_3V3'])
        self.assertEqual(item['watchdog_inputs'][0]['state'], 'driven')
        self.assertEqual([x['role'] for x in item['reset_outputs'][0]['destinations']],
                         ['reset-input'])
        self.assertEqual(item['reset_outputs'][0]['pulls'], ['VCC_3V3'])
        self.assertEqual(findings(db), [])

    def test_floating_watchdog_input_reports_sv01(self):
        db = supervised_board(wdi='floating')
        self.assertEqual(supervisors_of(build_inventory(db))['U3']['watchdog_inputs'][0]['state'],
                         'floating')
        hits = findings(db)
        self.assertEqual([f['rule'] for f in hits], ['SV-01'])
        self.assertEqual(hits[0]['kind'], 'CANDIDATE')

    def test_watchdog_tied_to_a_rail_reports_sv01(self):
        self.assertEqual([f['rule'] for f in findings(supervised_board(wdi='tied'))], ['SV-01'])

    def test_dangling_reset_output_reports_sv02_finding(self):
        db = supervised_board(reset='dangling', pullup=False)
        hits = findings(db)
        self.assertEqual([f['rule'] for f in hits], ['SV-02'])
        self.assertEqual(hits[0]['kind'], 'FINDING')
        self.assertIn('复位链在此中断', hits[0]['detail'])

    def test_reset_reaching_only_a_gpio_is_a_candidate(self):
        hits = findings(supervised_board(reset='gpio-only'))
        self.assertEqual([f['rule'] for f in hits], ['SV-02'])
        self.assertEqual(hits[0]['kind'], 'CANDIDATE')

    def test_unmonitored_rail_reports_sv03(self):
        db = supervised_board(extra_rail=True)
        rails = rails_of(build_inventory(db))
        self.assertEqual(rails['VCC_3V3']['monitor'], 'divider')
        self.assertIsNone(rails['VCC_1V8']['monitor'])
        hits = findings(db)
        self.assertEqual([f['rule'] for f in hits], ['SV-03'])
        self.assertIn('VCC_1V8', hits[0]['detail'])

    def test_board_without_a_supervisor_reports_nothing(self):
        db = supervised_board()
        db['parts']['U3']['nc'] = True
        self.assertEqual(supervisors_of(build_inventory(db)), {})
        self.assertEqual(findings(db), [])

    def test_soc_reset_input_is_not_a_supervisor(self):
        db = supervised_board()
        db['parts']['U3']['nc'] = True
        self.assertNotIn('U4', supervisors_of(build_inventory(db)))


class PlanTest(unittest.TestCase):
    def test_plan_covers_reset_path_pulse_and_rail_coverage(self):
        db = supervised_board(extra_rail=True)
        plan = build_review_plan(db)
        checks = {x['check'] for x in plan['checks'] if x['object'].get('supervision')}
        self.assertEqual(checks, {'supervision-reset-path-U3', 'supervision-reset-pulse-U3',
                                  'supervision-rail-coverage-AS-BUILT'})
        coverage = next(x for x in plan['checks']
                        if x['check'] == 'supervision-rail-coverage-AS-BUILT')
        self.assertIn('rail-unmonitored:VCC_1V8', coverage['inventory_gaps'])
        self.assertEqual(plan['supervision_version'], 1)

    def test_rule_instances_list_the_unmonitored_rails(self):
        plan = build_review_plan(supervised_board(extra_rail=True))
        rules = {row['rule']: row for row in plan['rule_plan']}
        self.assertEqual(rules['SV-03']['instances'], ['VCC_1V8'])
        self.assertEqual(rules['SV-01']['instances'], ['U3'])

    def test_stale_object_binding_is_reported(self):
        plan = build_review_plan(supervised_board())
        item = next(x for x in plan['checks'] if x['object'].get('supervision'))
        stale = dict(item['object'], supervision_digest='0' * 64)
        self.assertEqual(SupervisionChecker().object_errors('X', stale, plan['supervision']),
                         ['X: stale supervision object binding'])

    def test_intent_validation_accepts_a_sound_section(self):
        db = supervised_board()
        section = {'schema_version': 1, 'db_sha256': db_fingerprint(db),
                   'states': [{'id': 'run', 'citation': 'BOM Rev.A', 'population': {}}],
                   'supervisors': [{'id': 'u3', 'ref': 'U3', 'citation': 'reset scheme'}]}
        self.assertEqual(validate_supervision_intent({'supervision': section}, db), [])


class HotRuleTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)

    def run_check(self, check, db=None):
        db = db or supervised_board()
        evidence = {'schema_version': 1, 'checks': [check]}
        audit = bind_evidence(db, evidence, self.directory.name)
        self.assertEqual(validate_evidence(evidence), [])
        lint = Lint(db, evidence=evidence, datasheet_audit=audit)
        lint.run()
        results = [x for x in lint.results if x['check_id'] == check['id']]
        self.assertEqual(len(results), 1)
        return results[0]

    def test_sufficient_pulse_with_pullup_passes(self):
        result = self.run_check(pulse_check())
        self.assertEqual(result['review_result'], 'PASS')
        self.assertEqual(result['calculation']['pullups'], ['R9'])

    def test_short_pulse_fails(self):
        result = self.run_check(pulse_check(pulse_width_s={'min': 0.01, 'max': 0.02}))
        self.assertEqual(result['review_result'], 'FAIL')
        self.assertIn('低于目标器件要求', result['detail'])

    def test_open_drain_without_pullup_fails(self):
        result = self.run_check(pulse_check(), db=supervised_board(pullup=False))
        self.assertEqual(result['review_result'], 'FAIL')
        self.assertIn('未找到到电源轨的上拉电阻', result['detail'])

    def test_push_pull_without_pullup_passes(self):
        result = self.run_check(pulse_check(output_type='push_pull'),
                                db=supervised_board(pullup=False))
        self.assertEqual(result['review_result'], 'PASS')

    def test_missing_output_type_stays_insufficient(self):
        check = pulse_check()
        check.pop('output_type')
        result = self.run_check(check)
        self.assertEqual(result['review_result'], 'INSUFFICIENT')
        self.assertIn('output_type 缺少声明', result['detail'])


if __name__ == '__main__':
    unittest.main()
