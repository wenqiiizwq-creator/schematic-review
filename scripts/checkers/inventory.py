#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""清单打包：逐装配状态扫描、输入指纹绑定与内容摘要。

摘要覆盖全部清单内容，计划项据此绑定；输入或状态一变，旧绑定立即过期。
"""
import hashlib
import json

from electrical_contract import db_fingerprint

from . import states as state_lib


def digest(payload):
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(',', ':'))
    return hashlib.sha256(blob.encode('utf-8')).hexdigest()


def build(db, cfg, key, scanner, discovery_gaps=(), extra=None):
    """按声明的装配状态逐一扫描；未声明时用按图状态并保留缺口。

    extra(state) 返回同一状态下的附加清单字段，避免为第二类对象重复解析状态。
    """
    states = []
    for state in state_lib.resolve(db, cfg):
        entry = {'id': state['id'], 'citation': state['citation'],
                 'declared': state['declared'], 'gaps': state['gaps'],
                 key: scanner(state)}
        entry.update(extra(state) if extra else {})
        states.append(entry)
    inventory = {
        'schema_version': 1,
        'input_sha256': db_fingerprint(db),
        'context': cfg,
        'discovery_gaps': sorted(discovery_gaps),
        'states': states,
    }
    inventory['digest'] = digest(inventory)
    return inventory


def walk(inventory, key):
    """遍历 (状态, 对象)。"""
    for state in inventory.get('states', []):
        for item in state.get(key, []):
            yield state, item
