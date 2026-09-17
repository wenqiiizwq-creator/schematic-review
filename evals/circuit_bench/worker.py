#!/usr/bin/env python3
"""Separate-process checker adapter. Never imports the oracle or reads answers."""
import argparse
import contextlib
import copy
import io
import json
import sys
import traceback
from pathlib import Path


def evaluate(payload, sut, directory):
    if payload.get('synthetic_only') is not True:
        raise ValueError('This adapter is restricted to explicit synthetic test specifications')
    sys.path.insert(0, str(sut / 'scripts'))
    from parse_netlist import build, self_check
    from lint import Lint
    from electrical_contract import db_fingerprint, dependency_refs, document_fingerprint, validate_evidence
    from audit_datasheets import build_datasheet_audit, validate_datasheet_audit
    for name, text in payload['pst'].items():
        if name not in ('pstchip.dat', 'pstxprt.dat', 'pstxnet.dat', 'netlist.log'):
            raise ValueError('Unexpected fixture path')
        (directory / name).write_text(text, encoding='utf-8')
    db = build(str(directory))
    with contextlib.redirect_stdout(io.StringIO()):
        self_check(db)
    expected = payload['connectivity']
    if db['nets'] != expected['nets'] or db['pinname'] != expected['pinname']:
        raise ValueError('Parser output differs from independently constructed connectivity')
    if set(db['parts']) != set(expected['parts']):
        raise ValueError('Parser lost or invented parts')
    for ref, part in expected['parts'].items():
        for key in ('value', 'prim', 'nc'):
            if db['parts'][ref][key] != part[key]:
                raise ValueError('Parser changed component %s.%s' % (ref, key))
    check = copy.deepcopy(payload['check'])
    required = dependency_refs(db, check)
    pending = build_datasheet_audit(db, required_refs=required)
    # A real JSON specification document, explicitly synthetic. Do not create
    # pretend PDFs or claim a manufacturer's identity was verified.
    specpath = directory / 'SYNTHETIC-specification.json'
    specpath.write_text(json.dumps(payload['specification'], sort_keys=True, indent=2) + '\n', encoding='utf-8')
    entries = []
    if payload['evidence_state'] != 'missing_document':
        for material in pending['materials']:
            entries.append({'identity': material['identity'], 'status': 'FOUND',
                            'identity_verified': True, 'source_kind': 'package', 'path': str(specpath),
                            'document_model': material['identity'], 'document_version': 'SYNTHETIC-1'})
    audit = build_datasheet_audit(db, required_refs=required,
                                 resolution={'schema_version': 1, 'entries': entries})
    sources = []
    for ref in required:
        material = next(x for x in audit['materials'] if ref in x['refdes'])
        if material['status'] != 'AVAILABLE':
            continue
        document = material['document']
        sources.append({'ref': ref, 'identity': material['identity'],
                        'document_model': document['document_model'], 'document_version': document['document_version'],
                        'sha256': document_fingerprint(str(specpath)),
                        'locator': 'SYNTHETIC-specification.json',
                        'identity_resolution': 'Synthetic component defined by this fixture; no real part or datasheet'})
    check['basis'] = {'db_sha256': db_fingerprint(db),
                      'state': 'Synthetic fixture; explicit population, bounds and sampling conditions', 'sources': sources}
    if payload['evidence_state'] == 'stale_db':
        check['basis']['db_sha256'] = '0' * 64
    elif payload['evidence_state'] == 'stale_document':
        for source in sources:
            source['sha256'] = '0' * 64
    evidence = {'schema_version': 1, 'checks': [check]}
    errors = validate_evidence(evidence) + validate_datasheet_audit(audit, db)
    if errors:
        raise ValueError('; '.join(errors))
    engine = Lint(db, evidence=evidence, datasheet_audit=audit)
    engine.run()
    rows = [r for r in engine.results if r['check_id'] == check['id']]
    if len(rows) != 1:
        raise ValueError('Expected exactly one explicit per-check result, got %d' % len(rows))
    return {'status': rows[0]['review_result'], 'result': rows[0], 'parser_match': True,
            'db_sha256': db_fingerprint(db), 'document_sha256': document_fingerprint(str(specpath)),
            'cold_candidate_count': len(engine.F),
            'scope': 'Synthetic PST parse plus one supplied check; not full review or document extraction'}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--sut', type=Path, required=True)
    parser.add_argument('--input', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    try:
        result = evaluate(json.loads(args.input.read_text(encoding='utf-8')), args.sut, args.input.parent)
    except (Exception, SystemExit) as error:
        result = {'status': 'ERROR', 'error': str(error), 'traceback': traceback.format_exc()}
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    sys.exit(2 if result['status'] == 'ERROR' else 0)
