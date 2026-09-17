# 电路原理图审查方法论

> V2.3，保留文件名兼容已有引用。唯一执行入口为[SKILL.md](../SKILL.md)，避免多份流程漂移。

旧编号L0对应AC0，L1–L7对应ER1–ER7。自动候选、工程判断、覆盖记录与准出独立。

- 资料、需求、页面/器件/物理脚、装配及状态：[覆盖协议](coverage-protocol.md)。
- 计划、冷/热合并、对象/判据及状态：[计划契约](review-plan-schema.md)。
- 身份、官方保证值和完整工况：[证据契约](datasheet-evidence-schema.md)。
- 电源/信号/保护与新电路检查器：[逐域清单](review-checklist.md)、[检查器](checkers.md)。
- 参数模型、容差及修改后复算：[公式与限制](wca-formulas.md)。
- error / warning / suggestion及独立阻断、置信度、HANDOFF：[分级](severity-calibration.md)、[边界](scope-boundary.md)。
- schema 3、新手修改步骤与简短前言：[结果契约](review-results-schema.md)、[改法](remediation-guide.md)、[报告模板](report-template.md)。
- 历史闭环和当前输入复验：[Diff断言](diff-claims-schema.md)、[改版影响](revision-impact-schema.md)。

READY、执行一次、零命中和字段变化都不是电气通过或修复证据。
