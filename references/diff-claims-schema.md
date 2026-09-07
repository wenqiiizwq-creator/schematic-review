# 改版网表 Diff 与历史意见闭环

先分别解析旧版和新版网表，再运行：

    python3 scripts/diff_netlists.py old-db.json new-db.json \
      --claims review-claims.json --json diff.json --fail-on-open-claims

Diff 自动列出器件新增/删除/字段变化、引脚换网和网络成员变化。工具伪网络默认不参与
网络成员 Diff，但引脚从伪 NC 转入真实网络等变化仍会出现在 pin change 中。

## 闭环断言格式

claim id 必须唯一；ref/node/net/field 等定位字段必须完整，字段名仅允许
part、value、jedec、prim、nc。声明结构不合法时工具直接退出，不进入闭环判定。

    {
      "schema_version": 1,
      "claims": [
        {
          "id": "F-05",
          "description": "U10 器件档位已改为 RTL8326BI",
          "expect": [
            {"kind": "part_field_changed", "ref": "U10", "field": "part"},
            {
              "kind": "part_field_equals",
              "ref": "U10",
              "field": "part",
              "value": "RTL8326BI"
            }
          ]
        },
        {
          "id": "F-07",
          "description": "U3.8 已从 3V3 上拉改到下拉网络",
          "expect": [
            {"kind": "pin_net_changed", "node": "U3.8"},
            {"kind": "pin_net_equals", "node": "U3.8", "net": "BOOT0_PD"}
          ]
        }
      ]
    }

支持的 kind：

- part_added / part_removed
- part_field_changed / part_field_equals
- pin_net_changed / pin_net_equals
- net_membership_changed

一条历史意见只有在其全部断言通过时才是真闭环；否则输出 Rule-17 FINDING。

## V2 电气状态断言

新增 `pins_connected` / `pins_disconnected`，给 `nodes: ["U1.1", "U2.2"]`：只比较同网或
经过已贴 0Ω 的通路，不跨二极管/开关/电容；目标缺失或在伪网时不能把“查不到”当断开验证。
新增 `net_members_equal`，给 net 和 nodes 完整列表，用于精确成员检查。

只含 part_field_changed / pin_net_changed / net_membership_changed 的 claim 现在输出
INSUFFICIENT，并在 --fail-on-open-claims 时阻断。必须增加期望状态断言；发生变化不证明修好。
复杂功能修复还需要专项 ER 复验，简单连通断言不证明额定/方向/时序正确。
