"""Synthetic direct-net/assembly/ledger tests, not physical PDN validation."""
import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
from decoupling import build_decoupling_inventory, input_fingerprint, parse_capacitance, validate_decoupling_intent
from plan_review import build_review_plan, validate_intent
from validate_review import validate_review, fingerprint
from test_i2c_topology import add, pending_report


def add_device(db, cfg, ref, supply='VCC_3V3', ground='GND'):
    add(db, ref, 'TEST-IC', [('1', 'VDD', supply), ('2', 'VSS', ground), ('3', 'GPIO', ref + '_IO')])
    cfg['devices'][ref] = {'mpn': 'TEST-IC-EXACT', 'package': 'TEST-PKG-3',
        'identity_citation': 'synthetic BOM identity mapping row ' + ref,
        'citation': 'synthetic full pinout table 1', 'pinout_complete': True,
        'pins': {'1': {'name': 'VDD', 'role': 'power'}, '2': {'name': 'VSS', 'role': 'return'},
                 '3': {'name': 'GPIO', 'role': 'other'}}}
    cfg['groups'].append({'id': ref + '-VDD', 'ref': ref, 'supply_nodes': [ref + '.1'],
        'return_nodes': [ref + '.2'], 'citation': 'synthetic grouping requirement section 2',
        'requirements': [{'id': kind, 'kind': kind, 'criterion': 'synthetic ' + kind + ' requirement',
                          'citation': 'synthetic applicable specification section ' + kind}
                         for kind in ('connection', 'capacitance', 'rating')]})


def fixture(values=('100nF', '1uF')):
    db = {'parts': {}, 'nets': {}, 'pin2net': {}, 'pinname': {}, 'pseudo_nets': []}
    cfg = {'schema_version': 1, 'devices': {}, 'components': {}, 'groups': [],
           'states': [{'id': 'run', 'citation': 'synthetic BOM B option A', 'population': {}}]}
    add_device(db, cfg, 'U1')
    for i, value in enumerate(values, 1):
        ref = 'C' + str(i)
        add(db, ref, value, [('1', '1', 'VCC_3V3'), ('2', '2', 'GND')])
        cfg['components'][ref] = {'kind': 'capacitor', 'citation': 'synthetic BOM type ' + ref}
    cfg['states'][0]['population'] = {r: True for r in db['parts']}
    cfg['input_sha256'] = input_fingerprint(db)
    return db, {'decoupling': cfg}


def rebind(db, intent):
    intent['decoupling']['input_sha256'] = input_fingerprint(db)


def state(inv, sid='run'):
    return next(s for s in inv['states'] if s['id'] == sid)


def group(inv, ref='U1', sid='run'):
    return next(g for g in state(inv, sid)['groups'] if g['ref'] == ref and g['origin'] == 'declared')


def move_pin(db, node, net):
    db['nets'][db['pin2net'][node]].remove(node)
    db['pin2net'][node] = net
    db['nets'].setdefault(net, []).append(node)


def ledger(plan, db):
    report = pending_report(plan, db)
    scope = report['scope_checks']['chains']
    for node in db.get('declared_pinname', {}):
        report['coverage']['pins'][node] = [scope]
    return report


def mark_pass(row):
    row.update(review_result='PASS', evidence_confidence='B',
               rationale='Synthetic narrow criterion reviewed, not physical performance')
    row.pop('missing_inputs', None)
    row.pop('potential_severity', None)


class CapacitanceParserTests(unittest.TestCase):
    def test_explicit_units_and_bom_suffixes(self):
        for value, expected in [('100nF', 1e-7), ('4.7u/10%/16V/X7R', 4.7e-6),
                                ('4n7', 4.7e-9), ('0.1 µF', 1e-7), ('1e-6F', 1e-6),
                                ('220p', 220e-12), ('2mF', .002), ('1F', 1.0)]:
            with self.subTest(value=value):
                self.assertAlmostEqual(parse_capacitance(value) / expected, 1)

    def test_ambiguous_invalid_nonfinite_or_underflow_values_stay_unknown(self):
        for value in (None, [], 104, '104', '10k', '16V', 'C1 100nF', '0uF', '-1uF',
                      'nanF', 'infF', '1e99999999F', '1e-99999999F', '100nF~1uF', '1MF'):
            with self.subTest(value=value):
                self.assertIsNone(parse_capacitance(value))


class DecouplingInventoryTests(unittest.TestCase):
    def test_direct_pair_has_unique_nominal_count_without_verdict(self):
        db, intent = fixture()
        inv = build_decoupling_inventory(db, intent)
        g = group(inv)
        self.assertEqual(g['coverage'], 'DISCOVERED')
        self.assertEqual(g['fitted_count'], 2)
        self.assertAlmostEqual(g['nominal_total_f'], 1.1e-6)
        self.assertNotIn('review_result', g)
        self.assertNotIn('effective_capacitance_f', g)

    def test_zero_caps_do_not_erase_group_or_become_failure(self):
        db, intent = fixture(())
        g = group(build_decoupling_inventory(db, intent))
        self.assertEqual(g['fitted_count'], 0)
        self.assertEqual(g['nominal_total_f'], 0)
        self.assertEqual(g['observations'], ['no-confirmed-fitted-matching-capacitor'])
        self.assertEqual(g['coverage'], 'DISCOVERED')

    def test_power_to_signal_cap_is_rejected(self):
        db, intent = fixture(('100nF',))
        move_pin(db, 'C1.2', 'U1_IO')
        rebind(db, intent)
        g = group(build_decoupling_inventory(db, intent))
        self.assertFalse(g['capacitors'])
        self.assertEqual(g['rejected_capacitors'][0]['ref'], 'C1')

    def test_named_grounds_are_not_merged(self):
        db, intent = fixture(('100nF',))
        move_pin(db, 'U1.2', 'AGND')
        rebind(db, intent)
        g = group(build_decoupling_inventory(db, intent))
        self.assertEqual(g['return_nets'], ['AGND'])
        self.assertEqual(g['fitted_count'], 0)

    def test_same_cap_shared_by_two_devices_is_one_physical_cap(self):
        db, intent = fixture(('100nF',))
        cfg = intent['decoupling']
        add_device(db, cfg, 'U2')
        cfg['states'][0]['population']['U2'] = True
        rebind(db, intent)
        inv = build_decoupling_inventory(db, intent)
        self.assertEqual(len(state(inv)['capacitors']), 1)
        self.assertEqual(len(state(inv)['capacitors'][0]['matched_groups']), 2)
        self.assertEqual(group(inv)['fitted_capacitors'], group(inv, 'U2')['fitted_capacitors'])
        self.assertNotIn('local_decoupling_pass', group(inv))

    def test_each_supply_pin_is_preserved_without_recounting_cap(self):
        db, intent = fixture(('100nF',))
        db['pin2net']['U1.4'] = 'VCC_3V3'
        db['pinname']['U1.4'] = 'VDD2'
        db['nets']['VCC_3V3'].append('U1.4')
        cfg = intent['decoupling']
        cfg['devices']['U1']['pins']['4'] = {'name': 'VDD2', 'role': 'power'}
        cfg['groups'][0]['supply_nodes'].append('U1.4')
        rebind(db, intent)
        g = group(build_decoupling_inventory(db, intent))
        self.assertEqual(g['supply_nodes'], ['U1.1', 'U1.4'])
        self.assertEqual(g['fitted_count'], 1)

    def test_official_power_pin_absent_from_netlist_becomes_candidate(self):
        db, intent = fixture()
        intent['decoupling']['devices']['U1']['pins']['EP'] = {'name': 'VDD_EP', 'role': 'power'}
        inv = build_decoupling_inventory(db, intent)
        candidates = [g for g in state(inv)['groups'] if g['origin'] == 'candidate']
        self.assertEqual(candidates[0]['supply_nodes'], ['U1.EP'])
        self.assertIn('unconnected-or-pseudo-pin:U1.EP', candidates[0]['gaps'])
        self.assertIn('official-pin-absent:U1.EP', inv['discovery_gaps'])

    def test_symbol_only_unconnected_power_pin_is_not_lost(self):
        db, intent = fixture()
        db['declared_pinname'] = {'U1.4': 'VDD_AUX'}
        db['declared_pintype'] = {'U1.4': 'POWER'}
        rebind(db, intent)
        inv = build_decoupling_inventory(db, intent)
        self.assertTrue(any('U1.4' in g['supply_nodes'] for g in state(inv)['groups']))
        self.assertIn('pin-not-in-official-map:U1.4', group(inv)['gaps'])

    def test_exact_pin_map_discovers_nonstandard_names_and_negative_rail(self):
        db, intent = fixture(('100nF',))
        move_pin(db, 'U1.1', 'NEGATIVE_A')
        move_pin(db, 'C1.1', 'NEGATIVE_A')
        db['pinname']['U1.1'] = 'P1'
        db['pinname']['U1.2'] = 'P2'
        rebind(db, intent)
        g = group(build_decoupling_inventory(db, intent))
        self.assertEqual(g['supply_nets'], ['NEGATIVE_A'])
        self.assertEqual(g['fitted_count'], 1)

    def test_ferrite_and_zero_ohm_are_boundaries_not_shortcuts(self):
        for value, ref in [('FERRITE', 'FB1'), ('0R', 'R1'), ('33R', 'R1')]:
            with self.subTest(value=value):
                db, intent = fixture(('10uF',))
                move_pin(db, 'U1.1', 'LOCAL')
                add(db, ref, value, [('1', '1', 'VCC_3V3'), ('2', '2', 'LOCAL')])
                intent['decoupling']['states'][0]['population'][ref] = True
                rebind(db, intent)
                g = group(build_decoupling_inventory(db, intent))
                self.assertEqual(g['fitted_count'], 0)
                self.assertEqual(g['supply_nets'], ['LOCAL'])
                self.assertEqual(g['boundaries'][0]['ref'], ref)
                self.assertFalse(g['boundaries'][0]['crossed'])

    def test_group_spanning_supply_or_return_nets_stays_incomplete(self):
        db, intent = fixture()
        for role, field in [('power', 'supply_nodes'), ('return', 'return_nodes')]:
            broken, cfg = copy.deepcopy(db), copy.deepcopy(intent)
            broken['pin2net']['U1.4'] = 'OTHER'
            broken['pinname']['U1.4'] = 'OTHER'
            broken['nets']['OTHER'] = ['U1.4']
            cfg['decoupling']['devices']['U1']['pins']['4'] = {'name': 'OTHER', 'role': role}
            cfg['decoupling']['groups'][0][field].append('U1.4')
            rebind(broken, cfg)
            g = group(build_decoupling_inventory(broken, cfg))
            self.assertEqual(g['coverage'], 'INCOMPLETE')
            self.assertFalse(g['fitted_capacitors'])

    def test_unparsed_fitted_cap_keeps_count_and_partial_sum(self):
        db, intent = fixture(('100nF', 'CAP_UNKNOWN'))
        g = group(build_decoupling_inventory(db, intent))
        self.assertEqual(g['fitted_count'], 2)
        self.assertAlmostEqual(g['known_nominal_subtotal_f'], 1e-7)
        self.assertIsNone(g['nominal_total_f'])
        self.assertIn('capacitance-unparsed:C2', g['capacitance_gaps'])

    def test_dnp_is_visible_but_not_counted(self):
        db, intent = fixture(('100nF', 'CAP_UNKNOWN'))
        intent['decoupling']['states'][0]['population']['C2'] = False
        inv = build_decoupling_inventory(db, intent)
        self.assertEqual(group(inv)['fitted_count'], 1)
        self.assertEqual(group(inv)['capacitance_gaps'], [])
        self.assertIn('C2', group(inv)['capacitors'])
        self.assertFalse(state(inv)['capacitors'][1]['populated'])

    def test_nc_false_does_not_prove_population(self):
        db, intent = fixture(('100nF',))
        del intent['decoupling']['states'][0]['population']['C1']
        g = group(build_decoupling_inventory(db, intent))
        self.assertIn('population:C1', g['gaps'])
        self.assertEqual(g['fitted_count'], 0)
        self.assertIsNone(g['nominal_total_f'])

    def test_cited_variant_can_override_parsed_nc(self):
        db, intent = fixture(('100nF/NC',))
        db['parts']['C1']['nc'] = True
        rebind(db, intent)
        inv = build_decoupling_inventory(db, intent)
        self.assertEqual(group(inv)['fitted_count'], 1)
        self.assertTrue(state(inv)['capacitors'][0]['parsed_nc'])

    def test_prefix_is_hint_not_confirmed_component_kind(self):
        db, intent = fixture(('100nF',))
        intent['decoupling']['components'] = {}
        g = group(build_decoupling_inventory(db, intent))
        self.assertIn('component-kind:C1', g['gaps'])
        self.assertEqual(g['capacitors'], ['C1'])
        self.assertEqual(g['fitted_count'], 0)

    def test_nonstandard_capacitor_ref_uses_explicit_type(self):
        db, intent = fixture(())
        add(db, 'XC_A', '220nF', [('A', 'A', 'VCC_3V3'), ('K', 'K', 'GND')])
        intent['decoupling']['components']['XC_A'] = {'kind': 'capacitor', 'citation': 'synthetic BOM'}
        intent['decoupling']['states'][0]['population']['XC_A'] = True
        rebind(db, intent)
        self.assertEqual(group(build_decoupling_inventory(db, intent))['fitted_capacitors'], ['XC_A'])

    def test_capacitor_array_or_missing_pin_cannot_be_counted(self):
        for missing in (False, True):
            db, intent = fixture(('100nF',))
            if missing:
                db['nets']['GND'].remove('C1.2')
                del db['pin2net']['C1.2']
            else:
                db['declared_pinname'] = {'C1.3': '3'}
            rebind(db, intent)
            g = group(build_decoupling_inventory(db, intent))
            self.assertEqual(g['fitted_count'], 0)
            self.assertTrue(g['gaps'])

    def test_capacitor_shorted_to_same_net_is_not_decoupling(self):
        db, intent = fixture(('100nF',))
        move_pin(db, 'C1.2', 'VCC_3V3')
        rebind(db, intent)
        inv = build_decoupling_inventory(db, intent)
        self.assertEqual(group(inv)['fitted_count'], 0)
        self.assertIn('capacitor-same-net:C1', state(inv)['capacitors'][0]['gaps'])

    def test_each_assembly_state_has_separate_inventory_and_ids(self):
        db, intent = fixture()
        other = copy.deepcopy(intent['decoupling']['states'][0])
        other['id'] = 'option-B'
        other['population']['C2'] = False
        intent['decoupling']['states'].append(other)
        inv = build_decoupling_inventory(db, intent)
        self.assertEqual(group(inv)['fitted_count'], 2)
        self.assertEqual(group(inv, sid='option-B')['fitted_count'], 1)
        self.assertNotEqual(group(inv)['id'], group(inv, sid='option-B')['id'])

    def test_without_intent_candidates_are_incomplete(self):
        db, _ = fixture()
        inv = build_decoupling_inventory(db)
        self.assertTrue(inv['discovery_gaps'])
        self.assertTrue(state(inv, 'UNSPECIFIED')['groups'])
        self.assertTrue(all(g['coverage'] == 'INCOMPLETE' for g in state(inv, 'UNSPECIFIED')['groups']))

    def test_unknown_functional_device_is_not_silently_ignored(self):
        db, intent = fixture()
        add(db, 'CUSTOM_IC', 'TEST', [('1', 'P', 'VCC_3V3')])
        rebind(db, intent)
        self.assertIn('CUSTOM_IC', build_decoupling_inventory(db, intent)['unverified_device_refs'])

    def test_order_determinism(self):
        db, intent = fixture()
        before = build_decoupling_inventory(db, intent)
        db['nets'] = {k: list(reversed(v)) for k, v in reversed(list(db['nets'].items()))}
        db['parts'] = dict(reversed(list(db['parts'].items())))
        self.assertEqual(build_decoupling_inventory(db, intent), before)

    def test_stale_connected_declared_and_pseudo_inputs_are_rejected(self):
        db, intent = fixture()
        changes = [('declared_pinname', {'U1.EP': 'VDD'}), ('pseudo_nets', ['GND'])]
        for field, value in changes:
            broken = copy.deepcopy(db)
            broken[field] = value
            with self.assertRaisesRegex(ValueError, 'stale input_sha256'):
                build_decoupling_inventory(broken, intent)
        db['parts']['C1']['value'] = '10uF'
        with self.assertRaisesRegex(ValueError, 'stale input_sha256'):
            build_review_plan(db, intent)

    def test_bad_indexes_remain_explicit_gaps(self):
        db, intent = fixture()
        db['nets']['GND'].append('C1.1')
        rebind(db, intent)
        inv = build_decoupling_inventory(db, intent)
        self.assertIn('inconsistent-index:C1.1', inv['discovery_gaps'])
        self.assertIsNone(group(inv)['nominal_total_f'])

    def test_invalid_schema_variants_fail_without_type_errors(self):
        db, intent = fixture()
        changes = [('states', None), ('states', [[]]), ('devices', []), ('devices', {'U1': None}),
                   ('groups', [None]), ('groups', [{'id': [], 'ref': [], 'supply_nodes': [[]]}]),
                   ('components', {'C1': {'kind': [], 'citation': 'x'}}), ('unknown_field', True)]
        for key, value in changes:
            broken = copy.deepcopy(intent)
            broken['decoupling'][key] = value
            with self.subTest(key=key):
                self.assertTrue(validate_decoupling_intent(broken, db))
                self.assertTrue(validate_intent(broken))
                with self.assertRaises(ValueError):
                    build_decoupling_inventory(db, broken)

    def test_duplicate_pin_membership_and_wrong_role_are_rejected(self):
        db, intent = fixture()
        extra = copy.deepcopy(intent['decoupling']['groups'][0])
        extra['id'] = 'duplicate'
        intent['decoupling']['groups'].append(extra)
        self.assertTrue(validate_decoupling_intent(intent, db))
        intent['decoupling']['groups'] = [extra]
        extra['supply_nodes'] = ['U1.2']
        self.assertTrue(validate_decoupling_intent(intent, db))


class DecouplingPlanTests(unittest.TestCase):
    def test_plan_separates_coverage_and_three_electrical_criteria(self):
        db, intent = fixture()
        plan = build_review_plan(db, intent)
        new = [p for p in plan['checks'] if p['check'].startswith('decoupling-')]
        self.assertEqual(len(new), 5)
        self.assertTrue(all(p['review_result'] is None for p in new))
        electrical = [p for p in new if p.get('analysis_required')]
        self.assertEqual(len(electrical), 3)
        self.assertTrue(all(p['readiness'] == 'WAITING_EVIDENCE' for p in electrical))
        self.assertTrue(all(p['required_material_refs'] == ['C1', 'C2', 'U1'] for p in electrical))
        self.assertEqual(sum(p['handoff']['required'] for p in new), 1)

    def test_pending_ledger_is_valid_no_go(self):
        db, intent = fixture()
        plan = build_review_plan(db, intent)
        outcome = validate_review(plan, ledger(plan, db), db, require_bindings=True)
        self.assertTrue(outcome['valid'], outcome['errors'])
        self.assertEqual(outcome['release'], 'NO_GO')

    def test_coverage_pass_never_promotes_electrical_rows(self):
        db, intent = fixture()
        plan = build_review_plan(db, intent)
        report = ledger(plan, db)
        selected = {p['id'] for p in plan['checks'] if p['check'].startswith('decoupling-coverage-')}
        for row in report['checks']:
            if row['id'] in selected:
                mark_pass(row)
        outcome = validate_review(plan, report, db, require_bindings=True)
        self.assertTrue(outcome['valid'], outcome['errors'])
        self.assertEqual(outcome['release'], 'NO_GO')

    def test_value_gap_blocks_capacitance_not_separate_connection_claim(self):
        db, intent = fixture(('CAP_UNKNOWN',))
        plan = build_review_plan(db, intent)
        report = ledger(plan, db)
        for kind in ('connection', 'capacitance'):
            selected = next(p['id'] for p in plan['checks'] if p['check'].startswith('decoupling-DECAP-') and p['criterion'] == 'synthetic ' + kind + ' requirement')
            mark_pass(next(r for r in report['checks'] if r['id'] == selected))
            outcome = validate_review(plan, report, db)
            self.assertEqual(outcome['valid'], kind == 'connection', outcome['errors'])

    def test_missing_requirements_or_population_cannot_pass(self):
        for missing in ('requirements', 'population'):
            db, intent = fixture()
            if missing == 'requirements':
                intent['decoupling']['groups'][0]['requirements'] = []
            else:
                del intent['decoupling']['states'][0]['population']['C1']
            plan = build_review_plan(db, intent)
            report = ledger(plan, db)
            key = next(p['id'] for p in plan['checks'] if p.get('domain') == 'DECOUPLING')
            mark_pass(next(r for r in report['checks'] if r['id'] == key))
            outcome = validate_review(plan, report, db)
            self.assertTrue(any('decoupling gaps must be resolved' in e for e in outcome['errors']))

    def test_changed_state_or_clause_rejects_merge_without_db_change(self):
        db, intent = fixture()
        before = build_review_plan(db, intent)
        for change in ('population', 'clause'):
            altered = copy.deepcopy(intent)
            if change == 'population':
                altered['decoupling']['states'][0]['population']['C1'] = False
            else:
                altered['decoupling']['groups'][0]['requirements'][0]['criterion'] = 'different'
            with self.assertRaisesRegex(ValueError, 'decoupling input/state/assembly changed'):
                build_review_plan(db, altered, previous_plan=before)

    def test_manual_check_is_preserved_without_result_migration(self):
        db, intent = fixture()
        before = build_review_plan(db, intent)
        manual = copy.deepcopy(next(p for p in before['checks'] if p['check'] == 'coverage-chains'))
        manual.update(id='MANUAL-POWER', review_result='PASS', evidence_confidence='A')
        before['checks'].append(manual)
        after = build_review_plan(db, intent, previous_plan=before)
        kept = next(p for p in after['checks'] if p['id'] == 'MANUAL-POWER')
        self.assertIsNone(kept['review_result'])

    def test_tampered_inventory_deleted_check_and_changed_criterion_rejected(self):
        db, intent = fixture()
        original = build_review_plan(db, intent)
        for mutation in ('inventory', 'delete', 'criterion', 'strip-inventory', 'clear-gaps'):
            plan = copy.deepcopy(original)
            selected = next(p for p in plan['checks'] if p.get('domain') == 'DECOUPLING')
            if mutation == 'inventory':
                plan['decoupling']['states'][0]['groups'][0]['fitted_count'] = 99
            elif mutation == 'delete':
                plan['checks'].remove(selected)
            elif mutation == 'criterion':
                selected['criterion'] = 'any cap means pass'
            elif mutation == 'strip-inventory':
                del plan['decoupling']
            else:
                selected['required_inputs'] = []
            outcome = validate_review(plan, ledger(plan, db), db)
            self.assertFalse(outcome['valid'], mutation)
            self.assertTrue(any('decoupling' in e for e in outcome['errors']), outcome['errors'])

    def test_inventory_validation_requires_current_db(self):
        db, intent = fixture()
        plan = build_review_plan(db, intent)
        outcome = validate_review(plan, ledger(plan, db))
        self.assertTrue(any('decoupling inventory validation requires --db' in e for e in outcome['errors']))

    def test_cli_three_entrypoints_emit_same_inventory(self):
        db, intent = fixture()
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            (folder / 'db.json').write_text(json.dumps(db))
            (folder / 'intent.json').write_text(json.dumps(intent))
            outputs = []
            for script in ('decoupling.py', 'plan_review.py', 'lint.py'):
                target = folder / (script + '-inventory.json')
                args = ['--json', str(target)] if script == 'decoupling.py' else ['--decoupling-json', str(target), '--json', str(folder / (script + '-output.json'))]
                run = subprocess.run([sys.executable, '-B', str(SCRIPTS / script), str(folder / 'db.json'),
                    '--intent', str(folder / 'intent.json')] + args, text=True, capture_output=True)
                self.assertEqual(run.returncode, 0, run.stderr)
                outputs.append(json.loads(target.read_text()))
            self.assertEqual(outputs[0], outputs[1])
            self.assertEqual(outputs[0], outputs[2])

    def test_cli_rejects_malformed_and_ambiguous_json_without_output(self):
        db, intent = fixture()
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            for bad in ('null', '[]', '{"decoupling":null}', '{"decoupling":{},"decoupling":{}}',
                        '{"decoupling":{"x":NaN}}'):
                (folder / 'db.json').write_text(json.dumps(db))
                (folder / 'intent.json').write_text(bad)
                for script in ('decoupling.py', 'plan_review.py', 'lint.py'):
                    target = folder / (script + '.json')
                    run = subprocess.run([sys.executable, '-B', str(SCRIPTS / script), str(folder / 'db.json'),
                        '--intent', str(folder / 'intent.json'), '--json', str(target)], text=True, capture_output=True)
                    self.assertNotEqual(run.returncode, 0, (script, bad))
                    self.assertNotIn('Traceback', run.stderr)
                    self.assertFalse(target.exists())

    def test_malformed_declared_pin_map_is_rejected(self):
        db, intent = fixture()
        for value in ([], None, {'U1.1': []}):
            db['declared_pinname'] = value
            with self.assertRaisesRegex(ValueError, 'db.declared_pinname'):
                build_decoupling_inventory(db, intent)


if __name__ == '__main__':
    unittest.main()
