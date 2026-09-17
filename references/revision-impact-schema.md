# 改版影响与逐项复验 v1

用途：在结构 Diff 之外，将新旧设计/装配/条件/资料变化关联到本轮检查项。
这是复验范围与证据交接，不是电气求解器，也不生成修复通过、严重度或准出结论。
结构修改断言继续按 [diff-claims-schema.md](diff-claims-schema.md) 执行；断言 PASS 不能
代替电平、时序、额定、保护和上下游影响的复验。

## 输入与操作

每次新生成的计划保存 `dependency_version: 1`、`review_inputs`、`check_dependencies`。
`review_inputs` 包含完整当前 db 的规范化摘要、intent、evidence、datasheet-audit 和本地
文档的实际内容哈希；自身也有 digest。它是项目内的审查记录，不放入公共仓库。
旧版最终计划作为 `--old-plan`；本版冷跑计划才是 `--merge-plan`，二者不能混用。

复审冷/热跑均传同一前版基线，热跑额外传本版证据、资料审计及 merge-plan：

```sh
python3 scripts/lint.py db.json --intent intent.json \
  --review-mode revision --old-db old-db.json --old-plan old-review-plan.json \
  --claims review-claims.json --plan-json review-plan-cold.json --json lint-cold.json

python3 scripts/lint.py db.json --intent intent.json \
  --review-mode revision --old-db old-db.json --old-plan old-review-plan.json \
  --claims review-claims.json --evidence evidence.json --datasheet-audit datasheet-audit.json \
  --merge-plan review-plan-cold.json --plan-json review-plan.json --json lint-hot.json \
  --revision-impact-json revision-impact.json
```

`plan_review.py` 同样支持 `--old-db`、`--old-plan`、`--revision-impact-json`。
提供旧基线且未显式指定模式时使用 revision；显式 first 与旧基线冲突则拒绝。
`--claims` 仍是历史断言材料的准备度输入，不代表已经执行或通过断言。

## 依赖清单

`check_dependencies` 按完整检查 ID 索引，包含：

- `check_digest`：本项定义的规范化摘要，含对象、状态、判据、适用性和 HANDOFF 等。
- `refs/nodes/nets/states`：来自明确对象、物理脚、电路组与声明的依赖。
- `check_ids`：其他检查的明确依赖边；使用完整 ID 传播，环不会导致无限遍历。
- `evidence_ids/sources`：该项热证据 ID 和已登记的本地文档路径。
- `scope`：GLOBAL / PARTIAL / DECLARED；`gaps` 与 `citation` 解释范围。

对象引用和同网变化提供候选关联，不证明完整供电/控制/保护依赖。
默认窄检查为 PARTIAL，覆盖/功能总项为 GLOBAL。只有工程人员核对完整依赖后，
才在 `intent.review_dependencies` 声明已确认范围；自动结果不会自行把 PARTIAL 改成完整。

合成格式示例；两个摘要须分别取当前 `review_inputs.db_digest` 和该项 `check_digest`：

```json
{
  "review_dependencies": {
    "EXACT-CHECK-ID": {
      "complete": true,
      "citation": "本版依赖审查记录 DEP-01；已逐跳核对上下游、返回路径和异常状态",
      "db_digest": "<当前完整db的64位摘要>",
      "check_digest": "<当前检查定义的64位摘要>",
      "refs": ["U1", "R1", "R2"],
      "nodes": ["U1.3", "R1.1", "R1.2"],
      "nets": ["SDA", "VCC_3V3"],
      "states": ["run", "external-on"],
      "check_ids": []
    }
  }
}
```

先生成草案取得准确 ID 与摘要，核对后填声明并重建计划。声明只能追加依赖，不能删掉
自动发现的对象；不存在的引用、未知检查 ID、摘要过期仍留缺口。未匹配的声明 ID 单列
`dependency_unmatched`，不能静默忽略。`complete:false` 可先登记已知依赖。
完整性声明是有出处的工程主张，机器不能证明其语义真实；不能为了缩小复验范围而批量签完整。

## 文件与条件变化

资料审计中 `document.path` 的本地文件自动按实际字节重算哈希，不仅比较文件名/版本号，
也不依赖文件大小或 mtime 缓存。旧计划保留旧哈希，不用今天的文件替换历史记录。
其他作为判据的需求、BOM、模型、库文件或日志可登记为 `intent.review_sources`：

```json
{
  "review_sources": [{
    "id": "REQ-A", "path": "/absolute/project/requirements.pdf",
    "citation": "受控需求 Rev.B section 3", "refs": ["U1"]
  }]
}
```

path 必须为本地绝对路径；不下载 URL，不跟踪未登记的任意外部文件。refs 为空的文件变化
按全局影响处理。此登记只绑定文件内容，不证明型号/条款/工况正确。不可读文件留阻断缺口。
上下游或多个电路共同使用的文档应登记完整引用范围，无法界定就用空 refs 全局处理。
引用字符串没有对应文件或输入内容时，工具不能发现引用背后的文件被替换。

所有实际值变化均保留，包括极小数值差、贴装、物理脚定义、网名/成员和符号声明脚变化。
不按百分比过滤放行，不把 R1 当作 R10，不假设改网名无电气影响。
intent 条件或 evidence/audit 内容变化在 v1 中保守触发全量复验；只记录了 review_mode
切换或更新依赖声明的出处，不将其当成电路参数变化。

## 影响结果与缺口

复审计划增加 `revision_impact_version: 1` 和 `revision_impact`；另存 JSON 与内嵌清单一致。

- `baseline`：旧 db 和旧完整计划的摘要。新旧对象分别使用自己的基线，不按相似 ID 猜配。
- `current`：当前 db、输入快照、检查定义集合摘要；`digest` 绑定完整影响清单。
- `changes`：精确变化记录，含 old/new、定位、关联 refs/nodes/nets 和全局范围标记。
- `entries`：每项的 check_id、required、原因、change_ids、check_digest。
- `strategy=EXACT_DEPENDENCIES`：在已声明范围内关联；`NO_RECORDED_CHANGE` 不等于电气 PASS，
  也不允许脚本自动继承旧结果。
- `strategy=FULL_REVIEW`：旧/新依赖不完整、未知全局变化或基线缺失时，扩大为本轮全部检查。
  不能用候选路径或“未匹配到变化”声明其余电路不受影响。
- `blocking_gaps`：旧基线/快照缺失、文件不可读、未匹配依赖声明等未决项。
  `revision-impact-coverage` 必须保持 INSUFFICIENT；先恢复材料、重建并重审，再清除缺口。

部分依赖本身不永久阻止交付：可执行 FULL_REVIEW，逐项记录本轮复核结果，并由 coverage
项说明实际采用的扩大范围。但这不消除真实缺证，不自动解除任何其他检查或 HANDOFF。
旧检查在新计划消失时新增独立 `revision-removed-check`，保留 prior_check 与旧 ID；人工
核对删除/替代、复发/撤回及连带影响。该历史记录在后续版本继续保留，结果每轮独立填写。

## 结果与最终闸门

结果顶层新增 `revision_impact_version: 1`、`revision_digest`。全部 required 项还需：

```json
{
  "reverification": {
    "revision_digest": "<本轮影响清单digest>",
    "inputs_digest": "<本轮review_inputs.digest>",
    "check_digest": "<本项check_digest>",
    "method": "本轮实际核对/计算的方法、状态与接受条件；未完成则具体记录缺口",
    "evidence": [{"source": "本版计算或核对记录", "locator": "确切章节/行/条件"}]
  }
}
```

PASS/FAIL/NA/INSUFFICIENT 的原有证据规则不变；reverification 不替代 evidence、rationale
或 binding。INSUFFICIENT 记录本轮缺失事实，不伪造已做计算。新版复审强制对象/判据绑定。

```sh
python3 scripts/validate_review.py review-plan.json review-results.json \
  --db db.json --old-db old-db.json --old-plan old-review-plan.json \
  --lint lint-cold.json --lint lint-hot.json \
  --require-bindings --require-actionable --require-revision-impact --json review-gate.json
```

校验从当前 db、保存的输入、仍可读的文件及明确旧基线重建依赖/影响，核对必需项、旧项
消失记录与自动检查；删条目、篡改变化、换基线、旧复验摘要不能靠更新 plan_digest 绕过。
冷跑暂缺、热跑恢复的旧状态检查，其暂缺/恢复处置记录也保留，不能破坏冷/热台账交接。
它验证的是记录一致性，不识别伪造的工程主张；单纯复制所有新摘要不会使旧计算变正确。
输入快照是本轮已声明版本；未提供的新需求/装配不能被工具凭空发现，输入版本仍须人工确认。

旧无标记计划可兼容读取，但 `revision_validation.enforced=false` 不代表新闸门通过。
旧计划用于新复审时若没有历史输入快照，不能按当前文件补造历史。只能用前版真实冻结
资料重建并记录依据，否则按缺失基线处理。首审不额外强制 revision 结果字段。
