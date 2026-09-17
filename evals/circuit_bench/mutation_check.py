#!/usr/bin/env python3
"""Inject known implementation faults ONLY into disposable checker copies."""
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
MUTATIONS = {
    'nominal_instead_of_corners': (
        "window.update(min=min(corners), max=max(corners))",
        "window.update(min=window['typ'], max=window['typ'])"),
    'first_pull_instead_of_parallel': (
        'for x in candidates)\n                      for key',
        'for x in candidates[:1])\n                      for key'),
}
ALWAYS_PASS = '''

# Deliberate mutation for benchmark testing; never install this copy.
def _mutant_hot(self):
    for check in self.evidence.get('checks', []):
        self.record_pass(check['rule'], check, 'MUTANT: bypass checks', scope='MUTANT')
Lint.run_hot = _mutant_hot
'''


def check_mutants(args):
    args.out.mkdir(parents=True, exist_ok=False)
    args.scratch_root.mkdir(parents=True, exist_ok=True)
    records = []
    with tempfile.TemporaryDirectory(prefix='circuit-mutants-', dir=args.scratch_root) as directory:
        root = Path(directory)
        for name in list(MUTATIONS) + ['always_pass']:
            target = root / name
            shutil.copytree(args.sut / 'scripts', target / 'scripts',
                            ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
            script = target / 'scripts' / 'lint.py'
            text = script.read_text(encoding='utf-8')
            if name in MUTATIONS:
                old, new = MUTATIONS[name]
                if text.count(old) != 1:
                    raise ValueError('Mutation anchor not unique; review against this version: ' + name)
                text = text.replace(old, new, 1)
            else:
                text += ALWAYS_PASS
            script.write_text(text, encoding='utf-8')
            out = args.out / name
            command = [sys.executable, '-B', str(HERE / 'run.py'), '--sut', str(target),
                       '--data', str(args.data), '--out', str(out), '--split', 'dev', '--require-pass',
                       '--scratch-root', str(args.scratch_root)]
            proc = subprocess.run(command, capture_output=True, text=True, timeout=90,
                                  env=dict(os.environ, PYTHONDONTWRITEBYTECODE='1'))
            if not (out / 'results.json').exists():
                raise ValueError('Mutant did not reach scoring: ' + proc.stderr[-2000:])
            result = json.loads((out / 'results.json').read_text(encoding='utf-8'))
            killed = proc.returncode == 3 and not result['acceptance']['declared_scope_pass']
            records.append({'mutation': name, 'detected': killed, 'returncode': proc.returncode,
                            'metrics': result['metrics'], 'sut_source_digest': result['sut_source_digest'],
                            'dataset_digest': result['dataset_digest'], 'protocol_digest': result['protocol_digest']})
            print(name + ': ' + ('DETECTED' if killed else 'SURVIVED'), flush=True)
    summary = {'mutants': records, 'all_detected': all(r['detected'] for r in records),
               'scope': 'Three deliberate code faults, dev partition only; not an exhaustive fault model'}
    (args.out / 'mutation-results.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return summary['all_detected']


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--sut', type=Path, default=HERE.parents[1])
    parser.add_argument('--data', type=Path, default=HERE / 'data')
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--scratch-root', type=Path, default=Path('/tmp/codex-work'))
    raise SystemExit(0 if check_mutants(parser.parse_args()) else 3)
