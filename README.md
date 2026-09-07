# schematic-review

面向硬件原理图首审、冻结前检查和改版复审的 Agent skill。V2.1 将需求、器件/引脚、
链路和工况覆盖，与证据、严重度和新手可执行的修改步骤连成可追溯流程。

它要求 Agent 系统发现已知资料范围内的不合理设计与需求偏离，给出修改建议，并按
**P0 致命 / P1 严重 / P2 一般 / P3 建议**分类。不能保证发现物理电路的所有未知缺陷；
必须公开覆盖缺口，不能以零 Lint 命中代替检查，也不能找到几个问题便结束全板审查。
结论只针对原理图冻结/进入 PCB Layout，不签署板级测试或投产。

## 使用

    按 schematic-review 审查 <项目路径> 的原理图，核对需求和所有适用电路，
    按严重度列出问题、证据和硬件新手能执行的修改步骤：定位到位号/物理脚，
    写明旧→新连接、参数依据和验收；区分可直接修改、条件方案和重新设计，缺证据单列。

先读 [SKILL.md](SKILL.md)。完整模式需原理图 PDF、有效网表、当前装配 BOM/配置、
需求/接口定义及相关器件资料。缺项继续完成可做检查并明确限制；只有 PDF 时可受限读图，
不声称已完成网表全量检查。随附解析器只支持 Cadence/OrCAD 三件套，其他 EDA 需验证适配器。

按所用 Agent 的技能目录安装，例如：

    git clone https://github.com/wenqiiizwq-creator/schematic-review.git <你的skills目录>/schematic-review

Python 脚本仅用标准库；读 PDF 需要现有提取/渲染工具（例如 Poppler）。安装环境依赖前先
盘点现有资源并确认；本仓库不自动安装任何软件。

## 运行

在项目审查目录持久保存输入清单和产物，命令中的 scripts 路径按 skill 位置调整：

    python3 scripts/parse_netlist.py <项目>/allegro -o db.json
    python3 scripts/plan_review.py db.json --intent intent.json --json review-plan.json
    python3 scripts/lint.py db.json --log <项目>/allegro/netlist.log --intent intent.json --json lint-cold.json
    python3 scripts/lint.py db.json --intent intent.json --evidence evidence.json --plan-json review-plan-hot.json --json lint-hot.json
    python3 scripts/validate_review.py review-plan.json review-results.json --db db.json --lint lint-cold.json --lint lint-hot.json --require-actionable --json review-gate.json
    python3 scripts/diff_netlists.py old-db.json db.json --claims review-claims.json --json diff.json --fail-on-open-claims
    python3 -m unittest discover -s scripts/tests -v

`review-results.json` 由 Agent 完成工程审查后按 schema 填写，不能从 Lint 自动造 PASS。
validate_review 退出 0 表示台账有效，不代表板卡可准出；冻结/CI 使用 `--require-release`。

## V2.1 的修改说明

每项建议给目的、定位、前提、顺序操作、修改前后连接、参数状态/来源、联动及明确验收。
新增 [修改说明规范](references/remediation-guide.md) 与结构化 `remediation` 校验，
READY不能含待定参数，换线须列新旧端点，条件方案须说明缺什么和如何确定。
准备度仅描述原理图编辑信息是否齐全，与严重度和整板准出分开。旧v2台账保留兼容校验。

## V2.0 的主要变化

- **覆盖与需求**：逐条 REQ、全物理脚双向差集、装配/状态/链路台账；一个 EN 的证据不再
  让同 IC 另一脚变 READY；热跑输出未覆盖实例。
- **定级**：缺陷、潜在风险、改善分开；不定区不能保证默认态，不能因样机调通降为普通建议；
  未知后果不写成必然损坏。统计唯一缺陷 ID，与 FAIL 行数分开。
- **机械修复**：普通平面网不再误排为 NC 伪网；重复归网/导出错误阻断；完整符号声明脚留存；
  0Ω/磁珠须追到可能电源，不再把跳线/TVS 自身当电源；分压未知支路/截断/共享阻值不产伪解。
- **电气判据**：纠正跨域上拉固定选高电压、检测点必须保护前、固定 ZQ 终接、RC 等于脉宽、
  固定 MLCC 降额和 ADC 量程等于耐压的泛化；建议修改必须复算关联条件。
- **闭环与报告**：最终 JSON 校验覆盖、证据、NA 状态、分级/位置/改法和准出；只证明“改过”
  不能关闭意见；新增同网/已贴 0Ω 连通断言。关键证据随报告持久保存。

旧 evidence/intent/diff 输入保持 schema_version=1（新增字段/断言可选）；最终结果为 v2。
Rule-08 的 WCA 需要显式基准 min/typ/max，旧标称证据保持候选，不能默认零公差通过。
旧报告 A=网表/B=datasheet 的来源用法和 BLOCKER/Warning/Info 需要按 V2 语义重新审核，不能机械替换标签。

## 文件导航

| 入口/文件 | 用途 |
|---|---|
| [coverage-protocol](references/coverage-protocol.md) | 版本、需求/对象/状态覆盖与留档 |
| [severity-calibration](references/severity-calibration.md) | P0–P3、未知后果、证据、准出 |
| [review-checklist](references/review-checklist.md) / [wca-formulas](references/wca-formulas.md) | 逐域检查与计算判据 |
| [review-plan-schema](references/review-plan-schema.md) / [datasheet-evidence-schema](references/datasheet-evidence-schema.md) | 计划与热跑输入 |
| [review-results-schema](references/review-results-schema.md) / [report-template](references/report-template.md) | 最终结果校验与分级交付 |
| [remediation-guide](references/remediation-guide.md) | 面向新手的逐项修改步骤、参数和验收 |
| [netlist-parsing](references/netlist-parsing.md) / [lint-rules](references/lint-rules.md) / [diff-claims-schema](references/diff-claims-schema.md) | 机械脚本边界 |
| [scope-boundary](references/scope-boundary.md) / [methodology](references/methodology-v1.0.md) | 审查边界和方法说明 |
| [合成示例](examples/worked-example-industrial-gateway.md) | 分级、证据和修改建议示范 |
| scripts/tests/ | 脱敏机械回归与结果闸门测试 |

公开仓库只保存合成用例。真实客户原理图、BOM、私有手册和审查过程产物留在各项目目录。

MIT License，见 [LICENSE](LICENSE)。
