# 最终审查台账 v2 与机器校验

`review-plan.json` 是待办，不是结果。最终结果另存 `review-results.json`，保证每个计划 ID
恰好一条结果；补查项先加入计划，再记录结果。`validate_review.py` 只验证记录的一致性，
不能验证来源文字是否真实、计算是否合理或审查判据是否穷尽。

## 顶层字段

- `schema_version`: 2。
- `remediation_version`: 新报告必须为1；要求每项finding包含详细`remediation`，字段及
  示例见 [remediation-guide.md](remediation-guide.md)。旧v2报告可不填，仅用于兼容校验。
- `plan_digest` / `db_digest`: Python `validate_review.fingerprint()` 对完整 JSON 对象排序并
  标准序列化后的 SHA-256；不是文件原始字节哈希。输入文件本身哈希另在 input-manifest 中。
- `checks`: 与计划 ID 集合一致的全部最终结果。
- `findings`: 唯一已确认缺陷/改善 ID，一项根因可被多个 checks 引用。
- `scope_checks`: 六个维度各对应一个检查 ID：`input_consistency`、`requirements`、`chains`、
  `states`、`datasheets`、`history`。默认计划已有 `coverage-*` 对应项；代表覆盖审计完成，
  不是该域全部电气通过。完成了有据的缺口登记也可通过“枚举完整性”检查，实际缺证结论仍为 INSUFFICIENT。
- `coverage`: 各维度“对象→检查 ID 数组”的完整映射。
- `summary` / `release`: 可省略，由校验器计算；填了必须与计算一致。

有 `--db` 时机械检查 coverage 的 `components`（所有 parts）、`pins`（pin2net 与
声明脚并集）、`nets`（去除已识别伪网）、`pages`（ref2page 的页号字符串），以及计划
`object.requirement_id` 的 `requirements`。每个对象必须关联至少一条有效检查。
这能挡住漏列对象，但不能防止把一条粗略 PASS 错误挂给很多对象：Agent 仍须提供
逐脚/逐域证据。原理图实际 PDF 页数可能多于 ref2page，额外页在 ER6 台账补齐，不能据脚本
页号集合宣称 PDF 全覆盖。仅 PDF 时不传 --db，coverage 要由页面/手工对象清单另审，
报告声明未获机器网表覆盖，不制作伪网表。

## 单条检查

```json
{
  "id": "ER3.PATH.U1-J1",
  "applicability": "APPLICABLE",
  "review_result": "FAIL",
  "evidence_confidence": "A",
  "evidence": [{"source": "db.json", "locator": "nets.SENSE_A; nets.SENSE_B"}],
  "rationale": "要求的检测端点不连通，见路径表 PATH-01。",
  "severity": "P1",
  "finding_id": "F-001",
  "blocking": true,
  "disposition": "OPEN",
  "handoff": {"required": false}
}
```

枚举定义见 severity-calibration.md。PASS/FAIL 只能 A/B，INSUFFICIENT 必须 C 并给
`missing_inputs` 非空数组与 `potential_severity`；非 FAIL 不填 severity。
所有结果含非空 `rationale` 与可定位 `evidence`（缺失清单本身也是定位证据）。
NA 必须 NOT_APPLICABLE；从自动计划修改适用性需 `applicability_evidence` 同样来源/定位数组。
UNDETERMINED 适用性只能落 INSUFFICIENT。未执行项不可填 PASS，不能用 NA 消失。

`FIXED_VERIFIED` / `RETRACTED` 只用于当前 PASS/NA 并须 `closure_evidence`。
`ACCEPTED` 保留当前 FAIL/INSUFFICIENT，附 `acceptance`：by/date/scope/reason/record
五个非空字符串；这些必须来自实际授权记录，脚本不能验证批准人权限，Agent 不得虚构。
P0 FAIL 即使有接受记录仍不准出。P0/P1 潜在未知默认阻断，不能写 blocking=false 绕过。
必需 handoff 的 receivers（数组）/constraint/verification 不可空；ACCEPTED/VERIFIED
须来源/定位形式的 evidence；OPEN 阻断。取消计划中的必需 handoff 须 handoff_evidence 留据，不能静默改 required=false。

## 发现项

每个 `findings[]` 含 id、kind（DEFECT / IMPROVEMENT）、severity、check_ids，及非空字段：
`title`、`observed`、`criterion`、`impact`、`scenario`、`root_cause`、`recommendation`、
`verification`、`severity_reason`。`location` 含 pages/refs/nets 三个字符串数组。
缺图面或跨文档问题用明确的“未定位：缺某版本 PDF”说明，不编造页码；补定位作为关闭条件。
DEFECT 与 FAIL 检查的 finding_id 双向引用；一个根因只用一个 ID。
IMPROVEMENT 仅 P3，关联的实际判据必须已 PASS；待核事项不能混为改善。

`recommendation`保留为总表摘要；`remediation`是可执行的逐项修改说明，不能相互替代。
包含准备度、前提/取得方法、旧→新操作、连接端点、规格/依据、联动ID及编辑/计算验收。
顶层声明`remediation_version: 1`时自动校验；`--require-actionable`要求声明存在，防止
遗漏整组字段。READY不允许未决输入、候选/TBD参数或待完成设计步骤；它只表示编辑细节齐全。

## 汇总与命令

汇总分别输出检查行数/PASS/FAIL/INSUFFICIENT/NA，以及唯一缺陷数量/P0–P3 和可选改善数。
不把多个 FAIL 行当多个致命项。历史修复/撤回记录放报告历史节，当前 findings 只保留当前项。

    python3 scripts/validate_review.py review-plan.json review-results.json --db db.json --lint lint-cold.json --lint lint-hot.json --require-actionable --json review-gate.json

退出 0 表示**台账格式/一致性有效**，即使板卡结论 NO_GO 也可正常交付报告；2 表示台账有错。
CI/冻结门使用 `--require-release`，NO_GO 也退出 2。GO/CONDITIONAL_GO 仍须工程负责人核实。
空计划、遗漏对象、C 写 PASS、NA 冲突、无位置/改法、虚假汇总和陈旧基线均被拒绝。
`remediation_validation`报告是否启用详细改法校验及三类准备度数量；不计入缺陷严重度，
也不把详细方案当已修复。脚本不能识别填满字段却仍含糊/错误的指令，Agent须逐步核对。

## AC0 候选处置核对

完整网表模式交付前用两次 `--lint` 提供冷/热完整 JSON；`lint_reviews` 数组每条含
`run_digest=fingerprint(lint_json)` 与 `items`（从零开始的 finding 索引字符串→结果检查 ID 数组）。
全部 FINDING/CANDIDATE/INFO 都有处置，不接受只复核前几项。没有热跑判据时保存运行了冷扫描
但 hot_pending 未清的 lint-hot.json，相关缺证项仍为 INSUFFICIENT。
不传 --lint 时校验器无法验证候选覆盖，不能声称通过此闸门。仅 PDF 模式明确 NA 并留依据。
