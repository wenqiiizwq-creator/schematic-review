#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
KiCad 网表 (kicadxml) -> 与 parse_netlist.py 相同的结构化索引

用法:
    python3 parse_kicad.py <文件.kicad_sch|文件.xml> [-o db.json]

输入可以是原理图（本脚本调用 `kicad-cli sch export netlist --format kicadxml`
导出）或已导出的 kicadxml 文件。输出字段与 Cadence 三件套解析器一致，后续
检查、计划与校验不区分输入来源。

三件必须分清的事（与 Cadence 流程相同）
--------------------------------------
1. **No-connect 属性**：KiCad 把带 NC 标记的引脚写成 `pintype="passive+no_connect"`，
   并单独放进 `unconnected-(...)` 网。这类网登记为伪网络，引脚另列
   `no_connect_nodes`——它是"图上声明不接"，不是"器件不贴"。
2. **真正悬空的引脚**：没有 NC 标记却落在 `unconnected-(...)` 网里的引脚保持为
   真实单节点网，Rule-01 照常扫出，不被伪网络掩盖。
3. **DNP 不贴**：`<property name="dnp"/>` 或 VALUE 带 NC 标记才写 `parts[ref].nc`。
   `exclude_from_bom` 只是 BOM 卫生标记，不作装配证据。

引脚功能名取 `pinfunction`（KiCad 的引脚名），缺失时回落到库符号的引脚名；
`~` 是 KiCad 的"无名"标记，按空处理，不伪造名字。
"""
import argparse
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET

from parse_netlist import is_not_populated, self_check

# KiCad 电气类型 -> 既有 PINUSE 词汇；未知类型保留原文，不静默当作 UNSPEC
PINUSE = {
    'input': 'IN', 'output': 'OUT', 'bidirectional': 'BI', 'tri_state': 'TRISTATE',
    'passive': 'UNSPEC', 'free': 'UNSPEC', 'unspecified': 'UNSPEC',
    'power_in': 'POWER', 'power_out': 'POWER',
    'open_collector': 'OCL', 'open_emitter': 'OCA', 'no_connect': 'NC',
}
MPN_FIELD = re.compile(
    r'^(MPN|MFR[._\s-]?P/?N|MANUFACTURER[_\s-]?PART[_\s-]?(NUMBER|NO\.?)|'
    r'PART[_\s-]?(NUMBER|NO\.?)|ORDER(ING)?[_\s-]?(CODE|NUMBER))$', re.I)
UNCONNECTED = re.compile(r'^unconnected-\(.*\)$', re.I)
NO_NAME = '~'


def _text(value):
    value = (value or '').strip()
    return '' if value in ('', NO_NAME) else value


def _pin_function(function, declared, pin):
    """引脚功能名：库符号的引脚名优先，导出器附加的 `_<脚号>` 装饰去掉。

    KiCad 的 kicadxml 把节点 `pinfunction` 写成 `名字_脚号`（官方库与
    easyeda2kicad 生成的库都如此）。带装饰的名字会让所有按引脚名识别的规则失效
    （EN_12 不是 EN），因此按库符号的引脚名还原；节点上真正不同的名字属图上指定的
    替代功能，保留不动。
    """
    if not declared:
        return function
    if not function or function in (declared, '%s_%s' % (declared, pin)):
        return declared
    return function


def export_netlist(schematic, target, executable=None):
    """调用 kicad-cli 导出 kicadxml；失败时把 kicad-cli 的原文抛出来。"""
    tool = executable or os.environ.get('KICAD_CLI') or shutil.which('kicad-cli')
    if not tool:
        raise RuntimeError(
            '找不到 kicad-cli；设置 KICAD_CLI 或先手工导出 kicadxml 再传入 .xml')
    command = [tool, 'sch', 'export', 'netlist', '--format', 'kicadxml',
               '-o', str(target), str(schematic)]
    done = subprocess.run(command, capture_output=True, text=True)
    if done.returncode != 0 or not os.path.isfile(target):
        raise RuntimeError('kicad-cli 导出失败: '
                           + (done.stderr or done.stdout or '无输出').strip())
    return [line.strip() for line in (done.stderr or '').splitlines()
            if re.search(r'\berror\b|\bfailed\b|\bcannot\b', line, re.I)]


def _libparts(root):
    """(lib:part) -> {'pins': {num: (name, pintype)}}"""
    table = {}
    for libpart in root.findall('./libparts/libpart'):
        key = '%s:%s' % (libpart.get('lib', ''), libpart.get('part', ''))
        pins = {}
        for pin in libpart.findall('./pins/pin'):
            number = _text(pin.get('num'))
            if number:
                pins[number] = (_text(pin.get('name')), _text(pin.get('type')))
        table[key] = {'pins': pins}
    return table


def _sheets(root):
    """sheetpath names -> 页号；层次页按导出顺序编号。"""
    pages = {}
    for sheet in root.findall('./design/sheet'):
        name = sheet.get('name')
        number = sheet.get('number')
        if name and number and number.isdigit():
            pages[name] = int(number)
    return pages


def _component(comp, libparts, pages):
    source = comp.find('libsource')
    lib = '%s:%s' % (source.get('lib', ''), source.get('part', '')) if source is not None else ''
    fields = {(field.get('name') or '').strip(): _text(field.text)
              for field in comp.findall('./fields/field')}
    properties = {(item.get('name') or '').strip(): (item.get('value') or '')
                  for item in comp.findall('./property')}
    mpn = next((value for name, value in sorted(fields.items())
                if MPN_FIELD.match(name) and value), '')
    value = _text(comp.findtext('value'))
    footprint = _text(comp.findtext('footprint'))
    sheetpath = comp.find('sheetpath')
    path = sheetpath.get('names') if sheetpath is not None else None
    part = {
        'prim': lib,
        'part': mpn or (source.get('part', '') if source is not None else ''),
        'jedec': footprint,
        'value': value,
        # dnp 是装配声明；exclude_from_bom 只是 BOM 卫生，不作装配证据。
        'nc': 'dnp' in properties or is_not_populated(lib, value),
    }
    return part, pages.get(path), lib


def parse(xml_text, export_errors=()):
    """kicadxml 文本 -> db 契约。"""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as error:
        raise ValueError('kicadxml 解析失败: %s' % error)
    if root.tag != 'export':
        raise ValueError('不是 kicadxml 网表（根元素为 %r）' % root.tag)

    libparts, pages = _libparts(root), _sheets(root)
    parts, ref2page, ref2lib = {}, {}, {}
    for comp in root.findall('./components/comp'):
        ref = _text(comp.get('ref'))
        if not ref:
            continue
        part, page, lib = _component(comp, libparts, pages)
        parts[ref] = part
        ref2lib[ref] = lib
        if page is not None:
            ref2page[ref] = page

    nets, pin2net, pinname, pintype = {}, {}, {}, {}
    no_connect_nodes, pseudo = [], []
    for net in root.findall('./nets/net'):
        name = _text(net.get('name'))
        if not name:
            continue
        nodes, declared_nc = [], []
        for node in net.findall('node'):
            ref, pin = _text(node.get('ref')), _text(node.get('pin'))
            if not ref or not pin:
                continue
            key = '%s.%s' % (ref, pin)
            nodes.append(key)
            pin2net[key] = name
            kinds = [x for x in (node.get('pintype') or '').split('+') if x]
            if 'no_connect' in kinds:
                declared_nc.append(key)
                no_connect_nodes.append(key)
            electrical = next((x for x in kinds if x != 'no_connect'), '')
            if electrical:
                pintype[key] = PINUSE.get(electrical, electrical.upper())
            declared = libparts.get(ref2lib.get(ref, ''), {}).get(
                'pins', {}).get(pin, ('', ''))[0]
            function = _pin_function(_text(node.get('pinfunction')), declared, pin)
            if function:
                pinname[key] = function
        nets.setdefault(name, []).extend(nodes)
        # 只有"图上声明不接"的汇集网算伪网络；没有 NC 标记的悬空引脚保持真实单节点网
        if UNCONNECTED.match(name) and nodes and len(declared_nc) == len(nodes):
            pseudo.append(name)

    declared_pinname, declared_pintype = {}, {}
    for ref, lib in ref2lib.items():
        for number, (name, kind) in libparts.get(lib, {}).get('pins', {}).items():
            node = '%s.%s' % (ref, number)
            if name:
                declared_pinname[node] = name
            if kind:
                declared_pintype[node] = PINUSE.get(kind, kind.upper())

    design = root.find('./design')
    return {
        'nets': nets, 'pin2net': pin2net, 'pinname': pinname, 'pintype': pintype,
        'parts': parts, 'ref2page': ref2page, 'pseudo_nets': sorted(set(pseudo)),
        'declared_pinname': declared_pinname, 'declared_pintype': declared_pintype,
        'export_errors': list(export_errors),
        'missing_primitives': sorted({lib for lib in ref2lib.values() if lib not in libparts}),
        'no_connect_nodes': sorted(set(no_connect_nodes)),
        'export_meta': {
            'source': (design.findtext('source') or '').strip() if design is not None else '',
            'tool': (design.findtext('tool') or '').strip() if design is not None else '',
            'date': (design.findtext('date') or '').strip() if design is not None else '',
            'format': 'kicadxml',
        },
    }


def build(path, executable=None):
    """输入 .kicad_sch 或 kicadxml；返回 db 契约。"""
    if not os.path.isfile(path):
        raise RuntimeError('缺少输入文件: %s' % path)
    if path.lower().endswith('.kicad_sch'):
        with tempfile.TemporaryDirectory() as directory:
            target = os.path.join(directory, 'netlist.xml')
            errors = export_netlist(path, target, executable)
            with io.open(target, encoding='utf-8', errors='replace') as stream:
                return parse(stream.read(), errors)
    with io.open(path, encoding='utf-8', errors='replace') as stream:
        return parse(stream.read())


def main():
    parser = argparse.ArgumentParser(description='解析 KiCad 网表为结构化索引')
    parser.add_argument('path', help='.kicad_sch 原理图，或已导出的 kicadxml')
    parser.add_argument('-o', '--out', default='db.json')
    parser.add_argument('--kicad-cli', dest='executable', default=None,
                        help='kicad-cli 路径（默认取 KICAD_CLI 或 PATH）')
    parser.add_argument('--no-strict', action='store_true',
                        help='自检失败时仅告警不退出（不建议）')
    args = parser.parse_args()
    try:
        db = build(args.path, args.executable)
    except (RuntimeError, ValueError) as error:
        sys.exit('[FATAL] %s' % error)
    db['integrity'] = {'self_check_passed': self_check(db, strict=not args.no_strict)}
    if db['no_connect_nodes']:
        print('  [No-connect 属性] %d 个引脚在图上声明不接；需核实每一处确实允许悬空'
              % len(db['no_connect_nodes']))
    json.dump(db, io.open(args.out, 'w', encoding='utf-8'), ensure_ascii=False)
    print(f'  -> {args.out}')


if __name__ == '__main__':
    main()
