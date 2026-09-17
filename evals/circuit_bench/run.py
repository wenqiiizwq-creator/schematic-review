#!/usr/bin/env python3
"""Run frozen circuits against a checker checkout; emit auditable per-case results."""
import argparse
import hashlib
import json
import math
import os
import subprocess
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path

from generate import canonical, digest
from stimulus import prepare

HERE = Path(__file__).resolve().parent
STATUSES = ('PASS', 'FAIL', 'INSUFFICIENT', 'ERROR')


def load_dataset(path):
    manifest = json.loads((path / 'manifest.json').read_text(encoding='utf-8'))
    cases = [json.loads(s) for s in (path / 'cases.jsonl').read_text(encoding='utf-8').splitlines()]
    answers = [json.loads(s) for s in (path / 'answers.jsonl').read_text(encoding='utf-8').splitlines()]
    if digest(cases) != manifest['case_digest'] or digest(answers) != manifest['answer_digest']:
        raise ValueError('Dataset/answer digest mismatch; do not silently relabel an existing benchmark')
    if len(cases) != manifest['case_count'] or len(answers) != len(cases):
        raise ValueError('Dataset is incomplete')
    ids = [c['id'] for c in cases]
    if len(ids) != len(set(ids)) or set(ids) != {a['id'] for a in answers}:
        raise ValueError('Duplicate or unmatched case IDs')
    by_id = {a['id']: a for a in answers}
    for c in cases:
        if by_id[c['id']]['case_sha256'] != digest(c):
            raise ValueError('Answer is bound to another case')
        if manifest['family_splits'].get(c['family']) != c['split']:
            raise ValueError('Topology/source family crosses dataset partitions')
    for name, expected in manifest['generator_sources'].items():
        if name not in ('generate.py', 'oracle.py') or hashlib.sha256((HERE / name).read_bytes()).hexdigest() != expected:
            raise ValueError('Generator/oracle revision changed; create a new versioned dataset')
    return manifest, cases, by_id


def code_hashes(sut):
    files = sorted((sut / 'scripts').glob('*.py'))
    if not files:
        raise ValueError('Checker scripts not found')
    return {str(p.relative_to(sut)): hashlib.sha256(p.read_bytes()).hexdigest() for p in files}


def score_record(case, answer, observed):
    status = observed.get('status', 'ERROR')
    if status not in STATUSES:
        status = 'ERROR'
    numerical_match = None
    if answer['window'] is not None and status in ('PASS', 'FAIL'):
        actual = observed.get('result', {}).get('calculation', {})
        if case['kind'] == 'rc':
            actual = actual.get('voltage_v', {})
        numerical_match = all(isinstance(actual.get(k), (int, float))
                              and math.isfinite(actual[k])
                              and math.isclose(actual[k], answer['window'][k], rel_tol=1e-8, abs_tol=1e-9)
                              for k in ('min', 'max'))
    exact = status == answer['status']
    passed = exact and observed.get('parser_match') is True and numerical_match is not False
    return {'id': case['id'], 'family': case['family'], 'split': case['split'], 'track': case['track'],
            'source': case['source'], 'expected': answer['status'], 'observed': status,
            'passed': passed, 'numerical_match': numerical_match, 'oracle': answer,
            'case_sha256': digest(case), 'raw': observed}


def metrics(records):
    expected = Counter(r['expected'] for r in records)
    confusion = {truth: {pred: sum(r['expected'] == truth and r['observed'] == pred for r in records)
                         for pred in STATUSES} for truth in STATUSES[:3]}
    return {'total': len(records), 'passed': sum(r['passed'] for r in records),
            'status_exact': sum(r['expected'] == r['observed'] for r in records),
            'expected_counts': dict(expected), 'confusion': confusion,
            'false_pass': sum(r['expected'] != 'PASS' and r['observed'] == 'PASS' for r in records),
            'false_alarm': confusion['PASS']['FAIL'],
            'known_defect_deferred': confusion['FAIL']['INSUFFICIENT'],
            'known_good_deferred': confusion['PASS']['INSUFFICIENT'],
            'unknown_overclaimed_fail': confusion['INSUFFICIENT']['FAIL'],
            'numeric_mismatches': sum(r['numerical_match'] is False for r in records),
            'errors': sum(r['observed'] == 'ERROR' for r in records),
            'defect_recall': confusion['FAIL']['FAIL'] / expected['FAIL'] if expected['FAIL'] else None,
            'unknown_handling': confusion['INSUFFICIENT']['INSUFFICIENT'] / expected['INSUFFICIENT']
                                if expected['INSUFFICIENT'] else None}


def acceptance(records):
    core = [r for r in records if r['split'] != 'challenge']
    unsafe = [r['id'] for r in records if r['expected'] != 'PASS' and r['observed'] == 'PASS']
    invalid = [r['id'] for r in records if r['observed'] == 'ERROR' or r['numerical_match'] is False
               or r['raw'].get('parser_match') is not True
               or (r['expected'] == 'INSUFFICIENT' and r['observed'] != 'INSUFFICIENT')]
    return {'declared_scope_pass': bool(core) and all(r['passed'] for r in core) and not unsafe and not invalid,
            'invalid_result_ids': invalid,
            'all_cases_pass': bool(records) and all(r['passed'] for r in records),
            'unsafe_pass_ids': unsafe,
            'challenge_gap_ids': [r['id'] for r in records if r['split'] == 'challenge' and not r['passed']],
            'meaning': 'Tool benchmark only; never a circuit/board release decision'}


def compare(baseline, candidate):
    for key in ('dataset_digest', 'answer_digest', 'protocol_digest'):
        if baseline[key] != candidate[key]:
            return {'comparable': False, 'reason': 'Changed ' + key, 'promote': False}
    old = {r['id']: r for r in baseline['records']}
    new = {r['id']: r for r in candidate['records']}
    if set(old) != set(new):
        return {'comparable': False, 'reason': 'Different evaluated cases', 'promote': False}
    regressed = [i for i in old if old[i]['passed'] and not new[i]['passed']]
    improved = [i for i in old if not old[i]['passed'] and new[i]['passed']]
    return {'comparable': True, 'regressed_ids': regressed, 'improved_ids': improved,
            'promote': bool(improved) and not regressed and candidate['acceptance']['declared_scope_pass'],
            'meaning': 'Eligible for reviewed tool upgrade; no automatic installation or board signoff'}


def render(report):
    m = report['metrics']
    lines = ['# 电路评测基线', '',
             '被测版本：`%s`。评测时间：%s。' % (report['sut_revision'], report['created_utc']), '',
             '共 %d 例，%d 例状态及数值符合预期；错误 PASS %d，误报 %d，运行错误 %d。' %
             (m['total'], m['passed'], m['false_pass'], m['false_alarm'], m['errors']), '',
             '| 分组 | 案例 | 符合预期 | 已知缺陷未定判 | 已知正常未定判 |',
             '|---|---:|---:|---:|---:|']
    for key, value in report['by_family'].items():
        lines.append('| %s | %d | %d | %d | %d |' %
                     (key, value['total'], value['passed'], value['known_defect_deferred'], value['known_good_deferred']))
    lines += ['', '## 范围', '',
              '- 原始合成 PST 三件套经现有解析器解析，并核对独立构造的网络/引脚/贴装索引。',
              '- 分压与上拉：被测脚本自己计算；期望数值来自独立 KCL 求解器。',
              '- RC：评测框架提供采样电压窗口，工具只完成证据就绪与门限判断；不计作自动瞬态分析。',
              '- 合成规格文档验证证据绑定流程，不验证真实 datasheet 的读取或原图视觉理解。',
              '- challenge 为超出现有串并联求解范围的桥式电阻网络。安全返回未定判仍是能力缺口。',
              '- 留出集按电路/来源家族划分，但公开且已经用于本次基线；未来严格盲测需新案例。',
              '- 未包含私有真实项目、完整 Agent 审查、PCB 或实测签署；本结果不表示全板正确率。', '',
              '## 未达到期望的案例', '']
    gaps = [r for r in report['records'] if not r['passed']]
    if not gaps:
        lines.append('本次所测案例均符合预期；仍受上述范围限制。')
    for row in gaps:
        detail = row['raw'].get('result', {}).get('detail') or row['raw'].get('error', '')
        lines.append('- `%s` %s：预期 %s，得到 %s。%s' %
                     (row['id'], row['family'], row['expected'], row['observed'], detail))
    lines += ['', '## 升级门', '',
              '- 既有正常/缺陷/缺证案例不得回归；禁止错误 PASS；数值与状态同时核对。',
              '- 新旧版本须使用同一数据、答案和评测程序指纹；`--baseline` 输出逐案例差异。',
              '- challenge 的进步单列；不能靠删案例、改答案或把所有结果改为 INSUFFICIENT 得分。',
              '- 有效用例增长需要新的数据版本；本基线不自动触发工具修改或发布。', '']
    if report.get('comparison'):
        lines += ['比较结果：`' + canonical(report['comparison']) + '`', '']
    return '\n'.join(lines)


def protocol_hashes():
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(HERE.glob('*.py'))}


def run(args):
    for name in ('sut', 'data', 'out', 'scratch_root', 'baseline'):
        if getattr(args, name, None) is not None:
            setattr(args, name, getattr(args, name).resolve())
    protocol = protocol_hashes()
    manifest, cases, answers = load_dataset(args.data)
    cases = [c for c in cases if args.split == 'all' or c['split'] == args.split]
    if not cases:
        raise ValueError('Empty evaluation selection')
    before = code_hashes(args.sut)
    args.out.mkdir(parents=True, exist_ok=False)
    args.scratch_root.mkdir(parents=True, exist_ok=True)
    records = []
    start = time.monotonic()
    with tempfile.TemporaryDirectory(prefix='circuit-bench-', dir=args.scratch_root) as scratch:
        root = Path(scratch)
        for case in cases:
            work = root / case['id']
            work.mkdir()
            request = work / 'input.json'
            response = work / 'output.json'
            payload = prepare(case)
            request.write_text(canonical(payload) + '\n', encoding='utf-8')
            command = [sys.executable, '-B', str(HERE / 'worker.py'), '--sut', str(args.sut),
                       '--input', str(request), '--output', str(response)]
            try:
                env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1', TMPDIR=str(work))
                result = subprocess.run(command, capture_output=True, text=True, timeout=args.timeout,
                                        cwd=work, env=env)
                observed = json.loads(response.read_text(encoding='utf-8')) if response.exists() else {
                    'status': 'ERROR', 'error': result.stderr[-2000:]}
                if result.returncode and observed.get('status') != 'ERROR':
                    observed = {'status': 'ERROR', 'error': 'Worker returned nonzero despite a status'}
            except (subprocess.TimeoutExpired, ValueError, OSError) as error:
                observed = {'status': 'ERROR', 'error': str(error)}
            observed['stimulus_sha256'] = digest(payload)
            records.append(score_record(case, answers[case['id']], observed))
    if code_hashes(args.sut) != before:
        raise ValueError('Checker changed during the evaluation; discard results')
    if protocol_hashes() != protocol:
        raise ValueError('Evaluation protocol changed during the run; discard results')
    revision = subprocess.run(['git', '-C', str(args.sut), 'rev-parse', 'HEAD'],
                              capture_output=True, text=True).stdout.strip() or 'not-a-git-checkout'
    report = {'schema_version': 1, 'created_utc': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
              'sut_revision': revision, 'sut_source_hashes': before, 'sut_source_digest': digest(before),
              'dataset_digest': manifest['case_digest'], 'answer_digest': manifest['answer_digest'],
              'protocol_digest': digest(protocol), 'protocol_sources': protocol,
              'elapsed_s': round(time.monotonic() - start, 3), 'selection': args.split,
              'metrics': metrics(records), 'acceptance': acceptance(records),
              'by_family': {f: metrics([r for r in records if r['family'] == f])
                            for f in sorted({r['family'] for r in records})},
              'by_track': {t: metrics([r for r in records if r['track'] == t])
                           for t in sorted({r['track'] for r in records})}, 'records': records}
    if args.baseline:
        report['comparison'] = compare(json.loads(args.baseline.read_text(encoding='utf-8')), report)
    (args.out / 'results.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    (args.out / 'report.md').write_text(render(report), encoding='utf-8')
    print(json.dumps({k: report[k] for k in ('metrics', 'acceptance')}, ensure_ascii=False, indent=2))
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--sut', type=Path, default=HERE.parents[1])
    parser.add_argument('--data', type=Path, default=HERE / 'data')
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--split', choices=('dev', 'holdout', 'challenge', 'all'), default='dev')
    parser.add_argument('--scratch-root', type=Path, default=Path('/tmp/codex-work'))
    parser.add_argument('--timeout', type=float, default=15)
    parser.add_argument('--baseline', type=Path)
    parser.add_argument('--require-pass', action='store_true', help='Nonzero if declared-scope gate fails')
    arguments = parser.parse_args()
    try:
        result = run(arguments)
    except (ValueError, OSError) as error:
        parser.exit(2, str(error) + '\n')
    sys.exit(3 if arguments.require_pass and not result['acceptance']['declared_scope_pass'] else 0)
