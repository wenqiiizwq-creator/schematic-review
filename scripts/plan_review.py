#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AC0 Applicability Discovery：在首轮检查时生成逐项执行计划。

这个脚本只回答“哪些检查需要执行、何时执行、还缺什么证据”，不提前给
PASS/FAIL。HANDOFF 是独立下游动作，可以与后续 PASS/FAIL/INSUFFICIENT 并存。

用法：
    python3 plan_review.py db.json --intent intent.json \
        --datasheet-audit datasheet-audit.json \
        --evidence evidence.json --json review-plan.json
"""
import argparse
from copy import deepcopy
from electrical_contract import db_fingerprint, check_matches, readiness_gaps, validate_evidence, bounded, finite
import io
import json
import os
import re
import sys
from collections import Counter

from audit_datasheets import (
    datasheet_audit_all_available,
    datasheet_entry_for_ref,
    validate_datasheet_audit,
)


APPLICABILITY = ('APPLICABLE', 'NOT_APPLICABLE', 'UNDETERMINED')
READINESS = ('READY', 'WAITING_EVIDENCE', 'NOT_SCHEDULED')
RESULT_STATUSES = ('PASS', 'FAIL', 'INSUFFICIENT', 'NA')
HANDOFF_STATES = ('OPEN', 'ACCEPTED', 'VERIFIED')

GNDS = {'GND', 'PGND', 'AGND', 'DGND', 'EGND'}
RAIL_RE = re.compile(
    r'^(VCC|VDD|VDDA|VCCA|VOUT|VBAT|AVDD|DVDD|VIN|VBUS|V\d|[0-9]+V)',
    re.I)
EN_RE = re.compile(
    r'(^|_)(EN|ENABLE|SHDN|SHUTDOWN|PWREN|PWR_EN)(_|\d|$)', re.I)
STRAP_RE = re.compile(
    r'(^|_)(BOOT\w*|STRAP\w*|TEST_MODE\w*|CFG\w*|CONFIG\w*|MODE\d*)(_|$)',
    re.I)
I2C_RE = re.compile(r'(^|_)(I2C\w*|SCL\d*|SDA\d*)(_|$)', re.I)
FB_NAMES = {'FB', 'ADJ', 'VFB', 'FBX', 'VSENSE', 'VOSNS', 'VOUT_SENSE'}


FEATURE_CATALOG = {
    'DDR': {
        'pattern': r'LPDDR|DDR[2345]?|SDRAM|DQS|\bZQ\b',
        'criterion': '执行 DDR 供电、ZQ/ODT、时序拓扑和平台规则检查包',
        'required_materials': ['requirements', 'datasheets', 'platform_checklist'],
        'handoff': {
            'required': True,
            'receivers': ['PCB Layout', 'SI'],
            'constraint': '阻抗、拓扑、等长、回流与布局规则按平台规范落实',
            'verification': 'PCB 约束/DRC 与 SI 验证',
        },
    },
    'USB': {
        'pattern': r'USB|VBUS|TYPEC|TYPE_C',
        'criterion': '执行 USB 方向、VBUS 检测、串阻、REXT 与 ESD 检查包',
        'required_materials': ['requirements', 'datasheets'],
        'handoff': {
            'required': True,
            'receivers': ['PCB Layout'],
            'constraint': '差分阻抗、等长、回流、stub 和防护器件顺序',
            'verification': 'PCB 规则与版图复核',
        },
    },
    'ETHERNET': {
        'pattern': r'ETH|RGMII|RMII|SGMII|MDIO|\bMDI\d|PHY',
        'criterion': '执行以太网 PHY、MDI、网变、时钟、strap 与管理口检查包',
        'required_materials': ['requirements', 'datasheets', 'platform_checklist'],
        'handoff': {
            'required': True,
            'receivers': ['PCB Layout', 'SI'],
            'constraint': 'MDI/RGMII 阻抗、等长、回流及网变到接口布局规则',
            'verification': 'PCB 规则、版图复核与必要 SI 验证',
        },
    },
    'CAN': {
        'pattern': r'CANH|CANL|CAN_TX|CAN_RX|\bCAN\d*\b',
        'criterion': '执行 CAN 收发器、端接、偏置、隔离和防护检查包',
        'required_materials': ['requirements', 'datasheets'],
        'handoff': {
            'required': True,
            'receivers': ['PCB Layout', 'EMC/Test'],
            'constraint': '差分走线、防护器件顺序、隔离与端接布局',
            'verification': '版图复核与接口测试',
        },
    },
    'RS485': {
        'pattern': r'RS485|485_TX|485_RX|485_A|485_B',
        'criterion': '执行 RS485 方向、端接、偏置、隔离与防护检查包',
        'required_materials': ['requirements', 'datasheets'],
        'handoff': {
            'required': True,
            'receivers': ['PCB Layout', 'EMC/Test'],
            'constraint': '差分走线、防护顺序和隔离布局要求',
            'verification': '版图复核与接口测试',
        },
    },
    'I2C': {
        'pattern': r'(^|[:_-])(I2C\w*|SCL\d*|SDA\d*)([:_-]|$)',
        'criterion': '执行 I2C 上拉、域电压、地址和总线连通检查包',
        'required_materials': ['requirements', 'datasheets'],
        'handoff': {'required': False},
    },
    'SPI': {
        'pattern': r'\bSPI\w*|MOSI|MISO|SCLK',
        'criterion': '执行 SPI 供电域、CS 默认态、时钟和串阻检查包',
        'required_materials': ['requirements', 'datasheets'],
        'handoff': {'required': False},
    },
    'UART': {
        'pattern': r'UART|\bTXD\w*|\bRXD\w*',
        'criterion': '执行 UART 方向、电平域、连接器与防护检查包',
        'required_materials': ['requirements', 'datasheets'],
        'handoff': {'required': False},
    },
    'STORAGE': {
        'pattern': r'EMMC|SDIO|SDMMC|MICROSD|TF_CARD',
        'criterion': '执行 eMMC/SDIO 供电、上拉、串阻与启动检查包',
        'required_materials': ['requirements', 'datasheets', 'platform_checklist'],
        'handoff': {
            'required': True,
            'receivers': ['PCB Layout'],
            'constraint': '高速信号阻抗、等长、stub 与测试点规则',
            'verification': 'PCB 规则与版图复核',
        },
    },
    'RF': {
        'pattern': r'(^|[:_-])(RF|ANT|WIFI|WLAN|LTE|GNSS|SIM)([:_-]|$)',
        'criterion': '执行射频/模组供电、控制、默认通路、SIM 与防护检查包',
        'required_materials': ['requirements', 'datasheets'],
        'handoff': {
            'required': True,
            'receivers': ['RF/PCB Layout', 'EMC/Test'],
            'constraint': '射频阻抗、匹配、布局隔离与认证测试约束',
            'verification': 'RF 版图复核、匹配和实测',
        },
    },
    'ISOLATION': {
        'pattern': r'ISOLAT|(^|_)ISO(_|$)|DIGITAL_ISO',
        'criterion': '执行隔离域、耐压、跨域器件与接地检查包',
        'required_materials': ['requirements', 'datasheets'],
        'handoff': {
            'required': True,
            'receivers': ['PCB Layout', 'Safety'],
            'constraint': '隔离分区、爬电/电气间隙和禁布要求',
            'verification': 'PCB 实距与安规复核',
        },
    },
    'CLOCK': {
        'pattern': r'CLK|CLOCK|OSC|XTAL|XIN|XOUT|32K',
        'criterion': '执行晶振/时钟源、负载、使能和端点检查包',
        'required_materials': ['datasheets'],
        'handoff': {
            'required': True,
            'receivers': ['PCB Layout'],
            'constraint': '晶振回路、时钟走线和噪声隔离布局要求',
            'verification': '版图复核',
        },
    },
    'RESET': {
        'pattern': r'RESET|(^|_)RST|POR(_|$)',
        'criterion': '执行复位源、默认态、脉宽与全链路连通检查包',
        'required_materials': ['requirements', 'datasheets'],
        'handoff': {'required': False},
    },
}


COLD_RULES = {
    'Rule-01': '单节点悬空网',
    'Rule-02': '疑似网络名分裂',
    'Rule-03': '自动命名无源孤岛',
    'Rule-04': '电源轨无驱动',
    'Rule-05': '电源球无驱动',
    'Rule-06': 'VSS 球未入地',
    'Rule-10': 'ESD/TVS 挂残网',
    'Rule-13': '钳位器件直连超压轨',
    'Rule-15': 'NC 真网络/伪网络判别',
    'Rule-18': '同基名多轨',
    'Rule-20': 'BOM/库字段卫生',
}


CIRCUIT_CHECKS = {
    'POWER_CONVERTER': [
        ('voltage-headroom', '按 Vin/负载/温度保证窗口核对输出及 LDO dropout；与负载推荐工作范围比较'),
        ('current-stress', '按拓扑计算电感峰值/RMS、Isat、开关限流最小值及器件降额'),
        ('timing-stability', '核对最小导通/关断时间、Cout 有效容量/ESR、补偿和稳定工作条件'),
        ('loss-reverse', '核对损耗、反向电流、预偏置与放电路径；结温实现转 HANDOFF'),
    ],
    'POWER_PROTECTION': [
        ('thresholds', '核对 UVLO/OVLO/限流容差与检测目的，明确保护前后采样点'),
        ('soa', '核对 MOS VDS-I-t SOA、限流/故障计时、热态降额与重复重试能量'),
        ('coordination', '核对 TVS VRWM/VBR/VC 对应波形及温度、熔断器时间电流/熔断能量与后级承受能力'),
    ],
    'ANALOG': [
        ('dc-range', '运放输入共模/输出摆幅及偏置/失调/增益误差，含供电和温度极限'),
        ('dynamic-load', '运放 GBW/压摆率/容性负载；ADC 源阻抗、采样保持获取时间与建立误差'),
        ('reference-protection', 'ADC 基准驱动/误差、输入满量程及钳位/注入电流，含掉电状态'),
    ],
    'I2C': [
        ('sink-rise', '逐电气段合并全部上拉公差：Rp_min=(Vpullup_max-VOL_max)/IOL_guaranteed，Rp_max=tr_max/(0.8473*Cb_max)；核对串联压降'),
        ('domain-off', '两端 VIH/VIL、VOL、耐压及 Ioff；逐域掉电和外部设备先上电的注入路径'),
        ('address-state', '核对地址/复用/复位态和板载及外部可选上拉的装配组合'),
    ],
    'STARTUP': [
        ('sampled-level', '逐采样窗口计算 strap/EN 保证电压，含内部拉阻、LED、泄漏、电容和门限'),
        ('reset-timing', '按 V(t) 穿越门限时刻核对复位脉宽/释放与采样 setup/hold，不能以 RC 时间常数代替'),
        ('power-order', '检查慢爬升、棕断、短暂掉电、单域掉电、外部先供电、重试及恢复模式'),
    ],
    'DDR': [
        ('calibration', '分别按控制器和 DRAM 的具体型号/代际核对 ZQ/校准脚端接与精度，禁止跨器件套用'),
        ('termination', '逐数据/地址/时钟/VREF/VTT 电源域核对拓扑、端接、基准及上电条件'),
    ],
    'USB_C': [
        ('cc-role', '按 Source/Sink/DRP 角色及 PD 模式核对 CC/Rp/Rd、方向检测、线缆 VCONN'),
        ('vbus', '核对 VBUS 供电资格、电压档位、放电、反灌、过流及端口未供电状态'),
    ],
    'CAN_RS485': [
        ('termination-bias', '按实际总线端点和节点数核对端接等效负载、空闲偏置及接收保证差分门限'),
        ('common-mode', '核对收发器 VIO/默认态、总线共模范围、地偏差与未供电负载'),
    ],
    'CLOCK': [
        ('load-startup', '按晶体准确料号核对 CL/ESR/驱动功率、振荡器适配和起振条件；寄生及实测裕量转 HANDOFF'),
        ('oscillator-domain', '有源时钟输出幅度/电源域、使能态和上电有效时间'),
    ],
}


def _text(value):
    return isinstance(value, str) and bool(value.strip())


def validate_intent(intent):
    """验证扩展 intent；兼容旧版仅含 expect 的输入。"""
    if intent is None:
        return []
    if not isinstance(intent, dict):
        return ['intent 根对象必须为 object']
    errors = []
    if 'schema_version' in intent and intent['schema_version'] != 1:
        errors.append('schema_version 必须为 1')
    if intent.get('review_mode') not in (None, 'first', 'revision'):
        errors.append('review_mode 必须为 first/revision')
    expect = intent.get('expect', {})
    if not isinstance(expect, dict):
        errors.append('expect 必须为 object')
    else:
        for key, value in expect.items():
            if not _text(key) or isinstance(value, bool) or not isinstance(value, int):
                errors.append(f'expect.{key!r} 必须为非空名称和整数数量')
            elif value < 0:
                errors.append(f'expect.{key} 不得小于 0')
    features = intent.get('features', {})
    if not isinstance(features, dict):
        errors.append('features 必须为 object')
    else:
        for key, item in features.items():
            label = f'features.{key}'
            if not _text(key) or not isinstance(item, dict):
                errors.append(f'{label} 必须为 object')
                continue
            state = item.get('applicability')
            if state not in APPLICABILITY:
                errors.append(f'{label}.applicability 不支持: {state!r}')
            if state in ('APPLICABLE', 'NOT_APPLICABLE') and not _text(
                    item.get('citation')):
                errors.append(f'{label}.citation 缺失')
    materials = intent.get('materials', {})
    if not isinstance(materials, dict):
        errors.append('materials 必须为 object')
    else:
        for key, item in materials.items():
            label = f'materials.{key}'
            if not _text(key) or not isinstance(item, dict):
                errors.append(f'{label} 必须为 object')
                continue
            if not isinstance(item.get('available'), bool):
                errors.append(f'{label}.available 必须为 boolean')
            if item.get('available') and not _text(item.get('citation')):
                errors.append(f'{label}.citation 缺失')
    rails = intent.get('power_rails', {})
    if not isinstance(rails, dict):
        errors.append('power_rails 必须为按网络名索引的 object')
    else:
        for net, budget in rails.items():
            if not _text(net) or not isinstance(budget, dict):
                errors.append('power_rails 每轨必须为 object')
                continue
            for field in ('voltage_v', 'load_a'):
                value = budget.get(field)
                if field in budget and (not bounded(value) or (field == 'load_a' and value['min'] < 0)):
                    errors.append(f'power_rails.{net}.{field} 需要有效 min/max；负载电流用非负幅值')
            if 'available_a_min' in budget and (not finite(budget['available_a_min']) or budget['available_a_min'] < 0):
                errors.append(f'power_rails.{net}.available_a_min 需要非负有限数')
            if 'refs' in budget and (not isinstance(budget['refs'], list) or not budget['refs']
                    or not all(_text(x) for x in budget['refs'])):
                errors.append(f'power_rails.{net}.refs 需要非空位号数组')
    for field in ('power_sources', 'power_paths', 'circuits'):
        if field in intent and (not isinstance(intent[field], list)
                or not all(isinstance(x, dict) for x in intent[field])):
            errors.append(f'{field} 必须为 object 数组')
    for source in intent.get('power_sources', []) if isinstance(intent.get('power_sources', []), list) else []:
        if not isinstance(source, dict):
            continue
        if not _text(source.get('node')) or not _text(source.get('citation')) or not (
                isinstance(source.get('states'), list) and source['states']
                and all(_text(x) for x in source['states'])):
            errors.append('power_sources 需要 node/states/citation')
    for edge in intent.get('power_paths', []) if isinstance(intent.get('power_paths', []), list) else []:
        if isinstance(edge, dict) and not all(_text(edge.get(k)) for k in ('ref', 'from', 'to', 'state', 'citation')):
            errors.append('power_paths 需要 ref/from/to/state/citation')
    if (intent.get('power_sources') or intent.get('power_paths')) and not _text(intent.get('active_state')):
        errors.append('电源路径需要 active_state')
    seen_circuits = set()
    for circuit in intent.get('circuits', []) if isinstance(intent.get('circuits', []), list) else []:
        if not isinstance(circuit, dict):
            continue
        cid = circuit.get('id')
        if not _text(cid) or cid in seen_circuits:
            errors.append('circuits.id 缺失或重复')
        else:
            seen_circuits.add(cid)
        if not isinstance(circuit.get('domain'), str) or circuit['domain'] not in CIRCUIT_CHECKS:
            errors.append('circuits.domain 不支持')
        for field in ('refs', 'states'):
            values = circuit.get(field)
            if not isinstance(values, list) or not values or not all(_text(x) for x in values):
                errors.append(f'circuits.{field} 必须为非空字符串数组')
        if not _text(circuit.get('citation')):
            errors.append('circuits.citation 缺失')
    requirements = intent.get('requirements', [])
    if not isinstance(requirements, list):
        errors.append('requirements 必须为数组')
    else:
        seen = set()
        for item in requirements:
            if not isinstance(item, dict):
                errors.append('requirement 必须为 object')
                continue
            for key in ('id', 'text', 'citation', 'criterion'):
                if not _text(item.get(key)):
                    errors.append(f'requirement.{key} 缺失')
            rid = item.get('id')
            if _text(rid):
                if rid in seen:
                    errors.append(f'requirement id 重复: {rid}')
                seen.add(rid)
    return errors


def _material_available(intent, key, datasheet_audit=None):
    if key == 'datasheets' and datasheet_audit is not None:
        return datasheet_audit_all_available(datasheet_audit)
    item = (intent or {}).get('materials', {}).get(key, {})
    return isinstance(item, dict) and item.get('available') is True


def _slug(value):
    value = re.sub(r'[^A-Za-z0-9]+', '-', str(value or '').upper()).strip('-')
    return value[:64] or 'GLOBAL'


def _empty_handoff():
    return {'required': False, 'state': None}


def _handoff(config, applicable):
    if not config.get('required') or applicable != 'APPLICABLE':
        return _empty_handoff()
    return {
        'required': True,
        'state': 'OPEN',
        'receivers': list(config.get('receivers', [])),
        'constraint': config.get('constraint', ''),
        'verification': config.get('verification', ''),
    }


def _database_blobs(db):
    blobs = []
    for net in db.get('nets', {}):
        blobs.append(f'NET:{net}')
    for node, pin in db.get('pinname', {}).items():
        blobs.append(f'PIN:{node}:{pin}')
    for ref, part in db.get('parts', {}).items():
        blobs.append('PART:' + ':'.join([
            ref, str(part.get('part', '')), str(part.get('value', '')),
            str(part.get('prim', '')), str(part.get('jedec', ''))]))
    return blobs


def _feature_hits(db, pattern):
    regex = re.compile(pattern, re.I)
    return sorted(blob for blob in _database_blobs(db) if regex.search(blob))[:12]


def _differential_pairs(db):
    nets = set(db.get('nets', {}))
    found = set()
    for net in sorted(nets):
        candidates = []
        if net.endswith('_P'):
            candidates.append(net[:-2] + '_N')
        if net.endswith('-P'):
            candidates.append(net[:-2] + '-N')
        if net.endswith('+'):
            candidates.append(net[:-1] + '-')
        if net.upper().endswith('_DP'):
            candidates.append(net[:-3] + '_DM')
        for other in candidates:
            if other in nets:
                found.add((net, other))
    return sorted(found)


class ReviewPlanner:
    def __init__(self, db, intent=None, evidence=None, review_mode=None,
                 old_db_available=False, claims_available=False,
                 datasheet_audit=None):
        self.db = db
        self.db_sha256 = db_fingerprint(db)
        self.intent = intent or {}
        self.evidence = evidence or {}
        self.datasheet_audit = datasheet_audit
        self.review_mode = (
            review_mode or self.intent.get('review_mode') or 'first')
        self.old_db_available = old_db_available
        self.claims_available = claims_available
        self.checks = []
        self.rule_plan = []
        self.diagnostics = []
        self._ids = set()

    def matching_evidence(self, rule, obj):
        return [check for check in self.evidence.get('checks', [])
                if check_matches(self.db, check, rule, obj)]

    def evidence_ready(self, rule, obj):
        matches = self.matching_evidence(rule, obj)
        return bool(matches) and all(not readiness_gaps(self.db, x, self.datasheet_audit, self.db_sha256)
                                     for x in matches)

    def add_check(self, check_key, obj, criterion, stage, executor,
                  applicability='APPLICABLE', readiness='READY',
                  required_inputs=None, trigger=None, rule=None,
                  handoff=None):
        anchor = (obj.get('node') or obj.get('net') or obj.get('ref')
                  or obj.get('feature') or obj.get('page') or 'GLOBAL')
        base = f'{stage}.{_slug(check_key)}.{_slug(anchor)}'
        check_id, suffix = base, 2
        while check_id in self._ids:
            check_id = f'{base}-{suffix}'
            suffix += 1
        self._ids.add(check_id)
        if applicability != 'APPLICABLE':
            readiness = 'NOT_SCHEDULED'
        item = {
            'id': check_id,
            'check': check_key,
            'rule': rule,
            'object': obj,
            'criterion': criterion,
            'applicability': applicability,
            'stage': stage,
            'executor': executor,
            'readiness': readiness,
            'required_inputs': sorted(set(required_inputs or [])),
            'trigger': sorted(set(trigger or [])),
            'review_result': 'NA' if applicability == 'NOT_APPLICABLE' else None,
            'evidence_confidence': None,
            'handoff': handoff or _empty_handoff(),
        }
        matches = self.matching_evidence(rule, obj) if executor == 'AC0-HOT' else []
        if matches:
            for evidence in matches:
                child = deepcopy(item)
                child_base = child['id'] + '.' + _slug(evidence['id'])
                child_id, child_suffix = child_base, 2
                while child_id in self._ids:
                    child_id = f'{child_base}-{child_suffix}'
                    child_suffix += 1
                child['id'] = child_id
                self._ids.add(child_id)
                child['evidence_check_id'] = evidence['id']
                child['object']['state'] = (evidence.get('basis') or {}).get('state')
                gaps = readiness_gaps(self.db, evidence, self.datasheet_audit, self.db_sha256)
                child['readiness'] = 'WAITING_EVIDENCE' if gaps else 'READY'
                child['required_inputs'] = gaps
                self.checks.append(child)
            return self.checks[-1]
        if check_key.startswith('feature-'):
            item['role'] = 'coverage_parent'
            item['aggregation'] = '逐电路/状态子检查完成后汇总，禁止整域一次性 PASS'
        self.checks.append(item)
        return item

    def add_rule(self, rule, name, applicability, readiness, instances=None,
                 required_inputs=None, reason=''):
        if applicability != 'APPLICABLE':
            readiness = 'NOT_SCHEDULED'
        self.rule_plan.append({
            'rule': rule,
            'name': name,
            'applicability': applicability,
            'readiness': readiness,
            'instances': sorted(instances or []),
            'required_inputs': sorted(set(required_inputs or [])),
            'reason': reason,
        })

    def plan_features(self):
        explicit = {
            str(key).upper(): value
            for key, value in self.intent.get('features', {}).items()
        }
        names = sorted(set(FEATURE_CATALOG) | set(explicit))
        for name in names:
            config = FEATURE_CATALOG.get(name, {
                'pattern': r'(?!x)x',
                'criterion': f'执行项目自定义功能 {name} 的原理图检查包',
                'required_materials': ['requirements'],
                'handoff': {'required': False},
            })
            hits = _feature_hits(self.db, config['pattern'])
            item = explicit.get(name)
            requested = item.get('applicability') if item else None
            trigger = [f'netlist:{x}' for x in hits]
            if item and item.get('citation'):
                trigger.append(f'intent:{item["citation"]}')

            if requested == 'NOT_APPLICABLE' and hits:
                applicability = 'UNDETERMINED'
                self.diagnostics.append({
                    'code': 'INTENT_NETLIST_CONFLICT',
                    'feature': name,
                    'detail': '意图声明不适用，但网表检测到对应特征',
                    'hits': hits,
                })
            elif requested in ('APPLICABLE', 'NOT_APPLICABLE'):
                applicability = requested
            elif hits:
                applicability = 'APPLICABLE'
            else:
                applicability = 'UNDETERMINED'

            required = config.get('required_materials', [])
            if applicability == 'NOT_APPLICABLE':
                missing = []
            elif applicability == 'UNDETERMINED':
                missing = [f'intent.features.{name}']
                if requested == 'NOT_APPLICABLE' and hits:
                    missing.append('resolve intent/netlist conflict')
            else:
                missing = [
                    x for x in required
                    if not _material_available(
                        self.intent, x, self.datasheet_audit)
                ]
            readiness = 'WAITING_EVIDENCE' if missing else 'READY'
            if applicability == 'APPLICABLE' and requested == 'APPLICABLE' and not hits:
                self.diagnostics.append({
                    'code': 'REQUIRED_FEATURE_NOT_DETECTED',
                    'feature': name,
                    'detail': '设计意图要求该功能，但网表未检测到对应特征',
                })
            self.add_check(
                f'feature-{name.lower()}', {'feature': name},
                config['criterion'], 'ER5', 'Expert Review',
                applicability=applicability, readiness=readiness,
                required_inputs=missing, trigger=trigger,
                handoff=_handoff(config.get('handoff', {}), applicability))
            if applicability == 'APPLICABLE' and requested == 'APPLICABLE' and not hits:
                self.add_check(
                    'required-feature-presence', {'feature': name},
                    '验证设计意图要求的功能是否已在原理图中实现',
                    'AC0', 'AC0-COLD', readiness='READY',
                    trigger=[f'intent:{item["citation"]}'])

    def plan_circuit_checks(self):
        for circuit in self.intent.get('circuits', []):
            domain = circuit['domain']
            for state in circuit['states']:
                obj = {'circuit': circuit['id'], 'ref': circuit['refs'][0],
                       'refs': circuit['refs'], 'nets': circuit.get('nets', []), 'state': state}
                absent = [ref for ref in circuit['refs']
                          if ref not in self.db.get('parts', {})]
                missing = [f'datasheet:{ref}' for ref in circuit['refs']
                           if not (datasheet_entry_for_ref(self.datasheet_audit, ref) or {}).get('status') == 'AVAILABLE']
                missing += [f'unknown ref:{ref}' for ref in absent]
                for key, criterion in CIRCUIT_CHECKS[domain]:
                    item = self.add_check(
                        f'{circuit["id"]}-{key}-{state}', obj, criterion,
                        'ER4' if domain in ('POWER_CONVERTER', 'POWER_PROTECTION', 'ANALOG', 'I2C') else 'ER5',
                        'Expert Review', readiness='WAITING_EVIDENCE' if missing else 'READY',
                        required_inputs=missing, trigger=[f'intent.circuits:{circuit["citation"]}'])
                    item['domain'] = domain
                    item['analysis_required'] = True
                    item['scope'] = '原理图电气条件；PCB/实测验证另建 HANDOFF'

    def plan_concrete_checks(self):
        db = self.db
        nets = db.get('nets', {})
        pinname = db.get('pinname', {})
        pin2net = db.get('pin2net', {})
        pseudo = set(db.get('pseudo_nets', []))

        # ER1/AC0-hot：反馈、EN、strap 与 I2C 上拉。
        for node, pin in sorted(pinname.items()):
            net = pin2net.get(node)
            upper_pin = str(pin).strip().upper()
            ref = node.split('.')[0]
            if upper_pin in FB_NAMES and net:
                obj = {'node': node, 'net': net, 'ref': ref}
                ready = self.evidence_ready('Rule-08', obj)
                self.add_check(
                    'feedback-divider-wca', obj,
                    '按实际电阻与 Vref 公差验证反馈/监控分压窗口',
                    'ER4', 'AC0-HOT', readiness='READY' if ready else 'WAITING_EVIDENCE',
                    required_inputs=[] if ready else ['datasheet:Vref/目标窗口'],
                    trigger=[f'pinname:{pin}'], rule='Rule-08')
            if net and (EN_RE.search(upper_pin) or (
                    EN_RE.search(net) and re.match(r'^[UMQ]\d', ref, re.I))):
                obj = {'node': node, 'net': net, 'ref': ref}
                ready = self.evidence_ready('Rule-12', obj)
                self.add_check(
                    'enable-default-absmax', obj,
                    '核对 EN 有效极性、默认态、上拉轨与绝对最大额定',
                    'ER1', 'AC0-HOT', readiness='READY' if ready else 'WAITING_EVIDENCE',
                    required_inputs=[] if ready else ['datasheet:pin function/Abs Max'],
                    trigger=[f'pinname:{pin}', f'net:{net}'], rule='Rule-12')
            if net and (STRAP_RE.search(upper_pin) or (
                    STRAP_RE.search(net) and re.match(r'^[UMQ]\d', ref, re.I))):
                obj = {'node': node, 'net': net, 'ref': ref}
                ready = self.evidence_ready('Rule-16', obj)
                self.add_check(
                    'strap-required-state', obj,
                    '核对 BOOT/strap/test 引脚的强制态与采样窗口',
                    'ER1', 'AC0-HOT', readiness='READY' if ready else 'WAITING_EVIDENCE',
                    required_inputs=[] if ready else ['datasheet:strap table/mandatory wording'],
                    trigger=[f'pinname:{pin}', f'net:{net}'], rule='Rule-16')

        i2c_nets = set()
        for net, nodes in nets.items():
            if I2C_RE.search(net) or any(
                    I2C_RE.search(str(pinname.get(node, ''))) for node in nodes):
                i2c_nets.add(net)
        for net in sorted(i2c_nets - pseudo):
            obj = {'net': net}
            ready = self.evidence_ready('Rule-09', obj)
            self.add_check(
                'i2c-required-pull', obj,
                '核对指定两网间电阻装配与等效阻值；电平/上升时间另行检查',
                'ER1', 'AC0-HOT', readiness='READY' if ready else 'WAITING_EVIDENCE',
                required_inputs=[] if ready else ['datasheet/platform:I2C pull requirement'],
                trigger=[f'net:{net}'], rule='Rule-09')

        # ER1：连接器/定制接口 pin map。是否“新增”需复审基线进一步收窄。
        for ref, part in sorted(db.get('parts', {}).items()):
            if not re.match(r'^(J|P|CN)\d', ref, re.I) or part.get('nc'):
                continue
            obj = {'ref': ref}
            ready = self.evidence_ready('Rule-14', obj)
            self.add_check(
                'connector-pin-map', obj,
                '逐脚核对连接器符号与官方/对端 pinout',
                'ER1', 'AC0-HOT', readiness='READY' if ready else 'WAITING_EVIDENCE',
                required_inputs=[] if ready else ['connector drawing/opposite-side pinout'],
                trigger=[f'refdes:{ref}'], rule='Rule-14')

        # ER2：每条电源轨分别检查拓扑和功耗预算。
        for net in sorted(nets):
            if net in pseudo or net in GNDS or not RAIL_RE.match(net):
                continue
            self.add_check(
                'power-rail-topology', {'net': net},
                '确认电源轨驱动源、负载、域电压、时序与反灌路径',
                'ER2', 'Expert Review', trigger=[f'rail-name:{net}'])
            budget = self.intent.get('power_rails', {}).get(net, {})
            missing = []
            for field in ('voltage_v', 'load_a'):
                if not bounded(budget.get(field)):
                    missing.append(f'power_rails.{net}.{field}.min/max')
            if not finite(budget.get('available_a_min')):
                missing.append(f'power_rails.{net}.available_a_min')
            for field in ('state', 'citation'):
                if not _text(budget.get(field)):
                    missing.append(f'power_rails.{net}.{field}')
            if not budget.get('refs'):
                missing.append(f'power_rails.{net}.refs')
            for ref in budget.get('refs', []):
                entry = datasheet_entry_for_ref(self.datasheet_audit, ref)
                if not entry or entry.get('status') != 'AVAILABLE':
                    missing.append(f'datasheet:{ref}')
            self.add_check(
                'power-rail-budget', {'net': net},
                '按最大负载、电压范围与器件能力验证功率预算和裕量',
                'ER2', 'Expert Review',
                readiness='WAITING_EVIDENCE' if missing else 'READY',
                required_inputs=missing, trigger=[f'rail-name:{net}'],
                handoff={
                    'required': True, 'state': 'OPEN',
                    'receivers': ['PCB Layout', 'Thermal/Test'],
                    'constraint': '大电流载流、压降、去耦与散热要求',
                    'verification': 'PCB 复核与温升/压降验证',
                })

        # ER3：差分连通 PASS/FAIL 与 PCB HANDOFF 可以并存。
        for positive, negative in _differential_pairs(db):
            self.add_check(
                'differential-pair-connectivity',
                {'net_p': positive, 'net_n': negative, 'net': positive},
                '核对差分 P/N 两端语义、耦合/端接拓扑和全链路连通',
                'ER3', 'Expert Review',
                trigger=[f'pair:{positive}/{negative}'],
                handoff={
                    'required': True, 'state': 'OPEN',
                    'receivers': ['PCB Layout'],
                    'constraint': '按接口规范落实差分阻抗、等长、间距与回流',
                    'verification': 'PCB 约束与版图复核',
                })

        # ER6：每张实际出现器件的页面独立目检。
        pages = sorted(
            {page for page in db.get('ref2page', {}).values()
             if page not in (None, '')}, key=lambda value: str(value))
        pdf_ready = _material_available(self.intent, 'schematic_pdf')
        for page in pages:
            self.add_check(
                'schematic-page-graphic-review', {'page': page},
                '目检极性、方向、pin1、Option/NC 表和图形语义',
                'ER6', 'Expert Review',
                readiness='READY' if pdf_ready else 'WAITING_EVIDENCE',
                required_inputs=[] if pdf_ready else ['schematic_pdf'],
                trigger=[f'ref2page:{page}'])

        # ER7：每颗 IC/模组独立做身份与封装一致性检查。
        datasheets_ready = _material_available(
            self.intent, 'datasheets', self.datasheet_audit)
        for ref, part in sorted(db.get('parts', {}).items()):
            if not re.match(r'^[UM]\d', ref, re.I) or part.get('nc'):
                continue
            audit_entry = datasheet_entry_for_ref(self.datasheet_audit, ref)
            if self.datasheet_audit is not None:
                ready = bool(
                    audit_entry and audit_entry.get('status') == 'AVAILABLE')
                identity = (
                    audit_entry.get('identity') if audit_entry
                    else part.get('value') or part.get('part') or ref)
                required_inputs = [] if ready else [f'datasheet:{identity}']
                audit_trigger = [
                    'datasheet-audit:'
                    + (audit_entry.get('status') if audit_entry else 'UNLISTED')
                ]
            else:
                ready = datasheets_ready
                required_inputs = [] if ready else ['datasheets']
                audit_trigger = []
            self.add_check(
                'component-identity-package', {'ref': ref},
                '核对 MPN、符号、引脚、封装字段、参数档位和替代兼容性',
                'ER7', 'Expert Review',
                readiness='READY' if ready else 'WAITING_EVIDENCE',
                required_inputs=required_inputs,
                trigger=[
                    f'refdes:{ref}', f'part:{part.get("part", "")}'
                ] + audit_trigger)

    def plan_rules(self):
        index_requirements = {
            'Rule-01': ['nets'], 'Rule-02': ['nets'],
            'Rule-03': ['nets', 'parts'], 'Rule-04': ['nets', 'parts'],
            'Rule-05': ['pinname'], 'Rule-06': ['pinname'],
            'Rule-10': ['nets', 'parts'], 'Rule-13': ['nets', 'parts'],
            'Rule-15': ['nets'], 'Rule-18': ['nets'], 'Rule-20': ['parts'],
        }
        for rule, name in sorted(COLD_RULES.items()):
            missing = [key for key in index_requirements[rule]
                       if not self.db.get(key)]
            self.add_rule(
                rule, name, 'APPLICABLE',
                'WAITING_EVIDENCE' if missing else 'READY',
                required_inputs=missing, reason='纯网表冷跑规则')

        by_rule = {}
        for check in self.checks:
            if check.get('rule'):
                by_rule.setdefault(check['rule'], []).append(check['id'])
        expect = self.intent.get('expect') or {}
        self.add_rule(
            'Rule-07', '关键器件计数',
            'APPLICABLE' if expect else 'UNDETERMINED',
            'READY' if expect else 'WAITING_EVIDENCE',
            required_inputs=[] if expect else ['intent.expect'],
            reason='必须由设计意图定义“该有/该删”')
        rail_instances = [
            x['id'] for x in self.checks
            if x['check'] == 'power-rail-topology']
        self.add_rule(
            'Rule-11', '检测点是否取在正确电源轨',
            'APPLICABLE' if rail_instances else 'UNDETERMINED',
            'READY' if rail_instances else 'WAITING_EVIDENCE',
            instances=rail_instances,
            required_inputs=[] if rail_instances else ['power-tree context'],
            reason='ER2 建电源树后逐检测点判定，非 AC0 Lint 定判')
        for rule, name in (
                ('Rule-08', '参数验算'), ('Rule-09', '必需上拉/串阻'),
                ('Rule-12', 'EN 默认态/耐压'), ('Rule-14', '符号引脚映射'),
                ('Rule-16', 'strap 强制态')):
            instances = by_rule.get(rule, [])
            if instances:
                ready = all(next(x for x in self.checks if x['id'] == cid)[
                            'readiness'] == 'READY' for cid in instances)
                self.add_rule(
                    rule, name, 'APPLICABLE',
                    'READY' if ready else 'WAITING_EVIDENCE', instances=instances,
                    required_inputs=[] if ready else ['ER1 structured evidence'],
                    reason='由网表中的具体位号/网络实例化')
            else:
                self.add_rule(
                    rule, name, 'UNDETERMINED', 'WAITING_EVIDENCE',
                    required_inputs=['design intent/platform applicability'],
                    reason='网表未检测到实例，但不能据此直接判 NA')

        pintype = self.db.get('pintype', {})
        self.add_rule(
            'Rule-19', 'PINUSE/ERC', 'APPLICABLE',
            'READY' if pintype else 'WAITING_EVIDENCE',
            required_inputs=[] if pintype else ['pintype/PINUSE'],
            reason='所有原理图均适用；输入缺失只影响准备度')

        if self.review_mode == 'first':
            self.add_rule(
                'Rule-17', '改版 Diff/历史意见闭环',
                'NOT_APPLICABLE', 'NOT_SCHEDULED',
                reason='首审没有旧版对比基线')
        else:
            missing = []
            if not self.old_db_available:
                missing.append('old_db')
            if not self.claims_available:
                missing.append('review_claims')
            self.add_rule(
                'Rule-17', '改版 Diff/历史意见闭环',
                'APPLICABLE', 'WAITING_EVIDENCE' if missing else 'READY',
                required_inputs=missing,
                reason='复审必须验证新旧网表与历史意见断言')

    def plan_coverage(self):
        for name, criterion in {
            'input_consistency': '核对本轮输入版本、哈希、导出完整性与装配配置',
            'requirements': '需求逐条拆解并与电路检查双向追溯，缺口不得隐藏',
            'chains': '全部接口/电源/检测/使能/复位/时钟逐路终点与返回路径覆盖',
            'states': '逐关键电路覆盖启动、复位、运行、掉电、外部带电及需求内故障状态',
            'datasheets': '全部关键器件适用章节/errata及官方物理脚双向差集已审',
            'history': '首审/复审已裁定，历史意见逐条复验并保留撤回/复发',
        }.items():
            self.add_check('coverage-' + name, {'feature': name}, criterion,
                           'ER7', 'Expert Review', trigger=['coverage-protocol'])
        for item in self.intent.get('requirements', []):
            self.add_check('requirement', {'requirement_id': item['id'], 'feature': item['id']},
                           item['criterion'], 'ER5', 'Expert Review',
                           trigger=[item['citation'], item['text']])
        # The declared inventory includes symbol pins omitted from connected nets.
        for ref, part in sorted(self.db.get('parts', {}).items()):
            if re.match(r'^(U|M|Q|D|J|P|CN)\d', ref, re.I) and not part.get('nc'):
                self.add_check('physical-pin-inventory', {'ref': ref},
                               '官方物理脚与符号声明/网表实有脚双向差集，含未连/EP/隐藏电源',
                               'ER1', 'Expert Review', readiness='WAITING_EVIDENCE',
                               required_inputs=['official full pinout + exact MPN/package'],
                               trigger=[f'refdes:{ref}'])

    def build(self):
        self.plan_coverage()
        self.plan_features()
        self.plan_concrete_checks()
        self.plan_circuit_checks()
        for material in (self.datasheet_audit or {}).get('materials', []):
            if material.get('status') == 'NOT_FOUND':
                self.diagnostics.append({
                    'code': 'DATASHEET_NOT_FOUND',
                    'identity': material.get('identity'),
                    'refdes': material.get('refdes', []),
                    'message': material.get('message'),
                })
        self.checks.sort(key=lambda x: x['id'])
        self.plan_rules()
        self.rule_plan.sort(key=lambda x: x['rule'])
        applicability = Counter(x['applicability'] for x in self.checks)
        readiness = Counter(x['readiness'] for x in self.checks)
        return {
            'schema_version': 1,
            'generated_by': 'AC0 Applicability Discovery',
            'review_mode': self.review_mode,
            'result_model': {
                'review_result': list(RESULT_STATUSES),
                'handoff_is_independent': True,
                'handoff_states': list(HANDOFF_STATES),
            },
            'aggregate_release_gate': {
                'evaluate_after_per_check_review': True,
                'requirements': [
                    'no_unresolved_blocking_fail',
                    'no_unresolved_blocking_insufficient',
                    'all_applicable_checks_executed_or_accepted',
                    'required_handoffs_have_receiver_constraint_verification',
                    'revision_diff_and_claims_pass_when_applicable',
                ],
            },
            'summary': {
                'checks_total': len(self.checks),
                'applicability': dict(sorted(applicability.items())),
                'readiness': dict(sorted(readiness.items())),
                'handoff_required': sum(
                    1 for x in self.checks if x['handoff']['required']),
                'diagnostics': len(self.diagnostics),
                'datasheet_unresolved': (
                    (self.datasheet_audit or {}).get(
                        'summary', {}).get('unresolved')),
            },
            'datasheet_audit': {
                'provided': self.datasheet_audit is not None,
                'summary': (
                    (self.datasheet_audit or {}).get('summary')
                    if self.datasheet_audit is not None else None),
                'agent_requests': (
                    (self.datasheet_audit or {}).get('agent_requests', [])
                    if self.datasheet_audit is not None else []),
                'user_messages': (
                    (self.datasheet_audit or {}).get('user_messages', [])
                    if self.datasheet_audit is not None else []),
            },
            'rule_plan': self.rule_plan,
            'checks': self.checks,
            'diagnostics': self.diagnostics,
        }


def build_review_plan(db, intent=None, evidence=None, review_mode=None,
                      old_db_available=False, claims_available=False,
                      datasheet_audit=None):
    if evidence is not None:
        errors = validate_evidence(evidence)
        if errors:
            raise ValueError('evidence.json 无效: ' + '; '.join(errors))
    return ReviewPlanner(
        db, intent, evidence, review_mode, old_db_available,
        claims_available, datasheet_audit).build()


def main():
    parser = argparse.ArgumentParser(
        description='AC0 首轮检查适用性发现与逐项执行计划')
    parser.add_argument('db', help='parse_netlist.py 产出的 db.json')
    parser.add_argument('--intent', help='设计意图 JSON')
    parser.add_argument('--evidence', help='ER1 结构化证据 JSON')
    parser.add_argument(
        '--datasheet-audit',
        help='audit_datasheets.py 产出的逐物料覆盖审计 JSON')
    parser.add_argument('--review-mode', choices=('first', 'revision'))
    parser.add_argument('--old-db', help='复审旧版 db.json（只判定可用性）')
    parser.add_argument('--claims', help='历史意见断言 JSON（只判定可用性）')
    parser.add_argument('--json', required=True, help='写出 review-plan.json')
    args = parser.parse_args()

    for label, path in (('--old-db', args.old_db), ('--claims', args.claims)):
        if path and not os.path.isfile(path):
            sys.exit(f'[FATAL] {label} 文件不存在: {path}')

    db = json.load(io.open(args.db, encoding='utf-8'))
    intent = json.load(io.open(args.intent, encoding='utf-8')) if args.intent else None
    evidence = (json.load(io.open(args.evidence, encoding='utf-8'))
                if args.evidence else None)
    datasheet_audit = (
        json.load(io.open(args.datasheet_audit, encoding='utf-8'))
        if args.datasheet_audit else None)
    errors = validate_intent(intent)
    if errors:
        sys.exit('[FATAL] intent.json 无效:\n  - ' + '\n  - '.join(errors))
    if evidence is not None:
        errors = validate_evidence(evidence)
        if errors:
            sys.exit('[FATAL] evidence.json 无效: ' + '; '.join(errors))
    if datasheet_audit is not None:
        errors = validate_datasheet_audit(datasheet_audit, db)
        if errors:
            sys.exit('[FATAL] datasheet-audit.json 无效:\n  - '
                     + '\n  - '.join(errors))
    plan = build_review_plan(
        db, intent, evidence, args.review_mode,
        old_db_available=bool(args.old_db), claims_available=bool(args.claims),
        datasheet_audit=datasheet_audit)
    json.dump(plan, io.open(args.json, 'w', encoding='utf-8'),
              ensure_ascii=False, indent=2)
    summary = plan['summary']
    print('=== AC0 Applicability Discovery ===')
    print(f"  checks={summary['checks_total']}  "
          f"applicability={summary['applicability']}")
    print(f"  readiness={summary['readiness']}  "
          f"handoff_required={summary['handoff_required']}")
    print(f'  -> {args.json}')


if __name__ == '__main__':
    main()
