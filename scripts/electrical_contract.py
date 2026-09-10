#!/usr/bin/env python3
"""Shared, fail-closed evidence dependencies for the planner and hot checks.

The hashes bind reviewed inputs, not the truth of a datasheet interpretation.
The agent must still verify ordering code, package, conditions and citations.
"""
import argparse
import hashlib
import json
import math
import os
import re
from functools import lru_cache


def db_fingerprint(db):
    data = {key: db.get(key, {}) for key in
            ('parts', 'pin2net', 'pinname', 'pintype')}
    data['nets'] = {net: sorted(nodes) for net, nodes in db.get('nets', {}).items()}
    return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False,
                                    separators=(',', ':')).encode()).hexdigest()


@lru_cache(maxsize=128)
def _file_hash(path, size, mtime_ns):
    digest = hashlib.sha256()
    with open(path, 'rb') as stream:
        for chunk in iter(lambda: stream.read(1048576), b''):
            digest.update(chunk)
    return digest.hexdigest()


def document_fingerprint(path):
    stat = os.stat(path)
    return _file_hash(os.path.abspath(path), stat.st_size, stat.st_mtime_ns)


def finite(value):
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value))


def bounded(value, positive=False):
    return (isinstance(value, dict) and finite(value.get('min'))
            and finite(value.get('max')) and value['min'] <= value['max']
            and (not positive or value['min'] > 0))


def dependency_refs(db, check):
    """Mandatory target devices plus explicitly declared parameter dependencies.

Do not fan out over a large supply rail. Extra devices on intermediate branches
must be declared in depends_on by the analysis; the topology solver separately
rejects unmodelled loads.
    """
    refs = set(check.get('depends_on') or [])
    if check.get('ref'):
        refs.add(check['ref'])
    if check.get('node'):
        refs.add(check['node'].split('.')[0])
    net = check.get('net') or db.get('pin2net', {}).get(check.get('node'))
    refs.update(node.split('.')[0] for node in db.get('nets', {}).get(net, [])
                if re.match(r'^(U|M|Q|D)\d', node, re.I))
    return sorted(refs)


def dependency_gaps(db, check, audit, db_sha256=None):
    gaps = []
    basis = check.get('basis') or {}
    if not isinstance(basis, dict):
        return ['basis 必须为 object']
    if basis.get('db_sha256') != (db_sha256 or db_fingerprint(db)):
        gaps.append('basis.db_sha256 未绑定当前网表/BOM/引脚定义')
    if not isinstance(basis.get('state'), str) or not basis['state'].strip():
        gaps.append('basis.state 缺失：需指定装配版本及供电/采样状态')
    sources = basis.get('sources') or []
    if not isinstance(sources, list) or any(not isinstance(x, dict) for x in sources):
        return gaps + ['basis.sources 必须为 object 数组']
    refs = dependency_refs(db, check)
    if not refs:
        gaps.append('depends_on 缺失：需声明判据对应的器件')
    for ref in sorted(set(refs) | {x.get('ref') for x in sources if x.get('ref')}):
        part = db.get('parts', {}).get(ref)
        if not part or part.get('nc'):
            gaps.append(f'{ref}: 依赖器件不存在或未装配')
            continue
        material = next((x for x in (audit or {}).get('materials', [])
                         if ref in x.get('refdes', [])), None)
        if not material or material.get('status') != 'AVAILABLE':
            gaps.append(f'{ref}: datasheet audit 未确认 AVAILABLE')
            continue
        matched = [x for x in sources if x.get('ref') == ref]
        if len(matched) != 1:
            gaps.append(f'{ref}: 需要唯一的参数来源绑定')
            continue
        source, document = matched[0], material.get('document', {})
        if document.get('identity_verified') is not True:
            gaps.append(f'{ref}: 文档身份未核实')
        for field in ('document_model', 'document_version'):
            if source.get(field) != document.get(field) or not source.get(field):
                gaps.append(f'{ref}: {field} 与审计文档不一致')
        if source.get('identity') != material.get('identity'):
            gaps.append(f'{ref}: 参数型号与已审计物料不一致')
        for field in ('locator', 'identity_resolution'):
            if not isinstance(source.get(field), str) or not source[field].strip():
                gaps.append(f'{ref}: 缺少 {field}')
        try:
            actual_hash = document_fingerprint(document.get('path', ''))
        except (OSError, TypeError):
            gaps.append(f'{ref}: 已审计文档不可读取')
        else:
            if source.get('sha256') != actual_hash:
                gaps.append(f'{ref}: 文档 SHA256 未绑定或已变更')
    return gaps


def model_gaps(check):
    gaps = []
    rule = check.get('rule')
    if rule == 'Rule-08':
        vref = check.get('vref')
        if not (bounded(vref, positive=True) and finite(vref.get('typ'))
                and vref['min'] <= vref['typ'] <= vref['max']):
            gaps.append('Vref 缺少保证 min/max，禁止以 typ 代替')
        model = check.get('divider_model') or {}
        for key in ('source_net', 'reference_net'):
            if not isinstance(model.get(key), str) or not model[key].strip():
                gaps.append(f'divider_model.{key} 缺失')
        if not bounded(model.get('bias_current_a')):
            gaps.append('反馈输入偏置电流范围缺失（正号表示流入 IC）')
    elif rule in ('Rule-12', 'Rule-16'):
        if rule == 'Rule-12' and check.get('required_default') != 'float':
            if not finite(check.get('abs_min_v')) or not finite(check.get('abs_max_v')):
                gaps.append('Rule-12 需正负引脚电压绝对额定；注入电流仍独立检查')
        required = check.get('required_default') or check.get('required')
        if required != 'float':
            analysis = check.get('voltage_analysis') or {}
            if not bounded(analysis.get('voltage_v')):
                gaps.append('采样/有效时段的引脚电压保证窗口缺失')
            for field in ('method', 'calculation', 'loading', 'conditions'):
                if not isinstance(analysis.get(field), str) or not analysis[field].strip():
                    gaps.append(f'voltage_analysis.{field} 缺失')
            if not bounded(analysis.get('sample_window_s')):
                gaps.append('voltage_analysis.sample_window_s 缺失')
            threshold = 'vih_min_v' if required == 'high' else 'vil_max_v'
            if not finite(check.get(threshold)):
                gaps.append(f'{threshold} 保证门限缺失')
    return gaps


def check_matches(db, check, rule, obj):
    if check.get('rule') != rule:
        return False
    # Every supplied coordinate must agree; a shared net cannot override a wrong pin.
    if not any(check.get(k) for k in ('node', 'net', 'ref')):
        return False
    node = check.get('node')
    if node and node not in db.get('pin2net', {}):
        return False
    coords = dict(check)
    if node:
        coords.setdefault('net', db['pin2net'][node])
        coords.setdefault('ref', node.split('.')[0])
    shared = False
    for key in ('node', 'net', 'ref'):
        if obj.get(key) and coords.get(key):
            shared = True
            if coords[key] != obj[key]:
                return False
    return shared


def readiness_gaps(db, check, audit, db_sha256=None):
    gaps = dependency_gaps(db, check, audit, db_sha256) + model_gaps(check)
    for key, table in (('node', 'pin2net'), ('net', 'nets'), ('ref', 'parts')):
        if check.get(key) and check[key] not in db.get(table, {}):
            gaps.append(f'{key} 未匹配当前网表: {check[key]}')
    if check.get('node') and check.get('net') and (
            db.get('pin2net', {}).get(check['node']) != check['net']):
        gaps.append('node/net 坐标不一致')
    if check.get('node') and check.get('ref') and (
            check['node'].split('.')[0] != check['ref']):
        gaps.append('node/ref 坐标不一致')
    return gaps



HOT_RULE_IDS = {'Rule-08', 'Rule-09', 'Rule-12', 'Rule-14', 'Rule-16'}

def validate_evidence(evidence):
    """验证 ER1 结构化证据；拒绝让残缺判据静默进入热跑。"""
    errors = []
    seen_ids = set()

    def number(value):
        if isinstance(value, bool):
            return None
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return None
        return parsed if math.isfinite(parsed) else None

    def range_errors(value, label, *, nonnegative=False):
        if not isinstance(value, dict):
            return [f'{label} 必须为 object']
        found = {}
        result = []
        for key in ('min', 'max'):
            if key not in value:
                continue
            parsed = number(value[key])
            if parsed is None:
                result.append(f'{label}.{key} 必须为有限数值')
            elif nonnegative and parsed < 0:
                result.append(f'{label}.{key} 不得小于 0')
            else:
                found[key] = parsed
        if 'min' in found and 'max' in found and found['min'] > found['max']:
            result.append(f'{label}.min 不得大于 max')
        return result

    def text_value(value):
        return isinstance(value, str) and bool(value.strip())

    if not isinstance(evidence, dict):
        return ['根对象必须是 JSON object']
    if evidence.get('schema_version') != 1:
        errors.append('schema_version 必须为 1')
    checks = evidence.get('checks')
    if not isinstance(checks, list):
        return errors + ['checks 必须为数组']
    for index, check in enumerate(checks):
        label = f'checks[{index}]'
        if not isinstance(check, dict):
            errors.append(f'{label} 必须为 object')
            continue
        rule = check.get('rule')
        if not isinstance(rule, str) or rule not in HOT_RULE_IDS:
            errors.append(f'{label}.rule 不支持: {rule!r}')
        check_id = check.get('id')
        if not text_value(check_id):
            errors.append(f'{label}.id 缺失')
        elif check_id in seen_ids:
            errors.append(f'{label}.id 重复: {check_id!r}')
        else:
            seen_ids.add(check_id)
        if not text_value(check.get('citation')):
            errors.append(f'{label}.citation 缺失（需文档/版本/页码或表号）')
        for field in ('basis', 'divider_model', 'voltage_analysis'):
            if field in check and not isinstance(check[field], dict):
                errors.append(f'{label}.{field} 必须为 object')
        if 'depends_on' in check and (not isinstance(check['depends_on'], list)
                or not check['depends_on'] or not all(text_value(x) for x in check['depends_on'])):
            errors.append(f'{label}.depends_on 必须为非空位号数组')
        for field in ('vih_min_v', 'vil_max_v', 'abs_min_v'):
            if field in check and not finite(check[field]):
                errors.append(f'{label}.{field} 必须为有限数值')
        basis = check.get('basis')
        if isinstance(basis, dict) and 'sources' in basis:
            sources = basis['sources']
            if not isinstance(sources, list) or not all(isinstance(x, dict) for x in sources):
                errors.append(f'{label}.basis.sources 必须为 object 数组')
            else:
                refs = []
                for source in sources:
                    ref = source.get('ref')
                    if not text_value(ref):
                        errors.append(f'{label}.basis.sources.ref 必须为位号字符串')
                    else:
                        refs.append(ref)
                if len(refs) != len(set(refs)):
                    errors.append(f'{label}.basis.sources.ref 重复')
        if finite(check.get('abs_min_v')) and finite(check.get('abs_max_v')) and check['abs_min_v'] > check['abs_max_v']:
            errors.append(f'{label}.abs_min_v 不得大于 abs_max_v')
        model = check.get('divider_model')
        if isinstance(model, dict):
            if 'ignored_nodes' in model and (not isinstance(model['ignored_nodes'], dict)
                    or not all(text_value(k) and text_value(v) for k, v in model['ignored_nodes'].items())):
                errors.append(f'{label}.divider_model.ignored_nodes 必须逐节点说明依据')
            if 'bias_current_a' in model and not bounded(model['bias_current_a']):
                errors.append(f'{label}.divider_model.bias_current_a 必须有 min/max')
        analysis = check.get('voltage_analysis')
        if isinstance(analysis, dict):
            for field in ('voltage_v', 'sample_window_s'):
                if field in analysis and not bounded(analysis[field]):
                    errors.append(f'{label}.voltage_analysis.{field} 必须有 min/max')
            window = analysis.get('sample_window_s')
            if bounded(window) and window['min'] < 0:
                errors.append(f'{label}.voltage_analysis.sample_window_s 不得为负')
        kind = check.get('kind')
        kind_by_rule = {
            'Rule-08': {'divider'},
            'Rule-09': {'required_pull', 'required_series'},
            'Rule-12': {'pin_bias'},
            'Rule-14': {'pin_map'},
            'Rule-16': {'strap'},
        }
        expected_kind = kind_by_rule.get(rule, set()) if isinstance(
            rule, str) else set()
        if not isinstance(kind, str) or kind not in expected_kind:
            errors.append(f'{label}.kind={kind!r} 与 {rule} 不匹配')
        if rule == 'Rule-08':
            for key in ('net', 'vref', 'expected'):
                if key not in check:
                    errors.append(f'{label}.{key} 缺失')
            if not text_value(check.get('net')):
                errors.append(f'{label}.net 必须为非空字符串')
            vref = check.get('vref')
            if isinstance(vref, dict):
                if 'typ' not in vref:
                    errors.append(f'{label}.vref.typ 缺失')
                parsed_vref = {}
                for key in ('min', 'typ', 'max'):
                    if key not in vref:
                        continue
                    parsed = number(vref[key])
                    if parsed is None or parsed <= 0:
                        errors.append(f'{label}.vref.{key} 必须为有限正数')
                    else:
                        parsed_vref[key] = parsed
                if all(key in parsed_vref for key in ('min', 'typ', 'max')) and not (
                        parsed_vref['min'] <= parsed_vref['typ']
                        <= parsed_vref['max']):
                    errors.append(f'{label}.vref 必须满足 min <= typ <= max')
            elif number(vref) is None or number(vref) <= 0:
                errors.append(f'{label}.vref 必须为有限正数或 object')
            if not isinstance(check.get('expected'), dict):
                errors.append(f'{label}.expected 必须为 object')
            elif not ({'min', 'max'} & set(check['expected'])):
                errors.append(f'{label}.expected 至少给 min 或 max')
            else:
                errors.extend(range_errors(check['expected'], f'{label}.expected'))
            if 'resistor_tolerance' in check:
                tolerance = number(check['resistor_tolerance'])
                if tolerance is None or not 0 <= tolerance < 1:
                    errors.append(
                        f'{label}.resistor_tolerance 必须在 [0, 1) 内')
        elif rule in ('Rule-09', 'Rule-12', 'Rule-16'):
            if not (text_value(check.get('net'))
                    or text_value(check.get('node'))):
                errors.append(f'{label} 必须给 net 或 node')
            if 'to' in check and not text_value(check.get('to')):
                errors.append(f'{label}.to 必须为非空字符串')
            if rule == 'Rule-09' and kind == 'required_pull' and (
                    check.get('direction') not in ('up', 'down')):
                errors.append(f'{label}.direction 必须为 up/down')
            if rule == 'Rule-12' and check.get('required_default') not in (
                    'high', 'low', 'float'):
                errors.append(f'{label}.required_default 必须为 high/low/float')
            if rule == 'Rule-16' and check.get('required') not in (
                    'high', 'low', 'float'):
                errors.append(f'{label}.required 必须为 high/low/float')
            if rule == 'Rule-09' and 'resistance_ohm' in check:
                errors.extend(range_errors(
                    check['resistance_ohm'], f'{label}.resistance_ohm',
                    nonnegative=True))
            if rule == 'Rule-12' and 'abs_max_v' in check:
                abs_max = number(check['abs_max_v'])
                if abs_max is None or abs_max <= 0:
                    errors.append(f'{label}.abs_max_v 必须为有限正数')
        elif rule == 'Rule-14':
            if (not text_value(check.get('ref'))
                    or not isinstance(check.get('expected'), dict)
                    or not check.get('expected')):
                errors.append(f'{label} 必须给 ref 与 expected pin map')
    return errors


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='生成证据绑定指纹，不生成审查结论')
    parser.add_argument('db')
    parser.add_argument('--document', action='append', default=[])
    args = parser.parse_args()
    with open(args.db, encoding='utf-8') as stream:
        result = {'db_sha256': db_fingerprint(json.load(stream))}
    result['documents'] = {p: document_fingerprint(p) for p in args.document}
    print(json.dumps(result, ensure_ascii=False, indent=2))
