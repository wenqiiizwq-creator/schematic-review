#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""热跑计算器的共享数值工具。

只做区间算术与字段检查：保证值缺失就报缺口（INSUFFICIENT），不用典型值顶替，
也不代替专家判断适用条件。
"""
import math

WORST = ('min', 'max')


def number(value):
    """有限数值或 None；布尔不算数值。"""
    if isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if math.isfinite(parsed) else None


def span(value):
    """保证区间 (min, max)；缺任一端或次序颠倒都返回 None。"""
    if not isinstance(value, dict):
        return None
    low, high = number(value.get('min')), number(value.get('max'))
    if low is None or high is None or low > high:
        return None
    return (low, high)


def negate(bounds):
    """P 沟道等负压量纲翻转到与 N 沟道同向比较。"""
    return (-bounds[1], -bounds[0])


def structure_errors(check, label, spans=(), numbers=(), texts=(), choices=()):
    """热跑证据的结构校验；与既有 validate_evidence 的文案风格一致。"""
    errors = []
    for field in spans:
        if field in check and span(check[field]) is None:
            errors.append('%s.%s 必须为带有限 min/max 的 object' % (label, field))
    for field in numbers:
        if field in check and number(check[field]) is None:
            errors.append('%s.%s 必须为有限数值' % (label, field))
    for field in texts:
        value = check.get(field)
        if field in check and not (isinstance(value, str) and value.strip()):
            errors.append('%s.%s 必须为非空字符串' % (label, field))
    for field, allowed in choices:
        if field in check and check[field] not in allowed:
            errors.append('%s.%s 必须为 %s 之一' % (label, field, '/'.join(sorted(allowed))))
    return errors


def missing_gaps(check, spans=(), numbers=(), texts=()):
    """按保证值缺口生成 readiness gaps；文案指明缺的是哪一项保证值。"""
    gaps = []
    for field in spans:
        if span(check.get(field)) is None:
            gaps.append('%s 缺少保证 min/max' % field)
    for field in numbers:
        if number(check.get(field)) is None:
            gaps.append('%s 缺少保证值' % field)
    for field in texts:
        value = check.get(field)
        if not (isinstance(value, str) and value.strip()):
            gaps.append('%s 缺少声明' % field)
    return gaps


def fmt(value, unit=''):
    return '%.6g%s' % (value, unit)
