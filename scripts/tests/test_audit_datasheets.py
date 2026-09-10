import pathlib
import sys
import tempfile
import unittest

SCRIPTS = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))

from audit_datasheets import (
    build_datasheet_audit,
    validate_datasheet_audit,
    validate_resolution,
)


def sample_db():
    return {
        'parts': {
            'U1': {
                'part': 'REG-X', 'value': 'REG-X', 'prim': 'REG-X',
                'jedec': 'QFN', 'nc': False,
            },
            'U2': {
                'part': 'SOC-X-PRIM', 'value': 'SOC-X', 'prim': 'SOC-X-PRIM',
                'jedec': 'BGA', 'nc': False,
            },
            'U3': {
                'part': 'SOC-X-PRIM', 'value': 'SOC-X', 'prim': 'SOC-X-PRIM',
                'jedec': 'BGA', 'nc': False,
            },
            'D1': {
                'part': 'TVS-X', 'value': 'TVS-X', 'prim': 'TVS-X',
                'jedec': 'SMA', 'nc': True,
            },
            'R1': {
                'part': 'R', 'value': '10K', 'prim': 'R',
                'jedec': '0402', 'nc': False,
            },
        },
    }


def by_identity(audit):
    return {item['identity']: item for item in audit['materials']}


class DatasheetAuditTests(unittest.TestCase):
    def test_local_filename_is_candidate_not_automatic_proof(self):
        with tempfile.TemporaryDirectory() as temp:
            path = pathlib.Path(temp) / 'REG-X_datasheet_RevA.pdf'
            path.write_bytes(b'%PDF-1.4\n')
            audit = build_datasheet_audit(sample_db(), [temp])

        materials = by_identity(audit)
        self.assertEqual(materials['REG-X']['status'], 'NEEDS_VERIFICATION')
        self.assertEqual(materials['SOC-X']['status'], 'MISSING')
        self.assertEqual(materials['SOC-X']['refdes'], ['U2', 'U3'])
        self.assertEqual(audit['summary']['required_materials'], 2)
        self.assertEqual(audit['summary']['unresolved'], 2)
        self.assertEqual(validate_datasheet_audit(audit, sample_db()), [])
        actions = {item['identity']: item['action']
                   for item in audit['agent_requests']}
        self.assertEqual(actions['REG-X'], 'VERIFY_LOCAL_DATASHEET')
        self.assertEqual(actions['SOC-X'], 'FETCH_DATASHEET_ONLINE')

    def test_missing_datasheet_directory_still_generates_agent_fetches(self):
        audit = build_datasheet_audit(
            sample_db(), ['/definitely/missing/datasheet-directory'])
        self.assertEqual(audit['summary']['missing'], 2)
        self.assertTrue(any(
            item['code'] == 'DATASHEET_PATH_MISSING'
            for item in audit['diagnostics']))
        self.assertEqual(
            {item['action'] for item in audit['agent_requests']},
            {'FETCH_DATASHEET_ONLINE'})

    def test_agent_found_and_not_found_results_are_material_specific(self):
        with tempfile.TemporaryDirectory() as temp:
            found = pathlib.Path(temp) / 'reg-x.pdf'
            found.write_bytes(b'%PDF-1.4\n')
            resolution = {
                'schema_version': 1,
                'entries': [
                    {
                        'identity': 'REG-X',
                        'status': 'FOUND',
                        'identity_verified': True,
                        'source_kind': 'network',
                        'path': str(found),
                        'source_url': 'https://example.test/reg-x.pdf',
                        'document_model': 'REG-X',
                        'document_version': 'Rev.A',
                        'retrieved_at': '2026-09-02',
                    },
                    {
                        'identity': 'SOC-X',
                        'status': 'NOT_FOUND',
                        'searched_sources': [
                            'LCSC query SOC-X',
                            'manufacturer official website query SOC-X',
                        ],
                        'searched_at': '2026-09-02',
                    },
                ],
            }
            self.assertEqual(validate_resolution(resolution, temp), [])
            audit = build_datasheet_audit(
                sample_db(), resolution=resolution, resolution_base=temp)

        materials = by_identity(audit)
        self.assertEqual(materials['REG-X']['status'], 'AVAILABLE')
        self.assertEqual(materials['SOC-X']['status'], 'NOT_FOUND')
        self.assertEqual(
            audit['user_messages'],
            ['找不到这颗物料的 datasheet：SOC-X（位号：U2、U3）。'
             '请提供该物料的原厂 datasheet。'])
        request = next(item for item in audit['agent_requests']
                       if item['identity'] == 'SOC-X')
        self.assertEqual(request['action'], 'REQUEST_USER_DATASHEET')
        self.assertEqual(validate_datasheet_audit(audit), [])

    def test_resolution_rejects_unverified_or_shallow_outcomes(self):
        resolution = {
            'schema_version': 1,
            'entries': [
                {
                    'identity': 'REG-X',
                    'status': 'FOUND',
                    'identity_verified': False,
                    'source_kind': 'network',
                    'path': '/does/not/exist.pdf',
                },
                {
                    'identity': 'SOC-X',
                    'status': 'NOT_FOUND',
                    'searched_sources': ['manufacturer only'],
                },
            ],
        }
        errors = validate_resolution(resolution)
        self.assertTrue(any('identity_verified' in item for item in errors))
        self.assertTrue(any('searched_sources' in item for item in errors))
        self.assertTrue(any('searched_at' in item for item in errors))

    def test_build_rejects_resolution_without_manufacturer_search(self):
        resolution = {
            'schema_version': 1,
            'entries': [{
                'identity': 'SOC-X',
                'status': 'NOT_FOUND',
                'searched_sources': ['LCSC query SOC-X', 'generic search engine'],
                'searched_at': '2026-09-02',
            }],
        }
        with self.assertRaisesRegex(ValueError, '原厂官网'):
            build_datasheet_audit(sample_db(), resolution=resolution)

    def test_audit_validation_rejects_missing_agent_request_or_stale_db(self):
        audit = build_datasheet_audit(sample_db())
        audit['agent_requests'] = []
        errors = validate_datasheet_audit(audit, sample_db())
        self.assertTrue(any('agent request' in item for item in errors))

        stale_db = sample_db()
        stale_db['parts']['U2']['value'] = 'DIFFERENT-SOC'
        errors = validate_datasheet_audit(
            build_datasheet_audit(sample_db()), stale_db)
        self.assertTrue(any('当前 db.json' in item for item in errors))


if __name__ == '__main__':
    unittest.main()
