<!-- 合成格式示例；机器输入和合成判据见 three-level/，不用于真实设计。 -->

# 合成示例：三等级报告

SYN-C Rev.1；全部数值与器件均为测试设定

当前不满足原理图冻结条件。

全部事实与判据为合成设定，展示报告契约；没有真实器件参数或真实网表分析结论。

| 等级 | 数量 |
|---|---:|
| error | 1 |
| warning | 2 |
| suggestion | 3 |

优先处理：F-101，修改后按条目复验。

<!-- pagebreak: PDF详情从第2页开始；前言最多1页 -->

## 1. 已确认问题及详细修改方法

### error | F-101 | 已证实接口电压超过保证范围

位置：PDF 1 页；R1；网络 A

**证据与处理状态**

已核实偏离，影响限于所述工况；尚未关闭

**连接与参数**

接口空载时的分析窗口为4.6至4.8V，合成接收端允许上限为3.6V；该条件由本例直接设定。

**设计依据**

SYN-SPEC §1：接收端工作电压不得超过3.6V。

**风险原因与影响**

已超出保证范围；实际损伤时间和钳位电压未由本例证明。

**触发条件**

源有效且输出释放时。

**问题来源**

上拉所用电源与接收域不匹配。

**归类理由**

合成条件明确给出违规窗口，应修改后复验。

**冻结判断依据**

- 合成规格中的工作范围已经违反，必须修改并复验。

**修改目的与准备度**

Restore the specified fitted pull-up.；编辑细节已明确，尚不代表实际修改

**详细修改步骤**

synthetic evidence

1. p1 R1：DNP → fitted, quantity 1。Enable R1 in configuration A and its BOM.

**参数：R1（已选）**

10k, 1%, 0402, fitted

- synthetic-fixture.json：all values stipulated for tests

**联动检查**

Confirm the stipulated reset load remains within its limit.

**复验与完成条件**

synthetic evidence

- 网表回读：Re-export configuration A and trace R1.；预期：R1 is fitted between A and U1.2.

**证据定位**

关联检查：C1

- synthetic-fixture.json：all values stipulated for tests

### warning | F-102 | 局部非关键指示通道未满足应用条件

位置：PDF 1 页；R1；网络 A

**证据与处理状态**

已核实偏离，影响限于所述工况；尚未关闭

**连接与参数**

合成指示输出需要外部恢复路径，但配置A未装该支路；电路只承担非关键维护指示。

**设计依据**

SYN-SPEC §2：本例指示通道需要外部恢复路径，不能按推挽驱动使用。

**风险原因与影响**

指示可能不正确；没有证据扩展为核心链路故障。

**触发条件**

维护指示变化时。

**问题来源**

按另一种输出结构套用了外围。

**归类理由**

确认局部应用偏离，未证实主要链路或器件损坏后果。

**冻结判断依据**

- 合成测试规定：除error外，本项不属于当前冻结必需条件。

**修改目的与准备度**

Restore the specified fitted pull-up.；编辑细节已明确，尚不代表实际修改

**详细修改步骤**

synthetic evidence

1. p1 R1：DNP → fitted, quantity 1。Enable R1 in configuration A and its BOM.

**参数：R1（已选）**

10k, 1%, 0402, fitted

- synthetic-fixture.json：all values stipulated for tests

**联动检查**

Confirm the stipulated reset load remains within its limit.

**复验与完成条件**

synthetic evidence

- 网表回读：Re-export configuration A and trace R1.；预期：R1 is fitted between A and U1.2.

**证据定位**

关联检查：C2

- synthetic-fixture.json：all values stipulated for tests

## 2. 风险项及验证方法

### warning | R-101 | 已知参数裕量偏窄，尚缺动态工况

位置：PDF 1 页；R1；网络 A

**证据与处理状态**

整体结论尚缺证据，后果未被证实；尚未关闭

**连接与参数**

静态电压窗口距上限很近；本例没有给负载阶跃、纹波及温度边界。

**设计依据**

SYN-SPEC §3：输入范围必须包含动态和温度变化。

**风险原因与影响**

动态偏移可能使电压越窗，尚不能给出最终结论。

**触发条件**

负载变化或温度变化时。

**问题来源**

计算仅覆盖静态模型。

**归类理由**

已有具体裕量风险，尚需负载与瞬态验证。

**待补齐的条件**

- 受控负载上限

**冻结判断依据**

- 合成测试规定：除error外，本项不属于当前冻结必需条件。

**修改目的与准备度**

Restore the specified fitted pull-up.；前提满足后才能确定修改

**前提：受控负载上限**

检查参数裕量

取得方式：从受控接口规格读取

采用条件：给出适用于本配置的min/max

**详细修改步骤**

synthetic evidence

1. p1 R1：DNP → fitted, quantity 1。Enable R1 in configuration A and its BOM.

**参数：R1（候选）**

10k, 1%, 0402, fitted

待补输入：受控负载上限

选择方法：按保证窗口和电阻公差计算，再选择标准值。

- synthetic-fixture.json：all values stipulated for tests

**联动检查**

Confirm the stipulated reset load remains within its limit.

**复验与完成条件**

synthetic evidence

- 网表回读：Re-export configuration A and trace R1.；预期：R1 is fitted between A and U1.2.

**证据定位**

关联检查：C3

- synthetic-fixture.json：all values stipulated for tests

## 3. 建议项及处理方法

### suggestion | F-103 | 库字段与Value不一致，实际脚位已核一致

位置：PDF 1 页；R1；网络 A

**证据与处理状态**

已核实偏离，影响限于所述工况；尚未关闭

**连接与参数**

物料表的库字段与Value使用不同名称；本例已经规定真实物理脚映射一致。

**设计依据**

SYN-SPEC §4：受控物料字段应能追溯到同一完整型号。

**风险原因与影响**

影响后续选料和文档维护，当前没有焊盘或接线错误证据。

**触发条件**

更新BOM或复用器件库时。

**问题来源**

库字段没有同步更新。

**归类理由**

当前仅为物料字段整理，没有实际焊盘或接线错误证据。

**冻结判断依据**

- 合成测试规定：除error外，本项不属于当前冻结必需条件。

**修改目的与准备度**

整理合成物料/文档记录；编辑细节已明确，尚不代表实际修改

**详细修改步骤**

核对原始受控记录，再更新对应文档或属性。

1. 合成物料表 R1：对应资料未统一 → 依据受控记录完成一致性核对。核对完整物料、真实脚位和字段；仅在身份确认后修正文档。

**联动检查**

Confirm the stipulated reset load remains within its limit.

**复验与完成条件**

修订文件能对应受控记录；已核实的物理脚到网络映射不变。

- 文档：核对修订文档与受控记录，并比较物理脚到网络映射。；预期：资料出处可定位且未改变已核实的电气映射。

**证据定位**

关联检查：C4

- synthetic-fixture.json：all values stipulated for tests

### suggestion | I-101 | 清理与正确连线并存的残留属性

位置：PDF 1 页；R1；网络 A

**证据与处理状态**

已知判据已满足后的可选改善；尚未关闭

**连接与参数**

实际连线及工作条件已在合成设定中判定符合，仅有多余的图纸属性残留。

**设计依据**

SYN-SPEC §6：电气判据已满足，文档可进一步清理。

**风险原因与影响**

只影响图纸可读性与后续维护。

**触发条件**

阅读和整理图纸时。

**问题来源**

旧属性未清理。

**归类理由**

已知电气判据已满足后的图纸整理。

**冻结判断依据**

- 合成测试规定：除error外，本项不属于当前冻结必需条件。

**修改目的与准备度**

整理合成物料/文档记录；编辑细节已明确，尚不代表实际修改

**详细修改步骤**

核对原始受控记录，再更新对应文档或属性。

1. 合成物料表 R1：对应资料未统一 → 依据受控记录完成一致性核对。核对完整物料、真实脚位和字段；仅在身份确认后修正文档。

**联动检查**

Confirm the stipulated reset load remains within its limit.

**复验与完成条件**

修订文件能对应受控记录；已核实的物理脚到网络映射不变。

- 文档：核对修订文档与受控记录，并比较物理脚到网络映射。；预期：资料出处可定位且未改变已核实的电气映射。

**证据定位**

关联检查：C6

- synthetic-fixture.json：all values stipulated for tests

### suggestion | R-102 | 补齐完整物料及封装依据

位置：PDF 1 页；R1；网络 A

**证据与处理状态**

整体结论尚缺证据，后果未被证实；尚未关闭

**连接与参数**

本例未给完整订货后缀及对应封装文件；未观察到具体接线或工作范围违规。

**设计依据**

SYN-SPEC §5：完整物料身份与封装应有受控出处。

**风险原因与影响**

身份确认尚未完成；不能断言实际物料错误或电路失效。

**触发条件**

采购确认或封装复用时。

**问题来源**

资料包缺少受控身份记录。

**归类理由**

仅缺完整物料资料；保留未知，不认定电路已失效。

**待补齐的条件**

- 完整订货后缀与封装版本

**冻结判断依据**

- 合成测试规定：除error外，本项不属于当前冻结必需条件。

**修改目的与准备度**

整理合成物料/文档记录；前提满足后才能确定修改

**前提：完整订货后缀与封装版本**

确认身份对应

取得方式：从合成采购规格和封装记录取得

采用条件：完整型号、封装和资料版本一致

**详细修改步骤**

核对原始受控记录，再更新对应文档或属性。

1. 合成物料表 R1：对应资料未统一 → 依据受控记录完成一致性核对。核对完整物料、真实脚位和字段；仅在身份确认后修正文档。

**联动检查**

Confirm the stipulated reset load remains within its limit.

**复验与完成条件**

修订文件能对应受控记录；已核实的物理脚到网络映射不变。

- 文档：核对修订文档与受控记录，并比较物理脚到网络映射。；预期：资料出处可定位且未改变已核实的电气映射。

**证据定位**

关联检查：C5

- synthetic-fixture.json：all values stipulated for tests

## 4. 索引与完整台账

| 等级 | ID | 内容 |
|---|---|---|
| error | F-101 | 已证实接口电压超过保证范围 |
| warning | F-102 | 局部非关键指示通道未满足应用条件 |
| warning | R-101 | 已知参数裕量偏窄，尚缺动态工况 |
| suggestion | F-103 | 库字段与Value不一致，实际脚位已核一致 |
| suggestion | I-101 | 清理与正确连线并存的残留属性 |
| suggestion | R-102 | 补齐完整物料及封装依据 |

完整逐项检查、覆盖、候选处置、版本指纹与独立交接保存在随附review-results.json和review-gate.json；计算与图面证据按条目路径回读。

报告及校验通过不表示已经修改电路或完成实物验证。
