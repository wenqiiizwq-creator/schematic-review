"""Synthetic electrical and fail-closed coverage regressions; not a board sign-off."""
from copy import deepcopy
import math
from pathlib import Path
import random
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from checkers.connector_esd import build_inventory as esd_inventory
from checkers.passive_networks import build_inventory as passive_inventory, evaluate, nominal, model_errors
from checkers.coverage_base import input_fingerprint
from electrical_contract import validate_evidence, check_matches
from electrical_fixtures import bind_evidence
from lint import Lint
from passive_ac import analytic_metrics, response
from plan_review import build_review_plan
from render_report import render
from validate_review import validate_review, fingerprint
from test_inductive_load import add
from test_upstream_v3_integration import pending_v3


def board():
    return {'parts': {}, 'nets': {}, 'pin2net': {}, 'pinname': {}, 'pintype': {}, 'pseudo_nets': [], 'ref2page': {}}


def cfg(db):
    return {'schema_version': 1, 'input_sha256': input_fingerprint(db),
            'states': [{'id': 'RUN', 'citation': 'Synthetic assembly Rev.A',
                        'population': {r: not p.get('nc') for r, p in db['parts'].items()}}],
            'discovery_citation': 'Synthetic fixture entire part and port inventory'}


def ports(protector=False):
    db = board()
    add(db, 'J1', 'CONN', [('1', 'IO', 'IO'), ('2', 'PWR', 'VDD'), ('3', 'GND', 'GND')])
    add(db, 'U1', 'TEST-IC', [('1', 'GPIO', 'IO')])
    if protector:
        add(db, 'D1', 'TVS', [('1', 'K', 'IO'), ('2', 'A', 'GND')])
    context = cfg(db)
    context['connectors'] = {'J1': {'citation': 'Synthetic 3-pin official drawing', 'pinout_complete': True,
        'exposure': 'external', 'pins': {p: {'role': role, 'requirement': requirement,
                                           'citation': 'Synthetic per-pin requirement'}
        for p, role, requirement in [('1', 'signal', 'required'), ('2', 'power', 'required'), ('3', 'return', 'exempt')]}}}
    if protector:
        context['protectors'] = {'D1': {'citation': 'Synthetic TVS physical pinout', 'channels': [
            {'signal_pin': '1', 'return_pins': ['2'], 'citation': 'Synthetic channel map'}]}}
    return db, {'connector_esd': context}


def rg(v, tolerance=0):
    return {'min': v * (1-tolerance), 'max': v * (1+tolerance), 'nominal': v}


def rc(kind='lowpass', tolerance=0):
    db = board()
    add(db, 'U1', 'SOURCE', [('1', 'OUT', 'IN')])
    add(db, 'U2', 'RECEIVER', [('1', 'IN', 'OUT')])
    if kind == 'lowpass':
        add(db, 'R1', '1K', [('1', '1', 'IN'), ('2', '2', 'OUT')])
        add(db, 'C1', '1uF', [('1', '1', 'OUT'), ('2', '2', 'GND')])
    elif kind == 'highpass':
        add(db, 'C1', '1uF', [('1', '1', 'IN'), ('2', '2', 'OUT')])
        add(db, 'R1', '1K', [('1', '1', 'OUT'), ('2', '2', 'GND')])
    elif kind == 'lc':
        add(db, 'L1', '10mH', [('1', '1', 'IN'), ('2', '2', 'OUT')])
        add(db, 'C1', '1uF', [('1', '1', 'OUT'), ('2', '2', 'GND')])
    refs = ['L1' if kind == 'lc' else 'R1', 'C1']
    context = cfg(db)
    context['networks'] = [{'id': 'FILTER', 'refs': refs, 'input_net': 'IN', 'output_net': 'OUT',
                            'reference_net': 'GND', 'citation': 'Synthetic signal direction and complete topology'}]
    intent = {'passive_networks': context}
    inventory = passive_inventory(db, intent)
    params = {r: dict(rg(0.01 if r == 'L1' else 1000 if r == 'R1' else 1e-6, tolerance),
                      citation='Synthetic guaranteed effective value') for r in refs}
    for r in refs:
        if not r.startswith('R'):
            params[r]['series_resistance_ohm'] = rg(1 if r == 'L1' else 0)
    model = {'input_net': 'IN', 'output_net': 'OUT', 'reference_net': 'GND',
             'source_resistance_ohm': rg(100), 'load_open': False, 'load_resistance_ohm': rg(1000),
             'load_capacitance_f': rg(0), 'components': params,
             'boundaries': {'U1.1': {'role': 'source', 'citation': 'Synthetic Thevenin source'},
                            'U2.1': {'role': 'load', 'citation': 'Synthetic resistive receiver'}},
             'citation': 'Synthetic model specification', 'validity_citation': 'Synthetic linear ideal-C model stipulated for these frequencies',
             'frequency_hz': [10, 100, 1000, 10000], 'metric_requirements': {'dc_gain': {'min': 0, 'max': 1.01}}}
    evidence = {'schema_version': 1, 'checks': [{'id': 'FILTER-RUN', 'rule': 'PN-10', 'kind': 'passive_ac',
        'ref': refs[0], 'net': 'OUT', 'network_id': 'FILTER', 'inventory_digest': inventory['digest'],
        'model': model, 'citation': 'Synthetic acceptance specification Rev.A'}]}
    return db, intent, evidence


class ESDCoverageTests(unittest.TestCase):
    def test_no_protector_still_scans_every_pin(self):
        db, intent = ports()
        items = esd_inventory(db, intent)['states'][0]['pins']
        self.assertEqual(len(items), 3)
        self.assertEqual([x['coverage'] for x in items], ['no-protector-found', 'no-protector-found', 'exemption-needs-review'])
        hits = [x for x in Lint(db, intent=intent).run() if x['rule'] == 'ES-01']
        self.assertEqual({x['node'] for x in hits}, {'J1.1', 'J1.2'})
        self.assertTrue(all(x['kind'] == 'CANDIDATE' for x in hits))

    def test_one_tvs_does_not_cover_other_pins(self):
        db, intent = ports(True)
        items = esd_inventory(db, intent)['states'][0]['pins']
        self.assertEqual(items[0]['coverage'], 'mapped-topology-candidate')
        self.assertEqual(items[1]['coverage'], 'no-protector-found')
        self.assertEqual(items[0]['protectors'][0]['return_nodes'], {'D1.2': 'GND'})

    def test_presence_without_channel_map_never_claims_mapped(self):
        db, intent = ports(True)
        intent['connector_esd'].pop('protectors')
        item = esd_inventory(db, intent)['states'][0]['pins'][0]
        self.assertEqual(item['coverage'], 'unmapped-protector-candidate')

    def test_dnp_device_not_protection(self):
        db, intent = ports(True)
        intent['connector_esd']['states'][0]['population']['D1'] = False
        item = esd_inventory(db, intent)['states'][0]['pins'][0]
        self.assertEqual(item['coverage'], 'no-protector-found')
        self.assertEqual(item['excluded_protectors'], ['D1'])

    def test_symbol_only_and_official_only_pins_retained(self):
        db, intent = ports()
        db['declared_pinname'] = {'J1.4': 'UNCONNECTED'}
        intent['connector_esd']['connectors']['J1']['pins']['5'] = {
            'role': 'signal', 'requirement': 'required', 'citation': 'Synthetic full pinout'}
        intent['connector_esd']['input_sha256'] = input_fingerprint(db)
        items = esd_inventory(db, intent)['states'][0]['pins']
        self.assertEqual({x['node'] for x in items}, {'J1.' + str(n) for n in range(1, 6)})
        self.assertTrue(all(x['gaps'] for x in items))

    def test_no_connect_is_not_esd_exemption(self):
        db, intent = ports()
        db['no_connect_nodes'] = ['J1.1']
        intent['connector_esd']['input_sha256'] = input_fingerprint(db)
        item = esd_inventory(db, intent)['states'][0]['pins'][0]
        self.assertIn('unconnected/pseudo/NC-pin-needs-exposure-decision:J1.1', item['gaps'])

    def test_unusual_connector_ref_can_be_declared(self):
        db, _ = ports()
        add(db, 'PORT_A', 'CUSTOM', [('A1', 'IO', 'IO')])
        c = cfg(db)
        c['connectors'] = {'PORT_A': {'citation': 'Synthetic connector identity', 'exposure': 'unknown',
                                   'pinout_complete': False, 'pins': {}}}
        ids = {x['id'] for x in esd_inventory(db, {'connector_esd': c})['states'][0]['pins']}
        self.assertIn('PORT_A.A1', ids)

    def test_series_path_is_retained_and_not_short_circuited(self):
        db, intent = ports(True)
        db['nets']['IO'].remove('D1.1')
        db['pin2net']['D1.1'] = 'PROTECTED'
        db['nets']['PROTECTED'] = ['D1.1']
        add(db, 'R1', '22R', [('1', '1', 'IO'), ('2', '2', 'PROTECTED')])
        intent['connector_esd'].update(input_sha256=input_fingerprint(db), states=cfg(db)['states'])
        item = esd_inventory(db, intent)['states'][0]['pins'][0]
        self.assertEqual(item['protectors'][0]['series_path'], ['R1'])

    def test_multichannel_array_does_not_spread_one_channel_mapping(self):
        db, intent = ports(True)
        add(db, 'J2', 'CONN', [('1', 'IO', 'IO2')])
        db['pin2net']['D1.3'] = 'IO2'; db['nets']['IO2'].append('D1.3')
        intent['connector_esd'].update(input_sha256=input_fingerprint(db), states=cfg(db)['states'])
        items = {p['node']: p for p in esd_inventory(db, intent)['states'][0]['pins']}
        self.assertEqual(items['J1.1']['coverage'], 'mapped-topology-candidate')
        self.assertEqual(items['J2.1']['coverage'], 'unmapped-protector-candidate')

    def test_zero_connectors_still_requires_discovery_reconciliation(self):
        plan = build_review_plan(board())
        item = next(c for c in plan['checks'] if c['check'] == 'connector_esd-discovery')
        self.assertTrue(item['inventory_gaps'])

    def test_unknown_jumper_position_cannot_connect_a_remote_protector(self):
        db,intent=ports(True)
        db['nets']['IO'].remove('D1.1');db['pin2net']['D1.1']='REMOTE';db['nets']['REMOTE']=['D1.1']
        add(db,'JP1','JUMPER',[('1','1','IO'),('2','2','REMOTE')])
        intent['connector_esd'].update(input_sha256=input_fingerprint(db),states=cfg(db)['states'])
        item=esd_inventory(db,intent)['states'][0]['pins'][0]
        self.assertEqual(item['coverage'],'no-protector-found')
        self.assertTrue(any('unconfirmed-jumper' in g for g in item['gaps']))
        intent['connector_esd']['states'][0]['jumpers']={'JP1':'closed'}
        self.assertEqual(esd_inventory(db,intent)['states'][0]['pins'][0]['coverage'],'mapped-topology-candidate')

    def test_changed_symbol_pin_or_no_connect_invalidates_context(self):
        for field,value in [('declared_pinname',{'J1.4':'EXTRA'}),('no_connect_nodes',['J1.1'])]:
            db,intent=ports();db[field]=value
            with self.assertRaisesRegex(ValueError,'input_sha256'):
                esd_inventory(db,intent)

    def test_path_limit_is_visible(self):
        db,intent=ports(True)
        db['nets']['IO'].remove('D1.1');db['pin2net']['D1.1']='N10';db['nets']['N10']=['D1.1']
        for i in range(10):
            add(db,'R'+str(i+1),'1R',[('1','1','IO' if i==0 else 'N'+str(i)),('2','2','N'+str(i+1))])
        intent['connector_esd'].update(input_sha256=input_fingerprint(db),states=cfg(db)['states'])
        item=esd_inventory(db,intent)['states'][0]['pins'][0]
        self.assertTrue(any('path-limit' in g for g in item['gaps']))
        self.assertEqual(item['coverage'],'no-protector-found')


class PassiveMathTests(unittest.TestCase):
    def fixture(self, kind='lowpass', tolerance=0):
        db, intent, evidence = rc(kind, tolerance)
        item = passive_inventory(db, intent)['states'][0]['networks'][0]
        return item['edges'], evidence['checks'][0]['model']

    def test_loaded_lowpass_matches_independent_impedance_divider(self):
        edges, model = self.fixture()
        metrics, notes = analytic_metrics(edges, model)
        fc = 1 / (2*math.pi * (1100*1000/2100) * 1e-6)
        self.assertAlmostEqual(metrics['cutoff_hz']['min'], fc, places=9)
        self.assertAlmostEqual(metrics['dc_gain']['max'], 1000/2100)
        for f in model['frequency_hz']:
            z = 1 / (1/1000 + 1j*2*math.pi*f*1e-6)
            expected = z / (1100 + z)
            actual = response(edges, model, f)
            bounds = response(edges, model, f, True)
            self.assertAlmostEqual(actual['gain'], abs(expected), places=12)
            self.assertLessEqual(bounds['min'], abs(expected)); self.assertGreaterEqual(bounds['max'], abs(expected))

    def test_loaded_highpass_uses_source_and_parallel_load(self):
        edges, model = self.fixture('highpass')
        metrics, _ = analytic_metrics(edges, model)
        self.assertAlmostEqual(metrics['cutoff_hz']['min'], 1/(2*math.pi*600*1e-6), places=9)
        self.assertAlmostEqual(metrics['high_frequency_gain']['min'], 500/600)
        zc = 1 / (1j*2*math.pi*100*1e-6)
        self.assertAlmostEqual(response(edges, model, 100)['gain'], abs(500/(100+zc+500)))

    def test_capacitive_load_changes_lowpass_cutoff(self):
        edges, model = self.fixture()
        base = analytic_metrics(edges, model)[0]['cutoff_hz']['min']
        model['load_capacitance_f'] = rg(1e-6)
        self.assertAlmostEqual(analytic_metrics(edges, model)[0]['cutoff_hz']['min'], base/2)

    def test_lc_f0_q_cutoff_and_frequency_response_are_distinct(self):
        edges, model = self.fixture('lc')
        metrics, _ = analytic_metrics(edges, model)
        a, b, d = 0.01*1e-6, 101*1e-6 + 0.01/1000, 1+101/1000
        self.assertAlmostEqual(metrics['natural_frequency_hz']['min'], math.sqrt(d/a)/(2*math.pi))
        self.assertAlmostEqual(metrics['q']['min'], math.sqrt(a*d)/b)
        fc = metrics['cutoff_hz']['min']
        self.assertAlmostEqual(response(edges, model, fc)['gain'], (1/d)/math.sqrt(2), places=12)
        self.assertNotAlmostEqual(fc, metrics['natural_frequency_hz']['min'])
        for f in [100, 2000, 10000]:
            s = 1j*2*math.pi*f
            self.assertAlmostEqual(response(edges, model, f)['gain'], abs(1/(a*s*s+b*s+d)), places=12)

    def test_nonzero_esr_retained_in_general_ac_not_silently_ignored(self):
        edges, model = self.fixture()
        model['components']['C1']['series_resistance_ohm'] = rg(100)
        self.assertTrue(analytic_metrics(edges, model)[1])
        f = 1000
        zc = 100 + 1/(1j*2*math.pi*f*1e-6)
        z = 1/(1/1000+1/zc)
        self.assertAlmostEqual(response(edges, model, f)['gain'], abs(z/(1100+z)), places=12)

    def test_general_two_stage_rc_uses_loading_between_stages(self):
        edges, model = self.fixture()
        edges.extend([{'ref':'R2','kind':'resistor','nets':['OUT','LAST']},
                      {'ref':'C2','kind':'capacitor','nets':['LAST','GND']}])
        model['components'].update(R2=rg(2000), C2=dict(rg(2e-6),series_resistance_ohm=rg(0)))
        model['output_net']='LAST'
        f=100
        s=1j*2*math.pi*f
        z2=1/(1/1000+s*2e-6)
        z1=1/(s*1e-6+1/(2000+z2))
        wanted=abs(z1/(1100+z1)*z2/(2000+z2))
        self.assertAlmostEqual(response(edges,model,f)['gain'],wanted,places=12)
        bound=response(edges,model,f,True)
        self.assertLessEqual(bound['min'],wanted); self.assertGreaterEqual(bound['max'],wanted)

    def test_interval_bounds_contain_interior_samples_not_only_corners(self):
        edges, model = self.fixture(tolerance=.02)
        expected = response(edges, model, 100, True)
        cutoff = analytic_metrics(edges, model)[0]['cutoff_hz']
        rng = random.Random(812)
        for _ in range(100):
            one = deepcopy(model)
            for spec in one['components'].values():
                x = rng.uniform(spec['min'],spec['max'])
                spec.update(min=x,max=x,nominal=x)
            actual=response(edges,one,100)['gain']
            self.assertLessEqual(expected['min'],actual); self.assertGreaterEqual(expected['max'],actual)
            fc=analytic_metrics(edges,one)[0]['cutoff_hz']['min']
            self.assertLessEqual(cutoff['min'],fc);self.assertGreaterEqual(cutoff['max'],fc)

    def test_ideal_source_and_explicit_open_load(self):
        edges, model = self.fixture()
        model['source_resistance_ohm']=rg(0);model['load_open']=True;model.pop('load_resistance_ohm')
        self.assertAlmostEqual(response(edges,model,1/(2*math.pi*.001))['gain'],1/math.sqrt(2))
        self.assertAlmostEqual(analytic_metrics(edges,model)[0]['dc_gain']['min'],1)

    def test_inductor_parser_requires_inductance_units(self):
        self.assertAlmostEqual(nominal('inductor','4u7'),4.7e-6)
        self.assertAlmostEqual(nominal('inductor','1e-3H'),1e-3)
        self.assertIsNone(nominal('inductor','600R@100MHz'))
        self.assertIsNone(nominal('inductor','103'))

    def test_lc_highpass_uses_general_ac(self):
        edges,model=self.fixture('lc')
        edges[0]['nets']=['OUT','GND'];edges[1]['nets']=['IN','OUT']
        f=1000;s=1j*2*math.pi*f
        zl=1+.01*s;zc=1/(1e-6*s);load=1/(1/1000+1/zl)
        expected=abs(load/(100+zc+load))
        self.assertAlmostEqual(response(edges,model,f)['gain'],expected,places=12)
        self.assertTrue(analytic_metrics(edges,model)[1])

    def test_oversize_and_floating_networks_cannot_pass_solver(self):
        edges,model=self.fixture()
        model['components']['R9']=rg(1000)
        edges.append({'ref':'R9','kind':'resistor','nets':['FLOAT1','FLOAT2']})
        with self.assertRaises(ValueError):response(edges,model,100)
        for i in range(20):
            ref='RX'+str(i);model['components'][ref]=rg(1000)
            edges.append({'ref':ref,'kind':'resistor','nets':['T'+str(i),'T'+str(i+1)]})
        with self.assertRaisesRegex(ValueError,'model-size'):response(edges,model,100)


class PassiveIntegrationTests(unittest.TestCase):
    def run_fixture(self, db, intent, evidence, directory):
        audit=bind_evidence(db,evidence,directory)
        evidence['checks'][0]['basis']['state']='RUN'
        self.assertEqual(validate_evidence(evidence),[])
        lint=Lint(db,intent=intent,evidence=evidence,datasheet_audit=audit)
        lint.run()
        return next(r for r in lint.results if r['check_id']=='FILTER-RUN'), audit

    def test_auto_recognition_and_unmatched_parts_are_accounted(self):
        db, _, _ = rc()
        add(db,'R9','0R',[('1','1','A'),('2','2','B')])
        items=passive_inventory(db)['states'][0]['networks']
        self.assertTrue(any(x['topology']=='RC-lowpass' for x in items))
        self.assertTrue(any(x['id']=='unresolved-R9' for x in items))

    def test_shared_parallel_caps_are_not_cartesian_filters(self):
        db,_,_=rc()
        for i in range(2,12):
            add(db,'C'+str(i),'1uF',[('1','1','OUT'),('2','2','GND')])
            add(db,'R'+str(i),'1K',[('1','1','IN'),('2','2','OUT')])
        items=passive_inventory(db)['states'][0]['networks']
        lowpass=[x for x in items if x['topology']=='RC-lowpass']
        self.assertEqual(len(lowpass),1)
        self.assertEqual(len(lowpass[0]['refs']),22)

    def test_pass_and_fail_are_actual_requirement_comparisons(self):
        for maximum, verdict in [(400,'PASS'),(100,'FAIL')]:
            with self.subTest(verdict=verdict), tempfile.TemporaryDirectory() as directory:
                db,intent,evidence=rc()
                evidence['checks'][0]['model']['metric_requirements']={'cutoff_hz':{'min':0,'max':maximum}}
                result,_=self.run_fixture(db,intent,evidence,directory)
                self.assertEqual(result['review_result'],verdict,result)

    def test_missing_inputs_never_default_to_ideal(self):
        for key in ('source_resistance_ohm','load_resistance_ohm','load_capacitance_f','validity_citation'):
            with self.subTest(key=key),tempfile.TemporaryDirectory() as directory:
                db,intent,evidence=rc();evidence['checks'][0]['model'].pop(key)
                result,_=self.run_fixture(db,intent,evidence,directory)
                self.assertEqual(result['review_result'],'INSUFFICIENT',result)

    def test_missing_tolerance_and_esr_remain_insufficient(self):
        for key in ('min','series_resistance_ohm'):
            with self.subTest(key=key),tempfile.TemporaryDirectory() as directory:
                db,intent,evidence=rc();evidence['checks'][0]['model']['components']['C1'].pop(key)
                result,_=self.run_fixture(db,intent,evidence,directory)
                self.assertEqual(result['review_result'],'INSUFFICIENT',result)

    def test_extra_parallel_branch_cannot_be_ignored(self):
        db,intent,evidence=rc()
        add(db,'R2','1K',[('1','1','OUT'),('2','2','GND')])
        intent['passive_networks'].update(input_sha256=input_fingerprint(db),states=cfg(db)['states'])
        inventory=passive_inventory(db,intent)
        evidence['checks'][0]['inventory_digest']=inventory['digest']
        evidence['checks'][0]['model']['boundaries']['R2.1']={'role':'load','citation':'Attempt to ignore branch'}
        with tempfile.TemporaryDirectory() as directory:
            result,_=self.run_fixture(db,intent,evidence,directory)
        self.assertEqual(result['review_result'],'INSUFFICIENT',result)
        self.assertIn('unmodelled passive branch:R2',result['detail'])

    def test_false_nominal_cannot_replace_bom(self):
        db,intent,evidence=rc()
        evidence['checks'][0]['model']['components']['R1'].update(rg(2000))
        with tempfile.TemporaryDirectory() as directory:
            result,_=self.run_fixture(db,intent,evidence,directory)
        self.assertEqual(result['review_result'],'INSUFFICIENT',result)

    def test_effective_capacitance_can_be_bound_separately_to_bom_nominal(self):
        db,intent,evidence=rc()
        spec=evidence['checks'][0]['model']['components']['C1']
        spec.update(rg(.5e-6),bom_nominal=1e-6,citation='Synthetic guaranteed capacitance at declared DC bias')
        with tempfile.TemporaryDirectory() as directory:
            result,_=self.run_fixture(db,intent,evidence,directory)
        self.assertEqual(result['review_result'],'PASS',result)
        self.assertAlmostEqual(result['calculation']['metrics']['cutoff_hz']['min'],607.6825099872367)

    def test_inactive_parallel_branch_follows_declared_assembly(self):
        db,intent,evidence=rc()
        add(db,'R2','1K',[('1','1','OUT'),('2','2','GND')])
        context=intent['passive_networks'];context.update(input_sha256=input_fingerprint(db),states=cfg(db)['states'])
        context['states'][0]['population']['R2']=False
        evidence['checks'][0]['inventory_digest']=passive_inventory(db,intent)['digest']
        with tempfile.TemporaryDirectory() as directory:
            result,_=self.run_fixture(db,intent,evidence,directory)
        self.assertEqual(result['review_result'],'PASS',result)

    def test_malformed_models_remain_pending_instead_of_crashing(self):
        for field,value in [('frequency_hz',12),('components',{'R1':None}),('boundaries',None),
                            ('source_resistance_ohm',True),('metric_requirements',{'cutoff_hz':None})]:
            db,intent,evidence=rc();evidence['checks'][0]['model'][field]=value
            self.assertTrue(model_errors(evidence['checks'][0]))

    def test_changed_inventory_or_state_cannot_use_old_result(self):
        for key in ('inventory_digest','state'):
            with self.subTest(key=key),tempfile.TemporaryDirectory() as directory:
                db,intent,evidence=rc()
                result,audit=self.run_fixture(db,intent,evidence,directory)
                self.assertEqual(result['review_result'],'PASS',result)
                check=evidence['checks'][0]
                if key=='state':check['basis']['state']='OFF'
                else:check[key]='stale'
                lint=Lint(db,intent=intent,evidence=evidence,datasheet_audit=audit);lint.run()
                self.assertEqual(lint.results[0]['review_result'],'INSUFFICIENT')
                plan=build_review_plan(db,intent,evidence=evidence,datasheet_audit=audit)
                bound=[c for c in plan['checks'] if c['object'].get('passive_networks')=='FILTER']
                self.assertTrue(all(not c.get('evidence_check_id') for c in bound))

    def test_changed_document_or_db_cannot_borrow_parameter_guarantee(self):
        for change in ('document','db'):
            with self.subTest(change=change),tempfile.TemporaryDirectory() as directory:
                db,intent,evidence=rc();result,audit=self.run_fixture(db,intent,evidence,directory)
                self.assertEqual(result['review_result'],'PASS')
                if change=='document':
                    for doc in Path(directory).glob('*.pdf'):doc.write_bytes(doc.read_bytes()+b'changed')
                else:
                    db['parts']['C1']['value']='2uF'
                    intent['passive_networks']['input_sha256']=input_fingerprint(db)
                lint=Lint(db,intent=intent,evidence=evidence,datasheet_audit=audit);lint.run()
                self.assertEqual(lint.results[0]['review_result'],'INSUFFICIENT')

    def test_overlap_is_not_false_failure_or_false_pass(self):
        db,intent,evidence=rc(tolerance=.1)
        item=passive_inventory(db,intent)['states'][0]['networks'][0]
        evidence['checks'][0]['model']['metric_requirements']={'cutoff_hz':{'min':290,'max':320}}
        self.assertEqual(evaluate(db,item,evidence['checks'][0])['status'],'INSUFFICIENT')

    def test_frequency_bound_requirement_can_prove_or_disprove_gain(self):
        for upper,want in [(1,'PASS'),(.1,'FAIL')]:
            db,intent,evidence=rc()
            model=evidence['checks'][0]['model'];model['metric_requirements']={}
            model['gain_requirements']=[{'frequency_hz':100,'min':0,'max':upper}]
            item=passive_inventory(db,intent)['states'][0]['networks'][0]
            self.assertEqual(evaluate(db,item,evidence['checks'][0])['status'],want)

    def test_plan_report_preserves_v3_and_rejects_deleted_pin_check(self):
        db,intent=ports()
        plan=build_review_plan(db,intent)
        report=pending_v3(plan,db)
        md,_,gate=render(plan,report,db,require_bindings=True)
        self.assertTrue(gate['valid'],gate)
        self.assertEqual(gate['release'],'NO_GO')
        removed=next(c for c in plan['checks'] if c['object'].get('node')=='J1.1' and c['object'].get('connector_esd'))
        plan['checks'].remove(removed);report['plan_digest']=fingerprint(plan)
        report['checks']=[c for c in report['checks'] if c['id']!=removed['id']]
        gate=validate_review(plan,report,db,require_bindings=True)
        self.assertFalse(gate['valid'])
        self.assertTrue(any('incomplete connector_esd planned coverage' in e for e in gate['errors']),gate)

    def test_hot_passive_child_survives_bound_v3_report_generation(self):
        db,intent,evidence=rc()
        with tempfile.TemporaryDirectory() as directory:
            result,audit=self.run_fixture(db,intent,evidence,directory)
            self.assertEqual(result['review_result'],'PASS')
            plan=build_review_plan(db,intent,evidence=evidence,datasheet_audit=audit)
            report=pending_v3(plan,db)
            _,_,gate=render(plan,report,db,require_bindings=True)
            self.assertTrue(gate['valid'],gate)
            bound=[c for c in plan['checks'] if c['object'].get('passive_networks')=='FILTER' and c.get('evidence_check_id')]
            self.assertEqual(len(bound),1)
            changed=deepcopy(bound[0]);changed.update(id='FAKE-CHILD',criterion='Different unreviewed metric')
            plan['checks'].append(changed);report=pending_v3(plan,db)
            gate=validate_review(plan,report,db,require_bindings=True)
            self.assertFalse(gate['valid'])

    def test_missing_protection_cannot_be_marked_pass(self):
        db,intent=ports();plan=build_review_plan(db,intent);report=pending_v3(plan,db)
        key=next(c['id'] for c in plan['checks'] if c['object'].get('node')=='J1.1' and c['object'].get('connector_esd'))
        row=next(c for c in report['checks'] if c['id']==key)
        row.update(review_result='PASS',evidence_confidence='A',blocking=False)
        gate=validate_review(plan,report,db,require_bindings=True)
        self.assertTrue(any('connector_esd gaps must be resolved' in e for e in gate['errors']))


if __name__=='__main__':
    unittest.main()
