#!/usr/bin/env python3
"""Project-local Vref facts; materialize inputs, never electrical conclusions.

Only explicitly reviewed guarantees covering every declared condition are used.
No downloads, extraction, unit guessing, interpolation or confidence ranking.
"""
import argparse
import copy
import hashlib
import json
import os
import re
import sys

from electrical_contract import bounded, coordinate_gaps, dependency_gaps, finite, load_json


CONDITION_KEYS = {'temperature_c', 'temperature_basis', 'vin_v', 'load_a',
                  'mode', 'qualifiers'}


def _text(value):
    return isinstance(value, str) and bool(value.strip())


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    allow_nan=False, separators=(',', ':')).encode()).hexdigest()


def _file_digest(path):
    # Read actual bytes on each use, including after an in-process file change.
    digest = hashlib.sha256()
    with open(path, 'rb') as stream:
        for chunk in iter(lambda: stream.read(1048576), b''):
            digest.update(chunk)
    return digest.hexdigest()


def condition_gaps(conditions):
    if not isinstance(conditions, dict):
        return ['conditions 必须为 object']
    gaps = []
    if set(conditions) != CONDITION_KEYS:
        gaps.append('conditions 必须完整给出 temperature_c/temperature_basis/vin_v/load_a/mode/qualifiers；不接受未知字段')
    for key in ('temperature_c', 'vin_v', 'load_a'):
        value = conditions.get(key)
        if not bounded(value) or set(value) != {'min', 'max'}:
            gaps.append(f'conditions.{key} 需要有限且有序的 min/max')
        elif key in ('vin_v', 'load_a') and value['min'] < 0:
            gaps.append(f'conditions.{key} 不得为负')
    if conditions.get('temperature_basis') not in ('junction', 'ambient'):
        gaps.append('temperature_basis 必须明确 junction/ambient，不能互换')
    if not _text(conditions.get('mode')):
        gaps.append('conditions.mode 缺失')
    qualifiers = conditions.get('qualifiers')
    if not isinstance(qualifiers, dict) or not all(
            _text(k) and _text(v) for k, v in qualifiers.items()):
        gaps.append('conditions.qualifiers 必须为字符串键值表；无额外限制时显式给 {}')
    return gaps


def validate_facts(store):
    """Validate storage, while permitting incomplete/unverified draft values."""
    if not isinstance(store, dict) or type(store.get('schema_version')) is not int or store['schema_version'] != 1:
        return ['facts.schema_version 必须为整数 1']
    if not isinstance(store.get('facts'), list):
        return ['facts 必须为数组']
    errors, seen = [], set()
    for index, fact in enumerate(store['facts']):
        label = f'facts[{index}]'
        if not isinstance(fact, dict):
            errors.append(f'{label} 必须为 object')
            continue
        for key in ('id', 'mpn', 'package', 'raw_conditions'):
            if not _text(fact.get(key)):
                errors.append(f'{label}.{key} 缺失')
        fact_id = fact.get('id')
        if _text(fact_id):
            if fact_id in seen:
                errors.append(f'{label}.id 重复: {fact_id}')
            seen.add(fact_id)
        if fact.get('parameter') != 'vref' or fact.get('unit') != 'V':
            errors.append(f'{label} 仅支持 parameter=vref、unit=V；不得猜测或自动换算单位')
        values = fact.get('values')
        if not isinstance(values, dict) or set(values) - {'min', 'typ', 'max'}:
            errors.append(f'{label}.values 必须为 min/typ/max object')
        else:
            for key, value in values.items():
                if value is not None and (not finite(value) or value <= 0):
                    errors.append(f'{label}.values.{key} 必须为有限正数或 null')
            if all(finite(values.get(k)) for k in ('min', 'typ', 'max')) and not (
                    values['min'] <= values['typ'] <= values['max']):
                errors.append(f'{label}.values 必须满足 min <= typ <= max')
        if type(fact.get('guaranteed')) is not bool:
            errors.append(f'{label}.guaranteed 必须为 boolean')
        errors.extend(f'{label}: {gap}' for gap in condition_gaps(fact.get('conditions')))
        source = fact.get('source')
        if not isinstance(source, dict):
            errors.append(f'{label}.source 必须为 object')
        else:
            for key in ('path', 'document_model', 'document_version', 'locator'):
                if not _text(source.get(key)):
                    errors.append(f'{label}.source.{key} 缺失')
            if not isinstance(source.get('sha256'), str) or not re.fullmatch(r'[0-9a-f]{64}', source['sha256']):
                errors.append(f'{label}.source.sha256 必须为 SHA256 小写十六进制')
            if not isinstance(source.get('footnotes'), list) or not all(_text(x) for x in source['footnotes']):
                errors.append(f'{label}.source.footnotes 必须为原文脚注数组（无则 []）')
        verification = fact.get('verification')
        if not isinstance(verification, dict):
            errors.append(f'{label}.verification 必须为 object')
        else:
            if verification.get('status') not in ('VERIFIED', 'UNVERIFIED'):
                errors.append(f'{label}.verification.status 必须为 VERIFIED/UNVERIFIED')
            if type(verification.get('conditions_complete')) is not bool:
                errors.append(f'{label}.verification.conditions_complete 必须为 boolean')
            if not _text(verification.get('note')):
                errors.append(f'{label}.verification.note 缺失（需原件核对记录）')
    return errors


def _covers(available, requested):
    return (all(available[key]['min'] <= requested[key]['min'] <= requested[key]['max'] <= available[key]['max']
                for key in ('temperature_c', 'vin_v', 'load_a'))
            and all(available[key] == requested[key]
                    for key in ('temperature_basis', 'mode', 'qualifiers')))


def _target_source(db, check):
    request = check.get('vref_request')
    if not isinstance(request, dict) or set(request) != {'ref', 'conditions'} or not _text(request.get('ref')):
        return None, ['vref_request 必须明确 ref 和 conditions']
    gaps = condition_gaps(request['conditions']) + coordinate_gaps(db, check)
    ref = request['ref']
    if any(key in check and not _text(check[key]) for key in ('node', 'net', 'ref')):
        return None, gaps + ['Vref 目标 node/net/ref 必须为非空字符串']
    if (check.get('rule') != 'Rule-08' or check.get('kind') != 'divider'
            or (check.get('node') and check['node'].split('.')[0] != ref)
            or (check.get('ref') and check['ref'] != ref)
            or not any(node.startswith(ref + '.') for node in db.get('nets', {}).get(check.get('net'), []))):
        gaps.append('vref_request.ref 必须是本 Rule-08 反馈目标器件')
    sources = (check.get('basis') or {}).get('sources') or []
    matched = [source for source in sources if isinstance(source, dict) and source.get('ref') == ref]
    if len(matched) != 1:
        return None, gaps + ['Vref 目标需要唯一 basis.sources 来源绑定']
    source = matched[0]
    for key in ('mpn', 'package'):
        if not _text(source.get(key)):
            gaps.append(f'Vref 来源缺少已核实的精确 {key}；不能从系列标题/文件名推断')
    return source, gaps


def resolve_vref(db, check, audit, facts_path):
    """Return exactly one applicable reviewed fact, or actionable gaps."""
    source, gaps = _target_source(db, check)
    if gaps:
        return None, gaps
    gaps = dependency_gaps(db, check, audit)
    if gaps:
        return None, gaps
    try:
        store = load_json(facts_path)
    except (OSError, TypeError, ValueError) as error:
        return None, [f'Vref facts 不可读取: {error}']
    errors = validate_facts(store)
    if errors:
        return None, errors
    request = check['vref_request']
    material = next(item for item in audit['materials'] if request['ref'] in item['refdes'])
    # Audit identity may include a reviewed alias; require the original per-ref
    # BOM fields to still match, including package, not merely a copied hash.
    snapshots = [item for item in material.get('bom_fields', []) if item.get('ref') == request['ref']]
    part = db['parts'][request['ref']]
    if len(snapshots) != 1 or any(snapshots[0].get(key, '') != part.get(key, '')
                                  for key in ('value', 'part', 'prim', 'jedec')):
        return None, ['Vref 身份审计的 BOM/封装快照已变更或缺失，需重新核对身份']
    identity_field = material.get('identity_source')
    if (identity_field not in ('value', 'part') or source['mpn'] != material.get('identity')
            or source['mpn'] != str(part.get(identity_field, '')).strip()):
        return None, ['Vref MPN 未精确对应当前已审计的 BOM VALUE/PART；别名/型号冲突不得自动复用']
    if source['package'] != part.get('jedec'):
        return None, ['Vref package 未精确对应当前 BOM JEDEC；封装别名或缺字段需人工核验']
    try:
        actual_pdf_hash = _file_digest(material['document']['path'])
    except (OSError, TypeError) as error:
        return None, [f'Vref 审计 PDF 不可读取: {error}']
    eligible, rejected = [], []
    for fact in store['facts']:
        if (fact['mpn'], fact['package']) != (source['mpn'], source['package']):
            continue
        reason = []
        if not _covers(fact['conditions'], request['conditions']):
            reason.append('工况未完整覆盖或温度定义/模式/附加条件不同')
        values, verification, document = fact['values'], fact['verification'], fact['source']
        if not (fact['guaranteed'] is True and bounded(values, positive=True)
                and finite(values.get('typ')) and values['min'] <= values['typ'] <= values['max']):
            reason.append('缺保证 min/typ/max；typ 不补齐边界')
        if verification['status'] != 'VERIFIED' or verification['conditions_complete'] is not True:
            reason.append('参数及全部条件/脚注尚未完成原件核对')
        for key in ('document_model', 'document_version', 'sha256'):
            if document[key] != source.get(key):
                reason.append(f'{key} 与当前来源绑定不一致')
        if document['sha256'] != actual_pdf_hash:
            reason.append('审计 PDF 内容已变更')
        pdf_path = os.path.join(os.path.dirname(os.path.abspath(facts_path)), document['path'])
        try:
            if _file_digest(pdf_path) != document['sha256']:
                reason.append('fact PDF SHA256 已过期')
        except OSError:
            reason.append('fact PDF 缺失或不可读取')
        if reason:
            rejected.append(f"{fact['id']}: " + '; '.join(reason))
        else:
            eligible.append(fact)
    if len(eligible) != 1:
        if len(eligible) > 1:
            return None, ['Vref 存在多条适用保证，禁止按顺序/置信度择一: ' + ', '.join(x['id'] for x in eligible)]
        return None, ['没有匹配精确 MPN/封装、文档及完整工况的已核实 Vref 保证'] + rejected
    return eligible[0], []


def _context_digest(check):
    # A changed state/target/source requires materialization and review again.
    return _digest({key: check.get(key) for key in
                    ('id', 'rule', 'kind', 'node', 'net', 'ref', 'vref_request', 'basis')})


def vref_binding_gaps(db, check, audit):
    """Shared live gate for planner and lint; legacy manual evidence is unchanged."""
    if 'vref_request' not in check and 'vref_binding' not in check:
        return []
    binding = check.get('vref_binding')
    if not isinstance(binding, dict) or not _text(binding.get('facts_path')) or not os.path.isabs(binding['facts_path']):
        return ['Vref 未物化或缺少绝对 facts_path；运行 datasheet_facts.py materialize']
    fact, gaps = resolve_vref(db, check, audit, binding['facts_path'])
    if gaps:
        return gaps
    if binding.get('fact_id') != fact['id'] or binding.get('fact_sha256') != _digest(fact):
        gaps.append('Vref fact 记录已变更或选择发生变化，必须重新核对/物化')
    if binding.get('context_sha256') != _context_digest(check):
        gaps.append('Vref 的对象/网表/状态/条件/来源绑定已变更，必须重新物化')
    if check.get('vref') != fact['values']:
        gaps.append('evidence.vref 与当前 fact 保证值不一致')
    return gaps


def materialize_evidence(db, evidence, audit, facts_path):
    """Copy into a new evidence artifact, clearing stale values on any failure."""
    output, rows = copy.deepcopy(evidence), []
    for check in output['checks']:
        if 'vref_request' not in check and 'vref_binding' not in check:
            continue
        check.pop('vref', None)
        check.pop('vref_binding', None)
        fact, gaps = resolve_vref(db, check, audit, facts_path)
        row = {'check_id': check['id'], 'status': 'UNRESOLVED' if gaps else 'RESOLVED', 'gaps': gaps}
        if fact is not None:
            check['vref'] = copy.deepcopy(fact['values'])
            source, _ = _target_source(db, check)
            # Preserve existing bias-current/identity citations; add the Vref row.
            locators = source.setdefault('parameter_locators', {})
            if not isinstance(locators, dict):
                raise ValueError('basis.sources.parameter_locators 必须为 object')
            locators['vref'] = fact['source']['locator']
            check['vref_binding'] = {
                'facts_path': os.path.abspath(facts_path), 'fact_id': fact['id'],
                'fact_sha256': _digest(fact), 'context_sha256': _context_digest(check),
            }
            row['fact_id'] = fact['id']
        rows.append(row)
    return output, _report(rows)


def _report(rows):
    return {'schema_version': 1, 'checks': rows,
            'affected_check_ids': [row['check_id'] for row in rows if row['gaps']],
            'scope': '仅 Vref 参数复用/失效影响；不是热跑就绪或电气 PASS/准出结论'}


def check_bindings(db, evidence, audit):
    rows = []
    for check in evidence['checks']:
        if 'vref_request' not in check and 'vref_binding' not in check:
            continue
        gaps = vref_binding_gaps(db, check, audit)
        rows.append({'check_id': check['id'], 'status': 'UNRESOLVED' if gaps else 'RESOLVED',
                     'fact_id': (check.get('vref_binding') or {}).get('fact_id'), 'gaps': gaps})
    return _report(rows)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('materialize', 'check'))
    parser.add_argument('db')
    parser.add_argument('--evidence', required=True)
    parser.add_argument('--datasheet-audit', required=True)
    parser.add_argument('--facts', help='materialize: 项目内 facts.json')
    parser.add_argument('--out', help='materialize: 新 evidence 文件；不覆盖原件')
    parser.add_argument('--report', required=True, help='新建参数复用/失效影响报告')
    args = parser.parse_args()
    if args.action == 'materialize' and (not args.facts or not args.out):
        parser.error('materialize 需要 --facts 和 --out')
    if args.action == 'check' and (args.facts or args.out):
        parser.error('check 使用 evidence 中的绑定；不接受 --facts/--out')
    try:
        from audit_datasheets import validate_datasheet_audit
        from electrical_contract import validate_evidence
        db, evidence, audit = (load_json(path) for path in (args.db, args.evidence, args.datasheet_audit))
        errors = validate_evidence(evidence) + validate_datasheet_audit(audit, db)
        if errors:
            raise ValueError('; '.join(errors))
        destinations = [os.path.abspath(path) for path in (args.out, args.report) if path]
        if len(destinations) != len(set(destinations)) or any(os.path.lexists(path) for path in destinations):
            raise ValueError('--out/--report 必须是不同的新文件；不覆盖输入或旧证据')
        if args.action == 'materialize':
            output, report = materialize_evidence(db, evidence, audit, args.facts)
            with open(args.out, 'x', encoding='utf-8') as stream:
                json.dump(output, stream, ensure_ascii=False, indent=2, allow_nan=False)
        else:
            report = check_bindings(db, evidence, audit)
        with open(args.report, 'x', encoding='utf-8') as stream:
            json.dump(report, stream, ensure_ascii=False, indent=2, allow_nan=False)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 2 if report['affected_check_ids'] else 0
    except (OSError, TypeError, ValueError) as error:
        print(f'[FATAL] {error}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
