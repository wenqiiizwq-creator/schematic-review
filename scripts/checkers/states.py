#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""新检查器共享的装配状态解析。

名称、nc=false、默认闭合都不是状态证据：没有声明就保留缺口，不当作已核实。
"""
import re

from electrical_contract import db_fingerprint

STATE_FIELDS = ('id', 'citation', 'population', 'jumpers')
MAX_STATES = 32


def _text(value):
    return isinstance(value, str) and bool(value.strip())


def state_errors(cfg, db, label):
    """校验 states 段；文案与既有模块保持一致。"""
    errors = []

    def require(ok, message):
        if not ok:
            errors.append(label + ': ' + message)

    states = cfg.get('states')
    require(isinstance(states, list) and 0 < len(states) <= MAX_STATES,
            'states must contain 1..%d states' % MAX_STATES)
    seen = set()
    for state in states if isinstance(states, list) else []:
        if not isinstance(state, dict):
            require(False, 'state must be an object')
            continue
        require(not set(state) - set(STATE_FIELDS), 'state has unsupported fields')
        sid = state.get('id')
        require(_text(sid) and sid not in seen, 'state id missing/duplicate')
        if _text(sid):
            seen.add(sid)
        require(_text(state.get('citation')), 'state needs assembly/configuration citation')
        for key in ('population', 'jumpers'):
            entries = state.get(key, {})
            require(isinstance(entries, dict), key + ' must be an object')
            for ref, value in entries.items() if isinstance(entries, dict) else []:
                require(_text(ref) and (db is None or ref in db.get('parts', {})),
                        key + ': unknown ref ' + str(ref))
                ok = (type(value) is bool if key == 'population'
                      else isinstance(value, str) and value in ('closed', 'open', 'unknown'))
                require(ok, key + ': invalid value for ' + str(ref))
    return errors


def fingerprint_errors(cfg, db, label):
    errors = []
    digest = cfg.get('db_sha256')
    if not (isinstance(digest, str) and re.fullmatch('[0-9a-f]{64}', digest)):
        errors.append(label + ': db_sha256 is required')
    elif db is not None and digest != db_fingerprint(db):
        errors.append(label + ': stale db_sha256')
    return errors


def as_built(db):
    """无声明时的按图状态：只用网表 nc，装配证据缺口照常登记。"""
    fitted = sorted(ref for ref, part in db.get('parts', {}).items() if not part.get('nc'))
    return [{
        'id': 'as-built',
        'citation': None,
        'fitted': fitted,
        'jumpers': {},
        'gaps': ['assembly state unverified: netlist nc only'],
        'declared': False,
    }]


def resolve(db, cfg):
    """按声明展开逐状态视图；未声明装配的器件形成逐条缺口。"""
    if not cfg or not isinstance(cfg.get('states'), list):
        return as_built(db)
    parts = db.get('parts', {})
    resolved = []
    for state in cfg['states']:
        population = state.get('population', {}) or {}
        jumpers = state.get('jumpers', {}) or {}
        gaps, fitted = [], []
        for ref in sorted(parts):
            if ref in population:
                if population[ref]:
                    fitted.append(ref)
            else:
                gaps.append('population:' + ref)
        for ref, position in sorted(jumpers.items()):
            if position == 'unknown':
                gaps.append('jumper:' + ref)
            elif position == 'open' and ref in fitted:
                fitted.remove(ref)
        resolved.append({
            'id': state.get('id'),
            'citation': state.get('citation'),
            'fitted': fitted,
            'jumpers': dict(sorted(jumpers.items())),
            'gaps': sorted(gaps),
            'declared': True,
        })
    return resolved


def section_errors(intent, db, label, *, item_field, item_key, fields,
                   ref_fields=('ref',), net_fields=()):
    """检查器 intent 段的共用校验：根字段、指纹、状态、声明条目、排除项。

    没有该段时返回空列表——未声明是缺口（由清单登记），不是输入错误。
    """
    if intent is None or (isinstance(intent, dict) and label not in intent):
        return []
    if not isinstance(intent, dict) or not isinstance(intent.get(label), dict):
        return ['%s must be an object' % label]
    cfg, errors = intent[label], []

    def require(ok, message):
        if not ok:
            errors.append(label + ': ' + message)

    allowed = ('schema_version', 'db_sha256', 'states', item_field, 'exclusions')
    require(not set(cfg) - set(allowed), 'root has unsupported fields')
    require(type(cfg.get('schema_version')) is int and cfg['schema_version'] == 1,
            'schema_version must be 1')
    errors.extend(fingerprint_errors(cfg, db, label))
    errors.extend(state_errors(cfg, db, label))
    items = cfg.get(item_field, [])
    require(isinstance(items, list), item_field + ' must be an array')
    seen = set()
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            require(False, item_key + ' must be an object')
            continue
        require(not set(item) - set(fields), item_key + ' has unsupported fields')
        iid = item.get('id')
        require(_text(iid) and iid not in seen, item_key + ' id missing/duplicate')
        if _text(iid):
            seen.add(iid)
        require(_text(item.get('citation')), item_key + ' needs a citation')
        for key in ref_fields:
            ref = item.get(key)
            require(isinstance(ref, str) and (db is None or ref in db.get('parts', {})),
                    item_key + ' ' + key + ' unknown: ' + str(ref))
        for key in net_fields:
            net = item.get(key)
            require(net is None or (isinstance(net, str)
                                    and (db is None or net in db.get('nets', {}))),
                    key + ' unknown: ' + str(net))
    exclusions = cfg.get('exclusions', [])
    require(isinstance(exclusions, list), 'exclusions must be an array')
    for item in exclusions if isinstance(exclusions, list) else []:
        require(isinstance(item, dict) and isinstance(item.get('ref'), str)
                and _text(item.get('citation')), 'exclusion needs ref and citation')
    return errors


def declared_refs(cfg, item_field):
    items = (cfg or {}).get(item_field, []) if isinstance(cfg, dict) else []
    return {item['ref'] for item in items if isinstance(item, dict) and isinstance(item.get('ref'), str)}


def excluded_refs(cfg):
    items = (cfg or {}).get('exclusions', []) if isinstance(cfg, dict) else []
    return {item['ref'] for item in items if isinstance(item, dict) and isinstance(item.get('ref'), str)}
