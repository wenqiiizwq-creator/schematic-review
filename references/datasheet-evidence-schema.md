# ER1 热跑证据契约（schema_version=1，V2.1 保留电气扩展）

旧 JSON 可读取；缺少新依赖、保证范围或状态时不执行热跑，输出逐项
INSUFFICIENT。不自动填 1% 电阻公差、零 Vref 误差、零偏置电流或稳态采样。
数值必须有限，min≤max；每项 id 唯一、citation 可定位。热跑结果不是整板准出。

## 共有字段与依赖

每条 checks 项包含 id、rule、kind、citation、目标 node/net/ref、depends_on 和 basis。
多个目标坐标必须一致；同网不同引脚不共享门限。depends_on 要列全参数来源，包括
非目标 IC、输入负载、外部驱动及用于保证曲线/额定值的关键无源器件。
脚本至少强制检查目标器件及目标网上的 U/M/Q/D；跨网的额外依赖由 agent 明确声明。

basis 包含：

- db_sha256：当前 db 的 parts/nets/pin2net/pinname/pintype 指纹。
- state：准确的装配版本、供电状态、温度/负载角点及采样情景；同一对象不同状态使用不同检查 id。
- sources：每个依赖位号恰有一个来源绑定；ref、identity、document_model、document_version 与 audit 的 AVAILABLE 物料一致；sha256 绑定实际文档；locator 给章节/页码/表号。
- 每个 source 的 identity_resolution：说明实际订货码、封装/档位、VALUE/PART/PRIM 别名或冲突如何被原始资料解决。文档涵盖多个后缀时要定位订货表对应行，不能只比文档标题。

指纹工具不生成结论，也不证明身份解释、计算或引文真实；agent 必须读原件核实。

```sh
python3 scripts/electrical_contract.py db.json --document /tmp/codex-work/task/datasheets/part.pdf
python3 scripts/audit_datasheets.py db.json --evidence evidence.json \
  --require-ref L1 --require-ref F1 --resolution datasheet-resolution.json \
  --json datasheet-audit.json
python3 scripts/plan_review.py db.json --intent intent.json --evidence evidence.json \
  --datasheet-audit datasheet-audit.json --json review-plan.json
python3 scripts/lint.py db.json --intent intent.json --evidence evidence.json \
  --datasheet-audit datasheet-audit.json --json lint.json
```

audit 按需覆盖依赖位号。NOT_FOUND 必须先记录 LCSC/立创与原厂检索；MISSING/
NEEDS_VERIFICATION/NOT_FOUND 均不能让依赖检查变 READY。无关物料缺资料不阻断
已具备全部依赖的检查；总准出仍需处理所有适用阻断项。

## Rule-08：已建模的反馈设定窗口

```json
{
  "schema_version": 1,
  "checks": [{
    "id": "U1-FB-STATIC",
    "rule": "Rule-08", "kind": "divider", "node": "U1.1", "net": "FB",
    "citation": "REG-X Rev.A section 7.5 and BOM Rev.B R1/R2",
    "depends_on": ["U1"],
    "vref": {"min": 0.792, "typ": 0.8, "max": 0.808},
    "expected": {"min": 2.30, "max": 2.50},
    "divider_model": {
      "source_net": "VOUT", "reference_net": "GND",
      "bias_current_a": {"min": -0.0000001, "max": 0.0000001},
      "ignored_nodes": {"U1.1": "FB input loading included in bias_current_a; REG-X section 7.5"}
    },
    "basis": {
      "db_sha256": "replace-with-current-db-fingerprint",
      "state": "BOM Rev.B; feedback regulating; specified Vin/load/temperature range",
      "sources": [{
        "ref": "U1", "identity": "REG-X",
        "document_model": "REG-X", "document_version": "Rev.A",
        "sha256": "replace-with-verified-document-fingerprint",
        "locator": "section 7.5",
        "identity_resolution": "Verify actual ordering code/package against BOM Rev.B and ordering table"
      }]
    }
  }]
}
```

示例数值为合成输入，指纹和身份文字必须替换为实际证据；不能原样用于设计。
电阻公差从各 VALUE 提取。resistor_tolerance 是有 BOM/采购规格支持时才可显式设置的
统一回退值；不覆盖已写明的单颗公差。未知支路、多参考域、未解析电阻、共享电阻
或递归截断会返回 INSUFFICIENT。ignored_nodes 只允许已说明输入负载的 U/M 和 DC
下已证明可忽略的 C 节点，不支持用该字段绕过 Q/D。给定 reference_net 为计算的零点，
它与实际负载地的偏差另建检查；不把 PGND/AGND 等名称视作同一网。

## Rule-09：无源连接与等效阻值

kind 为 required_pull（direction=up/down）或 required_series；给精确目标 net/node，
用 to 明确另一端。resistance_ohm 可给 min/max。直接连接在同两网间的所有已装配
电阻按并联及单颗公差计算；不再接受“其中一颗符合即可”。其他电阻支路/多源/
未解析公差需节点分析，返回 INSUFFICIENT。

没有 resistance_ohm 时，PASS 仅证明连接存在。该规则不计算 I2C 灌电流、上升时间、
端点电平或掉电能力。用 intent.circuits 的 I2C 域分别建立这些检查；完整电气段不能
漏掉经串阻/电平转换器连接的外部上拉。required_series 的结果也仅指指定两网间
直接电阻网络，不证明它是唯一信号通路或符合布局要求。

## Rule-12 / Rule-16：引脚电压及采样保证

分别用 kind=pin_bias、required_default=high/low/float，或 kind=strap、required=high/low/float。
高电平提供 vih_min_v，低电平提供 vil_max_v；Rule-12 的非 float 项还须给 abs_min_v 和 abs_max_v，核对正负电压额定。
非 float 检查还要 voltage_analysis：

```json
{
  "voltage_v": {"min": 0.30, "max": 0.33},
  "sample_window_s": {"min": 0.001, "max": 0.0011},
  "method": "single-pole RC corner calculation",
  "calculation": "V(t)=Vf+(V0-Vf)*exp(-t/(Rth*C)); list actual corners and artifact location",
  "loading": "List all external branches, internal pulls and leakage guarantees",
  "conditions": "List supply/ramp, temperature, population, initial capacitor voltage and sampling setup/hold"
}
```

此窗口由 agent 用完整电路模型计算/仿真并留档，脚本不求瞬态，只与保证门限比较。
禁止只填稳态分压、典型翻转点或某样品实测值。窗口不满足门限输出 FAIL，措辞是
“保证条件不满足”，不是“每颗必死”。scope 限定本状态和给定额定；其他时段的峰值、
钳位/注入电流、Ioff、时序条件仍要单独核对。

float 必须给精确 node；仅核对没有已装配的外部连接，不推断芯片内部拉阻或电压。
没有外部上拉不能直接证明默认电平错误，可能存在合适的内部拉阻/驱动。

## Rule-14：引脚映射

kind=pin_map、ref 和 expected（引脚号到名称或允许名称数组的映射）。同样需要 basis
及资料审计；连接器可通过 --require-ref 加入。PASS 仅覆盖 expected 列出的引脚，
封装方向、全部引脚覆盖与对端定义需独立复核。

## 输出与迁移

lint.json 的 check_results 每条保留 check_id、review_result、detail/citation；PASS
含 scope/state，计算项保留 calculation。readiness 与 review_result 独立，READY
只表示具备所需证据，求解仍可能因拓扑不支持而 INSUFFICIENT。规则级 hot_pending
是摘要，不能替代逐项结果（同规则可同时存在已执行和未就绪的检查）。
