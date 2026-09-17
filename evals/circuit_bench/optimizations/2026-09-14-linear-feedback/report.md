# 本批电路案例自动优化结果

已完成一轮案例驱动的检查器优化。保留原始 166 例、答案、分区和评测协议；
修改的是求解与证据依赖代码，不是模型权重，也没有修改考试答案。
最终候选经两名独立审查代理复核后保留；本轮不提交或推送。

| 项目 | 原版 | 最终候选 |
|---|---:|---:|
| 冻结电路评测符合预期 | 158 / 166 | 166 / 166 |
| 已知缺陷正确判 FAIL | 56 / 62 | 62 / 62 |
| 已知正常正确判 PASS | 17 / 19 | 19 / 19 |
| 缺证案例保持 INSUFFICIENT | 85 / 85 | 85 / 85 |
| 错误 PASS / 误报 / 运行错误 | 0 / 0 / 0 | 0 / 0 / 0 |
| 独立新增电路回归 | 16 / 22 | 22 / 22 |

本批逐例回归为 0，新增正确结果 8 例：CB-0055/0056/0057/0058/0064/0065/0066/0067。
改进包括正确识别 6 个违规反馈网络和确认 2 个给定模型下正常的网络，
不是把 8 例统一改成 PASS。详见 [机器结果与版本比较](results.json)。

## 实际改动

1. 原串并联路径保持；不能归并的完整正电阻网络可回退到节点分析。
   使用原 VALUE 十进制的精确分数、LDLᵀ 双右端求解和全参数角点。
   状态用精确界限比较；展示数值向外舍入，并保留极值所用元件值、Vref 与偏置。
2. 新网络中的电阻及已证明可忽略的输入/C 统一纳入来源依赖，计划与热跑保持一致。
   缺资料、缺公差、旧网表/旧文档、未建模负载仍无法放行。
3. 新增 13 项本地单元测试，并只更新与新能力直接有关的技能说明和证据契约。

## 独立复核确实改变了候选

最初候选虽然已通过 166 例，仍被独立复核阻止安装：

- 浮点求解在极端阻值、窄门限下可出现错误 PASS：改为精确有理数求解与比较。
- Lint 拒绝缺来源，但计划仍显示 READY：将新依赖纳入共享发现逻辑。
- 非字符串端点触发 TypeError，中止计划生成：修复类型守卫，保留受控缺证结果。

独立测试还提出 13 颗固定值电阻被粗糙的总数上限拒绝。最终分开限制网络规模和
不确定参数数目：最多 20 颗正电阻、12 个网络、10 颗非零公差电阻；
因此电阻二值角点仍最多 1024 组，不以抽样替代全角。

[独立安全审查](independent/safety-review.md) 保留发现、关闭证据及源码指纹；
[初始阻断记录](independent/safety-initial-findings.md) 也予以保留。
这次采用 Darwin 的实测、独立复核和保留/退回原则；按用户指定的电路结果优化，
未将提示词的九维文案评分作为电气能力分数。迭代轨迹见 [results.tsv](results.tsv)。

## 完成的验证

- 源码单元测试 146 / 146，评测程序测试 19 / 19；技能结构校验通过。
- 独立代理预先冻结的电路回归 22 / 22，[前后记录](independent/after.json)；
  输入与答案 SHA-256 从冻结到复验未变，未读取本批 benchmark 的答案来生成新预期。
- 另一代理的安全测试 17 / 17；513 个数值压力网络中 493 个被接受，其精确界限
  493 / 493 与独立 KCL 完全一致，20 个因支持边界被拒绝；256 个内点均被角点包络覆盖。
- 原评测参考答案的 ngspice 数值交叉验证 150 / 150；
  独立新增电路的解析预期另经 30 / 30 次 ngspice 检查。
- 三类实际源码故障注入仍全部被识别：[故障记录](mutation-results.json)。
  三类故障并不代表穷尽全部潜在实现错误。

## 指纹与复验

- 案例：`5addda6a8fc2f0cee5d78d96b06ea86354befa365ccc101ea782aaeba171bdbb`
- 答案：`43fdd3c63b3ef3f5d245e069b462ae42fcc04d4cb9116225915811fcf05fc697`
- 冻结评测协议：`d192c7de4dab37887929a9edd42748bbd4bb1f69f960242caa1ad42bd425e73d`
- 原版脚本集合：`4d2671f0a3fdd6b0f030dad9809b10d8f094dbab9da8c76790b774fb230dd812`
- 最终脚本集合：`3487101bbe7d86fafa343e2bda8b7cd25c91746019b3f2e656af3bba24898d1c`
- 独立 22 例输入/答案：`de11ceed96910c349c61a586337c35aaf402ead7264269ac46d7253b90650d2f`

从 schematic-review 仓库根目录复验：

```sh
mkdir -p /tmp/codex-work
OPT_RUN=$(mktemp -d /tmp/codex-work/feedback-verify.XXXXXX)
OPT_CASES=evals/circuit_bench/optimizations/2026-09-14-linear-feedback

python3 -B evals/circuit_bench/run.py --split all \
  --out "$OPT_RUN/batch" --scratch-root "$OPT_RUN" --require-pass \
  --baseline evals/circuit_bench/baselines/2aeaee5/results.json

mkdir -p "$OPT_RUN/independent-regression" "$OPT_RUN/before/scripts/tests"
cp "$OPT_CASES/independent/independent_rule08.py" "$OPT_RUN/independent-regression/"
cp "$OPT_CASES/independent/frozen_cases.json" "$OPT_RUN/independent-regression/"
cp scripts/tests/electrical_fixtures.py "$OPT_RUN/before/scripts/tests/"
TMPDIR="$OPT_RUN" PYTHONDONTWRITEBYTECODE=1 \
  python3 -B "$OPT_RUN/independent-regression/independent_rule08.py" \
  --source "$PWD" --out "$OPT_RUN/independent-regression/result"
```

独立电路运行器绑定了冻结时的临时目录结构，因此上述命令先原样复制运行器、答案和夹具，
再在临时工作区运行；不在技能目录生成审查产物。安装后已复跑 166 / 166、22 / 22、
17 / 17 与全部单元测试，源码指纹与独立复核版本一致。

独立安全脚本原样保存（未更改其测试预期或代码指纹）。它保留当次临时根目录常量，
复验时先创建该目录；该目录只容纳新建临时夹具，不含生产文件：

```sh
mkdir -p /tmp/codex-work/schematic-optimize.2Xfcdr/independent-safety
TMPDIR="$OPT_RUN" PYTHONDONTWRITEBYTECODE=1 \
  python3 -B "$OPT_CASES/independent/check_candidate.py" "$PWD"
TMPDIR="$OPT_RUN" PYTHONDONTWRITEBYTECODE=1 \
  python3 -B "$OPT_CASES/independent/stress_numeric.py" "$PWD"
```

## 不能从本结果推断的结论

新增能力只覆盖单一理想源/参考地、FB 集总输入偏置的有界静态正电阻模型。
0Ω、非线性器件、多源、未建模负载和超限图仍不支持；CLI 仍只作探索。
源供电余量、启动、环路稳定性、真实资料提取、完整 Agent 审查和整板准出没有因此通过。

本批数据已用于优化，不是新的盲测。22 例独立测试用于补充验证，也不是整板真实项目。
尚未导入私有真实电路；本次完成有限迭代，不创建持续自动改代码或自动发布任务。

冻结评测器生成的 [原始文本报告](frozen-runner-report.md) 沿用初始基线的“挑战集”描述；
为保持协议指纹没有修改其模板。新能力范围以本报告与机器结果为准。
