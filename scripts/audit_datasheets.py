#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Audit per-material datasheet coverage without performing network access.

The script turns a parsed netlist into deterministic agent work items. The
agent verifies local candidates, retrieves missing datasheets online, and
records FOUND/NOT_FOUND outcomes in a separate resolution JSON file.

Typical flow::

    python3 audit_datasheets.py db.json --datasheet-dir ./datasheets \
        --json datasheet-audit.json
    # Agent resolves every request and writes datasheet-resolution.json.
    python3 audit_datasheets.py db.json --datasheet-dir ./datasheets \
        --resolution datasheet-resolution.json --json datasheet-audit.json
"""
import argparse
import io
import json
import os
import re
import sys
from collections import Counter


REQUIRED_REF_RE = re.compile(r'^(U|M|Q|D)\d', re.I)
AUDIT_STATUSES = ('AVAILABLE', 'NEEDS_VERIFICATION', 'MISSING', 'NOT_FOUND')
RESOLUTION_STATUSES = ('FOUND', 'NOT_FOUND')
SOURCE_KINDS = ('package', 'network')
PDF_EXTENSIONS = {'.pdf'}


def _text(value):
    return isinstance(value, str) and bool(value.strip())


def normalize_identity(value):
    """Normalize a material identity for exact cross-file joins."""
    return re.sub(r'[^A-Z0-9]+', '', str(value or '').upper())


def _identity_for(ref, part):
    """Follow the skill contract: VALUE first, then PART/PRIM as fallbacks."""
    for field in ('value', 'part', 'prim'):
        value = part.get(field)
        if _text(value) and str(value).strip().upper() not in {
                'N/A', 'NA', 'NONE', 'TBD', 'UNKNOWN'}:
            return str(value).strip(), field
    return ref, 'refdes'


def _alternate_identities(part, identity):
    selected = normalize_identity(identity)
    result = []
    for field in ('value', 'part', 'prim'):
        value = part.get(field)
        if not _text(value) or normalize_identity(value) == selected:
            continue
        result.append({'field': field, 'value': str(value).strip()})
    return result


def _slug(value):
    text = re.sub(r'[^A-Za-z0-9]+', '-', str(value or '').upper()).strip('-')
    return text[:64] or 'UNKNOWN'


def _failure_message(identity, refdes):
    refs = '、'.join(sorted(refdes))
    return (f'找不到这颗物料的 datasheet：{identity}（位号：{refs}）。'
            '请提供该物料的原厂 datasheet。')


def _has_manufacturer_source(sources):
    markers = ('MANUFACTURER', 'OFFICIAL', '原厂', '官网')
    return any(any(marker in str(source).upper() for marker in markers)
               for source in (sources or [])[1:])


def _collect_pdf_files(paths):
    files = []
    missing_paths = []
    for raw_path in paths or []:
        path = os.path.abspath(raw_path)
        if not os.path.exists(path):
            missing_paths.append(path)
            continue
        if os.path.isfile(path):
            if os.path.splitext(path)[1].lower() in PDF_EXTENSIONS:
                files.append(path)
            continue
        for root, dirs, names in os.walk(path):
            dirs.sort()
            for name in sorted(names):
                candidate = os.path.join(root, name)
                if os.path.splitext(name)[1].lower() in PDF_EXTENSIONS:
                    files.append(os.path.abspath(candidate))
    return sorted(set(files)), sorted(set(missing_paths))


def _local_candidates(identity, files):
    target = normalize_identity(identity)
    if len(target) < 3:
        return []
    result = []
    for path in files:
        stem = normalize_identity(os.path.splitext(os.path.basename(path))[0])
        if target in stem or (len(stem) >= 4 and stem in target):
            result.append(path)
    return result


def validate_resolution(resolution, base_dir=None, check_paths=True):
    """Validate agent-recorded FOUND/NOT_FOUND outcomes."""
    if resolution is None:
        return []
    if not isinstance(resolution, dict):
        return ['resolution 根对象必须为 object']
    errors = []
    if resolution.get('schema_version') != 1:
        errors.append('resolution.schema_version 必须为 1')
    entries = resolution.get('entries')
    if not isinstance(entries, list):
        return errors + ['resolution.entries 必须为数组']
    seen = set()
    base_dir = os.path.abspath(base_dir or os.curdir)
    for index, entry in enumerate(entries):
        label = f'entries[{index}]'
        if not isinstance(entry, dict):
            errors.append(f'{label} 必须为 object')
            continue
        identity = entry.get('identity')
        key = normalize_identity(identity)
        if not _text(identity) or not key:
            errors.append(f'{label}.identity 缺失')
        elif key in seen:
            errors.append(f'{label}.identity 重复: {identity!r}')
        else:
            seen.add(key)
        status = entry.get('status')
        if status not in RESOLUTION_STATUSES:
            errors.append(f'{label}.status 必须为 FOUND/NOT_FOUND')
            continue
        if status == 'FOUND':
            if entry.get('identity_verified') is not True:
                errors.append(f'{label}.identity_verified 必须为 true')
            if entry.get('source_kind') not in SOURCE_KINDS:
                errors.append(f'{label}.source_kind 必须为 package/network')
            for field in ('path', 'document_model', 'document_version'):
                if not _text(entry.get(field)):
                    errors.append(f'{label}.{field} 缺失')
            path = entry.get('path')
            if _text(path) and check_paths:
                resolved = path if os.path.isabs(path) else os.path.join(base_dir, path)
                if not os.path.isfile(resolved):
                    errors.append(f'{label}.path 文件不存在: {path}')
            if entry.get('source_kind') == 'network':
                for field in ('source_url', 'retrieved_at'):
                    if not _text(entry.get(field)):
                        errors.append(f'{label}.{field} 缺失（网络补取必填）')
        else:
            sources = entry.get('searched_sources')
            if not isinstance(sources, list) or len(sources) < 2 or not all(
                    _text(item) for item in sources):
                errors.append(f'{label}.searched_sources 至少记录两个已检索来源')
            elif not ('LCSC' in str(sources[0]).upper() or '立创' in str(sources[0])):
                errors.append(f'{label}.searched_sources 第一项必须记录 LCSC/立创检索')
            elif not _has_manufacturer_source(sources):
                errors.append(f'{label}.searched_sources 必须记录原厂官网检索')
            if not _text(entry.get('searched_at')):
                errors.append(f'{label}.searched_at 缺失')
    return errors


def validate_datasheet_audit(audit, db=None):
    """Validate an audit before it is allowed to influence review readiness."""
    if not isinstance(audit, dict):
        return ['datasheet audit 根对象必须为 object']
    errors = []
    if audit.get('schema_version') != 1:
        errors.append('datasheet audit schema_version 必须为 1')
    materials = audit.get('materials')
    if not isinstance(materials, list):
        return errors + ['datasheet audit materials 必须为数组']
    seen = set()
    counts = Counter()
    for index, material in enumerate(materials):
        label = f'materials[{index}]'
        if not isinstance(material, dict):
            errors.append(f'{label} 必须为 object')
            continue
        identity = material.get('identity')
        key = normalize_identity(identity)
        if not _text(identity) or not key:
            errors.append(f'{label}.identity 缺失')
        elif key in seen:
            errors.append(f'{label}.identity 重复: {identity!r}')
        else:
            seen.add(key)
        refs = material.get('refdes')
        if not isinstance(refs, list) or not refs or not all(_text(x) for x in refs):
            errors.append(f'{label}.refdes 必须为非空字符串数组')
        status = material.get('status')
        if status not in AUDIT_STATUSES:
            errors.append(f'{label}.status 不支持: {status!r}')
        else:
            counts[status] += 1
        if status == 'AVAILABLE':
            document = material.get('document')
            if not isinstance(document, dict):
                errors.append(f'{label}.document 缺失')
            else:
                if document.get('identity_verified') is not True:
                    errors.append(f'{label}.document.identity_verified 必须为 true')
                if document.get('source_kind') not in SOURCE_KINDS:
                    errors.append(f'{label}.document.source_kind 无效')
                for field in ('path', 'document_model', 'document_version'):
                    if not _text(document.get(field)):
                        errors.append(f'{label}.document.{field} 缺失')
                if document.get('source_kind') == 'network':
                    for field in ('source_url', 'retrieved_at'):
                        if not _text(document.get(field)):
                            errors.append(f'{label}.document.{field} 缺失')
        elif status == 'NOT_FOUND':
            expected_message = _failure_message(
                str(identity or ''), material.get('refdes') or [])
            if material.get('message') != expected_message:
                errors.append(f'{label}.message 必须使用标准逐物料提示')
            sources = material.get('searched_sources')
            if (not isinstance(sources, list) or len(sources) < 2
                    or not all(_text(item) for item in sources)):
                errors.append(f'{label}.searched_sources 缺失')
            elif not ('LCSC' in str(sources[0]).upper()
                      or '立创' in str(sources[0])):
                errors.append(f'{label}.searched_sources 第一项必须为 LCSC/立创')
            elif not _has_manufacturer_source(sources):
                errors.append(f'{label}.searched_sources 必须包含原厂官网')
            if not _text(material.get('searched_at')):
                errors.append(f'{label}.searched_at 缺失')
    summary = audit.get('summary')
    if not isinstance(summary, dict):
        errors.append('datasheet audit summary 必须为 object')
    else:
        expected = {
            'required_materials': len(materials),
            'available': counts['AVAILABLE'],
            'needs_verification': counts['NEEDS_VERIFICATION'],
            'missing': counts['MISSING'],
            'not_found': counts['NOT_FOUND'],
        }
        for key, value in expected.items():
            if summary.get(key) != value:
                errors.append(f'datasheet audit summary.{key} 应为 {value}')
        unresolved = len(materials) - counts['AVAILABLE']
        if summary.get('unresolved') != unresolved:
            errors.append(f'datasheet audit summary.unresolved 应为 {unresolved}')
        if summary.get('all_required_available') is not (unresolved == 0):
            errors.append('datasheet audit summary.all_required_available 不一致')
    requests = audit.get('agent_requests')
    if not isinstance(requests, list):
        errors.append('datasheet audit agent_requests 必须为数组')
    else:
        expected_actions = {
            normalize_identity(item.get('identity')): {
                'NEEDS_VERIFICATION': 'VERIFY_LOCAL_DATASHEET',
                'MISSING': 'FETCH_DATASHEET_ONLINE',
                'NOT_FOUND': 'REQUEST_USER_DATASHEET',
            }.get(item.get('status'))
            for item in materials if isinstance(item, dict)
        }
        seen_requests = set()
        for index, request in enumerate(requests):
            label = f'agent_requests[{index}]'
            if not isinstance(request, dict):
                errors.append(f'{label} 必须为 object')
                continue
            key = normalize_identity(request.get('identity'))
            if not key or key not in expected_actions:
                errors.append(f'{label}.identity 未匹配审计物料')
                continue
            if key in seen_requests:
                errors.append(f'{label}.identity 重复')
                continue
            seen_requests.add(key)
            expected_action = expected_actions[key]
            if expected_action is None:
                errors.append(f'{label} 不应为 AVAILABLE 物料生成任务')
            elif request.get('action') != expected_action:
                errors.append(
                    f'{label}.action 应为 {expected_action}')
        for key, action in expected_actions.items():
            if action is not None and key not in seen_requests:
                errors.append(
                    f'datasheet audit 缺少 {action} agent request: {key}')
    messages = audit.get('user_messages')
    if not isinstance(messages, list) or not all(_text(x) for x in messages):
        errors.append('datasheet audit user_messages 必须为字符串数组')
    else:
        not_found = [
            item for item in materials
            if isinstance(item, dict) and item.get('status') == 'NOT_FOUND'
        ]
        expected_messages = [
            item.get('message') for item in not_found
            if _text(item.get('message'))
        ]
        if (len(expected_messages) != len(not_found)
                or sorted(messages) != sorted(expected_messages)):
            errors.append('datasheet audit user_messages 未逐颗覆盖 NOT_FOUND')
    if db is not None:
        extra_refs = audit.get('required_refs', [])
        if not isinstance(extra_refs, list) or not all(_text(x) for x in extra_refs):
            errors.append('datasheet audit required_refs 必须为位号数组')
            extra_refs = []
        if any(ref not in db.get('parts', {}) for ref in extra_refs):
            errors.append('datasheet audit required_refs 包含不存在的位号')
        expected_groups, _diagnostics = _material_groups(db, extra_refs)
        expected_pairs = sorted(
            (normalize_identity(item.get('identity')),
             tuple(sorted(item.get('refdes', []))))
            for item in expected_groups)
        actual_pairs = sorted(
            (normalize_identity(item.get('identity')),
             tuple(sorted(item.get('refdes', []))))
            for item in materials if isinstance(item, dict))
        if actual_pairs != expected_pairs:
            errors.append('datasheet audit 与当前 db.json 的物料/位号不一致')
    return errors


def datasheet_entry_for_ref(audit, ref):
    for material in (audit or {}).get('materials', []):
        if ref in material.get('refdes', []):
            return material
    return None


def datasheet_audit_all_available(audit):
    return bool((audit or {}).get('summary', {}).get('all_required_available'))


def _resolution_index(resolution, base_dir):
    result = {}
    for entry in (resolution or {}).get('entries', []):
        item = dict(entry)
        if item.get('status') == 'FOUND' and _text(item.get('path')):
            path = item['path']
            item['path'] = os.path.abspath(
                path if os.path.isabs(path) else os.path.join(base_dir, path))
        result[normalize_identity(item.get('identity'))] = item
    return result


def _material_groups(db, required_refs=None):
    groups = {}
    diagnostics = []
    for ref, part in sorted((db.get('parts') or {}).items()):
        if (not REQUIRED_REF_RE.match(ref) and ref not in (required_refs or [])) or part.get('nc'):
            continue
        identity, source = _identity_for(ref, part)
        key = normalize_identity(identity) or f'REF{ref.upper()}'
        if source == 'refdes':
            diagnostics.append({
                'code': 'MATERIAL_IDENTITY_MISSING',
                'refdes': ref,
                'message': f'{ref} 缺少 VALUE/PART/PRIM，无法精确检索 datasheet',
            })
        group = groups.setdefault(key, {
            'material_id': 'DS-' + _slug(identity),
            'identity': identity,
            'identity_source': source,
            'refdes': [],
            'bom_fields': [],
            'alternate_identities': [],
        })
        group['refdes'].append(ref)
        group['bom_fields'].append({
            'ref': ref,
            'value': part.get('value', ''),
            'part': part.get('part', ''),
            'prim': part.get('prim', ''),
            'jedec': part.get('jedec', ''),
        })
        group['alternate_identities'].extend(_alternate_identities(part, identity))
    for group in groups.values():
        group['refdes'] = sorted(set(group['refdes']))
        unique = {(x['field'], x['value']): x
                  for x in group['alternate_identities']}
        group['alternate_identities'] = [unique[key] for key in sorted(unique)]
    return [groups[key] for key in sorted(groups)], diagnostics


def build_datasheet_audit(db, datasheet_dirs=None, resolution=None,
                          resolution_base=None, required_refs=None):
    files, missing_paths = _collect_pdf_files(datasheet_dirs or [])
    required_refs = sorted(set(required_refs or []))
    unknown = [ref for ref in required_refs if ref not in db.get('parts', {})]
    if unknown:
        raise ValueError(f'额外资料依赖位号不存在: {unknown}')
    groups, diagnostics = _material_groups(db, required_refs)
    base_dir = os.path.abspath(resolution_base or os.curdir)
    resolution_errors = validate_resolution(resolution, base_dir)
    if resolution_errors:
        raise ValueError('datasheet-resolution.json 无效:\n  - '
                         + '\n  - '.join(resolution_errors))
    diagnostics.extend({
        'code': 'DATASHEET_PATH_MISSING',
        'path': path,
        'message': f'datasheet 路径不存在，按无本地资料继续审计: {path}',
    } for path in missing_paths)
    resolved = _resolution_index(resolution, base_dir)
    materials = []
    requests = []
    messages = []

    for group in groups:
        material = dict(group)
        identity = material['identity']
        key = normalize_identity(identity)
        candidates = _local_candidates(identity, files)
        outcome = resolved.get(key)
        message = _failure_message(identity, material['refdes'])
        if outcome and outcome.get('status') == 'FOUND':
            material['status'] = 'AVAILABLE'
            material['document'] = {
                field: outcome.get(field) for field in (
                    'identity_verified', 'source_kind', 'path', 'source_url',
                    'document_model', 'document_version', 'retrieved_at')
                if outcome.get(field) is not None
            }
            material['local_candidates'] = candidates
        elif outcome and outcome.get('status') == 'NOT_FOUND':
            material['status'] = 'NOT_FOUND'
            material['local_candidates'] = candidates
            material['searched_sources'] = list(outcome.get('searched_sources', []))
            material['searched_at'] = outcome.get('searched_at')
            material['message'] = message
            messages.append(message)
            requests.append({
                'id': 'REQUEST-' + _slug(identity),
                'action': 'REQUEST_USER_DATASHEET',
                'identity': identity,
                'refdes': material['refdes'],
                'message': message,
            })
        elif candidates:
            material['status'] = 'NEEDS_VERIFICATION'
            material['local_candidates'] = candidates
            requests.append({
                'id': 'VERIFY-' + _slug(identity),
                'action': 'VERIFY_LOCAL_DATASHEET',
                'identity': identity,
                'refdes': material['refdes'],
                'local_candidates': candidates,
                'fallback_action': 'FETCH_DATASHEET_ONLINE',
                'query': f'"{identity}" datasheet PDF',
                'on_failure_message': message,
            })
        else:
            material['status'] = 'MISSING'
            material['local_candidates'] = []
            requests.append({
                'id': 'FETCH-' + _slug(identity),
                'action': 'FETCH_DATASHEET_ONLINE',
                'identity': identity,
                'refdes': material['refdes'],
                'query': f'"{identity}" datasheet PDF',
                'source_order': ['LCSC/立创商城', 'manufacturer official website'],
                'download_directory': '/tmp/codex-work/<task>/datasheets/',
                'on_failure_message': message,
            })
        materials.append(material)

    counts = Counter(item['status'] for item in materials)
    unresolved = len(materials) - counts['AVAILABLE']
    return {
        'schema_version': 1,
        'generated_by': 'scripts/audit_datasheets.py',
        'network_access': 'agent-only',
        'required_refs': required_refs,
        'summary': {
            'required_materials': len(materials),
            'available': counts['AVAILABLE'],
            'needs_verification': counts['NEEDS_VERIFICATION'],
            'missing': counts['MISSING'],
            'not_found': counts['NOT_FOUND'],
            'unresolved': unresolved,
            'all_required_available': unresolved == 0,
        },
        'materials': materials,
        'agent_requests': requests,
        'user_messages': messages,
        'diagnostics': diagnostics,
    }


def main():
    parser = argparse.ArgumentParser(
        description='逐物料审计 datasheet 覆盖，并生成 agent 联网补取任务')
    parser.add_argument('db', help='parse_netlist.py 产出的 db.json')
    parser.add_argument(
        '--datasheet-dir', action='append', default=[],
        help='资料包目录或 PDF；可重复提供。文件名命中只作为待验证候选')
    parser.add_argument(
        '--resolution', help='agent 写回的 datasheet-resolution.json')
    parser.add_argument('--require-ref', action='append', default=[],
                        help='需要额定值/曲线/引脚定义的 L/F/Y/J/C/R 等关键物料；可重复')
    parser.add_argument('--evidence', help='自动纳入 evidence 中 depends_on 和目标器件的资料依赖')
    parser.add_argument('--json', required=True, help='写出 datasheet-audit.json')
    parser.add_argument(
        '--fail-on-unresolved', action='store_true',
        help='仍有 NEEDS_VERIFICATION/MISSING/NOT_FOUND 时退出码为 2')
    args = parser.parse_args()

    db = json.load(io.open(args.db, encoding='utf-8'))
    resolution = None
    resolution_base = os.curdir
    if args.resolution:
        resolution = json.load(io.open(args.resolution, encoding='utf-8'))
        resolution_base = os.path.dirname(os.path.abspath(args.resolution))
        errors = validate_resolution(resolution, resolution_base)
        if errors:
            sys.exit('[FATAL] datasheet-resolution.json 无效:\n  - '
                     + '\n  - '.join(errors))
    required_refs = set(args.require_ref)
    if args.evidence:
        from electrical_contract import dependency_refs
        with open(args.evidence, encoding='utf-8') as stream:
            evidence = json.load(stream)
        for check in evidence.get('checks', []):
            required_refs.update(dependency_refs(db, check))
    try:
        audit = build_datasheet_audit(
            db, args.datasheet_dir, resolution, resolution_base, sorted(required_refs))
    except ValueError as error:
        sys.exit(f'[FATAL] {error}')
    errors = validate_datasheet_audit(audit, db)
    if errors:
        sys.exit('[FATAL] datasheet-audit.json 内部校验失败:\n  - '
                 + '\n  - '.join(errors))
    json.dump(audit, io.open(args.json, 'w', encoding='utf-8'),
              ensure_ascii=False, indent=2)

    summary = audit['summary']
    print('=== Datasheet 覆盖审计 ===')
    print(f"  required={summary['required_materials']}  "
          f"available={summary['available']}  "
          f"needs_verification={summary['needs_verification']}  "
          f"missing={summary['missing']}  not_found={summary['not_found']}")
    for request in audit['agent_requests']:
        if request['action'] == 'REQUEST_USER_DATASHEET':
            print('  ' + request['message'])
        else:
            print(f"  [{request['action']}] {request['identity']} "
                  f"refs={','.join(request['refdes'])}")
    print(f'  -> {args.json}')
    return 2 if args.fail_on_unresolved and summary['unresolved'] else 0


if __name__ == '__main__':
    sys.exit(main())
