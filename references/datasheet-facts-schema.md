# 项目内 Vref 参数复用（schema_version=1）

仅用于 Rule-08。资料事实存入项目审查目录的 `datasheets/facts.json`，原始 PDF 一并留档；
事实记录不含位号、电阻网络、审查结论或整板 PASS。每次使用绑定当前对象/状态并重算。
脚本不下载、不自动提取 PDF，也不按 LLM 置信度或完整性评分决定参数正确性。

## 1. 核对原件并记录事实

完整核对电气表头、保证值、适用温度定义、供电/负载、模式、脚注和订货表。
`VERIFIED` 只是已完成原件核对的记录，不是脚本证明了语义真实。缺项保存为
`UNVERIFIED`，未知数值用缺字段或 `null`；不填零、不以 typ 代替 min/max。

下面是**合成格式示例，不是真实器件规格**；哈希必须由实际 PDF 计算后替换。

```json
{
  "schema_version": 1,
  "facts": [{
    "id": "REG-X-QFN16-VREF-FULL-RANGE",
    "parameter": "vref",
    "mpn": "REG-X-QFN16-I",
    "package": "QFN-16",
    "unit": "V",
    "values": {"min": 0.792, "typ": 0.8, "max": 0.808},
    "guaranteed": true,
    "conditions": {
      "temperature_c": {"min": -40, "max": 85},
      "temperature_basis": "junction",
      "vin_v": {"min": 3, "max": 5.5},
      "load_a": {"min": 0, "max": 1},
      "mode": "regulating",
      "qualifiers": {}
    },
    "raw_conditions": "Synthetic table: TJ=-40..85 C, VIN=3..5.5 V, IOUT=0..1 A, regulating. No other restrictions.",
    "source": {
      "path": "REG-X.pdf",
      "sha256": "replace-with-actual-64-character-lowercase-sha256",
      "document_model": "REG-X family",
      "document_version": "Rev.A",
      "locator": "PDF p.7, Electrical Characteristics, VFB row; ordering table p.18",
      "footnotes": []
    },
    "verification": {
      "status": "VERIFIED",
      "conditions_complete": true,
      "note": "Synthetic example only; replace with actual table/header/footnote and ordering-code verification record."
    }
  }]
}
```

- `source.path` 相对于 **facts.json 所在目录**，也可为绝对路径；复制 PDF 但内容相同可复用。
- ID 在文件内唯一；每条记录只描述一个完整订货型号、封装、文档版本和适用行。
  不按去后缀、大小写归一化或“同系列”匹配。系列 PDF 标题不必等于 MPN。
- 数值只接受有限正数，单位必须 `V`。`guaranteed=true` 必须有原文保证依据；
  曲线估读、典型统计、样品值不能标成保证值。
- 三个范围都是闭区间，且请求的**整个范围**必须被同一条记录覆盖；不拼接多行范围。
  `temperature_basis` 明确结温/环境温度；脚本不作热转换。
- `mode`、`qualifiers` 精确相等。频率、档位、测试连接等额外限制用双方一致的 qualifier
  键值记录；没有时显式 `{}`。这只是精确条件匹配，不解析自由文本或做单位换算。
- `raw_conditions` 保留原始条件；`footnotes` 保留适用脚注（无则 `[]`），并将其限制
  编码进 conditions。无法完整表达时令 `conditions_complete=false`，改走人工核验。
  不能只填 true 来绕过尚未理解的脚注。完全缺少工况的提取草稿先留在阅读笔记中。
- 同时有多条适用、已核实的保证时，输出歧义及 ID，不按列表顺序、置信度、最新日期或
  更窄范围静默择一。回原件明确行的适用性，保留更正记录后再用。

## 2. 为本次检查声明身份与请求工况

仍按 [热跑证据契约](datasheet-evidence-schema.md) 建 `basis` 和全部依赖。
在目标 `basis.sources` 项添加 `mpn`、`package`：它们是按当前 BOM/订货表核实的精确
身份，**不是**工具自动识别的结果。原 `identity_resolution` 必须解释 VALUE/PART/PRIM/
JEDEC 与真实订货码/封装的对应及冲突解决；仅把事实里的型号复制过来不算身份核对。
脚本还比较 audit 中该位号的 BOM 字段快照，字段改变后要重新审计，不能只刷新 db 哈希。

本版自动复用要求 `mpn` 精确等于 audit 所选的当前 BOM `value` 或 `part`，不接受仅从
PRIM 得到型号；`package` 精确等于当前 `jedec`。事实中的 package 使用同一封装标识，
与原厂封装名称的对应仍须原件核实。BOM 字段为别名、缺封装或冲突尚待解决时，
继续走原有手工证据流程，不为通过匹配而修改原始 BOM/网表或猜测别名映射。

Rule-08 检查加以下请求，可暂不填写 `vref`：

```json
"vref_request": {
  "ref": "U1",
  "conditions": {
    "temperature_c": {"min": -40, "max": 85},
    "temperature_basis": "junction",
    "vin_v": {"min": 3.3, "max": 5},
    "load_a": {"min": 0.1, "max": 0.8},
    "mode": "regulating",
    "qualifiers": {}
  }
}
```

请求必须来自本次真实工况，与 `basis.state`、需求和实际配置相符，不从状态文字猜测。
目标必须是本检查反馈器件；多对象/多工况各自建检查 ID，多个 ID 可以复用同一 fact。

## 3. 生成证据，再用原热跑流程

以下命令的脚本路径按技能安装位置调整，输入/输出路径指向项目审查目录。
输出和报告必须使用**尚不存在的新文件名**，不要将项目证据写进技能仓库。
首次使用请求文件时，先让 `audit_datasheets.py --evidence evidence-request.json`
纳入该文件的依赖并更新 audit，再执行物化；其余资料补取和身份核对顺序不变。

```sh
python3 scripts/datasheet_facts.py materialize db.json \
  --facts datasheets/facts.json --evidence evidence-request.json \
  --datasheet-audit datasheet-audit.json \
  --out evidence.json --report vref-materialization.json
```

成功项填入现有 `vref.min/typ/max`，保留原来的来源/偏置电流依据，补充
`basis.sources[].parameter_locators.vref` 和 `vref_binding`（事实文件绝对路径、ID、
单条记录哈希、对象/工况/来源上下文哈希）。未解决项清除旧 vref 和绑定，保留请求及缺口；
退出 2，并在报告 `affected_check_ids` 中列出需要处理的检查，不回退到旧缓存数值。
退出 0 仅表示请求的 Vref 已物化，不保证偏置/公差/模型就绪，更不是电气 PASS。

再按 SKILL.md 对生成的 evidence.json 执行原有 lint/计划合并与独立结果校验。
计划和热跑每次都重读事实、复核 PDF 内容和唯一适用性；值被手改、记录变化、新增冲突
记录、对象/状态/条件/来源变化、PDF 缺失或变更均使使用者回到待核。原来的依赖、
公差、模型及准出门保留，旧的手工 evidence 未使用事实字段时行为不变。
资料审计、物化、检查、计划和热跑共用严格 JSON 读取器，重复键不会被“最后一个值”
静默覆盖；该类歧义输入退出 2，不生成新结果。物化与下游也共用物理脚/网络一致性检查。

## 4. 查看失效影响

```sh
python3 scripts/datasheet_facts.py check db.json \
  --evidence evidence.json --datasheet-audit datasheet-audit.json \
  --report vref-impact.json
```

只读输入，列出**这份 evidence 中**所有事实使用者及失效原因；不会扫描其他项目，
不会修改历史结果或自动准出。PDF/事实/工况变更后先核对原件、更新事实及 audit/basis，
再生成新版本 evidence，重新热跑并复核受影响的最终结果；不能只刷新哈希解除阻断。
项目搬迁后重新物化以更新绝对路径。事实和原件持久保存在项目，不放进全局技能缓存。
