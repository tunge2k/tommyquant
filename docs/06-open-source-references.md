# 06 · 开源借鉴清单与调研流程

> 目标：不重复造轮子。遇到问题先看成熟项目怎么做，能借规则就借规则，能借实现就借实现，只在没人做过的地方自研。
>
> 基线：2026-11-17；当前代码 `v14` / `m1`

---

## 1. 四条原则

1. **先搜后写**：新增逻辑超过约 100 行，或涉及回测口径，先找 2~3 个成熟实现再动手。
2. **借口径优先于借代码**：我们的事件驱动引擎 + 向量化内核已经跑通，换底座成本远大于收益；优先移植「规则/参数/校验」，而不是整段拷贝。
3. **许可证红线**：MIT / Apache-2.0 可参考实现；GPL（如 Backtrader）只借鉴设计思想，不复制代码。引用时在提交说明里注明来源。
4. **必须小样本对照**：同一段行情、同一组参数，新旧实现结果对齐后才合入；对不上就是没读懂对方的口径。

---

## 2. 当前问题 → 别人怎么解

| 我们的问题 | 观测到的事实 | 成熟做法 | 出处 |
|---|---|---|---|
| 换手过高，成本吃掉收益 | 日均换手 3.66%，3.9 年成本 1356 万，净收益 +2.2% | **Topk-Drop**：只在持仓跌出候选前 K 名时卖出，用 `n_drop` 做缓冲，日换手 ≈ `2×n_drop/topk`（50/5 配置约 10%/日，可按需下调） | Qlib `TopkDropoutStrategy` |
| 涨跌停仍能成交 | 回测成交价没有约束 | 涨停不买、跌停不卖（`limit_threshold≈0.095`），停牌日无 bar 直接不成交 | Qlib `backtest.limit_threshold`；RQAlpha `sys_simulation` |
| 成本模型零散 | 佣金/印花/过户/滑点分散硬编码 | 开仓与平仓费率分开配置 + `min_cost` 最低手续费 | Qlib `open_cost/close_cost/min_cost`；RQAlpha `commission_multiplier` |
| 绩效指标手写 20 项 | 无法与外部口径对齐 | 用 `empyrical`/`quantstats` 的口径对齐，直接出 HTML tearsheet（月度收益热力图、滚动夏普、回撤区间） | QuantStats |
| 单标的验证看不出为什么空仓 | 卖出后 10 个月无成交，界面无解释 | 策略内显式记录「未成交原因」，成交流水展示真实原因 | 本项目已实现（`ma30_exit`） |

---

## 3. 候选项目清单

| 项目 | 定位 | 值得借的部分 | 许可证 |
|---|---|---|---|
| [microsoft/qlib](https://github.com/microsoft/qlib) | AI 量化平台（数据/因子/组合/回测） | Topk-Drop 调仓、`limit_threshold`、成本配置、Alpha158 因子集、point-in-time 数据思想 | MIT |
| [ricequant/rqalpha](https://github.com/ricequant/rqalpha) | A 股回测/模拟/实盘 | A 股撮合规则（涨跌停/停牌/T+1）、滑点模型（比例/跳）、最小手续费 | Apache-2.0 |
| [vnpy/vnpy](https://github.com/vnpy/vnpy) | 实盘交易框架 | 事件引擎与网关抽象（我们暂不需要实盘，先不引入） | MIT |
| [mementum/backtrader](https://github.com/mementum/backtrader) | 事件驱动回测 | `CommInfo` 手续费/滑点抽象的设计思路 | GPL-3.0（只读思路） |
| [polakowo/vectorbt](https://github.com/polakowo/vectorbt) | 向量化回测 | 参数网格扫描、组合权重分配的向量化写法 | Apache-2.0 |
| [ranaroussi/quantstats](https://github.com/ranaroussi/quantstats) | 绩效分析报告 | `stats` / `plots` / `reports.html` 全套口径与 tearsheet | Apache-2.0 |
| [akfamily/akshare](https://github.com/akfamily/akshare) | 数据源（已在用） | 备用接口与交叉校验思路 | MIT |
| [QUANTAXIS/QUANTAXIS](https://github.com/QUANTAXIS/QUANTAXIS) | A 股数据 + 回测一体化 | 行情表结构与复权处理参考 | 以仓库为准 |

> 许可证以仓库最新声明为准；表中只影响「能不能复制代码」，不限制我们读文档、抄口径。

---

## 4. 建议立刻做的三件事（P1）

### P1-1 借用 Qlib 的 Topk-Drop 调仓（收益影响最大）✅ 已完成（2026-11-17）

现状：`_build_rebalance` 只要某只持仓市值偏离目标 5% 就调仓，几乎每天全额换手。

改法：引入「排名缓冲」——持仓只有在候选池中跌出 `top_n + buffer` 名时才卖出；买入只在空仓位时发生，不再做偏离度再平衡。

实现：`MarketCapStrategy(rebalance_mode="topk_drop", rank_buffer=2)`，界面「交易规则 → 调仓方式」。MA30 清仓优先级高于排名换出；换出回款当日即可用于补位买入。

对照回测（2023-01-01 ~ 2026-11-16，初始 100 万，top_n=5，市值 15–200 亿，投入 75%，四项不良过滤 + MA30 退出全开）：

| 指标 | deviation（旧） | topk_drop b2 | topk_drop b4 |
|---|---|---|---|
| 总收益 | +2.20% | +27.05% | **+65.24%** |
| CAGR | 0.56% | 6.38% | **13.86%** |
| 最大回撤 | −39.56% | −38.69% | −40.78% |
| 夏普 | 0.14 | 0.39 | **0.69** |
| 索提诺 | 0.20 | 0.54 | 0.99 |
| 期末净值 | 1,022,035 | 1,270,450 | 1,652,380 |
| 卖出笔数 | 600 | 164 | 130 |
| 成交笔数 | 1,262 | 333 | 265 |
| 总成本 | 62,600 | 104,326 | 96,336 |
| 成本/成交额 | 0.17% | 0.18% | 0.18% |
| 日均换手 | 3.66% | 5.04% | 3.96% |
| 单次回测耗时 | 131s | 279s | 279s |

结论与预期不同，值得记录：

1. **旧的偏离度再平衡是「卖强补弱」**：上涨标的被减仓、下跌标的被加仓，等于持续砍掉赢家，收益被磨平；Topk-Drop 只在排名跌出缓冲区时才换股，赢家能拿住。
2. **换手率并未下降**（3.66% → 3.96%）。原因是偏离度调仓每次只动几十股到几百股，金额小；Topk-Drop 每次换股是整仓进出，单笔金额大。所以「降低换手」不是收益改善的原因，**改善来自不再对持仓做反向再平衡**。
3. 总成本从 6.3 万升到 9.6 万（占初始资金 6.3% → 9.6%），相对 +63pp 的收益差可以接受。
4. `rank_buffer` 从 2 放宽到 4 后收益进一步提升、交易更少，但这是同一区间的参数比较，**不构成样本外证据**；默认仍取 2（与 Qlib 的 `n_drop=5/topk=50` 的保守比例接近）。

顺带修复：`Portfolio.apply_fill` 合并加仓时把买入本金误计入 `entry_cost_total`，导致多笔加仓的持仓在卖出时把本金当成本扣掉（旧报告的「成本 1356 万」即由此产生）。现已改为只累计费用，并加回归用例。

### P1-2 借用 rqalpha 的 A 股不可成交规则

补三条校验（撮合前）：涨停不买、跌停不卖、当日无 K 线（停牌）不成交。现有 `limit_up_pct` 只覆盖了买入侧的涨停，且没有停牌判断。不补这三条，任何回测的收益都会系统性偏高。

### P1-3 借用 QuantStats 出绩效报告（v1.1.0 已完成）

回测结果页新增「📑 研究报告」标签：点「生成绩效报告」即调用 QuantStats 生成 HTML tearsheet（约 765 KB），内嵌预览并提供下载。

实施要点：

- **必须用 lumi 分支**：官方 `quantstats 0.0.64` 锁定 `numpy<2.0`，而 Python 3.13 没有 numpy 1.x 的预编译包（会退回源码编译并失败）。改用 `quantstats-lumi==1.1.6`，它支持 numpy 2.x，模块名为 `quantstats_lumi`。代码里两个模块名都尝试，官方包可用时优先。
- **无头渲染**：容器内无显示设备，导入前强制 `matplotlib.use("Agg")`。
- **降级策略**：`perf_report.available()` 为 False 时界面提示重建镜像，不影响其他标签。
- **字体告警可忽略**：QuantStats 样式指定 Arial，容器内没有该字体，回退 DejaVu Sans，只输出 `findfont` 提示，图表正常。
- 依赖会带来 matplotlib / scipy / seaborn / yfinance，镜像体积增加约 250 MB，首次构建耗时约 2 分钟。

---

## 5. 平台技能/插件用法

| 技能 / 插件 | 用在哪 | 产出 |
|---|---|---|
| `deep-research-assistant`、`search` | 调研框架选型、比较实现细节 | 调研结论 + 出处链接 |
| `stock-picker` | 日常选股、市场扫描 | 候选票与逻辑 |
| `financial-analysis-cn`、`data-acquisition` | 财务分析、估值、DCF、数据核对 | 指标与结论 |
| `equity-research-cn` | 个股深度研报 | 研报文档 |
| `macro-analysis-cn` | 宏观周期、利率、流动性 | 周期定位 |
| `html-report` / `html-deck` | 回测报告、研究汇报 | 单文件 HTML |
| `office-plugin`（xlsx/docx/pdf） | 导出 Excel/Word/PDF | 交付文档 |
| `schedule` | 每日数据巡检、盘后摘要、周报 | 定时任务 |
| `dynamic-ui` | 对话里画架构图/对比表 | 内联图表 |
| `TRAE-browseruse` | 验证 Streamlit 页面交互 | 页面验收 |

> 这些能力已经接入当前会话，直接说「用 XX 技能做 XX」即可，不需要额外安装。

---

## 6. 调研 SOP（每次遇到问题照做）

1. **一句话定义问题**，并写出可验证指标（如「日均换手率」「成本占初始资金比例」）。
2. **搜 2 个以上实现**：GitHub 仓库 + 官方文档，优先看它们的参数定义而不是教程。
3. **记录四件事**：算法/参数、边界条件（停牌、涨跌停、复权）、许可证、维护活跃度。
4. **小样本对照**：取同一段行情跑新旧两版，结果差异要能解释。
5. **落地**：改代码 + 补测试 + 同步 01 变更记录与 04 口径。
6. **失败也记录**：写进 03「常见坑」或本文，避免下次重复踩。

---

## 7. 参考链接

- [Qlib · Portfolio Strategy（Topk-Drop）](https://qlib.readthedocs.io/en/latest/component/strategy.html)
- [Qlib · Workflow 配置（limit_threshold / open_cost / close_cost / min_cost）](https://qlib.readthedocs.io/en/stable/component/workflow.html)
- [RQAlpha · 介绍（A 股日线回测）](https://rqalpha.readthedocs.io/zh-cn/latest/intro/overview.html)
- [RQAlpha · 参数配置（滑点模型）](https://rqopen.ricequant.com/doc/rqalpha-plus/api/config)
- [Ricequant · 回测（滑点与手续费）](https://www.ricequant.com/doc/quant/backtest.html)
- [QuantStats · PyPI（绩效指标与 tearsheet）](https://pypi.org/project/QuantStats/)
