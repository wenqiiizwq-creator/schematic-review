# AC0 适用性发现与逐项执行计划

`scripts/plan_review.py` 在第一次 AC0 扫描时把“可能要查什么”变成可追踪的
逐项计划。它只决定适用性、准备度和执行阶段，不提前制造 PASS/FAIL。

## 输入：intent.json

最小示例：

```json
{
  "schema_version": 1,
  "review_mode": "first",
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

## 运行

```bash
python3 scripts/plan_review.py db.json \
  --intent intent.json \
  --evidence evidence.json \
  --datasheet-audit datasheet-audit.json \
  --json review-plan.json
```

也可由 lint 同步生成：

```bash
python3 scripts/lint.py db.json \
  --intent intent.json \
  --evidence evidence.json \
  --datasheet-audit datasheet-audit.json \
  --plan-json review-plan.json \
  --json lint-result.json
```

复审时增加 `--review-mode revision --old-db old-db.json --claims claims.json`。

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

## 结果与 HANDOFF

结果状态只有四种：

- `PASS`：本项在原理图范围内有充分证据且满足判据。
- `FAIL`：本项在原理图范围内有充分证据且不满足判据。
- `INSUFFICIENT`：本项适用，但材料不足，无法定判。
- `NA`：有依据证明本项不适用。

`HANDOFF` 不是第五种结果。它记录原理图审查产生的下游约束，生命周期为
`OPEN`、`ACCEPTED`、`VERIFIED`。例如差分链路的原理图连通性可以 `PASS`，同时
其阻抗、等长与回流约束仍保持 `handoff.state=OPEN`，交 PCB Layout 验证。

## 汇总准出

先完成每个适用检查项的独立记录，再计算总体准出。总体准出至少要求：

- 不存在未关闭的阻断级 `FAIL`；
- 不存在阻断级 `INSUFFICIENT`；
- 所有适用项均已执行或有书面接受；
- 所有必需 handoff 已形成明确的接收方、约束和验证方法；
- 复审时 Rule-17 的 Diff 与历史断言通过。

## V2 补充

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
则 WAITING_EVIDENCE。每条匹配 evidence 生成独立 evidence_check_id 和 object.state；
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
CAN_RS485、CLOCK。每个具体电路、状态和判据形成独立检查，按 refs 获取各自资料，
由 Expert Review 填公式、角点、证据、结果、意见和复验方法。无关器件缺资料不
改变已声明电路的准备度。新增 refs 中需要曲线/额定的无源器件用 audit 的
--require-ref 加入；不得用全局“datasheets available”掩盖逐物料缺口。

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
