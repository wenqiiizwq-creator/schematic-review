# 网表解析规范（Cadence allegro 三件套 + 其他 EDA 适配）

## 一、输入文件

| 文件 | 作用 | 获取方式 |
|---|---|---|
| 原理图 PDF | 页面结构、图形复核 | EDA 导出 |
| `pstxnet.dat` | 展开网表（核心数据源） | Cadence PSTWRITER 导出 allegro 网表 |
| `pstxprt.dat` | 器件实例清单（位号→原语） | 同上 |
| `pstchip.dat` | 原语库（型号/封装/参数值/引脚映射） | 同上 |
| `netlist.log` | **导出日志——免费证据，勿丢** | 同上，与三件套同目录 |

非 Cadence 工具链（PADS/Altium/KiCad）提供等效网表导出即可，解析规则按格式改写；方法论其余部分不变。

## 二、目标索引

- `parts`：位号 → {part（型号）, jedec（封装）, value（值）}
- `nets`：网络名 → [位号.引脚号, …]
- `pin2net`：位号.引脚号 → 网络名
- `pinname`：Uxxx.2A2 → "VCCIO2_VCC"（引脚功能名，藏在 `CDS_PINID` 字段，是识别 SoC 电源球/信号球身份的关键）

## 三、格式要点

- `pstxnet.dat`：`NET_NAME` 行独占一行，下一行是带引号的网络名；随后 `NODE_NAME\t<refdes> <pin>` 逐节点；`CDS_PINID` 给出引脚功能名。
- `pstxprt.dat`：` <refdes> '<primitive>':;` 每实例一行；primitive 名或 VALUE 带 NC 标记 = 该实例不贴（实例级 NC 信息只在这里，库不含）。各家命名不同，`/NC` 与 `_NC` 后缀都在用（`0R/1%/NC`、`0R/1%_NC`），`is_not_populated()` 只在 NC 被 `/ _ - 空格` 或串首尾界定时才判为不贴——否则 `NCP1117` 这类型号会被误判成不贴。**该标志漏判会静默放大**：把不贴的 0R/上拉当成已贴，Rule-12/Rule-16 的默认态判定和 Rule-05 的驱动判定都会跟着错。
- `pstchip.dat`：`primitive '<name>'; ... end_primitive;` 块内含 pin 名→PIN_NUMBER 映射（符号审计用）、PART_NAME/JEDEC_TYPE/VALUE。

## 四、解析实现

**直接运行脚本，不要照抄代码**：

```bash
python3 scripts/parse_netlist.py <allegro目录> -o db.json
```

### 4.1 pinname 有两种布局——这是最容易踩的坑

引脚功能名（`Uxxx.2A2 -> "VCCIO2_VCC"`）随 PSTWRITER 版本有两种放法：

| 变体 | 形态 |
|---|---|
| A | `NODE_NAME` 之后的属性行里带 `'\NAME\':CDS_PINID` |
| B | `NODE_NAME` → 实例行 → **紧接一行 `'NAME':;`** |

只处理其中一种，另一种会得到**空的 pinname 索引**。后果是静默的：
Rule-05（电源球无驱动）与 Rule-06（VSS 未入地）依赖 pinname，会扫出 0 条命中，
报告写成"数百个电源球全扫通过"而实际一个都没查过。

**实测**：某板 `pstxnet.dat` 中 `CDS_PINID` 出现 0 次，只按变体 A 解析得到
pinname = 0 条（应为 6047 条）。

`scripts/parse_netlist.py` 两种都试，**并在覆盖率低于 50% 时报错退出**——
宁可中断，也不交出一份静默残缺的索引。

### 4.2 伪网络自动识别

导出器会把"带 No-Connect 属性且无连线"的引脚汇集到一张名为 `NC` 的网。
它不是电气短路。脚本按 `C_SIGNAL` 是否带层次路径自动判别，结果放进
`pseudo_nets`（判别式详见 `lint-rules.md`）。

### 4.3 输出索引

| 键 | 含义 |
|---|---|
| `nets` | 网络名 → [refdes.pin, …] |
| `pin2net` | refdes.pin → 网络名 |
| `pinname` | refdes.pin → 引脚功能名（识别 SoC 电源球/信号球身份的关键） |
| `pintype` | refdes.pin → PINUSE（POWER/GROUND/…） |
| `parts` | refdes → {prim, part, jedec, value, **nc**}；`nc=True` 表示该实例不贴 |
| `ref2page` | refdes → 页号 |
| `pseudo_nets` | 工具生成的伪网络名 |

Cadence 的 `PIN_NUMBER` 可能是 BGA 字母数字脚号，且 `PINUSE` 与 `PIN_NUMBER` 之间
可能夹有其他属性；随附解析器按完整 pin block 提取，不依赖两行相邻。`pintype` 缺失或
覆盖不足时，Rule-19 必须报告 SKIPPED/部分执行，不能把 0 命中写成通过。

最小索引只支持部分拓扑检查；完整审查还需要 pinname/物理脚/页映射、BOM 和官方条款；
改写 `parse_*` 函数即可，其余脚本无需改动。


## 五、PDF 处理

- `pdftotext -layout sch.pdf out.txt`，按 `\f` 换页符分页；每页抓 `File:` 字段得页面标题 → 实际页面清单（目录页常过期，不可信）。
- 单页渲染读图：`pdftoppm -png -r 150 -f N -l N sch.pdf page`（引脚图/表格细节用 `-r 200` 以上并裁剪局部）。
- 扫描件 datasheet（无文本层）必须渲染目检。
- **硬规则：提取出的表格出现列错位，必须渲染目检后才允许下结论。** 判别信号——同一行里出现本属不同列的值、
  水印文字插进数据行、单元格合并处的值漂到相邻行。实测两次栽在这上面（订购信息表读错变体档位、
  电源时序表读不出参数），两次都是渲染后才改对结论。
- **旋转页坐标换算**（`Page rot: 90` 的图纸/datasheet 很常见）：用 `pdftotext -bbox` 拿到目标文字的
  `(x, y)`，注意该坐标已在旋转后的横向坐标系里（宽=页面 height）。映射到 `pdftoppm` 输出的像素：
  `px = x / page_h * img_w`，`py = y / page_w * img_h`。先按此裁一小块验证命中，再放大读图。
- 比对 PDF 生成时间与网表导出时间，防止拿旧图审新版。

## 六、其他 EDA 适配

本 skill 当前只随附 Cadence/OrCAD 三件套解析器；下列是适配输入路线，不代表仓内已有
对应脚本。适配后必须生成同一 db.json 契约并建立格式 fixture/覆盖自检。

- KiCad：`.kicad_sch` 本身即文本（S 表达式），可直接解析；或用 `kicad-cli sch export netlist`。
- Altium：导出 EDIF/Protel 网表，按 `(` 分组解析。
- PADS：ASCII 网表 `*SIGNAL*` 段。
- 适配后按每条规则输入依赖确定覆盖范围，缺引脚名/类型/页映射不能宣称完整适配。

## V2 完整性与适用范围

解析器保留 `declared_pinname` / `declared_pintype`：来自符号 primitive 的完整脚表（含未连接脚），
不是官方封装定义。必须与 `pin2net` 及官方 pinout 双向差集，解析过的引脚覆盖率不等于物理脚覆盖率。
自检拒绝跨网重复物理脚、索引不互反、网络引用缺失器件、缺失 primitive 及导出日志 ERROR/中止。
`--no-strict` 仅供诊断，输出 integrity.self_check_passed=false 的数据不能准出。
缺日志仍需在输入一致性项记录，不能假定导出成功；确认是历史追加日志时分离本次导出记录另行留证。

普通无层次 C_SIGNAL 不再被当伪网；仅已支持 PSTWRITER 格式的 NC 名称可被候选识别。
没有告警或告警不在该网只能作辅助线索，不能独立证明 NC 一定不短路；遇陌生导出格式须
对真实 NC 标签与 No-connect 属性构建小样本/读图交叉验证，再允许依赖 pseudo_nets 排除。
DNP/DNI/DNF/NC 只按分隔词识别为不贴，不能误判 NCP1117 型号。实际装配 BOM 优先核实，
命名未标识也不证明一定贴装。
