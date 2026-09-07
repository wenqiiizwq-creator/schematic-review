---
name: schematic-review
description: "审查硬件电路原理图的电气合理性和需求符合性，按严重度列出有证据的缺陷、修改建议和复验条件。用于首审、冻结前检查、改版 diff 和历史意见闭环；支持完整网表审查及明确受限的 PDF 审查。不签署 PCB 布局布线、SI/PI、EMC、实测热或生产准出。"
---

# 电路原理图系统审查

> V2.1｜需求追溯、对象覆盖、工况审查、严重度校准、面向新手的修改步骤与结果校验。

目标是在给定资料、工况和原理图边界内，系统寻找连接错误、参数/额定值不合理、功能遗漏、
要求偏离及可预见的异常状态问题，提出能执行和复验的修改建议。不能承诺发现物理电路的
“所有缺陷”。用覆盖台账和未决项说明查了什么、缺什么；不能以免责声明代替已有条件的检查，
也不能找到几项严重问题后停止其余审查。

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
| 已确认缺陷 `severity` | P0 致命 / P1 严重 / P2 一般 / P3 建议 |
| `evidence_confidence` | A 直接可复现 / B 有依据的工程推导 / C 尚缺关键证据 |
| 待核项 `potential_severity` | 潜在后果级别，不能计入已确认缺陷数量 |
| 关闭状态 | OPEN / FIXED_VERIFIED / ACCEPTED / RETRACTED |
| `handoff` | 独立 OPEN / ACCEPTED / VERIFIED，可与任何结果并存 |

P0：已证实的危险电气应力、损坏风险、关键安全保护失效或必需启动/核心链路断开。
P1：必须实现的功能/性能缺失，或工作范围、启动保证、强制接口条件不满足。
P2：局部功能、非关键裕量、测试维护或器件数据一致性的实际问题。
P3：符合已知要求后的可选改进或不影响电气的图纸卫生。
依据后果、暴露工况、独立保护与需求重要性定级，不能凭规则号或 must 一词定级。
“观察/待确认”是队列，不能一律定为最低等级。PASS/FAIL 不得以 C 为依据。

## 执行流程

AC0 是自动候选扫描；ER1–ER7 是工程审查职责。保持依赖顺序，身份/图形疑点立即前置。
大工程分批继续并保留进度，不缩小覆盖范围。

### 0. 基线、需求、工况

从资料和对话提取功能/量化指标、接口角色/数量、输入范围/负载、温度、降额依据、保留/
删除项和不可改动项。写入 `intent.json`，每条要求有稳定 REQ ID、验收判据及关联电路。
冲突/缺失标出，先做独立工作；允许集中提出关键缺口，不逐颗器件打断用户，不自行接受风险。

按 [coverage-protocol.md](references/coverage-protocol.md) 建输入版本/哈希、装配配置和覆盖台账。
PDF 与网表时间接近不能证明同版，需核修订号和关键改动。仅 PDF 时逐页读图并声明范围，
不能声称网表/ERC/逐脚全量通过。缺工具先盘点现有能力，安装另获授权。

### 1. 解析与完整性

    python3 scripts/parse_netlist.py <allegro目录> -o db.json

随附解析器仅实现 Cadence/OrCAD 三件套；其他 EDA 需生成同契约索引并验证适配器。
读 [netlist-parsing.md](references/netlist-parsing.md)：核对重复归网、缺失 primitive、索引互反、
符号声明引脚和网表实有引脚。解析率不等于官方封装覆盖率；对官方 pinout 双向做差集，
包括网表完全不存在的脚、EP、隐藏电源和多单元符号。

读导出日志；导出中止/错误先隔离为输入阻断，残留三件套不能证明当前版本有效。
NC 汇集伪网、No-connect 属性、DNP 不贴是三件事。`nc` 是解析标记，仍需核装配 BOM/
选项表；不贴串联通路视为断开，多配置分别分析。
逐页提取 PDF 文本，建 PDF 页码/页名/网表路径映射；错位、旋转、极性不清立即渲染局部，
放大至可辨，DPI 数字本身不构成方向证据。

### 2. AC0 计划与冷跑

    python3 scripts/plan_review.py db.json --intent intent.json --json review-plan.json
    python3 scripts/lint.py db.json --log netlist.log --intent intent.json --json lint-cold.json

读 [review-plan-schema.md](references/review-plan-schema.md) 和 [lint-rules.md](references/lint-rules.md)。
补齐命名启发式未发现的对象/需求/工况。排除候选须有反证；无特征不等于 NA。
分类错误时保留原计划，记录新适用性与出处，不能保留 APPLICABLE 又写 NA。

### 3. ER1 身份、官方条款与热跑

先核 MPN、封装/温度/固定可调档、BOM/符号。PART/VALUE 冲突时建立身份分支，
可按 VALUE 候选继续分析，不得认定其为实际物料。一个可信原厂文档可确认器件类别，
库名和商城转引同一 PDF 不算两个独立证据；身份冲突必须解决。

每颗关键器件读完整适用章节：引脚、Abs Max、推荐条件、电气 min/max、上掉电、默认态/
strap、模式、应用计算、封装订货、errata；保留“文档章节→检查项”阅读记录。
不能只读 Abs Max 或只追 AC0 命中器件。补资料先原厂，再核过型号/版本的授权分销商或
LCSC 原厂 PDF 镜像；搜索摘要/聚合参数/兄弟型号只作线索。系列手册须订货表覆盖后缀。

    python3 scripts/lint.py db.json --log netlist.log --intent intent.json --evidence evidence.json --plan-json review-plan-hot.json --json lint-hot.json

证据格式见 [datasheet-evidence-schema.md](references/datasheet-evidence-schema.md)。自动结果只覆盖
输入的具体对象与判据，未覆盖实例仍待查。保存冷/热计划，不能覆盖已填的最终结果。

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
再代入实际串并联、输入/温度/负载、公差区间。可用 `scripts/solve_dividers.py`；不支持/
截断/共享支路保持未判定。脚本默认公差、轨名电压只能作筛查假设。
区分设定目标与物理可达输出：LDO 的 FB 公式不代表升压能力；ADC FSR 不等于引脚耐压；
I²C 并联上拉须算等效值、VOL/IOL、上升时间；RC 不等于复位脉宽；有 TVS 不等于防护通过。
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
新报告设置 `remediation_version: 1`，每项包含 `remediation`；契约见
[review-results-schema.md](references/review-results-schema.md)。校验覆盖、修改说明及汇总后交付：

    python3 scripts/validate_review.py review-plan.json review-results.json --db db.json --lint lint-cold.json --lint lint-hot.json --require-actionable --json review-gate.json

校验器检查记录完整性/追溯/分级/准出逻辑，不替代电气判断，不证明未知缺陷为零。
交付前再按修改说明逐步演算一次：读者能否找到位置、知道删/改/加什么、接到哪里、
采用什么规格、核对什么结果？任一答案仍需猜测就补充说明或降低修改准备度。
已完成可做工作但材料不足时可交受限报告，结论仍为不准出。不能把未完成关键前提藏进
“有条件准出”。接受风险须有责任方明确记录，不能把 FAIL 改为 PASS。

复审比较前轮设计和模板基线，沿变更供电/控制/保护依赖扩展复验。保留每个历史 ID，
区分撤回、复发、接受、已修复；断言要证明期望电气状态，仅“字段变了”不足以关闭。

    python3 scripts/diff_netlists.py old-db.json db.json --claims review-claims.json --json diff.json --fail-on-open-claims

按 P0→P3 列全部确认问题，另列高潜在严重度待核项、修改顺序、复验标准及覆盖缺口。
保存输入哈希、意图、计划、结构化证据、结果、计算/关键裁图和 Diff 到项目审查目录；
临时全文/大图可放 /tmp，最终证据不得只留 /tmp。公开仓库只放脱敏合成用例。
