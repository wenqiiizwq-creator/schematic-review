"""Synthetic connectivity/state fixtures; no claims about real I2C compliance."""
import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
from electrical_contract import db_fingerprint
from i2c_topology import build_i2c_topology, validate_i2c_intent
from lint import Lint
from plan_review import build_review_plan, validate_intent
from validate_review import fingerprint, validate_review, SCOPE


def add(db, ref, value, pins, nc=False):
    db['parts'][ref] = {'value': value, 'part': value, 'nc': nc}
    for pin, name, net in pins:
        node = ref + '.' + pin
        db['nets'].setdefault(net, []).append(node)
        db['pin2net'][node] = net
        db['pinname'][node] = name


def fixture(links=()):
    db = {'nets': {}, 'pin2net': {}, 'pinname': {}, 'parts': {}, 'pseudo_nets': []}
    add(db, 'U1', 'TEST-I2C', [('1', 'SDA', 'I2C_SDA'), ('2', 'SCL', 'I2C_SCL')])
    models = {'U1': {'kind': 'endpoint', 'citation': 'synthetic pin map section 1'}}
    jumpers = {}
    for i, role in enumerate(('SDA', 'SCL'), 1):
        net = 'I2C_' + role
        for hop, value in enumerate(links):
            other = f'EXT_{role}_{hop}'
            ref = ('JP' if value == 'jumper' else 'R') + str(i) + str(hop)
            add(db, ref, value, [('1', '1', net), ('2', '2', other)])
            if value == 'jumper':
                models[ref] = {'kind': 'jumper', 'citation': 'synthetic jumper pin map'}
                jumpers[ref] = 'closed'
            net = other
        add(db, f'R{i}', '4.7K/1%', [('1', '1', net), ('2', '2', 'VCC_3V3')])
    cfg = {'schema_version': 1, 'db_sha256': db_fingerprint(db),
           'states': [{'id': 'run', 'citation': 'synthetic assembly B table 1',
                       'population': {r: True for r in db['parts']}, 'jumpers': jumpers}],
           'buses': [{'id': 'CTRL', 'sda': ['U1.1'], 'scl': ['U1.2'], 'citation': 'synthetic pin map section 1'}],
           'components': models, 'rails': {'VCC_3V3': 'synthetic power tree node A'}}
    return db, {'i2c_topology': cfg}


def rebind(db, intent):
    intent['i2c_topology']['db_sha256'] = db_fingerprint(db)


def regions(output, state='run'):
    return next(s['regions'] for s in output['states'] if s['id'] == state)


def at(output, net='I2C_SDA', state='run'):
    return next(r for r in regions(output, state) if net in r['nets'])


def report_for(plan, db):
    # Only used to isolate validator contract errors; not an electrical review.
    return {'schema_version': 2, 'plan_digest': fingerprint(plan), 'db_digest': fingerprint(db),
            'checks': [], 'findings': [], 'coverage': {}, 'summary': {}}


def pending_report(plan, db):
    """Complete but unresolved synthetic ledger for end-to-end gate validation."""
    rows = []
    for item in plan['checks']:
        row = {'id': item['id'], 'applicability': item['applicability'],
               'review_result': 'NA' if item['applicability'] == 'NOT_APPLICABLE' else 'INSUFFICIENT',
               'evidence_confidence': 'C', 'blocking': True,
               'rationale': 'Synthetic fixture has no electrical acceptance evidence',
               'evidence': [{'source': 'synthetic-fixture', 'locator': 'declared topology only'}],
               'handoff': copy.deepcopy(item['handoff']),
               'binding': {k: copy.deepcopy(item[k]) for k in ('object', 'criterion')}}
        if row['review_result'] == 'INSUFFICIENT':
            row.update(missing_inputs=['applicable specifications and state analysis'], potential_severity='P1')
        rows.append(row)
    scope = {k: next(p['id'] for p in plan['checks'] if p['check'] == 'coverage-' + k) for k in SCOPE}
    return {'schema_version': 2, 'binding_version': 1, 'plan_digest': fingerprint(plan), 'db_digest': fingerprint(db),
            'checks': rows, 'findings': [], 'scope_checks': scope,
            'coverage': {'components': {r: [scope['chains']] for r in db['parts']},
                'nets': {n: [scope['chains']] for n in db['nets']},
                'pins': {n: [scope['chains']] for n in db['pin2net']}, 'pages': {}, 'requirements': {}}}


class TopologyTests(unittest.TestCase):
    def test_direct_pullups_and_explicit_pair_are_discovered_without_verdict(self):
        db, intent = fixture()
        result = build_i2c_topology(db, intent)
        self.assertEqual(len(regions(result)), 2)
        self.assertEqual(at(result)['coverage'], 'DISCOVERED')
        self.assertEqual([p['ref'] for p in at(result)['pullups']], ['R1'])
        self.assertEqual(at(result)['gaps'], [])
        self.assertNotIn('review_result', at(result))
        self.assertEqual(len(result['states'][0]['buses']), 1)

    def test_multihop_closed_jumpers_find_renamed_remote_pullups(self):
        db, intent = fixture(('jumper', 'jumper', 'jumper'))
        r = at(build_i2c_topology(db, intent))
        self.assertEqual(len(r['nets']), 4)
        self.assertEqual(len(r['segments']), 1)
        self.assertEqual(len(r['edges']), 3)
        self.assertEqual([p['ref'] for p in r['pullups']], ['R1'])
        self.assertEqual(r['path_origin_net'], 'I2C_SDA')
        self.assertEqual(r['pullups'][0]['path'], ['JP10', 'JP11', 'JP12'])

    def test_fitted_zero_ohm_chain_keeps_all_physical_edges(self):
        db, intent = fixture(('0R', '0R'))
        r = at(build_i2c_topology(db, intent))
        self.assertEqual(len(r['edges']), 2)
        self.assertEqual(len(r['segments']), 1)
        self.assertEqual(len(r['pullups']), 1)

    def test_series_33_ohm_finds_pullup_without_shorting_nodes(self):
        db, intent = fixture(('33R/1%',))
        r = at(build_i2c_topology(db, intent))
        self.assertEqual(len(r['segments']), 2)
        self.assertEqual(r['edges'][0]['ohms'], 33)
        self.assertEqual(r['pullups'][0]['path'], ['R10'])
        self.assertEqual(len(r['pullups']), 1)
        self.assertNotIn('equivalent_ohms', r)
        self.assertEqual(Lint(db).pulls('I2C_SDA'), [])

    def test_open_jumper_cuts_conduction_but_far_side_stays_in_coverage(self):
        db, intent = fixture(('jumper',))
        intent['i2c_topology']['states'][0]['jumpers']['JP10'] = 'open'
        result = build_i2c_topology(db, intent)
        self.assertEqual(at(result)['nets'], ['I2C_SDA'])
        self.assertEqual(at(result)['pullups'], [])
        self.assertEqual(at(result, 'EXT_SDA_0')['pullups'][0]['ref'], 'R1')
        self.assertIn('confirm-bus-pins-and-pairing', at(result, 'EXT_SDA_0')['gaps'])

    def test_not_fitted_resistor_cannot_bridge(self):
        db, intent = fixture(('0R',))
        intent['i2c_topology']['states'][0]['population']['R10'] = False
        result = build_i2c_topology(db, intent)
        self.assertEqual(at(result)['nets'], ['I2C_SDA'])
        self.assertFalse(at(result)['pullups'])

    def test_unknown_population_not_inferred_from_nc_false(self):
        db, intent = fixture(('0R',))
        del intent['i2c_topology']['states'][0]['population']['R10']
        r = at(build_i2c_topology(db, intent))
        self.assertEqual(r['nets'], ['I2C_SDA'])
        self.assertIn('population:R10', r['gaps'])

    def test_explicit_variant_population_can_override_parsed_nc_with_citation(self):
        db, intent = fixture(('0R',))
        db['parts']['R10']['nc'] = True
        rebind(db, intent)
        self.assertEqual(len(at(build_i2c_topology(db, intent))['nets']), 2)

    def test_unproven_default_bridged_name_does_not_close_jumper(self):
        db, intent = fixture(('jumper',))
        db['parts']['JP10']['value'] = 'SolderJumper_2_Bridged'
        del intent['i2c_topology']['states'][0]['jumpers']['JP10']
        rebind(db, intent)
        r = at(build_i2c_topology(db, intent))
        self.assertIn('jumper-state-unverified:JP10', r['gaps'])
        self.assertFalse(r['pullups'])

    def test_each_state_gets_its_own_connectivity(self):
        db, intent = fixture(('jumper',))
        other = copy.deepcopy(intent['i2c_topology']['states'][0])
        other['id'] = 'option-open'
        other['jumpers']['JP10'] = 'open'
        intent['i2c_topology']['states'].append(other)
        result = build_i2c_topology(db, intent)
        self.assertTrue(at(result)['pullups'])
        self.assertFalse(at(result, state='option-open')['pullups'])
        self.assertNotEqual(at(result)['id'], at(result, state='option-open')['id'])

    def test_translator_ports_are_discovered_and_never_merged(self):
        db, intent = fixture()
        add(db, 'U3', 'TEST-LEVEL-SHIFTER', [('1', 'A1', 'I2C_SDA'), ('2', 'A2', 'I2C_SCL'),
            ('3', 'B1', 'REMOTE_DATA'), ('4', 'B2', 'REMOTE_CLOCK')])
        for i, net in enumerate(('REMOTE_DATA', 'REMOTE_CLOCK'), 3):
            add(db, 'R' + str(i), '2.2K/1%', [('1', '1', net), ('2', '2', 'VCC_1V8')])
        cfg = intent['i2c_topology']
        cfg['components']['U3'] = {'kind': 'level_shifter', 'citation': 'synthetic translator pinout',
            'ports': [{'sda': 'U3.1', 'scl': 'U3.2'}, {'sda': 'U3.3', 'scl': 'U3.4'}]}
        cfg['states'][0]['population'].update({'U3': True, 'R3': True, 'R4': True})
        cfg['rails']['VCC_1V8'] = 'synthetic power tree B'
        rebind(db, intent)
        result = build_i2c_topology(db, intent)
        self.assertEqual(len(regions(result)), 4)
        self.assertNotEqual(at(result)['id'], at(result, 'REMOTE_DATA')['id'])
        self.assertEqual({p['rail'] for p in at(result)['pullups']}, {'VCC_3V3'})
        self.assertEqual({p['rail'] for p in at(result, 'REMOTE_DATA')['pullups']}, {'VCC_1V8'})

    def test_unknown_ic_does_not_join_all_its_pins(self):
        db, intent = fixture()
        add(db, 'U9', 'UNKNOWN', [('1', 'A', 'I2C_SDA'), ('2', 'B', 'OTHER')])
        intent['i2c_topology']['states'][0]['population']['U9'] = True
        rebind(db, intent)
        r = at(build_i2c_topology(db, intent))
        self.assertNotIn('OTHER', r['nets'])
        self.assertIn('pin-role-or-model:U9', r['gaps'])

    def test_connector_keeps_external_pullup_unknown(self):
        db, intent = fixture()
        add(db, 'J1', 'CONN', [('1', '1', 'I2C_SDA'), ('2', '2', 'I2C_SCL')])
        intent['i2c_topology']['states'][0]['population']['J1'] = True
        rebind(db, intent)
        self.assertIn('external-port:J1', at(build_i2c_topology(db, intent))['gaps'])

    def test_parallel_pullups_are_listed_once_with_existing_equivalent_unchanged(self):
        db, intent = fixture(('0R',))
        add(db, 'R9', '4.7K/1%', [('1', '1', 'EXT_SDA_0'), ('2', '2', 'VCC_3V3')])
        intent['i2c_topology']['states'][0]['population']['R9'] = True
        rebind(db, intent)
        r = at(build_i2c_topology(db, intent))
        self.assertEqual([p['ref'] for p in r['pullups']], ['R1', 'R9'])
        lint = Lint(db)
        lint._hot_required_passive({'id': 'direct', 'net': 'EXT_SDA_0', 'kind': 'required_pull',
            'to': 'VCC_3V3', 'citation': 'synthetic', 'resistance_ohm': {'min': 2000, 'max': 3000}})
        self.assertEqual(lint.results[0]['review_result'], 'INSUFFICIENT')  # series branch still unsupported by Rule-09

    def test_no_intent_only_produces_unconfirmed_inventory(self):
        db, intent = fixture()
        result = build_i2c_topology(db)
        self.assertTrue(regions(result, 'UNSPECIFIED'))
        self.assertTrue(all(r['coverage'] == 'INCOMPLETE' for r in regions(result, 'UNSPECIFIED')))

    def test_spi_sclk_alone_is_not_an_i2c_seed(self):
        db = {'parts': {}, 'nets': {}, 'pin2net': {}, 'pinname': {}}
        add(db, 'U1', 'SPI', [('1', 'SCLK', 'SCLK')])
        self.assertFalse(regions(build_i2c_topology(db), 'UNSPECIFIED'))

    def test_nonstandard_names_use_explicit_pins_for_feature_and_topology(self):
        db, intent = fixture()
        for old, new in [('I2C_SDA', 'DATA'), ('I2C_SCL', 'CLOCK')]:
            db['nets'][new] = db['nets'].pop(old)
            for node in db['nets'][new]:
                db['pin2net'][node] = new
                db['pinname'][node] = 'GPIO'
        rebind(db, intent)
        plan = build_review_plan(db, intent)
        feature = next(p for p in plan['checks'] if p['check'] == 'feature-i2c')
        self.assertEqual(feature['applicability'], 'APPLICABLE')
        self.assertEqual(len(regions(plan['i2c_topology'])), 2)

    def test_pass_is_rejected_while_region_has_topology_gaps(self):
        db, intent = fixture()
        del intent['i2c_topology']['states'][0]['population']['R1']
        plan = build_review_plan(db, intent)
        item = next(p for p in plan['checks'] if p['check'].startswith('i2c-topology-') and p['object']['net'] == 'I2C_SDA')
        report = report_for(plan, db)
        report['checks'] = [{'id': item['id'], 'applicability': 'APPLICABLE', 'review_result': 'PASS'}]
        outcome = validate_review(plan, report, db)
        self.assertTrue(any('topology gaps must be resolved' in e for e in outcome['errors']))

    def test_validator_rejects_changed_generated_criterion(self):
        db, intent = fixture()
        plan = build_review_plan(db, intent)
        item = next(p for p in plan['checks'] if p['object'].get('i2c_region'))
        item['criterion'] = 'presence of any resistor is sufficient'
        outcome = validate_review(plan, report_for(plan, db), db)
        self.assertTrue(any('generated criterion/object changed' in e for e in outcome['errors']))

    def test_complete_pending_ledger_is_valid_but_never_releases(self):
        db, intent = fixture(('33R',))
        plan = build_review_plan(db, intent)
        outcome = validate_review(plan, pending_report(plan, db), db, require_bindings=True)
        self.assertTrue(outcome['valid'], outcome['errors'])
        self.assertEqual(outcome['release'], 'NO_GO')

    def test_reviewed_topology_can_pass_without_passing_electrical_checks(self):
        db, intent = fixture(('33R',))
        plan = build_review_plan(db, intent)
        report = pending_report(plan, db)
        ids = {p['id'] for p in plan['checks'] if p['check'].startswith('i2c-topology-')}
        for row in report['checks']:
            if row['id'] in ids:
                row.update(review_result='PASS', evidence_confidence='B', rationale='Synthetic topology and population verified')
                row.pop('missing_inputs')
                row.pop('potential_severity')
        outcome = validate_review(plan, report, db, require_bindings=True)
        self.assertTrue(outcome['valid'], outcome['errors'])
        self.assertEqual(outcome['release'], 'NO_GO')

    def test_sda_and_scl_conductively_short_together_remain_gap(self):
        db, intent = fixture()
        add(db, 'R9', '0R', [('1', '1', 'I2C_SDA'), ('2', '2', 'I2C_SCL')])
        intent['i2c_topology']['states'][0]['population']['R9'] = True
        rebind(db, intent)
        self.assertIn('sda-scl-connected:CTRL', at(build_i2c_topology(db, intent))['gaps'])

    def test_cycle_terminates_and_keeps_all_edges(self):
        db, intent = fixture(('0R', '0R'))
        add(db, 'R9', '0R', [('1', '1', 'I2C_SDA'), ('2', '2', 'EXT_SDA_1')])
        intent['i2c_topology']['states'][0]['population']['R9'] = True
        rebind(db, intent)
        r = at(build_i2c_topology(db, intent))
        self.assertEqual(len(r['nets']), 3)
        self.assertEqual(len(r['edges']), 3)

    def test_search_limit_is_an_explicit_gap(self):
        db, intent = fixture(('0R', '0R'))
        with patch('i2c_topology.MAX_TRACE_NETS', 2):
            result = build_i2c_topology(db, intent)
        self.assertTrue(result['states'][0]['gaps'])
        self.assertTrue(all(r['coverage'] == 'INCOMPLETE' for r in regions(result)))

    def test_rail_name_is_only_a_hint(self):
        db, intent = fixture()
        intent['i2c_topology']['rails'] = {}
        self.assertIn('rail-identity:VCC_3V3', at(build_i2c_topology(db, intent))['gaps'])

    def test_deterministic_under_input_order_change(self):
        db, intent = fixture(('0R', '33R'))
        expected = build_i2c_topology(db, intent)
        db['nets'] = {k: list(reversed(v)) for k, v in reversed(list(db['nets'].items()))}
        self.assertEqual(build_i2c_topology(db, intent), expected)

    def test_stale_db_is_rejected_by_both_api_and_plan(self):
        db, intent = fixture()
        db['parts']['R1']['value'] = '10K'
        for build in (build_i2c_topology, build_review_plan):
            with self.assertRaisesRegex(ValueError, 'stale db_sha256'):
                build(db, intent)

    def test_invalid_inputs_are_rejected_without_guessing(self):
        db, intent = fixture()
        changes = [('states', []), ('states', [{'id': 'run', 'citation': 'x', 'population': {'R1': 'true'}}]),
                   ('buses', [{'id': 'X', 'sda': ['MISSING.1'], 'scl': ['U1.2'], 'citation': 'x'}]),
                   ('components', {'U1': {'kind': ['jumper'], 'citation': 'x'}})]
        for key, value in changes:
            modified = copy.deepcopy(intent)
            modified['i2c_topology'][key] = value
            with self.subTest(key=key, value=value):
                self.assertTrue(validate_i2c_intent(modified, db))
        modified = copy.deepcopy(intent)
        modified['i2c_topology']['statse'] = []
        self.assertTrue(validate_intent(modified))

    def test_multi_pin_ic_cannot_be_declared_a_two_pin_bridge(self):
        db, intent = fixture()
        add(db, 'U3', 'TRANSLATOR', [('1', 'A', 'I2C_SDA'), ('2', 'B', 'OTHER'), ('3', 'VCC', 'VCC_3V3')])
        intent['i2c_topology']['components']['U3'] = {'kind': 'jumper', 'citation': 'invalid synthetic model'}
        rebind(db, intent)
        self.assertTrue(validate_i2c_intent(intent, db))

    def test_malformed_db_containers_are_rejected_before_hashing(self):
        db, intent = fixture()
        for field, value in [('nets', None), ('nets', {'A': None}), ('parts', []),
                             ('parts', {'R1': None}), ('pin2net', []), ('pin2net', {'U1.1': []}),
                             ('pinname', []), ('pseudo_nets', None), ('integrity', [])]:
            broken = copy.deepcopy(db)
            broken[field] = value
            with self.subTest(field=field, value=value):
                for build in (build_i2c_topology, build_review_plan):
                    with self.assertRaisesRegex(ValueError, 'db.'):
                        build(broken, intent)

    def test_null_db_nets_cli_reports_error_without_traceback(self):
        db, intent = fixture()
        db['nets'] = None
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            (folder / 'db.json').write_text(json.dumps(db))
            (folder / 'intent.json').write_text(json.dumps(intent))
            for script in ('i2c_topology.py', 'plan_review.py', 'lint.py'):
                target = folder / (script + '.json')
                run = subprocess.run([sys.executable, '-B', str(SCRIPTS / script), str(folder / 'db.json'),
                    '--intent', str(folder / 'intent.json'), '--json', str(target)], text=True, capture_output=True)
                self.assertNotEqual(run.returncode, 0)
                self.assertNotIn('Traceback', run.stderr)
                self.assertIn('db.nets', run.stderr)
                self.assertFalse(target.exists())

    def test_explicit_null_intent_is_not_silently_dropped_by_cli(self):
        db, _ = fixture()
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            (folder / 'db.json').write_text(json.dumps(db))
            (folder / 'intent.json').write_text('null')
            for script in ('i2c_topology.py', 'plan_review.py', 'lint.py'):
                target = folder / (script + '.json')
                base = [sys.executable, '-B', str(SCRIPTS / script), str(folder / 'db.json'), '--json', str(target)]
                run = subprocess.run(base + ['--intent', str(folder / 'intent.json')], text=True, capture_output=True)
                self.assertNotEqual(run.returncode, 0)
                self.assertIn('explicit intent.json root must be an object', run.stderr)
                self.assertNotIn('Traceback', run.stderr)
                self.assertFalse(target.exists())
                run = subprocess.run(base, text=True, capture_output=True)
                self.assertEqual(run.returncode, 0, run.stderr)

    def test_plan_has_per_region_coverage_plus_three_electrical_checks(self):
        db, intent = fixture(('33R',))
        plan = build_review_plan(db, intent)
        new = [p for p in plan['checks'] if p['object'].get('i2c_region')]
        self.assertEqual(len(new), 8)
        self.assertEqual(sum(p['check'].startswith('i2c-topology-') for p in new), 2)
        self.assertTrue(all(p['review_result'] is None for p in new))
        electrical = [p for p in new if p['stage'] == 'ER4']
        self.assertTrue(all(p['readiness'] == 'WAITING_EVIDENCE' for p in electrical))
        self.assertTrue(any(p['check'] == 'i2c-required-pull' for p in plan['checks']))

    def test_merge_rejects_changed_state_even_when_db_is_unchanged(self):
        db, intent = fixture(('jumper',))
        before = build_review_plan(db, intent)
        intent['i2c_topology']['states'][0]['jumpers']['JP10'] = 'open'
        with self.assertRaisesRegex(ValueError, 'I2C topology/state/assembly changed'):
            build_review_plan(db, intent, previous_plan=before)

    def test_same_context_merge_preserves_manual_check(self):
        db, intent = fixture()
        before = build_review_plan(db, intent)
        manual = copy.deepcopy(before['checks'][0])
        manual['id'] = 'MANUAL-1'
        before['checks'].append(manual)
        after = build_review_plan(db, intent, previous_plan=before)
        self.assertTrue(any(x['id'] == 'MANUAL-1' for x in after['checks']))

    def test_validator_recomputes_inventory_and_rejects_deleted_coverage(self):
        db, intent = fixture()
        plan = build_review_plan(db, intent)
        plan['checks'] = [p for p in plan['checks'] if not p['check'].startswith('i2c-topology-')]
        outcome = validate_review(plan, report_for(plan, db), db)
        self.assertTrue(any('incomplete I2C planned coverage' in e for e in outcome['errors']))
        plan['i2c_topology']['states'][0]['regions'][0]['gaps'] = ['tampered']
        outcome = validate_review(plan, report_for(plan, db), db)
        self.assertTrue(any('inventory/binding is stale or modified' in e for e in outcome['errors']))

    def test_cli_plan_lint_inventory_agree_and_duplicate_json_is_rejected(self):
        db, intent = fixture(('0R', '33R'))
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            (folder / 'db.json').write_text(json.dumps(db))
            (folder / 'intent.json').write_text(json.dumps(intent))
            for script, args in [('i2c_topology.py', ['--json', str(folder / 'inventory.json')]),
                ('plan_review.py', ['--json', str(folder / 'plan.json'), '--i2c-topology-json', str(folder / 'plan-topology.json')]),
                ('lint.py', ['--plan-json', str(folder / 'lint-plan.json'), '--i2c-topology-json', str(folder / 'lint-topology.json')])]:
                run = subprocess.run([sys.executable, '-B', str(SCRIPTS / script), str(folder / 'db.json'),
                    '--intent', str(folder / 'intent.json')] + args, text=True, capture_output=True)
                self.assertEqual(run.returncode, 0, run.stderr)
            expected = json.loads((folder / 'inventory.json').read_text())
            self.assertEqual(json.loads((folder / 'plan-topology.json').read_text()), expected)
            self.assertEqual(json.loads((folder / 'lint-topology.json').read_text()), expected)
            (folder / 'intent.json').write_text('{"i2c_topology":{},"i2c_topology":{}}')
            run = subprocess.run([sys.executable, '-B', str(SCRIPTS / 'i2c_topology.py'), str(folder / 'db.json'),
                '--intent', str(folder / 'intent.json'), '--json', str(folder / 'bad.json')], text=True, capture_output=True)
            self.assertNotEqual(run.returncode, 0)
            self.assertFalse((folder / 'bad.json').exists())


if __name__ == '__main__':
    unittest.main()
