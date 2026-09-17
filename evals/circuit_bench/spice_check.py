#!/usr/bin/env python3
"""Cross-check the oracle against ngspice, independently of schematic-review.

Use the oracle's minimum/maximum parameter corners, then solve the original
physical network in SPICE. Feedback uses an explicitly ideal high-gain servo.
This is a numerical cross-check of bounded ideal models, not silicon validation.
"""
import argparse
import hashlib
import json
import math
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from oracle import numerical_corners
from run import load_dataset


def deck(case, corner):
    name = {'OUT': 'out', 'FB': 'fb', 'GND': '0', 'N1': 'n1', 'N2': 'n2',
            'BUS': 'bus', 'SUPPLY': 'supply'}
    lines = ['Circuit Bench synthetic ideal-model cross-check', '.options reltol=1e-9 abstol=1e-15 vntol=1e-10']
    for r in corner['resistors']:
        lines.append('%s %s %s %.15g' % (r['ref'], name[r['a']], name[r['b']], r['ohm']))
    if case['kind'] == 'feedback':
        lines += ['Vreference ref 0 %.15g' % corner['vref'], 'Ereg out 0 ref fb 1e9',
                  'Ibias fb 0 %.15g' % corner['bias']]
        operation = ['op', 'let actual = v(out)', 'print actual']
    elif case['kind'] == 'pull':
        lines += ['Vsupply supply 0 0', 'Itest 0 bus 1']
        operation = ['op', 'let actual = v(bus)', 'print actual']
    else:
        lines += ['Vsupply supply 0 %.15g' % corner['supply'],
                  'Csample bus 0 %.15g ic=%.15g' % (corner['cap'], corner['initial']),
                  '.ic v(bus)=%.15g' % corner['initial']]
        sample = corner['time']
        tau = corner['resistors'][0]['ohm'] * corner['cap']
        step = min(sample / 2000, tau / 200)
        operation = ['tran %.15g %.15g 0 %.15g uic' % (step, sample * 1.001, step),
                     'meas tran actual find v(bus) at=%.15g' % sample]
    return '\n'.join(lines + ['.control', 'set noaskquit', 'set numdgt=15'] + operation + ['quit', '.endc', '.end', ''])


def crosscheck(data, out, binary, scratch_root):
    manifest, cases, answers = load_dataset(data)
    binary = shutil.which(binary) or (str(Path(binary)) if Path(binary).is_file() else None)
    if not binary:
        raise ValueError('ngspice not found; cannot claim independent numerical cross-check')
    version = subprocess.run([binary, '--version'], capture_output=True, text=True, timeout=10)
    out.mkdir(parents=True, exist_ok=False)
    scratch_root.mkdir(parents=True, exist_ok=True)
    results = []
    with tempfile.TemporaryDirectory(prefix='circuit-spice-', dir=scratch_root) as scratch:
        for case in cases:
            if answers[case['id']]['window'] is None:
                continue
            corners = numerical_corners(case)
            selected = [('minimum', min(corners, key=lambda x: x['value'])),
                        ('maximum', max(corners, key=lambda x: x['value']))]
            for boundary, corner in selected:
                text = deck(case, corner)
                path = Path(scratch) / (case['id'] + '-' + boundary + '.cir')
                path.write_text(text, encoding='utf-8')
                proc = subprocess.run([binary, '-n', '-b', str(path)], capture_output=True,
                                      text=True, timeout=15, cwd=scratch)
                found = re.findall(r'^actual\s*=\s*([-+0-9.eE]+)', proc.stdout, re.M | re.I)
                value = float(found[-1]) if found else None
                # RC transient interpolation has finite step error. Feedback's
                # 1e9 servo gain has a bounded nonzero error against ideal KCL.
                good = (proc.returncode == 0 and value is not None and math.isfinite(value)
                        and math.isclose(value, corner['value'], rel_tol=2e-5, abs_tol=2e-6))
                results.append({'id': case['id'], 'family': case['family'], 'boundary': boundary,
                                'expected': corner['value'], 'ngspice': value, 'passed': good,
                                'relative_error': abs(value - corner['value']) / max(abs(corner['value']), 1e-12)
                                                  if value is not None else None,
                                'deck': text, 'deck_sha256': hashlib.sha256(text.encode()).hexdigest(),
                                'stdout': proc.stdout, 'stderr': proc.stderr, 'returncode': proc.returncode})
    summary = {'schema_version': 1, 'dataset_digest': manifest['case_digest'],
               'answer_digest': manifest['answer_digest'], 'ngspice_version': version.stdout.strip(),
               'checked_corners': len(results), 'passed_corners': sum(r['passed'] for r in results),
               'case_count': len({r['id'] for r in results}),
               'rtol': 2e-5, 'atol': 2e-6,
               'scope': 'Extremal corners of fully specified ideal models; excludes unknown evidence and absent pulls',
               'results': results}
    (out / 'spice-results.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({k: v for k, v in summary.items() if k not in ('results', 'ngspice_version')}, indent=2))
    return bool(results) and all(r['passed'] for r in results)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--data', type=Path, default=Path(__file__).resolve().parent / 'data')
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--ngspice', default='ngspice')
    parser.add_argument('--scratch-root', type=Path, default=Path('/tmp/codex-work'))
    args = parser.parse_args()
    raise SystemExit(0 if crosscheck(args.data, args.out, args.ngspice, args.scratch_root) else 3)
