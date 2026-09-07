#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Cadence/OrCAD allegro 三件套 -> 结构化索引

用法:
    python3 parse_netlist.py <allegro目录> [-o db.json]

输出 JSON 含:
    nets      网络名 -> [refdes.pin, ...]
    pin2net   refdes.pin -> 网络名
    pinname   refdes.pin -> 引脚功能名（识别 SoC 电源球/信号球身份的关键）
    pintype   refdes.pin -> PINUSE（POWER/GROUND/IN/OUT/UNSPEC）
    parts     refdes -> {prim, part, jedec, value, nc}
    ref2page  refdes -> 页号
    pseudo_nets  工具生成的伪网络名列表（见下）

两个必须理解的点
----------------
1. **pinname 有两种布局**，取决于 PSTWRITER 版本：
     变体 A:  NODE_NAME 之后的属性行里带 'NAME':CDS_PINID
     变体 B:  NODE_NAME -> 实例行 -> 紧接一行 'NAME':;
   本脚本两种都试。**若解析后 pinname 覆盖率过低会直接报错退出**——
   否则 Rule-05(电源球无驱动)/Rule-06(VSS未入地) 会静默扫出 0 条，
   报告写成"电源球全扫通过"而实际一个都没查。

2. **伪网络**：PSTWRITER 会把"带 No-Connect 属性且无连线"的引脚
   统一塞进一张名为 NC 的网。它不是电气短路。
   启发式：仅把无层次路径、名称符合 NC 约定的汇集网列为 pseudo_nets。
   普通扁平命名网络仍是真实网络；NC 归类须结合图面/导出器语义复核。
"""
import argparse
import io
import json
import os
import re
import sys


def _read(path):
    if not os.path.isfile(path):
        sys.exit(f'[FATAL] 缺少文件: {path}')
    with io.open(path, encoding='utf-8', errors='replace') as stream:
        return stream.read()


# --------------------------------------------------------------------------
# pstxnet: nets / pinname / 伪网络识别
# --------------------------------------------------------------------------
def parse_pstxnet(text):
    nets, pinname, pseudo = {}, {}, []
    cur = last = state = None
    cur_has_hier = False

    for line in text.splitlines():
        s = line.strip()

        if line.startswith('NET_NAME'):
            state, last = 'want_net', None
            continue

        if state == 'want_net' and s.startswith("'") and s.endswith("'"):
            cur = s[1:-1]
            nets.setdefault(cur, [])
            cur_has_hier = False
            state = 'want_signal'
            continue

        # 网络头之后紧跟实例行与 C_SIGNAL；仅标记符合 NC 约定的平面汇集网
        if state == 'want_signal':
            if s.startswith("C_SIGNAL="):
                cur_has_hier = '@' in s
                if not cur_has_hier and cur and re.fullmatch(r'NC[_\-\d]*', cur, re.I):
                    pseudo.append(cur)
                state = None
                continue
            if s.startswith("'") and not s.endswith(':;'):
                continue

        m = re.match(r'^NODE_NAME\s+(\S+)\s+(\S+)\s*$', line)
        if m and cur:
            last = f'{m.group(1)}.{m.group(2)}'
            nets[cur].append(last)
            state = 'want_pinname'
            continue

        if last:
            # 变体 A: 属性行带 CDS_PINID
            a = re.search(r"'\\([^\\]+)\\':CDS_PINID", line)
            if a:
                pinname[last] = a.group(1)
                continue
            if state == 'want_pinname':
                # 变体 B: 实例行之后紧跟 'NAME':;
                if s.startswith("'@"):
                    continue
                b = re.match(r"^'(.*)':;$", s)
                if b:
                    pinname[last] = b.group(1)
                    state = None
                    continue

    return nets, pinname, sorted(set(pseudo))


# --------------------------------------------------------------------------
# pstchip: primitive 表（型号/封装/值/引脚映射/PINUSE）
# --------------------------------------------------------------------------
def parse_pstchip(text):
    prim = {}
    for m in re.finditer(r"primitive '(.*?)';(.*?)end_primitive;", text, re.S):
        name, body = m.group(1), m.group(2)

        def g(k):
            mm = re.search(k + r"='(.*?)'", body)
            return mm.group(1) if mm else ''

        pins, pinuse = {}, {}
        # PIN_NUMBER 既可能是数字，也可能是 BGA 字母数字脚号；PINUSE 与
        # PIN_NUMBER 之间还可能夹有其他属性，不能依赖两行严格相邻。
        pin_blocks = re.finditer(
            r"(?ms)^[ \t]*'([^'\n]+)':\s*(.*?)"
            r"(?=^[ \t]*'[^'\n]+':|\Z)", body)
        for pin_match in pin_blocks:
            pin_name, pin_body = pin_match.group(1), pin_match.group(2)
            number = re.search(
                r"PIN_NUMBER\s*=\s*'\(([^)]+)\)'", pin_body)
            if not number:
                continue
            pins[pin_name] = number.group(1)
            use = re.search(r"PINUSE\s*=\s*'([^']+)'", pin_body)
            if use:
                pinuse[pin_name] = use.group(1)

        prim[name] = {
            'part': g('PART_NAME'),
            'jedec': g('JEDEC_TYPE'),
            'value': g('VALUE'),
            'pins': pins,
            'pinuse': pinuse,
        }
    return prim


# --------------------------------------------------------------------------
# pstxprt: refdes -> primitive，以及页号
# --------------------------------------------------------------------------
def parse_pstxprt(text):
    ref2prim = dict(re.findall(r"^ ([A-Za-z0-9_]+) '(.*?)':;", text, re.M))
    ref2page, cur = {}, None
    for line in text.splitlines():
        m = re.match(r"^ ([A-Za-z0-9_]+) '(.*?)':;", line)
        if m:
            cur = m.group(1)
        p = re.search(r"P_PATH='[^']*?:page(\d+)_", line)
        if p and cur:
            ref2page[cur] = int(p.group(1))
    return ref2prim, ref2page


# --------------------------------------------------------------------------
# NC 只在被分隔符界定时才是"不贴"标记，避免 NCP1117 这类型号被误判
_NC_MARK = re.compile(r'(?:^|[/_\-\s])(?:NC|DNP|DNI|DNF)(?:$|[/_\-\s])')


def is_not_populated(prim, value):
    """primitive 名或 VALUE 带 /NC、_NC 等 NC 标记 = 该实例不贴。"""
    return any(_NC_MARK.search((s or '').upper()) for s in (prim, value))


# --------------------------------------------------------------------------
def build(dirpath):
    nets, pinname, pseudo = parse_pstxnet(_read(f'{dirpath}/pstxnet.dat'))
    prim = parse_pstchip(_read(f'{dirpath}/pstchip.dat'))
    ref2prim, ref2page = parse_pstxprt(_read(f'{dirpath}/pstxprt.dat'))

    pin2net = {}
    for n, nds in nets.items():
        for x in nds:
            pin2net[x] = n

    log_path = os.path.join(dirpath, 'netlist.log')
    export_errors = [line.strip() for line in _read(log_path).splitlines()
                     if re.search(r'ERROR\s*\(|Aborting Netlisting', line, re.I)] if os.path.isfile(log_path) else []
    parts = {}
    for r, p in ref2prim.items():
        d = prim.get(p, {})
        parts[r] = {
            'prim': p,
            'part': d.get('part', ''),
            'jedec': d.get('jedec', ''),
            'value': d.get('value', ''),
            # 实例级"不贴"信息只在这里：primitive 名或 VALUE 带 NC 标记
            'nc': is_not_populated(p, d.get('value', '')),
        }

    # Keep the symbol's full declared pin inventory, including unconnected pins.
    # It is independent of pinname read from connected net nodes, not an official pinout.
    declared_pinname, declared_pintype = {}, {}
    for ref, primitive in ref2prim.items():
        spec = prim.get(primitive, {})
        for name, numbers in spec.get('pins', {}).items():
            for number in re.split(r'[\s,]+', numbers.strip()):
                if not number:
                    continue
                node = f'{ref}.{number}'
                declared_pinname[node] = name
                if name in spec.get('pinuse', {}):
                    declared_pintype[node] = spec['pinuse'][name]
    pintype = {}
    for node, pn in pinname.items():
        p = parts.get(node.split('.')[0], {}).get('prim')
        u = prim.get(p, {}).get('pinuse', {}).get(pn) if p else None
        if u:
            pintype[node] = u

    return {
        'nets': nets, 'pin2net': pin2net, 'pinname': pinname,
        'pintype': pintype, 'parts': parts, 'ref2page': ref2page,
        'pseudo_nets': pseudo,
        'declared_pinname': declared_pinname,
        'declared_pintype': declared_pintype,
        'export_errors': export_errors,
        'missing_primitives': sorted({p for p in ref2prim.values() if p not in prim}),
    }


# --------------------------------------------------------------------------
# 自检：宁可报错退出，也不要交出一份静默残缺的索引
# --------------------------------------------------------------------------
def self_check(db, strict=True):
    problems = []
    n_pin, n_name = len(db['pin2net']), len(db['pinname'])
    owners = {}
    for net, nodes in db['nets'].items():
        for node in nodes:
            if node in owners and owners[node] != net:
                problems.append(f'{node} 重复归网: {owners[node]} / {net}')
            owners[node] = net
            if db['pin2net'].get(node) != net:
                problems.append(f'{node} nets/pin2net 不互反')
            if node.split('.')[0] not in db['parts']:
                problems.append(f'{node} 缺少器件实例')
    for node, net in db['pin2net'].items():
        if node not in db['nets'].get(net, []):
            problems.append(f'{node} pin2net 指向不存在的网络成员')
    if db.get('export_errors'):
        problems.append(f"导出日志含错误/中止: {db['export_errors'][:3]}")
    if db.get('missing_primitives'):
        problems.append(f"缺失 primitive: {db['missing_primitives']}")

    if not db['nets']:
        problems.append('nets 为空 —— pstxnet.dat 格式未被识别')
    if not db['parts']:
        problems.append('parts 为空 —— pstxprt.dat 格式未被识别')
    if n_pin and n_name / n_pin < 0.5:
        problems.append(
            f'pinname 覆盖率仅 {n_name}/{n_pin} = {n_name/n_pin:.0%} —— '
            '引脚功能名未被正确提取。Rule-05(电源球无驱动)/Rule-06(VSS未入地) '
            '将静默失效，扫出 0 条并被误读为"全部通过"')
    ic = [r for r in db['parts'] if r[0] in 'UM']
    if ic and not db['ref2page']:
        problems.append('ref2page 为空 —— 页号未提取，发现项将无法定位')

    print(f'  nets={len(db["nets"])}  parts={len(db["parts"])}  '
          f'pin2net={n_pin}  pinname={n_name}  '
          f'pintype={len(db["pintype"])}  pages={len(set(db["ref2page"].values()))}')
    if db['pseudo_nets']:
        for p in db['pseudo_nets']:
            print(f'  [伪网络] {p!r} 挂 {len(db["nets"].get(p, []))} 个引脚 —— '
                  '符合已支持格式的 No-Connect 汇集特征；准出前仍需图面/导出证据核实')

    if problems:
        sys.stdout.flush()
        print('\n[自检失败]', file=sys.stderr)
        for p in problems:
            print(f'  - {p}', file=sys.stderr)
        if strict:
            sys.exit(2)
    return not problems


def main():
    ap = argparse.ArgumentParser(description='解析 allegro 三件套为结构化索引')
    ap.add_argument('dirpath', help='含 pstxnet.dat/pstxprt.dat/pstchip.dat 的目录')
    ap.add_argument('-o', '--out', default='db.json')
    ap.add_argument('--no-strict', action='store_true',
                    help='自检失败时仅告警不退出（不建议）')
    a = ap.parse_args()

    db = build(a.dirpath)
    db['integrity'] = {'self_check_passed': self_check(db, strict=not a.no_strict)}
    json.dump(db, io.open(a.out, 'w', encoding='utf-8'), ensure_ascii=False)
    print(f'  -> {a.out}')


if __name__ == '__main__':
    main()
