"""Synthetic migration guards: new topology gates must survive v3/reporting.

The helpers create stipulated test records only, never real review conclusions.
"""
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
from checkers import REGISTRY
from plan_review import build_review_plan
from render_report import render
from validate_review import validate_review, fingerprint, primary_anchors
from lint import Lint
from electrical_fixtures import bind_evidence
import test_power_switch as ps
import test_input_filter as inf
import test_power_up as pu
import test_supervision as sv
import test_diff_levels as dl
import test_optocoupler as oc
from test_review_v3 import modern
from test_checkers_framework import whole_board
from test_i2c_topology import pending_report
from test_revision_impact import database, reviewed_records


def bound_modern(kind='DEFECT', severity='error'):
    plan, report, db = modern(kind, severity)
    for item in plan['checks']:
        item['criterion'] = 'Synthetic isolated requirement: ' + item['id']
        if item['id'] == 'C1':
            item['object'] = {'node': 'R1.1', 'state': 'RUN', 'configuration': 'ASSEMBLED'}
    planned = {c['id']: c for c in plan['checks']}
    for row in report['checks']:
        row['binding'] = {k: deepcopy(planned[row['id']][k]) for k in ('object', 'criterion')}
    report.update(binding_version=1, plan_digest=fingerprint(plan))
    return plan, report, db


def pending_v3(plan, db):
    """Preserve unknown results while explicitly recording v3 risks for tests."""
    report = pending_report(plan, db)
    report.update(schema_version=3, remediation_version=1)
    template = modern('RISK', 'warning')[1]['findings'][0]
    planned = {c['id']: c for c in plan['checks']}
    for row in report['checks']:
        row.pop('potential_severity', None)
        if row['review_result'] != 'INSUFFICIENT':
            continue
        fid = 'RISK-' + row['id']
        row.update(severity='warning', finding_id=fid,
                   blocking_reason='Synthetic fixture lacks acceptance evidence; do not release.')
        finding = deepcopy(template)
        refs, nets = primary_anchors(planned[row['id']]['object'], db)
        finding.update(id=fid, check_ids=[row['id']], missing_inputs=row['missing_inputs'],
                       criterion=planned[row['id']]['criterion'],
                       location={'pages': ['synthetic fixture'],
                                 'refs': sorted(refs) or ['unlocated synthetic scope'],
                                 'nets': sorted(nets) or ['unlocated synthetic scope']})
        report['findings'].append(finding)
    return report


class UpstreamV3IntegrationTests(unittest.TestCase):
    def test_all_six_new_hot_rules_reject_changed_input_and_document_bytes(self):
        fixtures = [(lambda: ps.switch_board(snubber='rc'), ps.gate_check),
                    (lambda: inf.converter(damper='rc'), inf.damping_check),
                    (pu.rail_board, pu.dropout_check), (sv.supervised_board, sv.pulse_check),
                    (lambda: dl.link_board(coupling='dc'), dl.level_check),
                    (oc.opto_board, oc.ctr_check)]
        for board, make_check in fixtures:
            for changed in ('db', 'document'):
                with self.subTest(rule=make_check()['rule'], changed=changed), tempfile.TemporaryDirectory() as directory:
                    db = board()
                    evidence = {'schema_version': 1, 'checks': [make_check()]}
                    audit = bind_evidence(db, evidence, directory)
                    def result():
                        lint = Lint(db, evidence=evidence, datasheet_audit=audit)
                        lint.run()
                        return next(x for x in lint.results if x.get('check_id') == evidence['checks'][0]['id'])
                    self.assertEqual(result()['review_result'], 'PASS')
                    if changed == 'db':
                        next(iter(db['parts'].values()))['value'] = 'CHANGED-SYNTHETIC-PART'
                    else:
                        for doc in Path(directory).glob('*.pdf'):
                            doc.write_bytes(doc.read_bytes() + b'\nCHANGED GUARANTEE\n')
                    self.assertEqual(result()['review_result'], 'INSUFFICIENT')

    def test_bound_v3_keeps_three_levels_and_novice_steps(self):
        for kind, severity in [('DEFECT', 'error'), ('RISK', 'warning'), ('IMPROVEMENT', 'suggestion')]:
            with self.subTest(kind=kind):
                plan, report, db = bound_modern(kind, severity)
                md, csv, gate = render(plan, report, db, require_bindings=True)
                self.assertTrue(gate['valid'], gate)
                self.assertEqual(gate['binding_validation']['bound_checks'], len(plan['checks']))
                self.assertIn('对象与判据绑定：已校验', md)
                self.assertIn(report['findings'][0]['remediation']['steps'][0]['instruction'], md)
                self.assertIn(severity, csv)
                if severity == 'error':
                    self.assertEqual(gate['release'], 'NO_GO')

    def test_other_state_criterion_or_physical_pin_cannot_borrow_a_result(self):
        for target, value in [('state', 'OFF'), ('node', 'R1.2'), ('criterion', 'different guarantee')]:
            plan, report, db = bound_modern()
            binding = report['checks'][0]['binding']
            if target == 'criterion': binding[target] = value
            else: binding['object'][target] = value
            with self.assertRaisesRegex(ValueError, 'binding .* differs'):
                render(plan, report, db, require_bindings=True)

    def test_bound_defect_cannot_consume_an_unrelated_pass(self):
        plan, report, db = bound_modern()
        report['findings'][0]['check_ids'].append('chains')
        gate = validate_review(plan, report, db, require_bindings=True)
        self.assertFalse(gate['valid'])
        self.assertTrue(any('every defect link' in x for x in gate['errors']))

    def test_whole_board_all_nine_inventories_work_with_v3_pending_report(self):
        db = whole_board()
        plan = build_review_plan(db)
        report = pending_v3(plan, db)
        for checker in REGISTRY:
            self.assertIn(checker.plan_key, plan)
        md, _, gate = render(plan, report, db, require_bindings=True)
        self.assertTrue(gate['valid'], gate)
        self.assertEqual(gate['release'], 'NO_GO')
        self.assertGreater(gate['summary']['risks'], 0)
        self.assertTrue('待补齐的条件' in md)

    def test_inventory_tampering_still_rejected_in_v3(self):
        db = whole_board()
        plan = build_review_plan(db)
        report = pending_v3(plan, db)
        plan['power_switch']['digest'] = '0' * 64
        report['plan_digest'] = fingerprint(plan)
        gate = validate_review(plan, report, db)
        self.assertFalse(gate['valid'])
        self.assertTrue(any('stale or modified' in x for x in gate['errors']))

    def revision(self):
        old_db = database()
        old_plan = build_review_plan(old_db)
        db = deepcopy(old_db)
        db['parts']['R1']['value'] = '4.7K/1%'
        plan = build_review_plan(db, old_db=old_db, old_plan=old_plan)
        report = reviewed_records(plan, pending_v3(plan, db))
        return plan, report, db, old_db, old_plan

    def test_v3_revision_renderer_requires_real_baseline_and_current_records(self):
        plan, report, db, old_db, old_plan = self.revision()
        opts = dict(require_bindings=True, old_db=old_db, old_plan=old_plan, require_revision=True)
        md, _, gate = render(plan, report, db, **opts)
        self.assertTrue(gate['revision_validation']['enforced'])
        self.assertIn('改版复验：', md)
        self.assertEqual(gate['release'], 'NO_GO')
        with self.assertRaises(ValueError):
            render(plan, report, db)
        row = next(c for c in report['checks'] if 'reverification' in c)
        saved = row.pop('reverification')
        with self.assertRaisesRegex(ValueError, 're-verification'):
            render(plan, report, db, **opts)
        row['reverification'] = saved
        saved['inputs_digest'] = 'old-inputs'
        with self.assertRaises(ValueError):
            render(plan, report, db, **opts)

    def test_renderer_cli_forwards_revision_flags_and_protects_old_inputs(self):
        plan, report, db, old_db, old_plan = self.revision()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name, data in [('plan', plan), ('results', report), ('db', db), ('old-db', old_db), ('old-plan', old_plan)]:
                (root/name).write_text(json.dumps(data))
            args = [sys.executable, '-B', str(SCRIPTS/'render_report.py'), str(root/'plan'), str(root/'results'),
                    '--db', str(root/'db'), '--old-db', str(root/'old-db'), '--old-plan', str(root/'old-plan'),
                    '--require-bindings', '--require-revision-impact', '--output', str(root/'report.md'),
                    '--csv', str(root/'issues.csv')]
            result = subprocess.run(args, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('改版复验：', (root/'report.md').read_text())
            original = (root/'old-plan').read_bytes()
            args[args.index('--output') + 1] = str(root/'old-plan')
            self.assertEqual(subprocess.run(args, capture_output=True).returncode, 2)
            self.assertEqual(original, (root/'old-plan').read_bytes())

    def test_renderer_rejects_duplicate_json_keys(self):
        plan, report, db = bound_modern()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/'plan').write_text(json.dumps(plan))
            raw = json.dumps(report)
            (root/'results').write_text('{"schema_version": 2,' + raw[1:])
            result = subprocess.run([sys.executable, '-B', str(SCRIPTS/'render_report.py'),
                str(root/'plan'), str(root/'results'), '--output', str(root/'report.md'),
                '--csv', str(root/'issues.csv')], capture_output=True)
            self.assertEqual(result.returncode, 2)
            self.assertFalse((root/'report.md').exists())


if __name__ == '__main__':
    unittest.main()
