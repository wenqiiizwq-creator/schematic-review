# schematic-review

面向硬件原理图首审、冻结前审查、PDF更新复审与历史意见闭环的可复用 Agent skill。
V2.2 将逐脚/网表审查、原厂证据、工况与参数计算，整理成能按步骤修改和复验的报告。

严重程度只用 **error / warning / suggestion**。物料二义、封装文字与Value对应、
库字段和一般资料整理默认列suggestion；已经证实的实际焊盘/接线错误仍按电气后果判断。
每项说明归类依据，未知后果不当成已确认故障，样机一次成功不覆盖保证范围违规。

报告开头最多一页，PDF第2页进入详细问题。全部条目保留定位、依据、风险原因、
旧→新连接、顺序修改、参数选择与复验；计算、覆盖、历史、交接及完整索引放在详情之后。

## 使用与安装

将完整仓库放入 Agent 可发现的技能目录，入口是[SKILL.md](SKILL.md)：

    git clone https://github.com/wenqiiizwq-creator/schematic-review.git <你的skills目录>/schematic-review

请求示例：

    使用 schematic-review 审查这个设计资料包，按 error、warning、suggestion 分级，
    给出证据和可执行修改步骤。前置说明简短，完整展开全部问题与风险。

    用新版PDF复审上一版报告，检查网表/图面是否同版，沿变更依赖复验并保留历史ID。

    仅重新校准报告等级和结构，物料与字段整理归suggestion，保留技术事实和未决条件。

完整网表模式使用原理图PDF、有效网表、BOM/装配配置、需求/接口定义与核心器件资料。
缺项继续完成可做检查；仅PDF时受限读图，不声称完成机器网表全覆盖。
随附解析器支持Cadence/OrCAD PST三件套，其它EDA需要验证适配器。

## 审查与生成

Python脚本仅依赖标准库。PDF读取/渲染使用环境现有工具，不能把Markdown成功当成PDF排版通过。
项目输入、工作台账和证据存放在独立审查目录；下列scripts路径按安装位置调整：

    python3 scripts/parse_netlist.py <项目>/allegro -o db.json
    python3 scripts/plan_review.py db.json --intent intent.json --json review-plan.json
    python3 scripts/lint.py db.json --log <项目>/allegro/netlist.log --intent intent.json --json lint-cold.json
    python3 scripts/audit_datasheets.py db.json --datasheet-dir <器件资料目录> --json datasheet-audit.json

按[资料补取契约](references/datasheet-resolution-schema.md)核对型号/版本并补齐记录后：

    python3 scripts/audit_datasheets.py db.json --datasheet-dir <器件资料目录> --resolution datasheet-resolution.json --evidence evidence.json --json datasheet-audit.json
    python3 scripts/lint.py db.json --intent intent.json --evidence evidence.json --datasheet-audit datasheet-audit.json --plan-json review-plan-hot.json --json lint-hot.json

Agent完成工程审查并填写schema_version=3的review-results.json，再校验、生成：

    python3 scripts/validate_review.py review-plan.json review-results.json --db db.json --lint lint-cold.json --lint lint-hot.json --require-actionable --json review-gate.json
    python3 scripts/render_report.py review-plan.json review-results.json --db db.json --lint lint-cold.json --lint lint-hot.json --output report.md --csv issues.csv
    python3 scripts/diff_netlists.py old-db.json db.json --claims review-claims.json --json diff.json --fail-on-open-claims

生成器先验证台账，按统一ID生成全部详情和三等级CSV；不自动判电气正确，也不自动从Lint生成PASS。
PDF按[报告模板](references/report-template.md)分页、渲染并目检所有页面后交付。

## 数据与兼容

- 新台账schema_version=3：所有偏离、风险和改善统一放findings，severity仅三个等级，
  每项都有归类理由和详细remediation；风险保留INSUFFICIENT与缺失输入。
- 技术结果、证据、处理状态、阻断理由和专业交接独立于严重程度。
  error未修复不能准出；降低等级不能自动消除阻断或把资料不足变为通过。
- 旧schema_version=2继续按原契约校验；迁移必须逐项重评，不机械替换标签，不覆盖历史。
- 报告生成拒绝覆盖输入台账；重建报告不代表源设计已经修改。
- 保留逐物料资料审计、文档/网表指纹、单电路/状态计划、保守分压求解及电气检查。
  READY、hot_executed和零命中均不等于全量通过。

validate_review退出0表示台账有效，电路仍可能不满足冻结条件。
需要冻结门时使用--require-release；结果仍需工程负责人核实。
本skill不签署PCB布线、SI/PI、EMC、实测热或生产准出，也不承诺发现物理电路全部未知缺陷。

## 可运行的合成示例

    python3 scripts/validate_review.py examples/three-level/plan.json examples/three-level/review-results.json --db examples/three-level/db.json --require-actionable
    python3 scripts/render_report.py examples/three-level/plan.json examples/three-level/review-results.json --db examples/three-level/db.json --output /tmp/review-example.md --csv /tmp/review-example.csv
    python3 -m unittest discover -s scripts/tests -v

示例应得error 1项、warning 2项、suggestion 3项，台账有效但存在未修复error。
[示例正文](examples/worked-example-industrial-gateway.md)只展示合成数据，不是器件设计依据。

## 文件导航

| 文件 | 用途 |
|---|---|
| [SKILL.md](SKILL.md) | 完整执行入口 |
| [分级校准](references/severity-calibration.md) | 三等级判断、误判边界与历史迁移 |
| [报告模板](references/report-template.md) / [修改说明](references/remediation-guide.md) | 简短前言、完整细节和复验 |
| [结果契约](references/review-results-schema.md) | v3字段、技术结果与闸门 |
| [覆盖协议](references/coverage-protocol.md) / [审查清单](references/review-checklist.md) | 对象、状态、需求和逐域审查 |
| [公式](references/wca-formulas.md) / [边界](references/scope-boundary.md) | 参数复算与专业交接 |
| [资料证据](references/datasheet-evidence-schema.md) / [补取](references/datasheet-resolution-schema.md) | 型号、文档及热跑依赖 |
| scripts/tests/ | 脱敏合成回归与结果、报告校验 |

公开仓库只包含通用方法、工具和合成用例。真实设计、BOM、私有资料与审查产物保留在项目目录。
MIT License，见[LICENSE](LICENSE)。
