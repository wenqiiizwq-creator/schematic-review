import copy
import csv
import io
import json
import pathlib
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from validate_review import validate_review, fingerprint
from render_report import render
from test_validate_review import fixture, fail, E
from test_remediation import ready


def modern(kind='DEFECT', severity='error'):
    p, r, db = fixture()
    c = fail(r, severity)
    r.update(schema_version=3, remediation_version=1,
             report_metadata={'title':'合成接口板审查', 'baseline':'SYN-C Rev.1；全部数值与器件均为测试设定',
                              'scope_note':'仅演示报告和台账契约，不作为真实设计依据。'})
    c['blocking_reason'] = '合成测试规定：除error外，本项不属于当前冻结必需条件。'
    f = r['findings'][0]
    f['kind'] = kind
    f['remediation'] = ready()
    if kind == 'RISK':
        c.update(review_result='INSUFFICIENT', evidence_confidence='C', missing_inputs=['受控负载上限'])
        f['missing_inputs'] = c['missing_inputs'][:]
        m = f['remediation']
        m.update(readiness='CONDITIONAL', prerequisites=[{'input':'受控负载上限','reason':'检查参数裕量',
                 'how_to_obtain':'从受控接口规格读取','acceptance':'给出适用于本配置的min/max'}])
        m['parameters'][0].update(status='CANDIDATE', needed_input='受控负载上限',
                                  selection_method='按保证窗口和电阻公差计算，再选择标准值。')
    if kind == 'IMPROVEMENT':
        c.update(review_result='PASS', evidence_confidence='A')
        c.pop('severity'); c.pop('finding_id')
    return p, r, db


class ThreeLevelTests(unittest.TestCase):
    def test_published_fixture_renders_every_item_with_matching_counts(self):
        root = pathlib.Path(__file__).resolve().parents[2] / 'examples/three-level'
        read = lambda name: json.loads((root / name).read_text())
        md, table, gate = render(read('plan.json'), read('review-results.json'), read('db.json'))
        self.assertEqual(gate['summary']['item_severity_counts'], {'error':1,'warning':2,'suggestion':3})
        self.assertEqual(gate['release'], 'NO_GO')
        rows = list(csv.DictReader(io.StringIO(table)))
        self.assertEqual(len(rows), 6)
        for row in rows:
            self.assertEqual(md.count('### %s | %s |' % (row['severity'],row['id'])), 1)

    def test_malformed_risk_remediation_is_rejected_without_crash(self):
        for value in (None, [], 'missing'):
            p,r,db = modern('RISK','warning')
            r['findings'][0]['remediation'] = value
            self.assertFalse(validate_review(p,r,db)['valid'])

    def test_current_error_cannot_be_accepted_or_marked_nonblocking(self):
        p, r, db = modern()
        c = r['checks'][0]
        c.update(blocking=False, disposition='ACCEPTED', acceptance={k:'synthetic' for k in ('by','date','scope','reason','record')})
        result = validate_review(p,r,db)
        self.assertTrue(result['valid'], result)
        self.assertEqual(result['release'], 'NO_GO')

    def test_warning_and_suggestion_do_not_erase_blocking(self):
        for severity in ('warning','suggestion'):
            p,r,db = modern('RISK',severity)
            r['checks'][0]['blocking'] = True
            self.assertEqual(validate_review(p,r,db)['release'], 'NO_GO')
            r['checks'][0]['blocking'] = False
            self.assertEqual(validate_review(p,r,db)['release'], 'GO')
            self.assertEqual(r['checks'][0]['review_result'], 'INSUFFICIENT')

    def test_document_gap_is_valid_suggestion_not_fake_compliance(self):
        p,r,db = modern('RISK','suggestion')
        result = validate_review(p,r,db)
        self.assertTrue(result['valid'], result)
        self.assertEqual(result['summary']['confirmed_defects'], 0)
        self.assertEqual(result['summary']['risks'], 1)
        self.assertEqual(result['summary']['item_severity_counts']['suggestion'], 1)

    def test_only_exact_three_levels(self):
        for level in ('P1','ERROR','Warning','info',None,['error']):
            p,r,db = modern()
            r['checks'][0]['severity'] = r['findings'][0]['severity'] = level
            self.assertFalse(validate_review(p,r,db)['valid'])

    def test_v3_rejects_potential_severity(self):
        p,r,db = modern('RISK','warning')
        r['checks'][0]['potential_severity'] = 'warning'
        self.assertFalse(validate_review(p,r,db)['valid'])

    def test_risk_cannot_be_error(self):
        p,r,db = modern('RISK','error')
        self.assertFalse(validate_review(p,r,db)['valid'])

    def test_risk_cannot_be_ready(self):
        p,r,db = modern('RISK','warning')
        r['findings'][0]['remediation'] = ready()
        self.assertFalse(validate_review(p,r,db)['valid'])

    def test_kind_and_bidirectional_links_required(self):
        for mutation in ('kind','link','missing','blocking_reason'):
            p,r,db = modern('RISK','warning')
            if mutation=='kind':r['findings'][0]['kind']='IMPROVEMENT'
            elif mutation=='link':r['checks'][0]['finding_id']='unknown'
            elif mutation=='missing':r['findings'][0]['missing_inputs']=[]
            else:r['checks'][0].pop('blocking_reason')
            self.assertFalse(validate_review(p,r,db)['valid'])

    def test_new_risks_cannot_hide_in_unvalidated_array(self):
        p,r,db=modern();r['risks']=[{'id':'hidden'}]
        self.assertFalse(validate_review(p,r,db)['valid'])

    def test_v3_always_requires_actionable_details(self):
        p,r,db=modern();r.pop('remediation_version')
        self.assertFalse(validate_review(p,r,db)['valid'])

    def test_render_preserves_conditions_parameters_and_verification(self):
        p,r,db=modern('RISK','warning')
        f=r['findings'][0]
        md,table,gate=render(p,r,db)
        for value in [f['observed'],f['criterion'],f['remediation']['parameters'][0]['selection_method'],
                      f['remediation']['verification'][0]['expected'],f['remediation']['prerequisites'][0]['acceptance']]:
            self.assertIn(value,md)
        self.assertEqual(list(csv.DictReader(io.StringIO(table)))[0]['severity'],'warning')
        self.assertIn('pagebreak',md)
        self.assertNotIn('P1',md)

    def test_renderer_handles_design_required(self):
        p,r,db=modern('RISK','warning')
        r['findings'][0]['remediation']['readiness']='DESIGN_REQUIRED'
        self.assertIn('需要重新设计', render(p,r,db)[0])

    def test_renderer_preserves_disconnect_and_new_endpoints(self):
        p,r,db=modern()
        s=r['findings'][0]['remediation']['steps'][0]
        s.update(kind='CONNECT',remove_connections=[{'from':'R1.2','to':'OLD_RAIL'}],
                 add_connections=[{'from':'R1.2','to':'NEW_RAIL'}])
        md=render(p,r,db)[0]
        self.assertIn('断开：R1.2 → OLD_RAIL',md)
        self.assertIn('连接：R1.2 → NEW_RAIL',md)

    def test_invalid_ledger_and_legacy_not_rendered_as_current_report(self):
        p,r,db=modern();r['findings'][0]['severity']='suggestion'
        with self.assertRaises(ValueError):render(p,r,db)
        p,r,db=fixture()
        self.assertTrue(validate_review(p,r,db)['valid'])
        with self.assertRaises(ValueError):render(p,r,db)

    def test_long_front_matter_rejected(self):
        p,r,db=modern();r['report_metadata']['scope_note']='长'*901
        with self.assertRaises(ValueError):render(p,r,db)

    def test_unique_item_count_not_fail_row_count(self):
        p,r,db=modern('DEFECT','suggestion')
        second=copy.deepcopy(r['checks'][0]);second['id']='C2'
        r['checks'].append(second);r['findings'][0]['check_ids'].append('C2')
        p['checks'].append({'id':'C2','object':{},'applicability':'APPLICABLE'});r['plan_digest']=fingerprint(p)
        md,table,gate=render(p,r,db)
        self.assertEqual(gate['summary']['items'],1)
        self.assertEqual(gate['summary']['results']['FAIL'],2)
        self.assertEqual(md.count('### suggestion | F1 |'),1)
        self.assertEqual(len(list(csv.DictReader(io.StringIO(table)))),1)

    def test_cli_does_not_overwrite_input(self):
        p,r,db=modern()
        script=pathlib.Path(__file__).resolve().parents[1]/'render_report.py'
        with tempfile.TemporaryDirectory() as directory:
            root=pathlib.Path(directory)
            for name,data in [('plan',p),('results',r),('db',db)]: (root/name).write_text(json.dumps(data))
            original=(root/'results').read_bytes()
            args=[sys.executable,str(script),str(root/'plan'),str(root/'results'),'--db',str(root/'db'),
                  '--output',str(root/'results'),'--csv',str(root/'issues.csv')]
            self.assertEqual(subprocess.run(args,capture_output=True).returncode,2)
            self.assertEqual((root/'results').read_bytes(),original)
            args[-3]=str(root/'report.md')
            result=subprocess.run(args,capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertIn('### error | F1 |',(root/'report.md').read_text())


if __name__ == '__main__':
    unittest.main()
