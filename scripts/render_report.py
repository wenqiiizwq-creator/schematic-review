#!/usr/bin/env python3
"""Render a validated v3 ledger as concise-opening Markdown and a three-level CSV.
PDF pagination and visual QA remain a separate, required delivery step.
"""
import argparse
import csv
import io
import json
from pathlib import Path
from validate_review import SEVERITIES, validate_review
from electrical_contract import load_json

RANK = {s: i for i, s in enumerate(SEVERITIES)}
STATE = {'OPEN': '尚未关闭', 'FIXED_VERIFIED': '已修复并复验',
         'ACCEPTED': '责任方已接受，技术偏离仍保留', 'RETRACTED': '已依据反证撤回'}
READY = {'READY': '编辑细节已明确，尚不代表实际修改',
         'CONDITIONAL': '前提满足后才能确定修改', 'DESIGN_REQUIRED': '需要重新设计'}
PARAM = {'SELECTED': '已选', 'CANDIDATE': '候选', 'TBD': '待定'}
STAGE = {'NETLIST': '网表回读', 'CALCULATION': '计算', 'BENCH': '实测', 'DOCUMENT': '文档', 'PCB': 'PCB验证'}


def inline(value):
    return str(value).replace('\n', ' ').replace('|', '\\|')


def render(plan, report, db=None, lint_runs=None, *, require_bindings=False,
           old_db=None, old_plan=None, require_revision=False):
    if report.get('schema_version') != 3:
        raise ValueError('新报告只支持v3；旧台账须逐项重评，不能机械改标签。')
    gate = validate_review(plan, report, db, lint_runs, require_actionable=True,
                           require_bindings=require_bindings, old_db=old_db,
                           old_plan=old_plan, require_revision=require_revision)
    if not gate['valid']:
        raise ValueError('台账未通过校验：' + '; '.join(gate['errors']))
    metadata = report.get('report_metadata', {})
    if not isinstance(metadata, dict):
        raise ValueError('report_metadata must be an object')
    title = metadata.get('title', '原理图审查报告')
    baseline = metadata.get('baseline', '输入版本见审查台账；交付前补齐版本摘要。')
    scope = metadata.get('scope_note', '结论只覆盖已记录的输入、工况和原理图范围。')
    if not all(isinstance(x, str) and x.strip() for x in (title, baseline, scope)):
        raise ValueError('报告标题、版本和范围必须为非空文字。')
    if sum(map(len, (title, baseline, scope))) > 900:
        raise ValueError('前置文字过长：请将方法、完整哈希和覆盖清单移至附录。')
    ordered = sorted(report['findings'], key=lambda x: (RANK[x['severity']], x['id']))
    checks = {x['id']: x for x in report['checks']}
    counts = gate['summary']['item_severity_counts']
    release = {'NO_GO': '当前不满足原理图冻结条件', 'GO': '台账未发现开放阻断，仍需工程负责人审阅',
               'CONDITIONAL_GO': '尚有已被责任方接受的偏离，按记录有条件处理'}[gate['release']]
    lines = ['# ' + inline(title), '', inline(baseline), '', release + '。', '', inline(scope), '',
             '| 等级 | 数量 |', '|---|---:|']
    lines += ['| %s | %s |' % (s, counts[s]) for s in SEVERITIES]
    priority = [x for x in ordered if x['severity'] == 'error'][:4]
    if priority:
        lines += ['', '优先处理：' + '；'.join(x['id'] for x in priority) + '，修改后按条目复验。']
    lines += ['', '<!-- pagebreak: PDF详情从第2页开始；前言最多1页 -->', '']

    def para(label, value):
        lines.extend(['', '**' + label + '**', '', str(value)])

    def sources(values):
        for source in values:
            lines.append('- ' + inline(source['source']) + '：' + inline(source['locator']))

    groups = [
        ('1. 已确认问题及详细修改方法', lambda x: x['kind'] == 'DEFECT' and x['severity'] != 'suggestion'),
        ('2. 风险项及验证方法', lambda x: x['kind'] == 'RISK' and x['severity'] != 'suggestion'),
        ('3. 建议项及处理方法', lambda x: x['severity'] == 'suggestion'),
    ]
    rendered = []
    for heading, predicate in groups:
        selected = [x for x in ordered if predicate(x)]
        if not selected:
            continue
        lines.extend(['## ' + heading, ''])
        for item in selected:
            rendered.append(item['id'])
            lines += ['### %s | %s | %s' % (item['severity'], inline(item['id']), inline(item['title'])), '']
            loc = item['location']
            lines += ['位置：PDF ' + '、'.join(loc['pages']) + ' 页；' + ' / '.join(loc['refs']) + '；网络 ' + ' / '.join(loc['nets'])]
            linked = [checks[k] for k in item['check_ids']]
            status = list(dict.fromkeys(STATE[c.get('disposition', 'OPEN')] for c in linked))
            known = {'DEFECT': '已核实偏离，影响限于所述工况', 'RISK': '整体结论尚缺证据，后果未被证实',
                     'IMPROVEMENT': '已知判据已满足后的可选改善'}[item['kind']]
            para('证据与处理状态', known + '；' + '；'.join(status))
            for key, label in [('observed','连接与参数'), ('criterion','设计依据'), ('impact','风险原因与影响'),
                               ('scenario','触发条件'), ('root_cause','问题来源'), ('severity_reason','归类理由')]:
                para(label, item[key])
            missing = list(dict.fromkeys(item.get('missing_inputs', []) + [v for c in linked for v in c.get('missing_inputs', [])]))
            if missing:
                para('待补齐的条件', '\n'.join('- ' + x for x in missing))
            reasons = list(dict.fromkeys(c['blocking_reason'] for c in linked if c.get('blocking_reason')))
            if reasons:
                para('冻结判断依据', '\n'.join('- ' + reason for reason in reasons))
            m = item['remediation']
            para('修改目的与准备度', m['purpose'] + '；' + READY[m['readiness']])
            for pre in m['prerequisites']:
                para('前提：' + pre['input'], pre['reason'] + '\n\n取得方式：' + pre['how_to_obtain'] + '\n\n采用条件：' + pre['acceptance'])
            para('详细修改步骤', item['recommendation'])
            for number, step in enumerate(m['steps'], 1):
                lines.extend(['', '%d. %s：%s → %s。%s' % (number, step['target'], step['before'], step['after'], step['instruction'])])
                for key, action in [('remove_connections','断开'), ('add_connections','连接')]:
                    for edge in step.get(key, []):
                        lines.extend(['', '   ' + action + '：' + edge['from'] + ' → ' + edge['to']])
            for parameter in m['parameters']:
                para('参数：' + parameter['target'] + '（' + PARAM[parameter['status']] + '）', parameter['specification'])
                for key, label in [('needed_input','待补输入'), ('selection_method','选择方法')]:
                    if parameter.get(key):
                        lines.extend(['', label + '：' + parameter[key]])
                lines.append('')
                sources(parameter['basis'])
            para('联动检查', m['impact_review'] + ('\n\n关联条目：' + '、'.join(m['related_findings']) if m['related_findings'] else ''))
            para('复验与完成条件', item['verification'])
            for v in m['verification']:
                lines.extend(['', '- %s：%s；预期：%s' % (STAGE.get(v['stage'], v['stage']), v['method'], v['expected'])])
            for c in linked:
                if c.get('acceptance'):
                    para('责任方接受记录', '；'.join(k + '：' + str(v) for k,v in c['acceptance'].items()))
            para('证据定位', '关联检查：' + '、'.join(item['check_ids']))
            refs = item.get('sources', []) + [e for c in linked for e in c['evidence']]
            unique = {json.dumps(e, sort_keys=True, ensure_ascii=False): e for e in refs}
            lines.append('')
            sources(unique.values())
            for c in linked:
                h = c.get('handoff', {})
                if h.get('required'):
                    para('专业交接', ' / '.join(h['receivers']) + '；约束：' + h['constraint'] + '；验证：' + h['verification'])
                    lines += ['', '接收状态：' + {'OPEN':'待接收','ACCEPTED':'已接收','VERIFIED':'已验证'}[h['state']], '']
                    sources(h.get('evidence', []))
            lines.append('')
    if set(rendered) != {x['id'] for x in ordered} or len(rendered) != len(ordered):
        raise ValueError('报告未能恰好展开每个条目一次。')
    lines += ['## 4. 索引与完整台账', '', '| 等级 | ID | 内容 |', '|---|---|---|']
    lines += ['| %s | %s | %s |' % (x['severity'], inline(x['id']), inline(x['title'])) for x in ordered]
    binding = gate['binding_validation']
    revision = gate['revision_validation']
    lines += ['', '对象与判据绑定：' + (f"已校验 {binding['bound_checks']} 条声明；不代替证据原文复核。"
              if binding['enforced'] else '历史兼容模式，未通过新绑定门。')]
    if revision['enforced']:
        lines += ['', f"改版复验：{revision['required_checks']} 条必需记录已校验；策略 {revision['strategy']}。",
                  '逐项复验方法、当前输入摘要与证据见 review-results.json 的 reverification；记录一致不表示实物验证完成。']
    lines += ['', '完整逐项检查、覆盖、候选处置、版本指纹与独立交接保存在随附review-results.json和review-gate.json；计算与图面证据按条目路径回读。', '',
              '报告及校验通过不表示已经修改电路或完成实物验证。', '']
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['id','severity','title','classification_reason'])
    writer.writerows((x['id'],x['severity'],x['title'],x['severity_reason']) for x in ordered)
    return '\n'.join(lines), output.getvalue(), gate


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('plan'); parser.add_argument('results')
    parser.add_argument('--db'); parser.add_argument('--lint', action='append')
    parser.add_argument('--old-db'); parser.add_argument('--old-plan')
    parser.add_argument('--require-bindings', action='store_true')
    parser.add_argument('--require-revision-impact', action='store_true')
    parser.add_argument('--output', required=True); parser.add_argument('--csv', required=True)
    args = parser.parse_args()
    read = load_json
    try:
        destinations = [Path(args.output).resolve(), Path(args.csv).resolve()]
        inputs = [Path(p).resolve() for p in [args.plan, args.results, args.db,
                  args.old_db, args.old_plan, *(args.lint or [])] if p]
        if len(set(destinations)) != 2 or any(p in inputs for p in destinations):
            raise ValueError('输出路径必须彼此不同，且不能覆盖输入台账。')
        markdown, csv_text, gate = render(read(args.plan), read(args.results), read(args.db) if args.db else None,
                                         [read(p) for p in args.lint] if args.lint else None,
                                         require_bindings=args.require_bindings,
                                         old_db=read(args.old_db) if args.old_db else None,
                                         old_plan=read(args.old_plan) if args.old_plan else None,
                                         require_revision=args.require_revision_impact)
        Path(args.output).write_text(markdown, encoding='utf-8')
        Path(args.csv).write_text(csv_text, encoding='utf-8-sig')
        print(json.dumps({'items':gate['summary']['items'], 'counts':gate['summary']['item_severity_counts']}, ensure_ascii=False))
    except (ValueError, OSError, TypeError, KeyError) as exc:
        parser.exit(2, str(exc) + '\n')


if __name__ == '__main__':
    main()
