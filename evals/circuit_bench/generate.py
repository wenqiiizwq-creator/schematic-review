#!/usr/bin/env python3
"""Deterministic, source-labelled circuits; no checker code is used to label them."""
import argparse
import copy
import hashlib
import json
from collections import Counter
from pathlib import Path

from oracle import judge

VERSION = 'circuit-bench-1.0'


def canonical(obj):
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def digest(obj):
    return hashlib.sha256(canonical(obj).encode()).hexdigest()


def r(a, b, ohm, index):
    return {'ref': 'R' + str(index), 'a': a, 'b': b,
            'ohm': ohm, 'tol': .01, 'fitted': True}


def make_cases():
    cases = []

    def add(seed, variants):
        for scale in (1, 2):
            for variant in variants:
                c = copy.deepcopy(seed)
                c.update(evidence_state='current', source='SYNTHETIC-1',
                         naming='semantic' if scale == 1 else 'anonymous')
                c['variant'] = variant
                c['scale'] = scale
                for resistor in c['resistors']:
                    resistor['ohm'] *= scale
                if c['kind'] == 'pull':
                    c['limits'] = [x * scale for x in c['limits']]
                if c['kind'] == 'rc':
                    c['cap_f'] /= scale
                if variant in ('high', 'low'):
                    for resistor in c['resistors']:
                        if c['kind'] == 'pull' or resistor['a'] == 'OUT':
                            resistor['ohm'] *= 4 if variant == 'high' else .2
                elif variant == 'missing_tolerance':
                    c['resistors'][0]['tol'] = None
                elif variant == 'missing_reference':
                    c['vref_bounds'] = None
                elif variant == 'absent':
                    for resistor in c['resistors']:
                        resistor['fitted'] = False
                elif variant in ('missing_document', 'stale_db', 'stale_document'):
                    c['evidence_state'] = variant
                elif variant == 'slow':
                    c['resistors'][0]['ohm'] *= 10
                elif variant == 'early':
                    c['sample_s'] = [.001, .0011]
                elif variant == 'missing_cap_tolerance':
                    c['cap_tol'] = None
                elif variant == 'missing_voltage_evidence':
                    c['voltage_evidence'] = False
                elif variant == 'corner_violation':
                    if c['kind'] == 'feedback':
                        c['limits'][1] = 2.41
                    elif c['kind'] == 'pull':
                        c['limits'][1] = 2350 * scale * 1.005
                    else:
                        c['sample_s'] = [.0094, .0096] if c['required'] == 'high' else [.0142, .0144]
                c['id'] = 'CB-%04d' % (len(cases) + 1)
                cases.append(c)

    feedback = {
        'feedback_two_resistors': ('dev', [('OUT', 'FB', 20000), ('FB', 'GND', 10000)]),
        'feedback_series_lower': ('dev', [('OUT', 'FB', 20000), ('FB', 'N1', 4000), ('N1', 'GND', 6000)]),
        'feedback_parallel_series': ('holdout', [('OUT', 'FB', 40000), ('OUT', 'FB', 40000),
                                               ('FB', 'N1', 4000), ('N1', 'GND', 6000)]),
        'feedback_bridge': ('challenge', [('OUT', 'N1', 10000), ('N1', 'FB', 10000),
                                         ('FB', 'N2', 5000), ('N2', 'GND', 5000), ('N1', 'N2', 30000)]),
    }
    for family, (split, edges) in feedback.items():
        seed = {'family': family, 'split': split, 'kind': 'feedback', 'track': 'native_calculation',
                'resistors': [r(*edge, i + 1) for i, edge in enumerate(edges)],
                'vref_typ': .8, 'vref_bounds': [.792, .808], 'bias_a': [-1e-7, 1e-7],
                'limits': [2.3, 2.5]}
        add(seed, ('normal', 'high', 'low', 'corner_violation', 'missing_tolerance', 'missing_reference',
                   'missing_document', 'stale_db', 'stale_document'))
    for number in (1, 2, 3):
        seed = {'family': 'pull_parallel_%d' % number, 'split': 'holdout' if number == 3 else 'dev',
                'kind': 'pull', 'track': 'native_calculation', 'limits': [1000, 4700],
                'resistors': [r('BUS', 'SUPPLY', 2350 * number, i + 1) for i in range(number)]}
        add(seed, ('normal', 'high', 'low', 'corner_violation', 'missing_tolerance', 'absent',
                   'missing_document', 'stale_db', 'stale_document'))
    for charge in (True, False):
        seed = {'family': 'rc_charge' if charge else 'rc_discharge',
                'split': 'dev' if charge else 'holdout', 'kind': 'rc', 'track': 'supplied_voltage_evidence',
                'resistors': [r('BUS', 'SUPPLY', 100000, 1)],
                'cap_f': 100e-9, 'cap_tol': .05, 'sample_s': [.02, .022],
                'supply_v': [3.2, 3.4] if charge else [0, 0],
                'initial_v': [0, 0] if charge else [3.2, 3.4],
                'threshold_v': 2 if charge else .8, 'required': 'high' if charge else 'low',
                'voltage_evidence': True}
        add(seed, ('normal', 'slow', 'early', 'corner_violation', 'missing_cap_tolerance', 'missing_voltage_evidence',
                   'missing_document', 'stale_db', 'stale_document'))
    # Reconstruct arithmetic only; no copied vendor schematic or model identity.
    for value, tol in ((1200, .01), (680, .01), (2200, .01), (1200, None)):
        cases.append({'id': 'CB-%04d' % (len(cases) + 1), 'family': 'ti_slva689_example',
                      'split': 'holdout', 'kind': 'pull', 'track': 'native_calculation',
                      'source': 'TI-SLVA689-2015', 'evidence_state': 'current', 'naming': 'semantic',
                      'variant': 'public_example', 'scale': 1,
                      'resistors': [dict(r('BUS', 'SUPPLY', value, 1), tol=tol)],
                      'limits': [(3.3 - .4) / .003, 300e-9 / (.8473 * 200e-12)],
                      'source_parameters': {'vcc_v': 3.3, 'vol_max_v': .4, 'iol_a': .003,
                                            'cb_f': 200e-12, 'tr_max_s': 300e-9},
                      'source_locator': 'SLVA689 February 2015, p.2 Eq.1/6 and p.4 Section 4'})
    return cases


def write_dataset(target):
    target.mkdir(parents=True, exist_ok=False)
    cases = make_cases()
    answers = [{'id': c['id'], 'case_sha256': digest(c), **judge(c)} for c in cases]
    for filename, rows in (('cases.jsonl', cases), ('answers.jsonl', answers)):
        (target / filename).write_text(''.join(canonical(row) + '\n' for row in rows), encoding='utf-8')
    hashes = {name: hashlib.sha256((Path(__file__).parent / name).read_bytes()).hexdigest()
              for name in ('generate.py', 'oracle.py')}
    manifest = {'version': VERSION, 'case_count': len(cases),
                'case_digest': digest(cases), 'answer_digest': digest(answers),
                'generator_sources': hashes,
                'split_counts': dict(Counter(c['split'] for c in cases)),
                'status_counts': dict(Counter(a['status'] for a in answers)),
                'family_splits': {c['family']: c['split'] for c in cases},
                'synthetic_or_reconstructed_only': True,
                'holdout_policy': 'Whole topology/source families. Public, not a secret test or security boundary.',
                'scope': 'Native resistor checks and supplied-voltage decision checks; no complete schematic review.'}
    (target / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return manifest


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--out', type=Path, required=True, help='New directory; existing datasets are never overwritten')
    print(json.dumps(write_dataset(parser.parse_args().out), ensure_ascii=False, indent=2))
