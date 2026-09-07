# ER1 datasheet 结构化证据

运行 “lint.py --evidence evidence.json” 只执行有明确官方出处的热跑检查。每条检查
必须给 id、rule、kind 和 citation；引用至少包含文档名、版本、页码或表号。缺字段会
直接退出，避免残缺判据被误读成通过。

id 必须唯一；数值必须有限，min 不得大于 max，`resistor_tolerance` 取 `[0, 1)`。

## 顶层格式

    {
      "schema_version": 1,
      "checks": []
    }

## Rule-08：分压最坏情况窗口

    {
      "id": "U1-FB",
      "rule": "Rule-08",
      "kind": "divider",
      "net": "U1_FB",
      "vref": {"typ": 0.8, "min": 0.792, "max": 0.808},
      "resistor_tolerance": 0.01,
      "expected": {"min": 3.25, "max": 3.35},
      "citation": "U1 datasheet Rev.B p.18 Eq.1, Table 6"
    }

电阻 VALUE 中写出的公差优先；resistor_tolerance 只补齐未写公差的器件，须在 citation 中给出 BOM/规格依据。两处均未提供时只输出待核项，不能把脚本默认 1% 当作已验证的 WCA。

## Rule-09：必需上拉/下拉或串阻

    {
      "id": "I2C-SCL-PULL",
      "rule": "Rule-09",
      "kind": "required_pull",
      "net": "I2C_SCL",
      "direction": "up",
      "to": "VCC_1V8",
      "resistance_ohm": {"min": 1000, "max": 4700},
      "citation": "SoC HDG v1.4 p.92"
    }

kind=required_series 时，从 net/node 向另一信号网查找串联电阻；可用 to 限定另一端。

## Rule-12：使能默认态与耐压

    {
      "id": "U2-EN",
      "rule": "Rule-12",
      "kind": "pin_bias",
      "node": "U2.4",
      "required_default": "low",
      "abs_max_v": 5.5,
      "citation": "U2 datasheet Rev.C p.5 Table 2; p.11 Pin Functions"
    }

required_default 取 high、low 或 float。轨名推不出电压时输出 CANDIDATE，不会把
未知值判成通过；同一节点同时存在上下拉时也输出 CANDIDATE，等待阻值与输入阈值判定。
`CANDIDATE` 是 AC0 中间产物，不是最终结果；到报告时仍未补齐判据的适用项写
`INSUFFICIENT`。

## Rule-14：符号引脚映射

    {
      "id": "J1-PINMAP",
      "rule": "Rule-14",
      "kind": "pin_map",
      "ref": "J1",
      "expected": {"1": "VBUS", "2": ["D-", "DM"], "3": ["D+", "DP"], "4": "GND"},
      "citation": "Connector J1 drawing Rev.A p.2"
    }

## Rule-16：strap 强制态

    {
      "id": "U3-BOOT0",
      "rule": "Rule-16",
      "kind": "strap",
      "node": "U3.8",
      "required": "low",
      "citation": "U3 datasheet Rev.D p.31 Table 9: must be pulled down"
    }

机器证据文件是 ER1 的输出，不替代原始 datasheet。报告仍须保留原文强制词和出处。

## V2 使用边界

citation 的语法存在不证明引用正确。按 coverage-protocol.md 先核准确 MPN、封装、工况与章节。
每条 pin_map 只覆盖 expected 中列的物理脚；不能用一脚 PASS 声称整颗 IC 已核。
Rule-09 的存在性检查不是总线上拉 WCA；并联/串联路径、内置上拉、缓冲器与掉电状态需专项审查。
Rule-12/16 的简单偏置检查不是 SPICE/时序模型；上下拉并存保持候选，不能将上拉源轨当作脚压。
`hot_uncovered_instances` 揭示计划中仍未获得证据的实例；unknown 分支不能隐藏在 hot_executed 后。
Rule-08 缺基准/电阻公差只能筛查标称值，不能按默认零误差声称最坏情况通过。
