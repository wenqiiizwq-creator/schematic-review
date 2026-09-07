#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
原理图结构化网表 Diff，并按复审断言核验 Rule-17“假闭环”。

用法:
    python3 diff_netlists.py old-db.json new-db.json
    python3 diff_netlists.py old-db.json new-db.json \
        --claims review-claims.json --json diff.json --fail-on-open-claims
"""
import argparse
import io
import json
import sys

from solve_dividers import parse_resistor

PART_FIELDS = ('part', 'value', 'jedec', 'prim', 'nc')


def _net_members(db, include_pseudo=False):
    pseudo = set(db.get('pseudo_nets', []))
    return {
        net: sorted(nodes)
        for net, nodes in db.get('nets', {}).items()
        if include_pseudo or net not in pseudo
    }


def diff_databases(old, new, include_pseudo=False):
    old_parts, new_parts = old.get('parts', {}), new.get('parts', {})
    added = sorted(set(new_parts) - set(old_parts))
    removed = sorted(set(old_parts) - set(new_parts))
    changed = []
    for ref in sorted(set(old_parts) & set(new_parts)):
        fields = {}
        for field in PART_FIELDS:
            before = old_parts[ref].get(field)
            after = new_parts[ref].get(field)
            if before != after:
                fields[field] = {'old': before, 'new': after}
        if fields:
            changed.append({'ref': ref, 'fields': fields})

    old_pin, new_pin = old.get('pin2net', {}), new.get('pin2net', {})
    pin_changes = []
    for node in sorted(set(old_pin) | set(new_pin)):
        before, after = old_pin.get(node), new_pin.get(node)
        if before != after:
            pin_changes.append({'node': node, 'old': before, 'new': after})

    old_nets = _net_members(old, include_pseudo)
    new_nets = _net_members(new, include_pseudo)
    net_added = sorted(set(new_nets) - set(old_nets))
    net_removed = sorted(set(old_nets) - set(new_nets))
    net_changed = []
    for net in sorted(set(old_nets) & set(new_nets)):
        before, after = old_nets[net], new_nets[net]
        if before != after:
            net_changed.append({
                'net': net,
                'added_nodes': sorted(set(after) - set(before)),
                'removed_nodes': sorted(set(before) - set(after)),
            })

    return {
        'parts': {'added': added, 'removed': removed, 'changed': changed},
        'pins': {'changed': pin_changes},
        'nets': {
            'added': net_added,
            'removed': net_removed,
            'changed': net_changed,
        },
        'summary': {
            'parts_added': len(added),
            'parts_removed': len(removed),
            'parts_changed': len(changed),
            'pins_changed': len(pin_changes),
            'nets_added': len(net_added),
            'nets_removed': len(net_removed),
            'nets_changed': len(net_changed),
        },
    }


def validate_claims(data):
    errors = []
    if not isinstance(data, dict) or data.get('schema_version') != 1:
        return ['schema_version 必须为 1']
    claims = data.get('claims')
    if not isinstance(claims, list):
        return ['claims 必须为数组']
    supported = {
        'part_added', 'part_removed', 'part_field_changed',
        'part_field_equals', 'pin_net_changed', 'pin_net_equals',
        'net_membership_changed', 'pins_connected', 'pins_disconnected', 'net_members_equal',
    }
    seen_ids = set()
    for i, claim in enumerate(claims):
        label = f'claims[{i}]'
        if not isinstance(claim, dict):
            errors.append(f'{label}.id 缺失')
            continue
        claim_id = claim.get('id')
        if not isinstance(claim_id, str) or not claim_id.strip():
            errors.append(f'{label}.id 缺失')
            continue
        if claim_id in seen_ids:
            errors.append(f'{label}.id 重复: {claim_id!r}')
        else:
            seen_ids.add(claim_id)
        checks = claim.get('expect')
        if not isinstance(checks, list) or not checks:
            errors.append(f'{label}.expect 必须为非空数组')
            continue
        for j, check in enumerate(checks):
            item = f'{label}.expect[{j}]'
            if not isinstance(check, dict):
                errors.append(f'{item} 必须为 object')
                continue
            kind = check.get('kind')
            if not isinstance(kind, str) or kind not in supported:
                errors.append(
                    f'{item}.kind 不支持: {kind!r}')
                continue
            required = {
                'part_added': ('ref',),
                'part_removed': ('ref',),
                'part_field_changed': ('ref', 'field'),
                'part_field_equals': ('ref', 'field', 'value'),
                'pin_net_changed': ('node',),
                'pin_net_equals': ('node', 'net'),
                'net_membership_changed': ('net',),
                'pins_connected': (), 'pins_disconnected': (),
                'net_members_equal': ('net',),
            }[kind]
            for key in required:
                if key == 'value':
                    missing = key not in check
                else:
                    missing = (not isinstance(check.get(key), str)
                               or not check[key].strip())
                if missing:
                    errors.append(f'{item}.{key} 缺失')
            if kind == 'part_field_equals' and 'value' in check:
                value = check['value']
                valid_value = isinstance(value, bool) if check.get('field') == 'nc' else isinstance(value, str)
                if not valid_value:
                    errors.append(f'{item}.value 类型须匹配字段')
            if kind in {'pins_connected', 'pins_disconnected', 'net_members_equal'}:
                nodes = check.get('nodes')
                if not isinstance(nodes, list) or not nodes or not all(isinstance(x, str) and x.strip() for x in nodes):
                    errors.append(f'{item}.nodes 必须为非空引脚列表')
                elif kind != 'net_members_equal' and len(nodes) != 2:
                    errors.append(f'{item}.nodes 必须为两个物理引脚')
            if kind in {'part_field_changed', 'part_field_equals'} and (
                    check.get('field') not in PART_FIELDS):
                errors.append(
                    f'{item}.field 不支持: {check.get("field")!r}')
    return errors


def _part_field_changed(old, new, check):
    ref, field = check.get('ref'), check.get('field')
    before = old.get('parts', {}).get(ref, {}).get(field)
    after = new.get('parts', {}).get(ref, {}).get(field)
    return before != after, {'old': before, 'new': after}


def connected(db, first, second):
    """Connectivity through same net or fitted zero-ohm links only; NC is not a wire."""
    pins = db.get('pin2net', {})
    pseudo = set(db.get('pseudo_nets', []))
    source, target = pins.get(first), pins.get(second)
    if not source or not target or source in pseudo or target in pseudo:
        return None
    graph = {}
    for ref, part in db.get('parts', {}).items():
        if not ref.startswith('R') or part.get('nc'):
            continue
        value = parse_resistor(part.get('value'))
        if not value or value['kohm'] != 0:
            continue
        ends = {net for node, net in pins.items() if node.startswith(ref + '.')}
        if len(ends) == 2 and not ends & pseudo:
            a, b = sorted(ends)
            graph.setdefault(a, set()).add(b)
            graph.setdefault(b, set()).add(a)
    todo, visited = [source], set()
    while todo:
        net = todo.pop()
        if net == target:
            return True
        if net not in visited:
            visited.add(net)
            todo.extend(graph.get(net, set()) - visited)
    return False


def evaluate_expectation(old, new, check):
    kind = check['kind']
    if kind in {'pins_connected', 'pins_disconnected'}:
        actual = connected(new, *check['nodes'])
        wanted = kind == 'pins_connected'
        return actual is not None and actual == wanted, {'connected': actual, 'nodes': check['nodes']}
    if kind == 'net_members_equal':
        actual = sorted(new.get('nets', {}).get(check['net'], []))
        return check['net'] in new.get('nets', {}) and actual == sorted(check['nodes']), {'actual': actual}
    if kind == 'part_added':
        ref = check['ref']
        ok = ref not in old.get('parts', {}) and ref in new.get('parts', {})
        return ok, {'ref': ref}
    if kind == 'part_removed':
        ref = check['ref']
        ok = ref in old.get('parts', {}) and ref not in new.get('parts', {})
        return ok, {'ref': ref}
    if kind == 'part_field_changed':
        return _part_field_changed(old, new, check)
    if kind == 'part_field_equals':
        ref, field = check['ref'], check['field']
        actual = new.get('parts', {}).get(ref, {}).get(field)
        exists = ref in new.get('parts', {}) and field in new['parts'][ref]
        return exists and actual == check.get('value'), {
            'actual': actual, 'expected': check.get('value')}
    if kind == 'pin_net_changed':
        node = check['node']
        before = old.get('pin2net', {}).get(node)
        after = new.get('pin2net', {}).get(node)
        return before != after, {'old': before, 'new': after}
    if kind == 'pin_net_equals':
        node = check['node']
        actual = new.get('pin2net', {}).get(node)
        return actual == check.get('net'), {
            'actual': actual, 'expected': check.get('net')}
    if kind == 'net_membership_changed':
        net = check['net']
        before = sorted(old.get('nets', {}).get(net, []))
        after = sorted(new.get('nets', {}).get(net, []))
        return before != after, {'old': before, 'new': after}
    raise ValueError(f'unsupported claim kind: {kind}')


def evaluate_claims(old, new, claims):
    results, findings = [], []
    for claim in claims.get('claims', []):
        checks = []
        for expectation in claim['expect']:
            ok, evidence = evaluate_expectation(old, new, expectation)
            checks.append({
                'kind': expectation['kind'],
                'pass': ok,
                'expectation': expectation,
                'evidence': evidence,
            })
        substantive = any(x['kind'] not in {'part_field_changed', 'pin_net_changed', 'net_membership_changed'} for x in checks)
        passed = all(x['pass'] for x in checks) and substantive
        result = {
            'id': claim['id'],
            'description': claim.get('description', ''),
            'status': 'PASS' if passed else ('INSUFFICIENT' if not substantive else 'FAIL'),
            'closure_basis': 'expected state verified' if substantive else 'change alone does not prove repair',
            'checks': checks,
        }
        results.append(result)
        if not passed:
            findings.append({
                'rule': 'Rule-17',
                'kind': 'FINDING',
                'name': '历史意见未被新网表证实（假闭环）',
                'detail': (
                    f"{claim['id']} {claim.get('description', '')}: "
                    f"failed={[x for x in checks if not x['pass']]}"),
            })
    return results, findings


def _print_summary(diff):
    print('=== 原理图网表 Diff ===')
    for key, value in diff['summary'].items():
        print(f'  {key:20s} {value:5d}')
    for item in diff['parts']['changed']:
        print(f"  PART {item['ref']}: {item['fields']}")
    for item in diff['pins']['changed']:
        print(f"  PIN  {item['node']}: {item['old']} -> {item['new']}")


def main():
    parser = argparse.ArgumentParser(description='原理图 db.json 新旧版本 Diff')
    parser.add_argument('old_db')
    parser.add_argument('new_db')
    parser.add_argument('--claims', help='历史评审意见的机器可验证断言 JSON')
    parser.add_argument('--include-pseudo', action='store_true',
                        help='网络成员 Diff 包含工具伪网络（默认排除）')
    parser.add_argument('--json', help='写出完整 Diff JSON')
    parser.add_argument('--fail-on-open-claims', action='store_true',
                        help='存在 Rule-17 未闭环项时退出 2')
    args = parser.parse_args()

    old = json.load(io.open(args.old_db, encoding='utf-8'))
    new = json.load(io.open(args.new_db, encoding='utf-8'))
    diff = diff_databases(old, new, args.include_pseudo)
    _print_summary(diff)

    claim_results, findings = [], []
    if args.claims:
        claims = json.load(io.open(args.claims, encoding='utf-8'))
        errors = validate_claims(claims)
        if errors:
            sys.exit('[FATAL] claims JSON 无效:\n  - ' + '\n  - '.join(errors))
        claim_results, findings = evaluate_claims(old, new, claims)
        print('\n=== 历史意见闭环断言 ===')
        for result in claim_results:
            print(f"  {result['id']:16s} {result['status']:4s} "
                  f"{result['description']}")

    payload = {
        'schema_version': 1,
        'diff': diff,
        'claims': claim_results,
        'findings': findings,
    }
    if args.json:
        json.dump(payload, io.open(args.json, 'w', encoding='utf-8'),
                  ensure_ascii=False, indent=2)
        print(f'  -> {args.json}')
    if args.fail_on_open_claims and findings:
        sys.exit(2)


if __name__ == '__main__':
    main()
