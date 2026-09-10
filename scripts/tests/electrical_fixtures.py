"""Synthetic, explicitly bounded source documents for electrical regression tests."""
from pathlib import Path
from audit_datasheets import build_datasheet_audit
from electrical_contract import db_fingerprint, dependency_refs, document_fingerprint


def bind_evidence(database, evidence, directory):
    required = sorted({ref for check in evidence['checks']
                       for ref in dependency_refs(database, check)})
    pending = build_datasheet_audit(database, required_refs=required)
    entries = []
    for material in pending['materials']:
        path = Path(directory) / (material['material_id'] + '.pdf')
        path.write_bytes(b'%PDF-1.4\nSYNTHETIC electrical test specification; not a real datasheet\n')
        entries.append({
            'identity': material['identity'], 'status': 'FOUND',
            'identity_verified': True, 'source_kind': 'package', 'path': str(path),
            'document_model': material['identity'], 'document_version': 'SYNTHETIC-1',
        })
    audit = build_datasheet_audit(database, resolution={'schema_version': 1, 'entries': entries},
                                  required_refs=required)
    for check in evidence['checks']:
        sources = []
        for ref in dependency_refs(database, check):
            material = next(x for x in audit['materials'] if ref in x['refdes'])
            doc = material['document']
            sources.append({
                'ref': ref, 'identity': material['identity'],
                'document_model': doc['document_model'], 'document_version': doc['document_version'],
                'sha256': document_fingerprint(doc['path']), 'locator': 'synthetic test definition',
                'identity_resolution': 'Synthetic MPN and package, as explicitly specified in test db',
            })
        check['basis'] = {'db_sha256': db_fingerprint(database),
                          'state': 'synthetic populated variant; stated test operating conditions',
                          'sources': sources}
    return audit


def pin_analysis(low, high, time_low=0, time_high=1):
    return {'voltage_v': {'min': low, 'max': high},
            'sample_window_s': {'min': time_low, 'max': time_high},
            'method': 'synthetic circuit corner calculation',
            'calculation': 'Voltage bounds follow the explicitly supplied synthetic circuit conditions',
            'loading': 'Synthetic test specifies all loading; no additional unmodelled leakage',
            'conditions': 'Synthetic test specifies rails, initial conditions, sample times and tolerances'}


def divider_model(source='VOUT_3V3', reference='GND'):
    return {'source_net': source, 'reference_net': reference,
            'bias_current_a': {'min': 0, 'max': 0},
            'ignored_nodes': {'U1.1': 'Synthetic zero-current FB input, bias included in model'}}
