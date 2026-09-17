#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""计划项生成的共享小工具（原 plan_review 内部函数，供检查器复用）。"""
import re


def slug(value):
    value = re.sub(r'[^A-Za-z0-9]+', '-', str(value or '').upper()).strip('-')
    return value[:64] or 'GLOBAL'


def empty_handoff():
    return {'required': False, 'state': None}


def handoff(config, applicable):
    if not config.get('required') or applicable != 'APPLICABLE':
        return empty_handoff()
    return {
        'required': True,
        'state': 'OPEN',
        'receivers': list(config.get('receivers', [])),
        'constraint': config.get('constraint', ''),
        'verification': config.get('verification', ''),
    }
