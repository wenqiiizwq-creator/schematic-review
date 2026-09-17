# 检查器框架、首批检查器与 KiCad 输入：设计

> 上游历史设计记录，保留其当时的阶段名、测试数和发布状态；不作为本地执行指令。
> 当前本地契约以SKILL.md及references为准：V2.2、schema 3、error / warning / suggestion。
> 2026-09-17已从公开上游a5371bf移植；本地验证结果另存交付目录。

- 日期：2026-09-15；基线提交 `c30f5ca`（327 项测试全绿）
- 需求来源：用户要求结合 HardwareWiki 检查方法与 kicad-happy 检查器优点，让审查更全面、更灵活、更准确，并减少冗余；已确认方案 A、顺序 SP1→SP2→SP3，其余决策由实现方自主完成（见“决策记录”）。
- 依据：HardwareWiki 检索（方法页为主）、kicad-happy 2.2.1 代码与文档核验、本仓库现状度量。

## 1. 目标与范围

| 维度 | 本轮目标 | 不在本轮 |
|---|---|---|
| 更全面 | 补 7 类缺口：感性负载续流钳位、功率开关栅驱/SOA/开关节点吸收、DC-DC 输入滤波负阻抗、上电过程、看门狗与逐电源域监控、高速差分电平、光耦 CTR | P2/P3 其余建议项 |
| 更灵活 | KiCad 网表输入，产出同一 `db.json` 契约 | Altium、审查深度分级、项目规则配置 |
| 更准确 | 按连接关系识别对象（识别依据分级）；新增有界热跑计算器，只用已核实保证值 | 引用原文机械校验、datasheet 自动提取 |
| 少冗余 | 模块接入收敛到注册表；两份模块 schema 文档合并为一份检查器参考，共用纪律只写一次 | 冷跑/热跑流程本身的简化 |

## 2. 现状问题（度量）

- 接入耦合：`c30f5ca` 为新增去耦、I²C、改版影响三个模块改了 24 个文件、+4,655 行；每个模块都要在 `plan_review.py`、`lint.py`、`validate_review.py` 各写一段专门代码。
- 核心文件超限：`plan_review.py` 1,223 行、`lint.py` 811 行，均超过 800 行上限。
- 发现层偏命名：计划主要靠网络名/器件名正则（`FEATURE_CATALOG`）与人工声明的 `intent.circuits`；没有按连接关系识别电路的能力。
- 文档重述：判定纪律在多份文档反复出现（“待核/INSUFFICIENT”17 份文件 54 次、“指纹/哈希绑定”10 份 63 次、“HANDOFF”10 份 33 次）。
- 覆盖缺口：上述 7 类在 `review-checklist.md` 与计划中无条目或只有防反接/热插拔等局部条目（逐项核验记录见检查项地图）。

## 3. 架构

### 3.1 检查器接口（`scripts/checkers/base.py`）

每个检查器是一个类，声明元数据与钩子，默认实现集中在基类：

| 成员 | 含义 |
|---|---|
| `id`、`title` | 稳定标识与中文名 |
| `plan_key`、`version_key`、`version` | 清单在计划 JSON 中的顶层键与版本字段；沿用既有键，保证旧计划与测试兼容 |
| `intent_key` | intent 中本检查器的配置段（可缺省） |
| `cold_rules` | 冷跑规则号 → 名称（新检查器用自有前缀，如 `IL-01`） |
| `hot_rules` | 热跑规则号 → 处理函数与证据模型校验函数（SP2 起使用） |
| `validate_intent(section, db)` | 校验 intent 配置段，返回错误列表 |
| `inventory(db, intent)` | 生成确定性清单，含 `digest` 与 `context`（重建所需输入） |
| `plan(planner, inventory)` | 通过 `planner.add_check` 生成计划项，不新造计划项结构 |
| `cold_findings(lint, inventory)` | 通过 `lint.add` 输出冷跑 FINDING/CANDIDATE |
| `binds(plan_item)` | 判断计划项是否由本检查器生成（校验用） |
| 校验钩子 | `generated_fields`、`object_errors`、`pass_gate`、报错文案；由基类的通用校验流程调用 |

### 3.2 注册表与接入

- `scripts/checkers/__init__.py` 维护有序 `REGISTRY`。顺序固定为 I²C 拓扑、去耦、然后新检查器，保证既有检查 ID 不变。
- `ReviewPlanner`：以 `self.inventories[id]` 保存清单，`plan_checkers()` 遍历注册表；保留 `i2c_topology`、`decoupling` 属性与 `plan_i2c_topology()`、`plan_decoupling()` 作为兼容入口。
- `validate_intent`：通用字段校验保留，各检查器段由注册表分发。
- `lint.py`：冷跑结束时遍历注册表调用 `cold_findings`；热跑分发表合并注册表的 `hot_rules`；`--i2c-topology-json`、`--decoupling-json` 保留，新增通用 `--checker-json ID=PATH`。
- `electrical_contract.HOT_RULE_IDS` 与模型校验合并注册表的热跑规则。
- `validate_review.py`：两段模块专用校验改为 `validate_checker_inventories()` 遍历注册表，报错文案由检查器提供，原有文案逐字保留；“有缺口不得 PASS”门槛由 `pass_gate` 提供。
- `plan_rules()`：冷跑规则表合并注册表的 `cold_rules`。

### 3.3 网表图工具（`scripts/checkers/netgraph.py`）

借鉴 kicad-happy detector helpers 的思路，按 `db.json` 重写（不移植代码）：

- 器件分类：按位号前缀、primitive/型号关键字、引脚名给出类别（电阻、电容、电感、磁珠、二极管、TVS/齐纳、MOSFET、BJT/IGBT、继电器、光耦、连接器、IC、未知），并记录分类依据；无法分类记为 `UNKNOWN`，不静默当作不适用。
- 引脚角色：MOSFET G/D/S、BJT/IGBT B(G)/C/E、二极管 A/K、继电器线圈、光耦 LED/输出；引脚名缺失时角色为未知并形成缺口。
- 连接查询：某网上的器件、两网之间的二端器件、按装配状态过滤。
- 共享常量：地网集合、电源轨正则从 `plan_review.py` 移入，供计划与检查器共用。

### 3.4 装配状态（`scripts/checkers/states.py`）

新检查器共用一套状态校验（`id`、`citation`、`population`、可选 `jumpers`，1–32 个状态），报错文案与 I²C 模块一致。没有 intent 时使用网表 `nc` 形成“按图状态”，并登记“装配状态未经证实”缺口：冷跑发现照常输出，依赖状态的计划项保持 `WAITING_EVIDENCE`。既有两个模块在其测试不变的前提下迁移到共用实现，否则保留原实现并记入后续项。

### 3.5 识别依据分级

| 依据 | 来源 | 能产生 |
|---|---|---|
| `declared` | intent 明确声明并带出处 | FINDING、计划项、热跑 |
| `topology` | 连接关系加引脚角色成立 | FINDING、计划项、热跑 |
| `name-hint` | 只有网络名/型号线索 | 计划项（`UNDETERMINED`，需 intent 确认） |

冷跑 FINDING 仍是“疑似缺陷，需专家复核”，与既有 Rule 同一语义；任何检查器都不直接给出最终 PASS。

## 4. 检查器

规则号使用检查器前缀：01–09 为冷跑，10 起为热跑。

### 4.1 感性负载续流与钳位 `inductive_load`（SP1 首个新检查器）

- 识别：继电器线圈；由分立开关管或驱动芯片输出驱动、另一端接电源轨的电感/变压器绕组；排除开关电源 SW/LX/PH 节点上的储能电感；经连接器外接且网名含 MOTOR/SOLENOID/VALVE/COIL/RELAY 的负载记为 `name-hint`。
- 钳位识别：负载两端的续流二极管（阴极在电源轨一侧）、开关管两端的 TVS/齐纳、负载或开关两端的 RC、二极管串齐纳；驱动芯片 COM 类引脚接电源轨记为“集成钳位候选”，需资料证据。
- 冷跑：`IL-01` 已识别负载在某装配状态下没有任何钳位路径（含钳位器件不贴）；`IL-02` 续流二极管方向接反。
- 计划项（逐负载、逐状态）：钳位拓扑与方向；钳位额定（反向耐压不低于电源最高电压，峰值电流不低于关断瞬间线圈电流，齐纳/TVS 钳位时开关管耐压覆盖电源电压加钳位电压，重复频率与耗散）；有释放时间要求时核钳位方式；布局 HANDOFF（钳位器件靠近负载与开关、回路面积）。
- 依据：`HardwareWiki:wiki/methodology/inductive-load-flyback-clamp-design.md`。

### 4.2 功率开关 `power_switch`（SP2）

- 识别：分立 MOSFET/IGBT（引脚角色成立），栅极网上的驱动源（IC 输出脚、串联电阻、驱动芯片 HO/LO 类输出）、栅源下拉、源极所在网（地、电源轨或开关节点，判断高/低边）、自举元件、开关节点上的 RC/RCD。
- 冷跑：`PS-01` 栅极网无任何驱动源且无下拉（悬空）；`PS-02` 开关节点接感性元件或半桥中点但无吸收网络（CANDIDATE）。
- 热跑 `PS-10`：最小实际栅源电压不低于 R<sub>DS(on)</sub> 保证值对应的 V<sub>GS</sub> 条件；最大实际栅源电压不高于栅源绝对最大值；输入均为带出处的保证值，缺任一项为 INSUFFICIENT。
- 计划项：SOA（电流、电压、功率、脉宽、换流速度、栅偏共同限定）；负压、死区、自举欠压、短路软关断；开关节点振铃证据；散热 HANDOFF。
- 依据：`concepts/mosfet-igbt-gate-drive-circuit.md`、`concepts/功率器件栅极驱动保护.md`、`concepts/功率开关安全工作区.md`、`concepts/开关节点振荡吸收.md`。

### 4.3 DC-DC 输入滤波 `input_filter`（SP2）

- 识别：开关稳压器（VIN/PVIN 与 SW/LX 类引脚）输入网向上游经串联电感/磁珠/共模扼流到源电源轨的滤波结构，两侧电容与阻尼电阻。
- 冷跑：`IF-01` 存在串联 L/磁珠滤波但未见阻尼元件（CANDIDATE）。
- 热跑 `IF-10`：负输入阻抗幅值 |R<sub>IN</sub>| = V<sub>IN,min</sub>²/P<sub>IN,max</sub>；判据 ESR<sub>bulk</sub> &lt; |R<sub>IN</sub>| 且 ESR<sub>bulk</sub> &gt; L/(C<sub>bulk</sub>·|R<sub>IN</sub>|)，C<sub>bulk</sub>/C<sub>in</sub> 不低于项目规定比值（未规定为 INSUFFICIENT）。结果注明为一阶判据，全频阻抗与阶跃验证为 HANDOFF。
- 依据：`methodology/dc-dc-输入滤波稳定性评估.md`、`methodology/输入滤波稳定性与阻尼评估.md`。

### 4.4 上电过程 `power_up`（SP2）

- 识别：带 EN 的稳压器及其 EN 来源（直连输入轨、RC、其他稳压器 PG、GPIO、监控器）、SS 类引脚电容、PG 去向、同步降压控制器（HG/LG/BST 类引脚）、LDO。
- 冷跑：`PU-01` 负载器件的使能/复位与其供电轨同时建立（CANDIDATE）；`PU-02` EN 直连输入轨且无 UVLO 分压或 RC（CANDIDATE）。
- 热跑 `PU-10`：LDO 低温最坏压差：V<sub>IN,min</sub> − V<sub>dropout,max</sub>(T<sub>min</sub>, I<sub>max</sub>) ≥ 负载要求的 V<sub>OUT,min</sub>。
- 计划项：同步降压预偏置支持（资料证据）；负载使能相对爬升时机；单调性与台阶交实测 HANDOFF。
- 依据：`methodology/power-rail-startup-review.md`、`concepts/电源上电时序故障诊断.md`、`concepts/同步降压预偏置启动.md`、`methodology/ldo最坏条件选型验证.md`。

### 4.5 监控与看门狗 `supervision`（SP2）

- 识别：监控器/看门狗（WDI/WDO/WDT/MR/RESET/SENSE/VMON 类引脚）、其监测网、输出到复位输入的路径、WDI 来源；电源轨清单（稳压器输出与供给 IC 电源脚的轨）。
- 冷跑：`SV-01` WDI 悬空或固定接电源/地（CANDIDATE，需确认是否有意禁用）；`SV-02` 看门狗/复位输出未连到任何复位输入；`SV-03` 电源轨无任何监测（CANDIDATE 清单）。
- 热跑 `SV-10`：看门狗/复位输出最小脉宽不低于目标复位输入要求；开漏输出需上拉存在。
- 计划项：启动期喂狗（固件证据）；逐电源域监控覆盖（ER2，需求确定哪些轨必须监测）；寄存器与存储器恢复。
- 依据：`methodology/复位时序与看门狗审核.md`、`methodology/multi-rail-brownout-reset-verification.md`。

### 4.6 高速差分电平 `diff_levels`（SP2）

- 识别：差分对（复用现有差分对识别）、电平标准线索（LVDS/LVPECL/PECL/CML/HCSL，来自网名、引脚名、型号或 intent）、串联耦合电容、跨接或到终端轨的终端电阻、偏置网络。
- 冷跑：`DL-01` 已知 LVPECL 且交流耦合，但发送端没有直流通路；`DL-02` 交流耦合且接收端未见偏置或终端（CANDIDATE）。
- 热跑 `DL-10`：直流耦合时发送端共模范围落在接收端共模范围内、摆幅落在接收差分输入范围内；交流耦合时接收端偏置共模落在范围内。
- 依据：`methodology/high-speed-level-interconnect-review.md`、`concepts/高速电平互连.md`。

### 4.7 光耦 `optocoupler`（SP2）

- 识别：光耦器件、LED 串联电阻与驱动源、输出侧上拉电阻与上拉轨、发射极参考。
- 冷跑：`OC-01` LED 回路无限流电阻且非恒流驱动；`OC-02` 输出集电极无上拉（CANDIDATE）。
- 热跑 `OC-10`：I<sub>F,min</sub> 由驱动电压、V<sub>F,max</sub>、电阻最大值求得；CTR<sub>min</sub> 乘以寿命衰减系数（项目规定，未规定为 INSUFFICIENT）后，I<sub>C,可用</sub> ≥ (V<sub>pullup,max</sub> − V<sub>OL,要求</sub>)/R<sub>pullup,min</sub>；同时 I<sub>F,max</sub> 不超过额定。
- 依据：`concepts/光耦合器隔离传输.md`。

## 5. KiCad 输入（SP3）

- `scripts/parse_kicad.py`：输入 `.kicad_sch`（调用 `kicad-cli sch export netlist --format kicadxml`）或已导出的 netlist XML；输出与 `parse_netlist.py` 相同的 `db.json` 契约（`nets`、`pin2net`、`pinname`、`pintype`、`parts{prim,part,jedec,value,nc}`、`ref2page`、`pseudo_nets`、`declared_pinname`、`declared_pintype`、`missing_primitives`、`export_errors`、完整性自检）。
- 映射：引脚电气类型映射到既有 PINUSE 词汇；型号优先取 MPN 类字段，否则取符号名；DNP/排除 BOM 标记映射为 `nc`；工具生成的 `unconnected-*` 网按真实导出结果确定归类；页码取层次页。
- 测试：仓库只放手写的合成 netlist XML；本机用 KiCad 自带演示工程做健壮性冒烟测试，不入库。

## 6. 文档

- 新增 `references/checkers.md`：共用契约一节（识别依据、候选语义、清单绑定、HANDOFF），各检查器一节（识别、冷跑、计划项、热跑模型、边界）。`decoupling-schema.md`、`i2c-topology-schema.md` 的规范性内容全部并入，原文件删除，引用处改指向新文件。
- `SKILL.md`：两段模块说明合并为一段检查器说明；新增 KiCad 解析命令；总行数不增加。
- `lint-rules.md`、`datasheet-evidence-schema.md`、`review-plan-schema.md`、`review-checklist.md` 只做必要的交叉引用与新规则登记。

## 7. 测试与验收

| 子项目 | 验收 |
|---|---|
| SP1 | 既有测试全绿（兼容入口保证不改测试或只改导入）；新增 netgraph、states、注册表接入、`inductive_load` 单元测试；对全部 circuit_bench 用例与测试夹具，重构前后的计划与 lint 输出在既有键上逐字一致；circuit_bench 全量不回退 |
| SP2 | 每个检查器识别/冷跑/计划项单元测试；每个热跑规则 PASS/FAIL/INSUFFICIENT 与缺证据用例；circuit_bench 新增独立家族（1.0 数据、答案与协议不改）；全量无错误 PASS |
| SP3 | 合成 netlist XML 契约测试；自检失败路径测试；KiCad 演示工程冒烟通过；现有检查器在 KiCad 输入上可运行 |

## 8. 决策记录

| 编号 | 决策 | 理由 |
|---|---|---|
| D1 | 方案 A（注册表 + 拓扑识别），SP1→SP2→SP3 | 用户确认 |
| D2 | 保留 `decoupling.py`、`i2c_topology.py` 及其命令行，适配器放 `checkers/` | 文档与测试直接使用这些入口，移动会造成无收益的破坏 |
| D3 | 计划 JSON 沿用各检查器顶层键；新检查器用自身 id 作键并带 `<id>_version` | 旧计划、改版比对与既有测试兼容；不引入镜像数据 |
| D4 | 识别依据三级，只有 `declared`/`topology` 能出 FINDING | 保持“命名不是证据”的既有纪律 |
| D5 | 新规则用检查器前缀编号，不改既有 Rule 编号 | 避免改动历史审查记录的规则引用 |
| D6 | 新计算器沿用 `evidence.json` 热跑管线，不另建事实流程 | 复用证据绑定、指纹与评测驱动，减少冗余 |
| D7 | 比值类门槛（C<sub>bulk</sub>/C<sub>in</sub>、CTR 寿命衰减）必须来自项目规定，缺省为 INSUFFICIENT | 知识库给出的是来源条件下的经验值，不能写成默认阈值 |
| D8 | 合并两份模块 schema 文档为 `checkers.md` | 共用纪律只写一次，符合“减少冗余” |
| D9 | 直接在 main 上开发，每个子项目通过验收后本地提交，不推送 | 用户选择在 main 上改；推送需另行授权 |
| D10 | 仓库只放合成用例；本机工程与 KiCad 演示工程仅用于本地冒烟 | 遵守“公开仓库只放脱敏合成用例” |
| D11 | 暂不实现引用原文机械校验与审查深度分级 | 用户本轮未选择；放入后续项 |

## 9. 风险与处理

- 兼容风险：重构改变既有检查 ID 或计划内容。处理：注册表顺序固定，并以重构前后输出逐字比对作为 SP1 验收门。
- 误报风险：拓扑识别在引脚名缺失的库上失效。处理：角色未知形成缺口而非结论；排除规则（稳压器储能电感）有测试。
- 规模风险：`plan_review.py`、`lint.py` 继续增长。处理：新逻辑全部进入 `checkers/`；两文件行数只减不增作为检查项。

## 10. 实施记录（2026-09-16）

三个子项目均已在 `main` 上本地提交（未推送）：

| 提交 | 内容 |
|---|---|
| `e8e9914` | SP1：检查器注册表、netgraph/states/inventory/planutil 共享层、`inductive_load`、文档合并为 `references/checkers.md` |
| `c134a4e` | SP2：`power_switch`、`input_filter`、`power_up`、`supervision`、`diff_levels`、`optocoupler` 六个检查器与六条热跑规则 |
| `091f7af` | SP3：`scripts/parse_kicad.py`（kicadxml → 同一 db 契约） |
| `1070722` | 真实板复核修正：KiCad 引脚名装饰还原、新增 PU-03、反馈脚不计作输出轨 |
| `54b580b` | 全检查器同板契约测试；PS-02 把接到电源轨的续流二极管计作钳位，消除误报 |

实施期间新增的决策：

| 编号 | 决策 | 理由 |
|---|---|---|
| D12 | 解析 KiCad 时按库符号引脚名还原 `pinfunction` 的 `_<脚号>` 装饰 | kicadxml 把引脚名写成 `EN_12`，官方库与 easyeda2kicad 库都如此；不还原则所有按引脚名识别的规则静默失效（实测一块真实板的 EN/SW/FB 全部漏识别） |
| D13 | 新增冷跑规则 `PU-03`（使能来源不确定） | 同一块真实板上稳压器使能脚悬空，PU-01/PU-02 都不覆盖；内部上/下拉需资料证据，故为 CANDIDATE |
| D14 | PS-02 把开关节点到任一相邻电源轨的钳位/吸收计入 | 低边继电器驱动的续流二极管跨负载接到电源轨，只看漏源之间会把标准接法报成缺吸收 |
| D15 | circuit_bench 新家族推迟，本轮不做 | `run.py` 的 `load_dataset` 校验 `generate.py`/`oracle.py` 的哈希，就地扩展会让 1.0 数据集直接不可运行（报 “create a new versioned dataset”）。按其既定约束，新家族应作为独立版本数据集另起一轮；本轮改动改用既有 166 例做回归门，并以全检查器同板契约测试 + 逐热跑规则单元测试承接验收：结果只有 PASS/FAIL/INSUFFICIENT 三类（缺保证值、资料未绑定、指纹过期都归 INSUFFICIENT），另有一类不产生结果的入口拒绝——`validate_evidence` 在热跑前打回结构非法的证据 |

验收实际执行情况：

- 每条新热跑规则的用例：PASS、逐条判据各一个 FAIL、保证值缺失的 INSUFFICIENT，以及被
  `validate_evidence` 拒绝的非法证据。**未覆盖**：证据过期（db/文档哈希变更）导致的
  INSUFFICIENT，目前只有 circuit_bench 对老规则覆盖。
- 单元测试 327 → 491 全绿（新增 `test_inductive_load`、`test_power_switch`、`test_input_filter`、
  `test_power_up`、`test_supervision`、`test_diff_levels`、`test_optocoupler`、`test_parse_kicad`、
  以及 `test_checkers_framework` 的全检查器同板契约）。
- 重构前后逐字比对：5 个夹具的计划与 lint 输出在既有键、既有检查项与既有规则上零差异
  （比对脚本排除新键/新检查项/新规则后仍为 0）。
- `circuit_bench --split all --require-pass`：166/166，`regressed_ids` 为空，与改动前逐例一致。
- KiCad 冒烟：5 个 KiCad 10 模板工程 + 1 块本机真实板，`.kicad_sch` 与已导出 XML 两条路径均通过
  自检，并能跑完计划与 lint。
- 真实板误报复核：六个工程上新规则仅在真实缺陷处命中一条（稳压器使能悬空）。

后续项（未做）：引用原文机械校验、审查深度分级、Altium 输入、circuit_bench 独立新家族（D15）。
