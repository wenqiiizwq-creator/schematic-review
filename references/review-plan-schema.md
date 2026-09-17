# AC0 适用性发现与逐项执行计划

`scripts/plan_review.py` 在第一次 AC0 扫描时把“可能要查什么”变成可追踪的
逐项计划。它只决定适用性、准备度和执行阶段，不提前制造 PASS/FAIL。

## 输入：intent.json

当前审查的合成输入示例（条件仅用于说明结构，不能移作真实设计依据）：

```json
{
  "schema_version": 1,
  "review_mode": "first",
  "requirements": [{
    "id": "REQ-USB", "text": "提供一路 USB 设备接口",
    "citation": "Requirements v1.2 section 4.1",
    "criterion": "PHY 与连接器双向链路、供电及外部带电状态满足接口条件"
  }],
  "circuits": [{
    "id": "USB-PORT", "domain": "USB_C",
    "refs": ["U1", "J1"], "nets": ["USB_DP", "USB_DM", "VBUS"],
    "states": ["startup", "run", "external-power-only"],
    "citation": "Requirements v1.2 section 4.1; schematic page 2"
  }],
  "expect": {
    "USB_PHY": 1
  },
  "features": {
    "USB": {
      "applicability": "APPLICABLE",
      "citation": "Requirements v1.2 section 4.1"
    },
    "DDR": {
      "applicability": "NOT_APPLICABLE",
      "citation": "Requirements v1.2 section 2.3"
    }
  },
  "materials": {
    "requirements": {
      "available": true,
      "citation": "Requirements v1.2"
    },
    "schematic_pdf": {
      "available": true,
      "citation": "SCH-100 rev.B.pdf"
    },
    "datasheets": {
      "available": true,
      "citation": "datasheet-manifest.json 2026-08-31"
    },
    "platform_checklist": {
      "available": false
    }
  }
}
```

约束：

- `review_mode` 只能是 `first` 或 `revision`。
- `features.<name>.applicability` 只能是 `APPLICABLE`、
  `NOT_APPLICABLE`、`UNDETERMINED`。
- 声明 `APPLICABLE` 或 `NOT_APPLICABLE` 必须同时给 `citation`。
- `materials.<name>.available=true` 必须同时给 `citation`。
- 仅从网表“没搜到某关键字”不能推出 `NOT_APPLICABLE`；没有设计意图证据时为
  `UNDETERMINED`。
- 旧版仅有 `expect` 的 intent 仍可使用。

## 计划与结果交接

常规冷/热命令只在 [SKILL.md](../SKILL.md) 维护；单独生成计划时仍可用
`plan_review.py db.json --intent intent.json --json review-plan.json`。

- 冷跑通过 `lint --plan-json review-plan-cold.json` 同时保存计划和候选，人工补查项加入该计划。
- 热跑通过 `--merge-plan review-plan-cold.json --plan-json review-plan.json` 产生最终计划。
  合并要求 `db_sha256` 与当前电气输入一致，保留已有检查及人工补查项；相同 ID 的对象/判据或移交明细
  不一致时拒绝自动覆盖，需明确处理冲突。旧版无指纹或输入已变时重建计划并逐项迁移补查项。
- 匹配热证据后保留基础覆盖项，按 evidence ID 生成带 `parent_check_id` 的状态子项。
  基础项只汇总覆盖，不能代替任何状态子项的结果；显式证据即使未命中命名启发式也会入计划。
- 最终结果只绑定最终计划；后续补查先更新该计划，再更新 digest 和逐项结果。计划合并不读取
  或迁移 review-results，已完成结果按当前判据重新核验，不能按基础 ID 自动复制给子项。
- 冷/热 Lint JSON 自带当次计划快照；最终校验要求这些检查全部出现在最终计划，热候选必须
  关联自己的状态子项。保存历史快照，不用最终计划覆盖它们。

复审时增加 `--review-mode revision --old-db old-db.json --old-plan old-review-plan.json --claims claims.json`。
新计划保存逐项依赖和当轮输入快照；变化关联、部分依赖扩大复验、历史消失项及结果绑定见
[revision-impact-schema.md](revision-impact-schema.md)。旧文件“可用”的布尔值不代表已形成有效影响基线。

## 输出字段

每个 `checks[]` 都是一个独立审查记录：

| 字段 | 含义 |
|---|---|
| `id` | 稳定的逐项检查 ID |
| `object` | 该项对应的 feature、网络、位号、引脚或页码 |
| `criterion` | 本项通过/失败的判据 |
| `applicability` | `APPLICABLE` / `NOT_APPLICABLE` / `UNDETERMINED` |
| `readiness` | `READY` / `WAITING_EVIDENCE` / `NOT_SCHEDULED` |
| `stage`、`executor` | 应在哪个 AC0/ER 阶段、由谁执行 |
| `required_inputs` | 当前缺失的输入；补齐后再定判 |
| `trigger` | 为什么实例化该检查项的可复现依据 |
| `review_result` | 最终仅允许 `PASS` / `FAIL` / `INSUFFICIENT` / `NA` |
| `evidence_confidence` | 独立证据置信度 A/B/C，不是审查结果 |
| `handoff` | 独立下游动作，可与任一结果并存（例如原理图不适用但需下游执行） |

`rule_plan[]` 给出 Rule-01～Rule-20 的规则级适用性和准备度；`checks[]` 再把
规则或专家检查实例化到具体对象。两者分别回答“这类规则要不要跑”和“具体要审哪一项”。
`aggregate_release_gate` 只声明逐项完成后的聚合门槛，不会用总体结论覆盖任何一条
独立审查意见。

## 判定规则

- `APPLICABLE + READY`：本轮应执行。
- `APPLICABLE + WAITING_EVIDENCE`：检查必须执行，但暂时缺材料；最终不能写
  PASS，未补齐时写 `INSUFFICIENT`。
- `UNDETERMINED`：先补设计意图或解决意图/网表冲突；不得偷换成 `NA`。
- `NOT_APPLICABLE`：必须有范围依据，`review_result=NA`。
- 明确要求某功能但网表未发现特征时，计划新增
  `required-feature-presence` 检查，并报告 `REQUIRED_FEATURE_NOT_DETECTED`。

## 结果、移交与准出

计划仅记录适用性和准备度；最终状态字段见 [review-results-schema.md](review-results-schema.md)，
严重度和准出政策统一见 [severity-calibration.md](severity-calibration.md)，专业边界见
[scope-boundary.md](scope-boundary.md)。READY 不代表 PASS；HANDOFF 独立于审查结果。

## 需求与物理引脚覆盖

`intent.requirements` 可选数组，每项必须含唯一 `id`、`text`、`citation`、`criterion`。
例如 `{"id":"REQ-01","text":"两个用户接口","citation":"需求 A §3","criterion":"两路完整链路到连接器"}`。
计划逐条实例化；还增加 input_consistency/requirements/chains/states/datasheets/history 六类
覆盖审计项，以及关键器件完整物理脚差集项。详细原理图检查仍须 Agent 补齐。
READINESS 只是“可开始该步骤”，不能由全局 datasheets.available 证明每颗器件的所有条款已齐。
ER1 完整物理脚审计须有准确型号/封装资料；同一 IC 的一个引脚证据不能把另一个引脚标 READY。

原始计划允许 review_result=null；最终结果不可为 null，另存 review-results.json 并运行
validate_review.py，见 review-results-schema.md。热跑新计划不能覆盖人工已完成结果。
最终适用性变更需 applicability_evidence 留痕，不保留 APPLICABLE + NA 的矛盾组合。

## 电气扩展：电路、状态及证据依赖

hot 检查的 READY 必须通过与 lint 相同的依赖函数：当前网表指纹、实际文档指纹、
准确型号/版本/定位、依赖物料 AVAILABLE 以及该规则必要的模型输入。缺失或过期
则 WAITING_EVIDENCE。每条匹配 evidence 生成独立 evidence_check_id、parent_check_id 和 object.state；
匹配时 node/net/ref 必须一致。READY 不证明求解器支持该拓扑，也不代表 PASS。

功能 feature 项是 coverage_parent，只汇总覆盖。agent 应从实际电路和需求填
intent.circuits，再按域内判据及状态展开。此扩展不声称自动识别任意电路：未声明的
域不能据此判 NA，需检查覆盖遗漏。示例：

```json
{
  "circuits": [{
    "id": "INPUT-PROTECTION", "domain": "POWER_PROTECTION",
    "refs": ["U1", "Q1", "D1", "F1"], "nets": ["VIN", "VOUT"],
    "states": ["cold-start", "hot-plug", "output-short-retry"],
    "citation": "Requirements Rev.B section 3; schematic page 2"
  }]
}
```

支持域：POWER_CONVERTER、POWER_PROTECTION、ANALOG、I2C、STARTUP、DDR、USB_C、
CAN_RS485、CLOCK，以及各检查器自带的 INDUCTIVE_LOAD、POWER_SWITCH、INPUT_FILTER、
POWER_UP、SUPERVISION、DIFF_LEVELS、OPTOCOUPLER。每个具体电路、状态和判据形成独立
检查，按 refs 获取各自资料，
由 Expert Review 填公式、角点、证据、结果、意见和复验方法。无关器件缺资料不
改变已声明电路的准备度。新增 refs 中需要曲线/额定的无源器件用 audit 的
--require-ref 加入；不得用全局“datasheets available”掩盖逐物料缺口。

I²C 另有有界的自动连接覆盖，输入 `intent.i2c_topology`、清单及状态迁移契约见
[checkers.md](checkers.md) 的 I²C 一节。每连接区域新增覆盖和独立电气判据；
不替代显式 `circuits` 或 Rule-09。计划中的清单需随 `--db` 重新校验，不能编辑清单消除缺口。

去耦另有 `intent.decoupling` 与 `review-plan.decoupling`，完整输入和判定边界见
[checkers.md](checkers.md) 的去耦一节。物理脚/装配/分组覆盖与电气判据分开，
包含零电容、共享位号、未解析容量及直接网络边界；不从清单自动生成 PASS。
新计划带 `decoupling_version: 1`，最终校验重建清单、对象和逐条要求，变更后须重新审查。

其余检查器同一套结构：清单写在同名顶层键并带 `<id>_version`，计划项的 `object` 带
`<id>` 与 `<id>_digest` 绑定清单摘要，`inventory_gaps` 保留该对象的缺口——有缺口就不能判
PASS。检查器自带规则（IL/PS/IF/PU/SV/DL/OC 前缀）与 `Rule-NN` 并列进 `rule_plan`，热跑规则
按证据实例化。逐检查器的识别范围、intent 段与边界见 [checkers.md](checkers.md)。

电源来源与条件导通另可声明：

```json
{
  "active_state": "external-on",
  "power_sources": [{"node": "J1.1", "states": ["external-on"], "citation": "Input specification section 2"}],
  "power_paths": [{"ref": "Q1", "from": "VIN", "to": "VOUT", "state": "external-on", "citation": "Gate-state and body-diode analysis Q1"}]
}
```

sources 表示该状态的供入节点；paths 表示该状态可导通的方向，不是理想短路、
额定载流或无压降声明。不得把未上电稳压器或端口任意标成 source 来消除告警。


### 电源预算输入

power-rail-budget 不再因 materials.datasheets/requirements 标为 available 就 READY。
需按轨填写 power_rails，给电压/最大负载、源端最小可供电流、条件和相关物料，例：

```json
{
  "power_rails": {
    "VCC_3V3": {
      "voltage_v": {"min": 3.2, "max": 3.4},
      "load_a": {"min": 0, "max": 0.5}, "available_a_min": 1.0,
      "refs": ["U1", "L1", "F1"], "state": "BOM B; stated Vin/load/temperature corners",
      "citation": "Load budget Rev.B table 2 and component guaranteed ratings"
    }
  }
}
```

数值是格式示例。可供电流要取限流最小值、温度降额、电感、连接路径等条件下的
最弱保证能力；READY 仍需专家比较功率、瞬态及余量，不自动转为 PASS。

逐物料审计通过 `--datasheet-audit` 传入时优先于全局材料布尔值。
关键器件完整 pinout 检查仍需逐脚证据；AVAILABLE 只证明资料身份已核实。
