# Circuit Bench 1.0：电路检查工具评测

用于衡量 schematic-review 的检查脚本是否改进，不是全板准出工具，也不是已经启动的自主 RSI。
先冻结输入、答案和评测协议，再比较候选工具；不让候选修改自己的考试答案。

## 本版交付

- 166 例：90 开发、58 留出、18 挑战；10 个拓扑/来源家族。
- 预期状态：19 PASS、62 FAIL、85 INSUFFICIENT。
- 162 个合成案例，加 4 个 TI 官方算例条件下的数值重建，见 [来源说明](SOURCES.md)。
- 原始合成 Cadence PST → 现有解析器 → 网络/引脚/贴装核对 → 显式电气检查 → 独立答案评分。
- [首次正式基线](baselines/2aeaee5/report.md)、[逐例结果](baselines/2aeaee5/results.json)、
  [独立数值验证](baselines/2aeaee5/spice-results.json)、
  [故障注入验证](baselines/2aeaee5/mutation-results.json)。

| 检查轨道 | 当前覆盖 | 不能由本版结果推断 |
|---|---|---|
| 原生计算 | Rule-08 分压/反馈、Rule-09 等效上拉；独立 KCL 与容差角点作答案 | 所有稳压器、完整 I2C 合规、任意非线性网络 |
| 提供电压证据 | Rule-16 采样电平、RC 充放电、缺证与过期证据 | 工具自动生成瞬态模型或完整启动时序 |
| 挑战集 | 非串并联桥式反馈网络；独立参考答案已确定 | 当前工具已具备通用节点分析能力 |

留出集按整个拓扑/来源家族划分，同一电路变体不会跨分区；但基础电路知识仍会重叠。
本版留出集公开且已用于本次基线，不是保密或从未见过的盲测。后续严格验收需要新家族
或未向优化过程开放的真实项目；不能反复调参后仍把本版叫作独立盲测。

## 已实施的案例驱动优化

[2026-09-14 线性反馈网络优化](optimizations/2026-09-14-linear-feedback/report.md)：
保留本版数据、答案和评测协议，扩展检查器的有界线性节点分析能力。
首次基线与优化结果分别保存，不能把同一批重复优化后的成绩称作全板准确率或严格盲测。

## 运行

Python 3.9+，评测脚本仅用标准库。以下命令从 schematic-review 仓库根目录执行。
所有输出目录必须尚不存在；同一批输出不会被静默覆盖。

```sh
mkdir -p /tmp/codex-work
BENCH_RUN=$(mktemp -d /tmp/codex-work/circuit-eval.XXXXXX)

python3 -B evals/circuit_bench/run.py \
  --split all --out "$BENCH_RUN/baseline" \
  --scratch-root "$BENCH_RUN" --require-pass
```

日常开发用 `--split dev`。报告中的 PASS/FAIL 是电路预期状态，`passed` 是工具回答是否符合预期：
正确识别 FAIL 和正确保留 INSUFFICIENT 都应得分。

`--require-pass` 对声明范围未通过返回 3；数据/协议错误返回 2；不加该选项时，
退出 0 仅表示报告已生成。挑战集已知能力缺口单列；任何分组的错误 PASS、
运行错误、解析失败、错误数值或对缺证案例作确定判断都会阻断升级门。
`all_cases_pass` 与 `declared_scope_pass` 分开，不隐藏挑战集失分。

比较其他检出目录的候选版本：

```sh
python3 -B evals/circuit_bench/run.py \
  --sut /absolute/path/to/candidate-schematic-review \
  --split all --out "$BENCH_RUN/candidate" --scratch-root "$BENCH_RUN" \
  --baseline evals/circuit_bench/baselines/2aeaee5/results.json --require-pass
```

比较要求数据、答案、评测程序指纹以及所测案例完全一致；工具源码指纹允许变化。
报告中 `comparison.promote` 必须为 true 才表示“值得人工审核的升级”：
有实际进步、既有得分案例零回归、声明范围达标。命令退出 0 不等于 promote 为 true。
本工具不会修改、合并、发布或安装候选版本，进步也不能替代真实项目校准。

## 检查评测本身

可选独立数值交叉验证需要 ngspice：

```sh
python3 -B evals/circuit_bench/spice_check.py \
  --ngspice ngspice --out "$BENCH_RUN/spice" --scratch-root "$BENCH_RUN"

python3 -B evals/circuit_bench/mutation_check.py \
  --out "$BENCH_RUN/mutants" --scratch-root "$BENCH_RUN"

TMPDIR="$BENCH_RUN" PYTHONDONTWRITEBYTECODE=1 \
  python3 -B -m unittest discover -s evals/circuit_bench/tests -v
TMPDIR="$BENCH_RUN" PYTHONDONTWRITEBYTECODE=1 \
  python3 -B -m unittest discover -s scripts/tests -q
```

故障注入只改临时脚本副本：只看标称值、漏算并联上拉、直接放行。
必须到达实际评分并被升级门拒绝；这三个错误被抓住不代表穷尽所有故障。
SPICE 检查使用原网络的理想元件模型，保存每次运行的 deck 和原始输出，
验证参考计算，不宣称真实 IC 行为或完整电路证明。

检查单例，或生成同一版本的数据副本：

```sh
python3 -B evals/circuit_bench/export_case.py \
  --id CB-0001 --out "$BENCH_RUN/CB-0001"
python3 -B evals/circuit_bench/generate.py --out "$BENCH_RUN/reproduced-data"
```

导出包含原始 PST、input.json、case.json 和单独的 expected.json。
worker 只接收工程条件与检查要求，不接收答案标签、分区或变体说明。
子进程隔离用于适配与复现，**不是安全沙箱**；不得运行不受信任的候选代码。
答案文件公开，优化代理也有可能读取；真正隐藏验收需另外部署权限边界。

## 维护与真实项目接入

`data/manifest.json` 锁定案例、答案、生成器和参考计算的 SHA-256。
不要原地改答案、删除失分用例或把所有答案改成 INSUFFICIENT。
新增能力或资料时创建新数据版本，记录变更理由，同时对旧版回归；不同版本的分数不直接比较。
oracle 使用独立节点方程和高精度 RC 公式，不复用被测求解器；缺证标签与物理错误分开。

真实项目使用 [接入模板](intake/real-project-template.json)，保留在授权项目目录：
原理图、网表/导出日志、BOM/装配、工况与需求、器件资料及版本哈希，
并逐条记录检查对象、判据、出处、预期结果、确认者与复验证据。
AI 可以整理资料、生成候选缺陷、计算和填表；未经独立确认的标签不得自动进入冻结答案。
目前 synthetic-only 适配器不接收真实项目，需要单独验证适配器和答案之后才计分。

建议下一步先选择少量已审结项目校准；本次没有导入私有项目，也没有启动持续自动升级。
