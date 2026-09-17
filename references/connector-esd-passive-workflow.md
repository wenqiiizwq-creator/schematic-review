# 逐针 ESD 与通用无源网络工作流

仅面向原理图；沿用 error / warning / suggestion、结果 schema 3 和原报告生成器。
本次不扩展完整电源树/负载枚举。ESD 的电气判定仍需 Agent 查官方资料；自动扫描负责逐针覆盖。
无源 PN-10 提供有边界的数值证明，不把单个滤波器 PASS 扩大为整板通过。

## 执行顺序与持久化产物

1. 固定当前 db/BOM/原理图版本，核完整脚表、装配状态及接口/带宽要求。先冷跑，保留没有任何
   保护件、没有识别出 RC/LC、未识别器件及未归属无源件的记录。
2. 导出两张清单；从连接器每个物理脚和全部 R/C/L 出发对账，而不是从已有保护器件或少数
   “看起来像滤波器”的电路出发。清单 `summary` 是计数，不能作为电气结论。
3. 补充有出处的 `intent.connector_esd` / `intent.passive_networks`。先取冷清单的
   `input_sha256`；该指纹还包含符号声明脚、NC/伪网及完整性信息。输入变更须重建，不能改摘要。
4. 审 ESD 电气条款；为每个无源网络确认模型边界、真实源/负载及参数保证范围，再填 PN-10
   evidence 并走现有资料审计和热跑。另保存未建模条件、数值结果及独立验算。
5. 每项结果绑定本次对象、判据、状态和输入。对不满足项展开具体改法，重导网表后复算和复验。
   最终 `review-results.json` 仍是 schema 3；用现有 validate_review / render_report 交付。

```sh
python3 scripts/lint.py db.json --plan-json review-plan-cold.json --json lint-cold.json \
  --checker-json connector_esd=connector-esd-cold.json \
  --checker-json passive_networks=passive-cold.json

python3 scripts/lint.py db.json --intent intent.json --evidence evidence.json \
  --datasheet-audit datasheet-audit.json --merge-plan review-plan-cold.json \
  --plan-json review-plan.json --json lint-hot.json \
  --checker-json connector_esd=connector-esd.json \
  --checker-json passive_networks=passive-networks.json
```

补录 intent 后须先用该 intent 重建冷计划，再合并热计划，保持两轮清单与判据一致；旧候选的
处置记录另存，不能为了合并旧计划而手改清单。证据/状态变动导致新增对象时重新对账覆盖。

## ESD：无保护器件时也必须有记录

`connector_esd` 检查器、冷规则 `ES-01`。原 Rule-10 的“保护器件挂残网”继续保留，二者职责不同。

连接器来源包括 J/P/CN 等位号、明确连接器关键字和人工有据声明；每个候选取网表实有脚、
符号声明脚及官方完整脚表的并集。只存在于官方脚表、没有连网的脚也登记。另存
`unclassified_refs`，须用 BOM、图面和准确型号补核；不宣称启发式发现了所有非标准端口。

```json
{
  "connector_esd": {
    "schema_version": 1,
    "input_sha256": "用本次冷清单的64位指纹替换",
    "discovery_citation": "BOM Rev.B、原理图全部页面与外露端口清单已逐项核对的记录",
    "states": [{"id": "RUN", "citation": "装配配置A/BOM Rev.B", "population": {"J1": true, "D1": true, "U1": true}}],
    "connectors": {
      "J1": {"citation": "准确型号完整官方脚表版本/页码", "exposure": "external", "pinout_complete": true,
        "pins": {
          "1": {"role": "signal", "requirement": "required", "citation": "REQ-ESD-01及接口条款"},
          "2": {"role": "return", "requirement": "exempt", "citation": "本针为已核实泄放返回节点；无需独立信号钳位的依据"}
        }}
    },
    "protectors": {
      "D1": {"citation": "准确保护管型号/封装和pinout", "channels": [
        {"signal_pin": "1", "return_pins": ["2"], "citation": "原厂内部电路图通道1及物理脚对应"}
      ]}
    }
  }
}
```

示意中的 population 必须扩展到本次 db 全部器件；缺装配项仍是未知。配置根段可以不提供，
此时按图扫描并保留状态缺口；不能以 `nc=false` 当成装配证据。

- `exposure` 为 external/internal/unknown；internal 不自动豁免。`pins` 逐针给 signal/power/
  return/shield/nc/unknown 角色及 required/exempt/unknown 判定依据。NC、地网名、电源网名都不
  自动等于豁免。豁免仍有人工检查项，不会自动 NA。
- `protectors.channels` 是逐通道的物理脚映射。多通道阵列不能因为一针接上就算全阵列覆盖；
  内置钳位/非标准位号器件可显式声明。返回脚可不止一个，但必须逐脚核实其内部作用、轨间
  泄放路径及掉电状态；字段齐全只形成拓扑候选，不证明可保护下游。
- 清单逐针列网络、贴装、需求、直接/串联路径、保护脚及返回网、可达端点、排除的不贴件和缺口。
  跨 R/L/磁珠/熔丝/跳线只做有界拓扑追踪，保留每段，绝不短接。共享电源/返回轨止步；最多
  8 跳/128 个网络，达到边界显式留缺口；不跨有源器件猜通路。实际接收端需结合功能逐脚确认。

| coverage | 含义与后续处置 |
|---|---|
| no-protector-found | 仍生成该针的覆盖和电气检查；需求明确时审是否缺少必需防护，需求未知则补需求，不能零命中放行 |
| unmapped-protector-candidate | 有器件线索，但物理通道/返回尚未核实；不得计为已保护 |
| mapped-topology-candidate | 有逐通道连接证据；继续核参数和适用工况，不能视为ESD通过 |
| connector-not-fitted | 接口未贴；核暴露焊盘、变体和实际接入方式，不直接忽略 |
| exemption-needs-review | 有豁免声明；核适用场景和出处后才能关闭 |

逐针电气检查至少包含：实际双向摆幅和容差、VRWM/漏电、正负极性、与指定波形/电流相对应的
VC/动态电阻、下游瞬态承受能力及限流/注入、信号速率/结电容/负载、返回及掉电反灌。
不能把 8/20µs 浪涌钳位数据直接当作 IEC ESD 保证值，不能把 HBM/CDM 器件额定当作系统 ESD。
缺少适用保证时保持 INSUFFICIENT。实际抗扰度由明确测试要求交接，不在本 skill 审 PCB。
这些选型维度可参考 [TI 数据线 ESD 保护应用说明](https://www.ti.com/lit/pdf/slvafr3)，
具体数值必须来自实际器件和项目条款。

## 无源网络：发现、建模、计算、判定、修改、复验

`passive_networks` 检查器、冷规则 `PN-01`、热规则 `PN-10`。

自动发现 RC 低通/高通、LC 低通/高通连接候选；同输出/参考端的并联支路归为一组，避免把每对
R/C 的组合虚构成独立滤波器。多个源端保留为多端口缺口。未匹配 R/C/L，包括不贴、0Ω、
多脚和未知值，进入 unresolved 清单；磁珠、耦合元件和未分类器件另列 special_or_unclassified_refs。
对非通用 AC 用途的器件明确记录去向，例如已有分压、去耦、时序或专用补偿审查项，不能批量排除。

```json
{
  "passive_networks": {
    "schema_version": 1, "input_sha256": "用本次冷清单的64位指纹替换",
    "discovery_citation": "完整R/C/L清单及特殊无源类型的对账记录",
    "states": [{"id": "RUN", "citation": "配置A/BOM Rev.B", "population": {"R1": true, "C1": true, "U1": true, "U2": true}}],
    "networks": [{"id": "FILTER-A", "refs": ["R1", "C1"], "input_net": "VIN_SIG", "output_net": "ADC_IN",
                  "reference_net": "AGND", "citation": "功能方向/原理图定位/全部支路与端口核对记录"}],
    "exclusions": {}
  }
}
```

不同于自动候选，声明模型的参考网可以是任意已核实网络，不局限 GND 名称。`refs` 必须包含
所有参与的真实二端 R/C/L；外部源、接收端及其他边界逐物理脚建模。未建模并联/反馈支路、
未连通或悬浮拓扑、DNP、未知/零元件值、缺公差都不能用二元公式绕过。
`exclusions` 格式为 `{"R9":{"reason":"不进通用AC的原因及另一检查项ID","citation":"依据"}}`，
仍生成处置核对项；同一器件不能同时声明进模型和排除。

PN-10 每状态每模型一条 evidence（顶层仍 `schema_version:1`；这是证据契约，不是结果 schema 3）：

```json
{
  "id": "FILTER-A-RUN", "rule": "PN-10", "kind": "passive_ac", "ref": "R1", "net": "ADC_IN",
  "network_id": "FILTER-A", "inventory_digest": "本次passive清单digest",
  "citation": "适用需求版本/条款及参数计算定位",
  "basis": {"db_sha256": "当前电气指纹", "state": "RUN", "sources": []},
  "model": {
    "input_net": "VIN_SIG", "output_net": "ADC_IN", "reference_net": "AGND",
    "citation": "源/负载/模型参数的定位",
    "validity_citation": "小信号幅度、频带、温度、偏压与所忽略寄生有界且适用的证明",
    "source_resistance_ohm": {"min": 100, "nominal": 100, "max": 100},
    "load_open": false,
    "load_resistance_ohm": {"min": 1000, "nominal": 1000, "max": 1000},
    "load_capacitance_f": {"min": 0, "nominal": 0, "max": 0},
    "components": {
      "R1": {"min": 1000, "nominal": 1000, "max": 1000, "citation": "合成例：精确1kΩ"},
      "C1": {"min": 0.000001, "nominal": 0.000001, "max": 0.000001, "citation": "合成例：精确1µF",
             "series_resistance_ohm": {"min": 0, "nominal": 0, "max": 0}}
    },
    "boundaries": {
      "U1.1": {"role": "source", "citation": "源端等效阻抗/频带保证"},
      "U2.1": {"role": "load", "citation": "负载电阻/输入电容/适用采样状态保证"}
    },
    "frequency_hz": [10, 100, 1000, 10000],
    "metric_requirements": {"cutoff_hz": {"min": 280, "max": 330}},
    "gain_requirements": [{"frequency_hz": 1000, "min": 0, "max": 0.2}]
  }
}
```

这是理想元件的合成输入示意，`sources:[]` 和占位指纹不能热跑通过。按原 evidence 契约为 R/C/L、
源/负载/边界器件逐一补真实资料绑定，状态必须精确匹配清单。脚本自动把这些依赖纳入审计；
不能只审被测 R1 而遗漏电容、接收端或第二级支路。

- 数值全部用 SI；R 用 Ω，C 用 F，L 用 H，频率用 Hz。每项要求 `min/nominal/max`，必须有限且
  有出处；电容用该偏压/温度/频率下的有效值范围，电感用该工况有效电感与 DCR。
  默认以 `nominal` 对照 BOM；若有效中心因偏压/温度改变，另填 `bom_nominal` 对照实际 BOM，
  `min/nominal/max` 用有出处的工况有效值。例如 BOM 10µF 而适用偏压下有效4–6µF，可填
  `bom_nominal:0.00001,min:0.000004,nominal:0.000005,max:0.000006` 并给型号、条件和曲线/保证依据。
  不能只修改数值而没有工况证据。参数区间按可独立取值建模；相关参数需另做相关性模型，
  典型曲线不能冒充保证界限。
- 源模型为 Thevenin 电压源及纯电阻范围；0Ω 理想源须显式声明并有适用依据。跨越0与非0的
  源阻抗范围要求分模型。`load_open=true` 只表示电阻支路开路；负载电容仍必须显式给出。
  未知源/未知外接负载不能默认0Ω/无穷大/0F。
- C 的 ESR 与 L 的 DCR 都显式给范围；没有参数时待核，不能由默认 R5%/C10%/L20% 代填。
  `boundaries` 必须精确覆盖模型非参考网上、模型以外的已贴物理节点；source/load 分别只可
  位于输入/输出端。`open` 需证明该频带内可忽略，不能只写“高阻”。实际 R/C/L 支路不能用
  open/load 标签删掉。多源、频变有源输出阻抗、非线性、耦合、负阻抗另建适用模型并人工验算。

### 数值范围与输出

| 模型 | 自动输出/界限 |
|---|---|
| 隔离的 RC 低通 | `dc_gain`、`cutoff_hz`、`time_constant_s`，含 Rs、RL 和并联负载电容 |
| 隔离的 RC 高通 | `dc_gain`、`high_frequency_gain`、`cutoff_hz`、`time_constant_s`；非零负载电容用通用频点分析 |
| 隔离的 LC 低通 | 理想 `1/(2π√LC)`、有负载自然频率、Q、阻尼比、相对DC的−3dB截止、峰值频率与增益；含Rs/DCR/RL/负载电容 |
| 一般二端元件 RLC 网络（含多级RC、LC高通） | 复数节点法给指定频点的标称增益/相位和参数区间的增益包络；保留全部级间负载 |

闭式 RC/LC 指标要求所用 C 的 ESR 明确为0的适用模型；非零 ESR 仍进入通用 AC 求解，不能
悄悄忽略。LC 的自然频率、响应峰值与−3dB截止分别计算；阻尼不足/跨零、特殊高阶拓扑没有
闭式证明时保留缺口。全频带峰值、任意拓扑截止点和启动/瞬态并未由稀疏采样证明。

一般网络限制为最多12个未知节点、24个元件、256个显式频点；超限、不支持、矩阵奇异或区间
过宽，返回 INSUFFICIENT，不截断网络后判通过。增益界使用带向外舍入的实/复区间运算及
节点方程消元；参数相关性会导致保守过宽，不以仅枚举容差角点冒充连续区间保证。

`metric_requirements` 键为上表的数值指标，`gain_requirements` 用线性电压比而非 dB，并且
频率必须在 frequency_hz 中。至少有一条量化要求。没有对应指标、证据缺失或包络与门限
重叠不能证明时 INSUFFICIENT；保证包络全部落在要求内才支持该窄判据 PASS。
包络完全在要求外，或存在已计算的标称反例，返回 FAIL。FAIL 与总体 error/warning 的分级仍
由具体电气影响决定。诊断频点成功不替代额定、模型有效性和应用时序的人工检查项。

可手算交叉核对：低通 `R=1kΩ,C=1µF,Rs=100Ω,RL=1kΩ,CL=0` 得
`Gdc=1000/2100≈0.47619`，`fc=1/[2π·(1100||1000)·1µF]≈303.84Hz`；
理想源空载的159.15Hz不适用于该加载模型。多级网络不能直接相乘各级空载传递函数。
采样 ADC 的动态输入还需核建立时间和采样条件，参见
[ADI 前端与 RC 滤波设计说明](https://www.analog.com/en/resources/analog-dialogue/articles/front-end-amp-and-rc-filter-design.html)。

## 面向新手的修改说明与复验

每个确认问题或未决风险仍填写现有 remediation 全字段，至少让读者能完成以下动作：

1. **定位与准备**：写明页码、连接器/IC物理脚、真实网名、R/C/L/保护器件位号、当前数值、
   适用装配状态；指出必须先取得的官方脚表、ESD等级、信号摆幅/速率、源阻抗和负载资料。
2. **旧→新连接**：保护件逐通道写“哪个物理脚接哪一网、哪个脚接已确认的返回网”；分流保护
   不得泛写“串入TVS”。若采用串联元件，写清断开哪条旧直连、两端分别连接何网；新位号须先查重。
   未选定器件时写功能端及选型后核实体脚的步骤，不编造脚号/封装。
3. **参数选择**：ESD按摆幅→VRWM/漏电→适用VC与下游承受能力→结电容/负载→波形/能量与返回
   顺序列约束及出处。无源问题先指出失败指标，再反算 R/C/L 可行区间，考虑容差/有效值/加载，
   选择实际可采购值后复算；缺参数给条件区间和获取方法，不给貌似精确的唯一答案。
4. **副作用**：修改R/C同时复算幅度、截止、建立时间/复位脉宽、ADC采样、电容充电负担与功耗；
   修改LC同时复算Q/峰值/阻尼、纹波和饱和；增加ESD同时复算信号加载和掉电泄漏/注入。
5. **复验标准**：重导网表确认旧直连已断、各通道脚/返回正确、DNP一致；重跑逐针清单、PN-10
   和相关原检查。写出应达到的具体数值窗口、状态及测试波形。保存前后结果和未决项，
   不以“重新ERC无报错”关闭电气缺陷。

没有保护器件且已有具体暴露风险、但验收条件尚缺时，记录 warning/INSUFFICIENT 及获取需求步骤；
只有用途/物料资料不明确时可为 suggestion/INSUFFICIENT，关键缺口仍单独标记blocking；
已核实必需防护缺失且影响主要功能/安全边界时可 error/FAIL；经证实不需要额外保护的针保留
豁免证据。禁止一律把所有信号针判error，也禁止用整组“有ESD”覆盖未保护的某一针。

实现借鉴 kicad-happy 的按连接器覆盖及无源识别思路，采用本仓库网表、证据和校验框架独立实现；
没有复制其 PCB 审查或默认公差。参照版本与原对比记录见 [移植边界](upstream-integration.md)。
