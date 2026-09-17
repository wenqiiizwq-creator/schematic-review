---
name: schematic-review
description: "仅审查硬件电路原理图的电气合理性和需求符合性，按error、warning、suggestion列出有证据的问题、详细修改步骤和复验条件，生成简短前言的审查报告。用于首审、冻结前检查、改版 diff 和历史意见闭环；支持完整网表审查及明确受限的 PDF 审查。不执行 PCB/Gerber、布局布线、SCH-PCB 核对或生产准出审查。"
---

# 电路原理图系统审查

> V2.2（2026-09-17 能力更新）｜需求追溯、对象覆盖、工况审查、三等级校准、精简前言、面向新手的修改步骤与结果校验。

目标是在给定资料、工况和原理图边界内，系统寻找连接错误、参数/额定值不合理、功能遗漏、
要求偏离及可预见的异常状态问题，提出能执行和复验的修改建议。不能承诺发现物理电路的
“所有缺陷”。用覆盖台账和未决项说明查了什么、缺什么；不能以免责声明代替已有条件的检查，
也不能找到几项严重问题后停止其余审查。

范围仅限硬件原理图：不接入或运行 PCB/Gerber 解析、布局布线/DRC、原理图与 PCB
跨文件核对、SI/PI、EMC 预检或热布局审查。HANDOFF 只记录由原理图导出的下游约束，
不执行下游审查；符号物理脚、准确封装型号与原厂 pinout 的对应仍属原理图检查。
说明工具能力、规划自动检查或判断是否已查全时，先核对
[当前能力与自动化边界](references/capability-boundary.md)，不能把清单要求写成已实现的自动功能。

## 判定纪律

1. **设计事实与判据分开**：当前网表/BOM/图面证明实际连接和贴装；适用版本的官方
   datasheet、errata、接口规范和受控需求证明应当怎样。矛盾时查版本、变体及适用条件，
   不能用一个总优先级覆盖另一种证据。历史报告与 demo 只作线索。
2. **证据落实到主张**：每项给页码、位号/物理脚号、网络及可定位出处。确认“不满足保证
   边界”不等于确认“实物必然失效”；未知后果单列，不得降低已经证实的违规。
3. **逐项覆盖**：需求、全部页面、器件/物理脚、电源轨、每路接口、检测/使能链、装配选项、
   运行状态及历史意见均有台账。按功能识别关键器件，不能只查 U 前缀。
   READY、热跑某规则一次、零命中都不表示完成。
4. **新手能按步骤修改**：每项发现按 [remediation-guide.md](references/remediation-guide.md)
   给修改准备度、定位、旧→新、顺序操作、参数依据和明确的通过标准。连线写到物理脚，
   明确哪些旧连接要断开；新增件给两端接法和规格，不能止于“加上拉/加保护/参考手册”。
   缺输入时给取得方法、计算/选择步骤及条件方案，不编造精确料号/阻值或空闲 GPIO。

## 结果与分级

先读 [severity-calibration.md](references/severity-calibration.md)。各维度互不替代：

| 维度 | 取值 |
|---|---|
| `review_result` | PASS / FAIL / INSUFFICIENT / NA |
| 全部条目 `severity` | error / warning / suggestion（仅这三个小写等级） |
| `evidence_confidence` | A 直接可复现 / B 有依据的推导 / C 尚缺关键证据（正文用中文说明） |
| 关闭状态 | OPEN / FIXED_VERIFIED / ACCEPTED / RETRACTED |
| `blocking` 与 `handoff` | 是否影响冻结及独立交接，不由等级自动替代 |

error：已证实的过压、主要功能连接错误、必需功能缺项或工作范围违规。
warning：有具体电气风险、局部非关键应用偏离，或关键工况尚未验证。
suggestion：物料二义、封装/Value/库字段对应、文档/资料补齐、后续阶段交接或可选改进。
实际焊盘/连接已被证实错误时按电气影响定级，不能仅因标题含“封装”降为建议。
逐项给归类理由；缺证不能当故障，已证实违规也不能因尚未实测就撤掉。
新台账用schema_version=3；旧v2只用于兼容复核，不机械转换旧等级。

## 执行流程

AC0 是自动候选扫描；ER1–ER7 是工程审查职责。保持依赖顺序，身份/图形疑点立即前置。
大工程分批继续并保留进度，不缩小覆盖范围。
将实际电路和适用状态填入 `intent.circuits`，由计划展开到单个判据；功能域仅汇总覆盖，
不能代替各电路/状态的检查。来源、条件导通和逐轨预算格式见 review-plan-schema。

### 0. 基线、需求、工况

从资料和对话提取功能/量化指标、接口角色/数量、输入范围/负载、温度、降额依据、保留/
删除项和不可改动项。写入 `intent.json`，每条要求有稳定 REQ ID、验收判据及关联电路。
冲突/缺失标出，先做独立工作；允许集中提出关键缺口，不逐颗器件打断用户，不自行接受风险。

按 [coverage-protocol.md](references/coverage-protocol.md) 建输入版本/哈希、装配配置和覆盖台账。
PDF 与网表时间接近不能证明同版，需核修订号和关键改动。仅 PDF 时逐页读图并声明范围，
不能声称网表/ERC/逐脚全量通过。缺工具先盘点现有能力，安装另获授权。

### 1. 解析与完整性

    python3 scripts/parse_netlist.py <allegro目录> -o db.json
    python3 scripts/parse_kicad.py <文件.kicad_sch|kicadxml> -o db.json

随附解析器实现 Cadence/OrCAD 三件套与 KiCad 网表（kicadxml）；其他 EDA 需生成同契约索引
并验证适配器。若 kicad-cli 未在 PATH，显式传 `--kicad-cli <已有可执行文件路径>`，不自行安装。
KiCad 输入把 No-connect 属性、真悬空引脚与 DNP 不贴分开记：声明 NC 的引脚
进伪网络并列入 `no_connect_nodes`，无标记的悬空引脚保持真实单节点网由 Rule-01 扫出，
`nc` 只认 dnp 属性或 VALUE 上的 NC 标记（`exclude_from_bom` 不是装配证据）。
读 [netlist-parsing.md](references/netlist-parsing.md)：核对重复归网、缺失 primitive、索引互反、
符号声明引脚和网表实有引脚。解析率不等于官方封装覆盖率；对官方 pinout 双向做差集，
包括网表完全不存在的脚、EP、隐藏电源和多单元符号。

读导出日志；导出中止/错误先隔离为输入阻断，残留三件套不能证明当前版本有效。
NC 汇集伪网、No-connect 属性、DNP 不贴是三件事。`nc` 是解析标记，仍需核装配 BOM/
选项表；不贴串联通路视为断开，多配置分别分析。
逐页提取 PDF 文本，建 PDF 页码/页名/网表路径映射；错位、旋转、极性不清立即渲染局部，
放大至可辨，DPI 数字本身不构成方向证据。

### 2. AC0 计划与冷跑

    python3 scripts/lint.py db.json --log netlist.log --intent intent.json --plan-json review-plan-cold.json --json lint-cold.json

读 [review-plan-schema.md](references/review-plan-schema.md) 和 [lint-rules.md](references/lint-rules.md)。
补齐命名启发式未发现的对象/需求/工况。排除候选须有反证；无特征不等于 NA。
人工补查项加入 review-plan-cold.json；保留 lint-cold.json 中的原始快照。适用性变更记出处。

检查器注册表每趟都跑，读 [检查器契约](references/checkers.md)：识别依据分声明/拓扑/名称线索
三级，名称线索只生成待核项；清单绑输入指纹，改了输入要重新生成，不能编辑清单消缺口。
`--i2c-topology-json`、`--decoupling-json` 与通用 `--checker-json <id>=out.json` 可另存清单。

I²C（`intent.i2c_topology`）：只跨已确认贴装的两脚电阻和闭合跳线找远端上拉，串阻保留节点，
有源器件两侧不合并；名称、`nc=false`、默认 Bridged 都不是状态证据；外接模块未知或路径未覆盖
保持待核，连接发现不等于 Rule-09 电气通过。

去耦（`intent.decoupling`）：补完整官方脚表、分组/返回节点与逐状态装配；零电容、未知容量和
未连物理脚都要登记；不跨 0Ω/磁珠合并，不以同网共享或标称总容量证明本地去耦/有效容量合格，
位置与回路另交 PCB HANDOFF。

其余检查器：感性负载钳位 IL、功率开关 PS、输入滤波 IF、上电使能与压差 PU、监控看门狗 SV、
差分电平 DL、光耦 OC。冷跑只按连接关系报疑点，引脚角色缺失就只留缺口；热跑
PS-10/IF-10/PU-10/SV-10/DL-10/OC-10 只吃带出处的保证值，缺一项即 INSUFFICIENT，
体电容比值与 CTR 寿命衰减等系数必须来自项目规定，不得默认。

逐针 ESD（`intent.connector_esd`）和通用无源网络（`intent.passive_networks`）按
[完整工作流](references/connector-esd-passive-workflow.md) 执行。ES-01 从每个连接器物理脚出发，
没有保护器件也生成缺口；NC/不贴/地名不自动豁免，有TVS不自动通过。无源扫描先归组RC/LC
候选并登记未归属R/C/L，再确认全部支路、源阻抗、真实负载及参数区间，用PN-10验算；
未知源/负载/公差不填默认值。零候选仍须完成器件/端口对账。

### 3. ER1 身份、官方条款与热跑

先核 MPN、封装/温度/固定可调档、BOM/符号。PART/VALUE 冲突时建立身份分支，
可按 VALUE 候选继续分析，不得认定其为实际物料。一个可信原厂文档可确认器件类别，
库名和商城转引同一 PDF 不算两个独立证据；身份冲突给出确认步骤；资料二义本身通常列suggestion，候选差异的具体电气风险另行定级。

每颗关键器件读完整适用章节：引脚、Abs Max、推荐条件、电气 min/max、上掉电、默认态/
strap、模式、应用计算、封装订货、errata；保留“文档章节→检查项”阅读记录。
不能只读 Abs Max 或只追 AC0 命中器件。按
[datasheet-resolution-schema.md](references/datasheet-resolution-schema.md) 先审资料包，
缺失时完成 LCSC/立创与原厂检索，核对原厂 PDF 身份并记录补取结果。搜索摘要/聚合参数/
兄弟型号只作线索；系列手册须订货表覆盖后缀，不能仅凭文件名判 AVAILABLE。

    python3 scripts/audit_datasheets.py db.json --datasheet-dir <资料目录> --json datasheet-audit.json

Agent 完成资料核对/补取并写出 `datasheet-resolution.json` 后，纳入判据依赖再热跑：

    python3 scripts/audit_datasheets.py db.json --datasheet-dir <资料目录> --resolution datasheet-resolution.json --evidence evidence.json --json datasheet-audit.json
    python3 scripts/lint.py db.json --log netlist.log --intent intent.json --evidence evidence.json --datasheet-audit datasheet-audit.json --merge-plan review-plan-cold.json --plan-json review-plan.json --json lint-hot.json

Rule-08 需要复用已核对的 Vref 时，按 [Vref 参数复用](references/datasheet-facts-schema.md)
把事实保存在项目内，明确精确 MPN/封装和本次完整工况，先物化为 evidence 再热跑。
事实与电路结论分开；过期、条件不覆盖或多条适用保证均待核，不以 typ/置信度代替保证值。

证据格式见 [datasheet-evidence-schema.md](references/datasheet-evidence-schema.md)。自动结果只覆盖
输入的具体对象与判据，未覆盖实例仍待查。热跑证据须绑定当前网表/物料、装配及状态、
文档内容指纹；关键 R/C/L/F/Y/J 的参数按需纳入依赖。资料未 AVAILABLE、指纹过期、
公差/负载/采样模型缺失时，计划和执行均保持待核。合并后的 review-plan.json 是唯一最终计划：
保留冷计划和人工补查项，热跑按状态展开子项。结果独立填写，旧结果不能自动传给新子项；
新增人工项继续加入最终计划。跨网表版本的旧计划不能自动合并，迁移规则见 review-plan-schema。

### 4. ER2 电源树与状态

每轨追到真正电源引脚/明确外部源，再到全部负载；0Ω/磁珠/二极管不是独立电源。
按装配状态、开关/体二极管方向、EN/PG、时序和地参考分析。核输入/输出/IO 电压与
负载 min/max、峰值和启动预算；裕量取项目依据，缺负载不写 PASS。
建立断电、启动/复位、运行、待机、掉电/棕断、热插拔及需求内故障状态表。
跨轨上拉先列候选，再查 Ioff/注入限流/掉电容忍，跨轨不等于反灌。
检测点须匹配被测量：输入存在/申请电压可取源侧，输出有效/负载保护可取负载侧；
检查“采样→判断→使能→供电”的循环依赖。

### 5. ER3 功能与控制链

逐跳记录起点物理脚→网络→已贴器件→网络→终点物理脚，同时核返回路径/参考地。
全部端口、时钟、复位、启动、编程救援、反馈/检测、使能/故障上报都要追。
TX/RX、P/N、Host/Device、Source/Sink 按两端官方语义复述，查对端连接器视图/线缆针序。
连通只证明导电路径，不证明带宽、逻辑极性或启动后能工作。

### 6. ER4 参数、容差与改法复算

读 [wca-formulas.md](references/wca-formulas.md)。先确认模型（固定/可调、内置反馈、负载效应），
再代入实际串并联、输入/温度/负载、公差区间。`scripts/solve_dividers.py` CLI 用于探索；
Rule-08 对已完整建模的共享支路可做有界线性节点分析，范围与角点限制见 wca-formulas。
不支持、超限或缺公差仍未判定；轨名电压仅为检索线索。
热跑分压模型明确源端、参考地、输入偏置及忽略支路依据；逻辑脚用采样窗口的保证电压
比较 VIH/VIL，单个上拉存在不能代替电平、时序和掉电状态验算。
区分设定目标与物理可达输出：LDO 的 FB 公式不代表升压能力；ADC FSR 不等于引脚耐压；
I²C 并联上拉须算等效值、VOL/IOL、上升时间；RC 不等于复位脉宽；有 TVS 不等于防护通过。
PN-10 支持常见RC/LC模型的有负载截止/谐振/阻尼指标及有界RLC网络的指定频点响应，
按上述工作流补全模型有效性、额定/时域检查和新手修改步骤；稀疏频点不能证明全频带。
改频率/阻值/保护管时复算 min on-time、电感、环路、掉电和额定值，不能只修一个数字。

### 7–9. 平台条款、全页目检、身份收口

逐条执行 [review-checklist.md](references/review-checklist.md) 适用项，将平台指南/checklist 拆成
有出处的原理图检查与下游约束。所有页面留目检记录，新页/变更区细读；极性、pin1/视图、
选项、同名端、NC 标记需图形证据。关键 IC/模组/保护/接口器件核物理脚号与封装。
沿用库只有同一 MPN/封装/符号版本的验证记录才可复用；demo 一致不证明需求/贴装正确。
PCB 阻抗/间距/回流和实测约束独立 HANDOFF，边界见 [scope-boundary.md](references/scope-boundary.md)。

### 10. 报告、校验与闭环

先读 [remediation-guide.md](references/remediation-guide.md)，将全部发现展开为逐项修改说明。
简单文字修正可用一步；多支路/控制电路给修改前后连接表或小图。参数状态与修改准备度
必须一致；“可直接修改”仅指原理图编辑细节齐全，不表示已改、可上电或整板准出。
按 [report-template.md](references/report-template.md) 形成报告与持久化 `review-results.json`。
新报告设置 `schema_version: 3`、`remediation_version: 1`、`binding_version: 1`，每项包含 `remediation`。
每条结果的 `binding.object`、`binding.criterion` 记录实际已审对象和判据，必须与最终计划一致，
包括装配配置及状态。同条证据和理由只支持这一范围；不能把同一 IC、相似 ID 或功能组的结论
移用到其他物理脚/判据，也不能只复制字段来迎合校验。发现定位同时列根因及受影响主对象。
绑定门验证声明一致性，不读取原文证明语义正确；工程判断仍须回读来源。契约见
[review-results-schema.md](references/review-results-schema.md)。校验覆盖、修改说明及汇总后交付：

    python3 scripts/validate_review.py review-plan.json review-results.json --db db.json --lint lint-cold.json --lint lint-hot.json --require-actionable --require-bindings --json review-gate.json

校验器对账全部冷/热计划检查及热候选的状态关联；遗漏热跑子项或人工补查项会被拒绝。
绑定校验核对声明的对象/判据及发现定位，不读取引用原文来判断语义；字段相符仍需核实证据内容。
退出 0 只表示台账有效；冻结时再加 `--require-release`，准出条件统一见 severity-calibration。
交付前再按修改说明逐步演算一次：读者能否找到位置、知道删/改/加什么、接到哪里、
采用什么规格、核对什么结果？任一答案仍需猜测就补充说明或降低修改准备度。
已完成可做工作但材料不足时可交受限报告，结论仍为不准出。不能把未完成关键前提藏进
“有条件准出”。接受风险须有责任方明确记录，不能把 FAIL 改为 PASS。

复审比较前轮设计和模板基线，沿变更供电/控制/保护依赖扩展复验。保留每个历史 ID，
区分撤回、复发、接受、已修复；断言要证明期望电气状态，仅“字段变了”不足以关闭。
读 [改版影响与复验](references/revision-impact-schema.md)：冷/热跑传同一 `--old-db` 和
`--old-plan`，与本版 `--merge-plan` 分开；可用 `--revision-impact-json` 另存清单。
依赖不完整时扩大为全量复验，旧项消失也须独立处置；每个必需项记录本轮方法、证据和摘要，
不迁移旧 PASS。最终校验传相同旧基线并加 `--require-revision-impact`；缺历史快照不补造。

    python3 scripts/diff_netlists.py old-db.json db.json --claims review-claims.json --json diff.json --fail-on-open-claims

开头最多一页，PDF第2页进入详细问题；不在详情前放长目录、方法论或全量清单。
按error / warning展开已确认问题，再列未决风险，suggestion集中展示；全部保留证据和详细改法。
计算、覆盖、历史、哈希、交接及完整索引后置。可生成Markdown与CSV，再按模板渲染检查PDF：

    python3 scripts/render_report.py review-plan.json review-results.json --db db.json --lint lint-cold.json --lint lint-hot.json --require-bindings --output report.md --csv issues.csv

复审生成报告时同样传 `--old-db old-db.json --old-plan old-plan.json --require-revision-impact`，
不能只在校验器核复验、在报告器绕过旧基线。

只改报告时保留旧版与技术事实、未决条件；逐ID记录重新分级理由，不能视作电路已修复。
保存输入哈希、意图、计划、结构化证据、结果、计算/关键裁图和 Diff 到项目审查目录；
临时全文/大图可放 /tmp，最终证据不得只留 /tmp。公开仓库只放脱敏合成用例。

## 工具维护时的回归评测

仅在维护检查脚本或评估工具升级时，按 [电路评测说明](evals/circuit_bench/README.md)
运行冻结用例和版本比较；这不是每次原理图审查的附加步骤。评测通过不等于全板审查通过，
不得以合成规格文档替代真实项目证据。
