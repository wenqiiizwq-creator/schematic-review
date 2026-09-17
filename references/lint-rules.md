# 检查器：共用契约与逐项说明

检查器从现有 `db.json` 发现对象、登记逐状态清单并生成审查计划。它们不是 EDA 解析器、
不是电气求解器，也不产生准出结论。加一个检查器只需新增模块并登记到
`scripts/checkers/__init__.py` 的 `REGISTRY`，计划、lint 与校验都按注册表遍历。

## 共用契约

以下规则对所有检查器一致，各节不再重复。

**识别依据**分三级，随对象一并记录：

| 依据 | 来源 | 可产生 |
|---|---|---|
| `declared` | intent 明确声明并带出处 | 冷跑发现、计划项、热跑 |
| `topology` | 连接关系加引脚角色成立 | 冷跑发现、计划项、热跑 |
| `name-hint` | 只有网名/型号线索 | 计划项（`UNDETERMINED`，需 intent 确认） |

名称、`nc=false`、库名 `Bridged`、焊盘默认图形都不是证据。无法分类的器件记为未知并形成
缺口，不按名称推定不适用。

**装配状态**：`states` 为 1–32 个唯一状态，每项必须有 `id` 与 `citation`。`population` 只接受
JSON `true/false`，缺项表示未知；`jumpers` 按位号填 `closed/open/unknown`，跳线还需
`population=true` 才导通。经核对的状态声明可以覆盖解析器的 `nc` 标记，输出保留原标记，
并在 citation 说明变体依据。未提供 intent 时按网表 `nc` 形成"按图状态"，同时登记
`assembly state unverified` 缺口。

**指纹绑定**：声明段必须带当前输入指纹（`db_sha256`，去耦用覆盖更广的 `input_sha256`），
旧状态声明不得静默用于新网表。取值：`python3 scripts/electrical_contract.py db.json`。

**清单与计划**：`plan_review.py` / `lint.py` 每次重新生成清单并嵌入 `review-plan.json` 的
对应顶层键，不读取旧清单当事实。另存清单：

```sh
python3 scripts/lint.py db.json --checker-json <checker-id>=inventory.json
```

**缺口与结论**：`coverage` 的 `DISCOVERED`/`INCOMPLETE` 都不是电气 PASS；清单里"有这颗器件"
不触发自动通过。缺口必须补齐或保持 `INSUFFICIENT`；带缺口的对象不能写 PASS。电气判据初始
一律 `WAITING_EVIDENCE`，需专家提供适用规格、工况计算与逐项结论。

**热跑**：检查器自带的热跑规则走同一条 `evidence.json` 管线（字段见
[datasheet-evidence-schema.md](datasheet-evidence-schema.md)）。输入只接受带出处的保证值：
缺任一项即 INSUFFICIENT，不用典型值顶替；比值与寿命系数必须来自项目规定。PASS 的 scope
写明未判定的部分，计算角点保留在 calculation。

**校验**：`validate_review.py --db` 重建清单并逐项对比，拒绝被删检查、改写判据、被篡改的
摘要/缺口与过期绑定。人工补查项必须使用独立对象，不得复用生成项的对象。摘要只绑定被审
输入，不验证引用文字真实，也不替代工程审查。

**规则编号**：检查器自带规则用检查器前缀，01–09 为冷跑、10 起为热跑，与既有 `Rule-NN` 并存
且互不改写。

## I²C 连接覆盖（`i2c_topology`）

维护范围：发现 I²C 端点、上拉、串阻路径和边界，生成逐状态审查计划。

不提供配置时按 SDA/SCL 引脚名、网名生成候选，`SCLK` 不独立触发 I²C，未识别的接口不能判 NA。
显式传入的 intent 根值必须是 object，`null`/数组会报输入错误；畸形 db 容器在入口拒绝。

```json
{
  "i2c_topology": {
    "schema_version": 1,
    "db_sha256": "填写当前 db_fingerprint(db) 的 64 位哈希",
    "states": [{"id": "run-option-A", "citation": "装配 BOM Rev.B 选项 A；跳线配置表 Rev.C 第 2 行",
                "population": {"U1": true, "JP1": true}, "jumpers": {"JP1": "closed"}}],
    "buses": [{"id": "CONTROL", "sda": ["U1.5"], "scl": ["U1.6"],
               "citation": "已核对的 U1 物理脚表与原理图页 2"}],
    "components": {"U1": {"kind": "endpoint", "citation": "U1 接口角色核对记录"},
                   "JP1": {"kind": "jumper", "citation": "JP1 两脚跳线定义"}},
    "rails": {"VCC_3V3": "本版电源树节点 A"}
  }
}
```

- `buses`：每组唯一 `id`，`sda/scl` 为非空、去重的物理节点数组并给角色证据。可包含同一逻辑
  总线不同侧的已核对端点；工具不会把隔离器或有源器件两侧短接。不熟悉的引脚命名用显式物理
  节点补齐，直接把不明节点加进数组不能替代查引脚功能。
- `components`：可选类型声明（`resistor/jumper/endpoint/connector/level_shifter/isolator/switch`），
  各带出处。R、JP、J/P/CN 前缀只提供候选类型，其他无源位号需显式声明。导通边只支持恰好两个
  物理脚的电阻/跳线，不支持把多脚排阻或 IC 当两脚桥接。
- `rails`：按网络名声明已核对的供电域及出处，只确认身份，不提供轨压或供电能力证据；轨名推断
  的供电域保留 `rail-identity` 缺口。
- 电平转换器/隔离器/开关可加 `ports`，每对 ports 单独提供 SDA/SCL 种子；端口未映射时记
  `boundary-port-map` 缺口，不猜另一侧引脚。各侧永远单独追踪；内部传输、EN/RESET、方向、掉电
  与漏电行为留给电气检查。

单独生成清单：`python3 scripts/i2c_topology.py db.json --intent intent.json --json i2c-topology.json`。

| 字段 | 含义 |
|---|---|
| `buses` | 声明/端口/名称候选，以及 SDA/SCL 所属连接区域 |
| `regions` | 仅经已确认导通的两脚无源器件可达的网络集合，不跨有源器件 |
| `regions[].edges` | 位号、物理脚、两端网、串阻值/公差、装配与模型出处 |
| `regions[].segments` | 仅由 0Ω/闭合跳线相连的拓扑段；非零串阻两侧仍是不同段 |
| `regions[].pullups` | 每只已确认贴装上拉只出现一次，含供电域、阻值与可复现路径 |
| `path_origin_net` / `pullups[].path` | 路径起点网及依序经过的边位号；不是电压传递模型 |
| `endpoints` / `boundaries` | 物理端点、串联断点、未知器件、有源器件或外接端口 |
| `gaps` | 未核装配/角色/供电域、未映射边界、未知外接模块、输入错误或追踪上限 |

有限串阻允许追踪到远端上拉，但不做理想短接或跨串阻直接并联。断开/不贴的选件不连接两个区域。
连接器后的外部上拉一律未知，需取得模块/线缆网表或另留专家证据。追踪用去重队列处理环路；
每状态最多 1024 个候选网络，达上限记录缺口及 `unvisited_seed_nets/unvisited_frontier_nets`。
`segments` 是连接分组，不证明 0Ω/跳线无压降或无限带宽。

计划：每状态、每连接区域新增 1 项连接覆盖检查及 3 项 I²C 电气判据（灌电流/上升时间、
电压域/掉电、地址/复用/装配状态）。有限串阻各段保留节点做关联分析，有源器件两侧另查传输条件。
不能由"有一只上拉"生成 PASS。本清单与原有 Rule-09 直接连接/直接并联计算并存，不扩大后者模型，
也不自动把清单或 state population 注入热跑；不同装配变体需各自的 db 与绑定该 db/状态的 evidence。

## 去耦覆盖（`decoupling`）

维护范围：逐器件、物理电源脚、直接供电/返回网络、装配状态与电容清单。不是 PDN 求解器，
也不判定去耦是否合格。

不传 intent 时按物理脚名称/类型发现候选，并纳入符号声明但未连的脚；未核器件另列
`unverified_device_refs`，不能因没有常见 VDD/VCC 名称便判不适用。官方脚表中网表完全不存在的
电源脚也会生成候选，不能只检查网表实有脚。

```sh
python3 scripts/decoupling.py db.json --json decoupling-candidates.json
python3 scripts/decoupling.py db.json --intent intent.json --json decoupling-inventory.json
```

`input_sha256` 取候选清单输出中的同名字段，它包含电气指纹及 `declared_pinname/declared_pintype`、
伪网、输入完整性/导出状态；旧的 `db_sha256` 单独不足以绑定符号未连接脚。本模块不修改 db、BOM
或任何电路。

```json
{
  "decoupling": {
    "schema_version": 1,
    "input_sha256": "替换为候选清单的 input_sha256",
    "states": [{"id": "run-option-A", "citation": "装配 BOM Rev.B 选项 A 与运行工况表第 2 行",
                "population": {"U1": true, "C1": true, "C2": false}}],
    "devices": {"U1": {"mpn": "TEST-IC-EXACT", "package": "TEST-PKG-3",
                        "identity_citation": "BOM 订货码/封装与官方订货表对应行核对记录",
                        "citation": "准确型号完整物理脚表 Rev.A 第 3 页", "pinout_complete": true,
                        "pins": {"1": {"name": "VDD", "role": "power"},
                                 "2": {"name": "VSS", "role": "return"},
                                 "3": {"name": "IO", "role": "other"}}}},
    "components": {"C1": {"kind": "capacitor", "citation": "BOM/符号确认 C1 为两脚电容"}},
    "groups": [{"id": "U1-VDD", "ref": "U1", "supply_nodes": ["U1.1"], "return_nodes": ["U1.2"],
                "citation": "准确器件电源分组与返回节点要求 Rev.A 第 8 页",
                "requirements": [{"id": "VDD-CONNECTION", "kind": "connection",
                                  "criterion": "填写本组实际适用的电容接法条款，不用通用每脚一颗规则",
                                  "citation": "官方适用条款版本/页码/表号"}]}]
  }
}
```

- `devices`：准确 MPN/封装、身份解释、完整官方物理脚表及出处。`pins` 含全部物理脚，角色仅
  `power/return/other`，脚号是字符串，支持 BGA/EP。`pinout_complete=false` 保持缺口。这是有出处的
  人工声明，脚本不读 PDF 验证真实性，也不因字段齐全自动判通过。
- 官方脚表与符号/网表脚表做双向差集，各自记录多出的脚；官方 power 脚未入 group 时自动列候选。
  不得为消除缺口把缺失电源脚改成 `other`。
- `groups`：按器件具体条款划分，不要求每个电源脚单独一颗电容。节点必须属于该器件对应官方角色，
  供电脚不能重复分组；每组只支持一个直接供电网和一个明确返回网。跨网分组不合并，保持缺口。
  飞跨/bootstrap/补偿电容按专用条款另审，不从"连到电容"推断属于本组。
- `components`：支持 `capacitor/resistor/ferrite/inductor/jumper/switch/other`，都要有出处。
  C/R/L/FB/JP 前缀仅为候选类型；计入确认容量前需核类型与装配。`other` 是有证据的类别排除，
  不是规避官方电源脚审查的通用开关。
- `requirements`：唯一 `id`、`kind`（`connection/capacitance/rating`）、`criterion`、`citation`，
  每条单独入计划；缺某类时生成对应待核项，不默认 NA。数组对声明的全部 states 适用，状态相关的
  限值必须在条款中写清条件。

| 字段 | 含义 |
|---|---|
| `groups[].capacitors` | 两端直接匹配本组供电/返回网络的电容，含不贴/未知候选 |
| `fitted_capacitors/fitted_count` | 类型、两脚连接与本状态贴装均确认的唯一位号/数量 |
| `known_nominal_subtotal_f` | 已贴电容中可解析标称值的已知小计，可为部分数据 |
| `nominal_total_f` | 仅当连接/覆盖/装配及容量均无缺口时给出，否则 null；不是有效容量 |
| `rejected_capacitors` | 接到供电侧但不匹配本组两端连接的电容 |
| `boundaries` | 供电网边缘的电阻/磁珠/电感/跳线/开关；始终 `crossed=false` |
| `gaps/capacitance_gaps` | 身份/脚表/连接/装配缺口与容量解析缺口分别记录 |

所有串联元件均不跨越，包括 0Ω 和闭合跳线；上游储能电容不计入下游本段。不同地网不因名字相近
而合并。同网共享电容不是每颗 IC 本地去耦充分的证据；各组容量不能相加成整板总量。实际距离、
回路、ESL/PDN/布局由 PCB 阶段验证。

标称值只解析有明确单位的首项（100nF、4.7u/10%/16V、4n7、1e-6F），不猜 104 等裸编码；无法解析的
已贴电容保留数量与缺口，不当作 0，不猜耐压/介质，不自动估算 ESR 或降容系数。

计划：清单完整性项 + 逐状态逐组连接覆盖项，随后分开审查接法、数量/容量与额定值。电气行的
`required_material_refs` 列出目标器件与本组已确认贴装的电容，供 ER1 按需
`audit_datasheets.py --require-ref C1`；参数需绑定准确电容订货码，不能用一般
`datasheets.available` 替代逐料号证据。接法行独立形成 PCB HANDOFF。容量缺口不自动否定另一个
已证实的窄连接结论；不含本功能标记的旧计划兼容读取，但不代表经过新去耦门。

## 感性负载续流与钳位（`inductive_load`）

维护范围：识别继电器线圈、由开关驱动的电感/绕组、经连接器外接的感性负载，登记每个装配状态下
的钳位路径。只回答"有没有、接法对不对、缺什么证据"；额定值是否足够由 ER4 按器件资料判定。

识别与排除：

- 线圈/绕组两端分别为开关节点与电源轨时成立；开关可以是分立 MOSFET/BJT（按引脚角色）或驱动
  芯片输出脚。
- 开关节点上出现 `SW/LX/PH/VSW/BOOT/BST` 类引脚名时判为开关电源储能电感，不属于本检查器。
- 网名含 `MOTOR/SOLENOID/VALVE/COIL/RELAY/PUMP/BRAKE/FAN/ACTUATOR` 且经连接器外接的负载只作
  `name-hint` 候选，计划项为 `UNDETERMINED`，不产生冷跑发现。
- 钳位识别：负载两端的续流二极管（按阳极/阴极角色判方向）、开关两端的 TVS/齐纳、跨负载或跨开关
  的 RC，以及驱动芯片 `COM/CLAMP/VS` 类引脚接电源轨形成的"集成钳位候选"（需资料证据）。
  引脚角色缺失时方向记 `unknown` 并形成缺口，不推定方向。

```json
{
  "inductive_loads": {
    "schema_version": 1,
    "db_sha256": "填写当前 db_fingerprint(db) 的 64 位哈希",
    "states": [{"id": "run", "citation": "装配 BOM Rev.B 选项 A",
                "population": {"K1": true, "D1": true, "Q1": true}}],
    "loads": [{"id": "K1-COIL", "ref": "K1", "kind": "relay",
               "switch_net": "RELAY_DRV", "rail_net": "V24",
               "citation": "继电器线圈参数与驱动方式核对记录"}],
    "exclusions": [{"ref": "L7", "citation": "已另行按变压器条款审查"}]
  }
}
```

冷跑规则：

| 规则 | 触发 |
|---|---|
| `IL-01` | 已识别负载在该状态下没有任何钳位路径（含钳位器件不贴）；驱动器内部钳位需资料证据 |
| `IL-02` | 续流二极管方向接反：阴极在开关节点、阳极在电源轨 |

计划项（逐负载、逐状态）：钳位拓扑与方向（ER3，带 PCB HANDOFF：钳位器件靠近负载与开关、
续流回路面积最小）；钳位额定（ER4）——反向耐压不低于电源最高电压，峰值/重复电流不低于线圈关断
瞬间电流，齐纳/TVS 钳位时开关器件耐压需覆盖电源电压加钳位电压，并核重复频率下的耗散。

依据：`HardwareWiki:wiki/methodology/inductive-load-flyback-clamp-design.md`。

## 功率开关 `power_switch`

维护范围：按引脚角色识别分立 MOSFET/BJT/IGBT，登记栅极驱动源、栅源下拉、开关节点上的
感性元件与吸收网络。引脚角色缺失时只登记缺口（`pin-roles:REF`），不推定拓扑。

- 驱动源：栅极网上的驱动输出脚（引脚名 HO/LO/OUT/DRV/GATE 或引脚类型为输出）、经串联
  电阻/磁珠一跳到达的同类脚、前级开关管漏极、连接器（外部驱动）。
- 高/低边由源极所在网判定：地=低边，电源轨=高边，其余=浮地/半桥。
- 吸收：漏源之间或漏到地的电容、TVS/齐纳/二极管、RC（漏→电阻→中间节点→电容→地/源）。
- 开关节点证据：节点上的电感/变压器/继电器、对管源极、`SW/LX/PH/HS/LS` 类引脚。

intent 段 `power_switches`（`switches[]`: `id/ref/role/gate_net/citation`）只用于声明角色与
装配状态；未声明时按图状态扫描并保留 `assembly state unverified` 缺口。

冷跑：`PS-01` 栅极既无驱动源也无下拉/上拉（上电与驱动高阻时状态不确定）；
`PS-02` 开关节点接感性元件或半桥中点但无吸收/钳位（CANDIDATE）。

热跑 `PS-10`（`gate_drive`）：最小栅源驱动不低于 RDS(on) 保证条件的 VGS，且驱动窗口不超
栅源绝限；P 沟道按量纲翻转后比较。PASS 不覆盖开关速度、米勒导通、SOA 与热。

计划项：栅源驱动（ER4 热跑）、安全工作区与降额（ER4，HANDOFF 给热与版图：结温按实际散热
路径，栅极与换流回路面积最小）、开关节点吸收与驱动器条款（ER3，仅在有开关节点证据时生成，
HANDOFF 给版图）。

依据：`HardwareWiki:concepts/mosfet-igbt-gate-drive-circuit.md`、`concepts/功率器件栅极驱动保护.md`、
`concepts/功率开关安全工作区.md`、`concepts/开关节点振荡吸收.md`。

## 开关电源输入滤波 `input_filter`

维护范围：识别同时具备 `VIN/PVIN` 与 `SW/LX/PH/BOOT` 类引脚的开关稳压器，登记其输入网上的
串联电感/磁珠、两侧对地电容、RC 阻尼支路与体电容候选。线性稳压器不进入本检查器。

- 串联元件只认两端元件；共模扼流等多端器件记 `series-element-topology:REF` 缺口，不猜接法。
- 体电容候选按型号/值中的 ELEC/ALUM/TANT/POLYMER 等线索给出，仅为候选，ESR 仍须资料证据。
- 电容标称值沿用去耦模块的解析规则：只解析带明确单位的首项，裸编码不猜。

冷跑：`IF-01` 输入经串联 L/磁珠滤波但既无 RC 阻尼支路也无体电容候选（CANDIDATE）。

热跑 `IF-10`（`input_filter_damping`）：|R<sub>IN</sub>| = V<sub>IN,min</sub>²/P<sub>IN,max</sub>；
判据为 ESR<sub>bulk,max</sub> < |R<sub>IN</sub>|、ESR<sub>bulk,min</sub> > L<sub>max</sub>/(C<sub>bulk,min</sub>·|R<sub>IN</sub>|)、
C<sub>bulk,min</sub>/C<sub>in,max</sub> ≥ 项目规定比值。**这是一阶判据**：全频输入阻抗裕量、
阶跃响应与温度角仍需仿真或实测，PASS 的 scope 已写明。

计划项：阻尼判据（ER4 热跑）、滤波元件饱和/压降/衰减需求（ER3，HANDOFF 给版图与 EMC）。

依据：`HardwareWiki:methodology/dc-dc-输入滤波稳定性评估.md`、`methodology/输入滤波稳定性与阻尼评估.md`。

## 上电过程 `power_up`

维护范围：识别带使能脚且有输出/开关脚的稳压器，归类使能来源，并登记使能/复位与自身供电轨
同网的负载。时序是否满足需求由 ER3 按需求与器件条款判定。

使能来源形态按连接关系归类，不按名称：`tied-to-input`（与自身输入网同网）、`uvlo-divider`
（到轨与到地各有电阻）、`rc-delay`（到轨电阻加对地电容）、`sequenced`（PG/PGOOD 类输出驱动）、
`controlled`（控制器输出脚）、`pulled`、`unknown`。

识别要求器件同时具备使能脚与输出/开关/反馈脚；反馈脚只用于认出稳压器，不计入输出轨。

冷跑：`PU-01` 器件的使能/复位输入与其自身供电脚同网（稳压器自身的直连由 PU-02 覆盖，不重复
登记）；`PU-02` 稳压器使能直连输入网且无分压/RC（CANDIDATE）；`PU-03` 使能网上既无驱动源也无
分压/RC/上下拉——悬空或来源不明（CANDIDATE；器件内部上/下拉需资料证据）。

热跑 `PU-10`（`dropout`）：V<sub>IN,min</sub> − V<sub>dropout,max</sub>(T<sub>min</sub>, I<sub>max</sub>)
≥ 负载要求的 V<sub>OUT,min</sub>。PASS 不覆盖负载瞬态、启动过程与热关断。

计划项：使能来源与 UVLO/时序（ER3，HANDOFF 给测试：上电/掉电单调性与台阶需实测）、线性轨的
压差（ER4 热跑）、同步变换器的预偏置启动与软启动（ER3，需资料证据）。

依据：`HardwareWiki:methodology/power-rail-startup-review.md`、`concepts/电源上电时序故障诊断.md`、
`concepts/同步降压预偏置启动.md`、`methodology/ldo最坏条件选型验证.md`。

## 监控与看门狗 `supervision`

维护范围：识别监控器/看门狗（需同时具备 WDI/SENSE/MR 类引脚与复位类输出脚），登记喂狗输入
状态、被监测网、复位输出的去向与上拉，以及给 IC 供电的电源轨及其监测覆盖。只有复位输入脚的
主控不是监控器。

- 喂狗输入状态：`tied`（直接坐在电源/地上）、`driven`（有其他 IC/连接器脚驱动）、
  `pulled-only`（仅有上/下拉）、`floating`。
- 轨的监测覆盖分 `sense-pin`、`divider`（经电阻到 sense 网）、`supervisor-supply`（仅供电给监控器，
  是否等于被监测需资料证据）与未监测。

冷跑：`SV-01` 喂狗输入悬空或固定电平（CANDIDATE）；`SV-02` 复位/看门狗输出未到任何复位输入
（网上无其他器件=FINDING，只接到其他脚=CANDIDATE）；`SV-03` 在已识别监控器的前提下列出未被
监测的轨（CANDIDATE）。**没有任何监控器时不报 SV-03**：全板是否需要监控属需求问题，由计划中的
逐电源域覆盖项（ER2）承接。

热跑 `SV-10`（`reset_pulse`）：复位输出最小脉宽不低于目标复位输入要求；`output_type=open_drain`
时还要求该网上存在到电源轨的上拉电阻（由网表核实）。PASS 不覆盖阈值精度、迟滞与喂狗时序。

计划项：复位链逐跳与喂狗策略（ER3）、复位脉宽（ER4 热跑）、逐电源域监控覆盖（ER2，逐状态一项，
未监测的轨写进 `inventory_gaps`）。

依据：`HardwareWiki:methodology/复位时序与看门狗审核.md`、`methodology/multi-rail-brownout-reset-verification.md`。

## 高速差分电平 `diff_levels`

维护范围：按网名成对（`_P/_N`、`_DP/_DN`、`_DP/_DM`、`P/N`、`+/-`）且**两条腿上出现同一个
器件**识别差分对，登记耦合方式、串联耦合电容、端接与偏置、方向。交流耦合的链路按电容两侧
各成一对登记，不把两侧短接成一个对象。

- 电平标准来自 intent 声明（按对象 id 或任一条腿网名落位）或名称/型号线索；只有声明依据能让
  计划项 `APPLICABLE`，名称线索一律 `UNDETERMINED` 并保留 `level-standard:*` 缺口。
- 方向按引脚类型判定；两侧都未知时记 `pair-direction:*` 缺口，相关冷跑规则不触发。

冷跑：`DL-01` 仅对 LVPECL/PECL 类电流型电平成立——交流耦合且发送侧无到地/到轨直流通路
（声明依据=FINDING，名称线索=CANDIDATE）；`DL-02` 交流耦合接收侧既无端接也无偏置（CANDIDATE）。

热跑 `DL-10`（`diff_level`）：直流耦合用发送端共模、交流耦合用偏置后共模，须落在接收端共模
范围内；发送摆幅须落在接收端差分输入范围内。PASS 不覆盖抖动、低频截止、阻抗与回流。

计划项：电平兼容（ER4 热跑）、端接与偏置（ER3，HANDOFF 给版图/SI：差分阻抗、等长、间距、
参考平面连续与端接就近）。

依据：`HardwareWiki:methodology/high-speed-level-interconnect-review.md`、`concepts/高速电平互连.md`。

## 光耦 `optocoupler`

维护范围：识别光耦器件与其 LED 回路（限流电阻、驱动源）和输出侧（集电极网、上拉电阻、发射极
参考）。引脚角色缺失时只登记 `pin-roles:REF` 缺口。隔离耐压、爬电距离与安规等级不在本检查器
定判，走计划项与结构/版图 HANDOFF。

冷跑：`OC-01` LED 两条腿上都没有串联电阻且未声明恒流驱动（intent 中 `drive: constant-current`
并给出处才可免除）；`OC-02` 集电极网上无到电源轨的上拉（CANDIDATE）。

热跑 `OC-10`（`opto_ctr`）：
I<sub>F,min</sub> = (V<sub>drive,min</sub> − V<sub>F,max</sub> − V<sub>drop,max</sub>)/R<sub>LED,max</sub>，
I<sub>C,可用</sub> = I<sub>F,min</sub>·CTR<sub>min</sub>·寿命衰减系数，
需满足 I<sub>C,可用</sub> ≥ (V<sub>pullup,max</sub> − V<sub>OL,要求</sub>)/R<sub>pullup,min</sub>，
同时 I<sub>F,max</sub> 不超额定。寿命衰减系数必须来自项目规定，缺规定即 INSUFFICIENT。
PASS 不覆盖开关速度、温度角与隔离耐压。

计划项：传输能力（ER4 热跑）、隔离归属与耐压（ER3，HANDOFF 给版图与结构）。

依据：`HardwareWiki:concepts/光耦合器隔离传输.md`。
