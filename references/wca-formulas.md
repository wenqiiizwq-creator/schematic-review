# ER4 参数与边界条件验算（WCA）

只使用当前装配版本的实际元件值和有出处的保证参数。E96 表示阻值系列，不能证明精度。
未知公差、温度系数、Vref 极限或负载不是零。范围内输入缺失为 INSUFFICIENT；
已知窗口违反保证条件为 FAIL；PCB 寄生、散热实现和样品测试另建 HANDOFF。

## 计算前的模型检查

记录对象、状态、目标量、端点、所有支路、数据来源和忽略项的量化依据。
不同地网不能直接合并；DNP 按装配版本移除。内部拉阻、输入偏置、LED、钳位、
开关以及电容在采样时段的充放电必须建模或证明可忽略。

solve_dividers.py 仅支持可归并的串联臂/不共享电阻的并联支路，CLI 用于探索。
Rule-08 的 divider_model 需指定 source_net、reference_net、ignored_nodes 的逐点理由和
bias_current_a 保证范围。ignored_nodes 只适合输入负载已计入偏置范围或 DC 下开路电容；
不能用“可忽略”文字绕过 MOS/二极管/LED。非零地偏差、复杂网络或非线性支路交由节点分析/
经过核实的仿真模型，保留算式与角点，当前自动求解器返回 INSUFFICIENT。

## 公式与验收依据

| 对象 | 计算 | 验收条件与限制 |
|---|---|---|
| 闭环 FB 设定值 | Vout−Vground = Vref(1+Rtop/Rbot)+Ibias·Rtop；Ibias 正号流入 IC | 用 R、Vref、Ibias 极限组合求窗口；与每个负载推荐工作范围比较，不用固定 1–2% 误差。结果不证明供电余量或环路稳定 |
| UVLO/OVLO/监控 | 按具体器件阈值、迟滞及分压公式 | 下降沿/上升沿分别计算，比较被保护对象的工作边界、动作延迟和正常电源变化；先确认检测目的及位置 |
| 有钳位的分压 | 源阻抗和器件 I–V 曲线联立 | Vpin 不能直接取 min(未钳位电压,Vz)；核对测试电流、低流拐点、泄漏、温度、限流电阻功率及输入注入额定 |
| strap / 数字输入 | 在 setup/hold 或有效时段求 Vpin_min/max | high 要 Vmin≥VIH(min)，low 要 Vmax≤VIL(max)；窗口越界表示未满足保证条件，不能推导每颗都会失败，也不能以典型翻转点免除问题 |
| 单极 RC、阶跃源 | V(t)=Vfinal+(Vinitial−Vfinal)exp(−t/(Rth·C))；t_cross=−Rth·C·ln((Vthreshold−Vfinal)/(Vinitial−Vfinal)) | 仅限源稳定且单极线性模型。检查阈值可达及对数定义域；源缓升/掉电残压/内部拉阻/泄漏和门限公差另计。τ 不是复位脉宽 |
| I2C 上拉 | Rp_min=(Vpullup_max−VOL_max)/IOL_guaranteed；Rp_max=tr_max/(0.8473·Cb_max) | 按实际速度与每个电气段，所有并联上拉的公差窗口应在两限之间；计入外接板上拉、缓冲器、串阻压降。阻值满足不代表 VIH/VIL、Ioff 或地址已通过 |
| Buck，理想 CCM 初算 | D≈Vout/Vin；ΔIL≈(Vin−Vout)D/(L·fs)；Ipk≈Iout+ΔIL/2；IL_rms≈sqrt(Iout²+ΔIL²/12) | 按实际拓扑与损耗修正；电感 Isat 定义和温度降额、开关限流 min/max、最小 on/off 时间均核对，禁止推广到所有变换器 |
| LDO | P≈(Vin−Vout)·Iout+Vin·Iq | 高温/满载保证 dropout、Cout 有效值/ESR/稳定性、反向电流和放电条件逐项核对；PCB 热阻未知不宣称结温通过 |
| MOS 保护/热插拔 | 导通 I²Rds(on)；线性态瞬时功耗 VDS·ID，能量积分 ∫VDS·ID dt | 依据相应 VDS、ID、脉宽和起始温度的 SOA 检查，不用额定电流或总能量代替 SOA；计时、重试和熔断器配合独立核对 |
| TVS | VRWM 对正常峰值；VBR 对规定 IT；VC 对规定 Ipp/波形 | 再看漏电、温度、脉冲宽度、源阻抗/可用能量和后级承受能力。VRWM 不是击穿电压；型号数字仅用于检索 |
| ADC / 运放 | 分压、偏置/失调/增益误差、RC 建立误差；简单模型误差约 ΔV·exp(−tacq/((Rsource+Ron)·Csample)) | 具体采样结构按 datasheet；同时检查共模范围、输出摆幅、GBW/压摆率、容性负载稳定性和参考驱动；不能只检查 ADC 直流满量程 |
| 晶体 | CL_eff=C1·C2/(C1+C2)+Cstray | 准确晶体 CL/ESR/驱动等级、振荡器适配和起振条件；Cstray 没有通用默认值，布局寄生/起振裕量转 HANDOFF |
| MLCC | Ceff=准确料号的偏压/温度/老化/公差修正 | 用厂家曲线和定义，不填通用 40–70%；有效最小/最大值与器件稳定及储能条件比较 |
| 电阻/连接器 | P=U²/R 或 I²R；额定工作电压、脉冲、温度降额 | 每颗电阻和每组触点分别检查；并针电流须有制造商组合额定或分流最坏模型，不能机械乘针数 |
| 供电窗口 | 将电源含全角窗口与每个负载推荐工作范围相交；绝限另查 | 未超 Abs Max 不能当作工作正常；轨名只是标称意图 |
| 限流/断路 | Itrip(min)=Vth(min)/Rs(max)，max 反向；分别算快断、恒流、功率限制、TIMER | 最大连续电流额定值不等于电流触发阈值；软件检测不自动满足硬件保护要求 |
| 电流/功率预算 | 各状态下负载峰值、效率、同时工作系数和上游能力；Ipeak≈Iout+ΔIL/2 | 裕量依据需求/策略；Isat 与温升电流分别核，不能只比平均值 |
| 电阻/泄放 | P=V²/R、I²R，电阻工作电压；放电 t=RC ln(Vstart/Vsafe) | 改 24V 泄放电阻同时算满压常态功耗和温度降额；不得仅按放电速度给封装 |
| 跨域/掉电 | 分别核接收端门限、允许输入/注入电流、Ioff、VDD=0 条件、转换器供电顺序 | 不固定接“较高域”或“较低域”；没有直接兼容窗口时用合适转换器/隔离器 |

## 必查状态与留档

在每个具体电路上判断适用性：冷启动、慢爬升、棕断、短暂掉电、单域掉电、
外部接口先上电、热插拔、满载/空载、短路、负载瞬态、温度极限和装配变体。
缺失需求要列为待补证据，不擅自填写最坏工况。对每项记录公式、位号/网络、数据与
文档定位、角点、结果范围、判据、审查意见和复验方法。

## 修改建议的复算

对每项建议写“旧值/旧网→新值/新网”、假设、修改后全角计算、关联器件、装配状态和通过标准。
未掌握输入/负载/保护窗口时不给唯一确定阻值或替代料。禁止用“软件强制某模式”替代硬件保护，
除非需求允许且启动/掉电/故障场景均有依据；不能自动豁免明确必需的功能。
只由 PCB/实测决定的寄生、真实温升、SI/EMC 列 HANDOFF，仍完成原理图可算的应力与损耗。

LDO 的反馈设定值须比较 Vin 与保证 dropout；计算设定高于输入不代表器件能升压。
ADC 的 FSR 是转换量程，模拟脚的绝限和共模窗口仍须独立核对。

## 原厂依据

- 数字保证边界：[TI SZZA036C pp.28–29](https://www.ti.com/lit/an/szza036c/szza036c.pdf)。
- I2C 等效上拉和上升时间：[TI SLVA689 pp.2–4](https://www.ti.com/lit/an/slva689/slva689.pdf)，[NXP UM10204](https://www.nxp.com/docs/en/user-guide/UM10204.pdf)。
- 掉电隔离需要具体 Ioff 规格，示例：[TI SN74LVC2G17 Rev.N §9.3](https://www.ti.com/lit/ds/symlink/sn74lvc2g17.pdf)；不能把某型号能力推广到所有输入。
- 热插拔 MOS SOA：[TI LM5069 Rev.G §9.2.1.2.5](https://www.ti.com/lit/ds/symlink/lm5069.pdf)。
- LDO 稳定条件：[TI SLVA115A](https://www.ti.com/lit/an/slva115a/slva115a.pdf)。
- ADC 源阻抗和获取时间：[TI SPNA061](https://www.ti.com/lit/an/spna061/spna061.pdf)。
- VRWM/VBR/VC 分列示例：[Littelfuse SMBJ 2025 v4 p.2](https://www.littelfuse.com/assetdocs/tvs-diodes-smbj-series-datasheet?assetguid=ba555e99-a12d-4f72-a0b6-86b06c67171e)。
