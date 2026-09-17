# 改版网表 Diff 与历史意见闭环

按 [SKILL.md](../SKILL.md) 的复审步骤解析旧/新网表并运行 Diff。输出器件新增/删除/字段变化、
引脚换网和网络成员变化；伪网络不参与成员 Diff，但引脚从伪 NC 转入真实网络仍保留变化记录。

## 闭环断言格式

claim id 唯一；ref/node/net/field 等定位字段完整。器件字段仅允许 part、value、jedec、prim、nc；
nc 的期望值为布尔值，其余为字符串。结构不合法时直接退出，不进入判定。

下例全部为合成设定：U3.9 已由引脚表核实为地，旧上拉 R40 停贴；R41 以 10K 1% 贴装，
两端分别连接 U3.8 与 U3.9。实际项目须用当前数据库的准确值字符串和装配 BOM，并核查全部支路。

```json
{
  "schema_version": 1,
  "claims": [{
    "id": "F-07",
    "description": "旧上拉 R40 停贴，R41 的贴装、阻值及下拉两端符合指定修改",
    "expect": [
      {"kind": "part_field_equals", "ref": "R40", "field": "nc", "value": true},
      {"kind": "part_field_equals", "ref": "R41", "field": "nc", "value": false},
      {"kind": "part_field_equals", "ref": "R41", "field": "value", "value": "10K 1%"},
      {"kind": "pins_connected", "nodes": ["U3.8", "R41.1"]},
      {"kind": "pins_connected", "nodes": ["R41.2", "U3.9"]},
      {"kind": "pins_disconnected", "nodes": ["R41.1", "R41.2"]}
    ]
  }]
}
```

最后一条仅排除跨 R41 的同网/已贴 0Ω 旁路，不能排除任意阻性或有源支路。
上述断言证明指定编辑的结构状态；仍须复算内部拉阻、漏电、全部外部支路、采样门限和时序，
将专项 ER 复验证据关联 F-07 后才可关闭功能问题，不能从网名或换线推断低电平已保证。

## 支持的断言及边界

- `part_added` / `part_removed`：核对指定器件新增或删除。
- `part_field_changed` / `part_field_equals`：核对字段变化或期望值。
- `pin_net_changed` / `pin_net_equals`：核对物理脚换网或指定网络归属，不证明该网的电气作用。
- `net_membership_changed`：核对网络成员变化。
- `pins_connected` / `pins_disconnected`：`nodes` 必须是两个物理脚，只比较同网或已贴 0Ω
  通路；不跨非零电阻、二极管、开关或电容。目标缺失/处于伪网不能证明断开。
- `net_members_equal`：给 net 和完整 nodes 列表，核对准确成员。

全部断言通过才报告该 claim 的 PASS；否则输出 Rule-17 FINDING。仅含字段/换网/成员变化的
claim 为 INSUFFICIENT，并在 `--fail-on-open-claims` 时阻断；必须补期望状态断言。
Diff 的 PASS 只覆盖声明的状态，不代替额定、方向、时序和其他受影响电路的工程复验。

具体复验范围由计划的 [改版影响清单](revision-impact-schema.md) 关联并校验；原有结构
Diff/断言仍独立保留。未发现结构差异不能排除装配意图、判据或资料内容变化。
