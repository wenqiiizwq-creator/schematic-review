# 电路评测基线

被测版本：`2aeaee5d4a0a42847ac264315ca0e37ae59d078d`。评测时间：2026-09-14T08:29:44Z。

共 166 例，166 例状态及数值符合预期；错误 PASS 0，误报 0，运行错误 0。

| 分组 | 案例 | 符合预期 | 已知缺陷未定判 | 已知正常未定判 |
|---|---:|---:|---:|---:|
| feedback_bridge | 18 | 18 | 0 | 0 |
| feedback_parallel_series | 18 | 18 | 0 | 0 |
| feedback_series_lower | 18 | 18 | 0 | 0 |
| feedback_two_resistors | 18 | 18 | 0 | 0 |
| pull_parallel_1 | 18 | 18 | 0 | 0 |
| pull_parallel_2 | 18 | 18 | 0 | 0 |
| pull_parallel_3 | 18 | 18 | 0 | 0 |
| rc_charge | 18 | 18 | 0 | 0 |
| rc_discharge | 18 | 18 | 0 | 0 |
| ti_slva689_example | 4 | 4 | 0 | 0 |

## 范围

- 原始合成 PST 三件套经现有解析器解析，并核对独立构造的网络/引脚/贴装索引。
- 分压与上拉：被测脚本自己计算；期望数值来自独立 KCL 求解器。
- RC：评测框架提供采样电压窗口，工具只完成证据就绪与门限判断；不计作自动瞬态分析。
- 合成规格文档验证证据绑定流程，不验证真实 datasheet 的读取或原图视觉理解。
- challenge 为超出现有串并联求解范围的桥式电阻网络。安全返回未定判仍是能力缺口。
- 留出集按电路/来源家族划分，但公开且已经用于本次基线；未来严格盲测需新案例。
- 未包含私有真实项目、完整 Agent 审查、PCB 或实测签署；本结果不表示全板正确率。

## 未达到期望的案例

本次所测案例均符合预期；仍受上述范围限制。

## 升级门

- 既有正常/缺陷/缺证案例不得回归；禁止错误 PASS；数值与状态同时核对。
- 新旧版本须使用同一数据、答案和评测程序指纹；`--baseline` 输出逐案例差异。
- challenge 的进步单列；不能靠删案例、改答案或把所有结果改为 INSUFFICIENT 得分。
- 有效用例增长需要新的数据版本；本基线不自动触发工具修改或发布。

比较结果：`{"comparable":true,"improved_ids":["CB-0055","CB-0056","CB-0057","CB-0058","CB-0064","CB-0065","CB-0066","CB-0067"],"meaning":"Eligible for reviewed tool upgrade; no automatic installation or board signoff","promote":true,"regressed_ids":[]}`
