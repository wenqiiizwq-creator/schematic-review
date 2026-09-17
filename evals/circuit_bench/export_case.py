#!/usr/bin/env python3
"""Export one frozen case as inspectable PST, inputs and a SEPARATE answer."""
import argparse
import json
from pathlib import Path

from run import load_dataset
from stimulus import prepare

HERE = Path(__file__).resolve().parent


def export_case(data, case_id, out):
    manifest, cases, answers = load_dataset(data)
    case = next((c for c in cases if c['id'] == case_id), None)
    if case is None:
        raise ValueError('Unknown case ID: ' + case_id)
    payload = prepare(case)
    out.mkdir(parents=True, exist_ok=False)
    for name, value in (('case.json', case), ('expected.json', answers[case_id]),
                        ('input.json', payload)):
        (out / name).write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    for name, content in payload['pst'].items():
        (out / name).write_text(content, encoding='utf-8')
    return {'id': case_id, 'out': str(out), 'dataset_digest': manifest['case_digest'],
            'scope': 'Synthetic fixture for inspection; expected.json is never a worker input'}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--data', type=Path, default=HERE / 'data')
    parser.add_argument('--id', required=True)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    try:
        print(json.dumps(export_case(args.data.resolve(), args.id, args.out.resolve()), indent=2))
    except (ValueError, OSError) as error:
        parser.exit(2, str(error) + '\n')
