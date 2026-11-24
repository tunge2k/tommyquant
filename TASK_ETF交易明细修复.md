# TASK — ETF 交易明细修复

| ID | 任务 | 输入/约束 | 输出 | 依赖 | 状态 |
|---|---|---|---|---|---|
| T1 | 审计交易链路 | `backtest_engine.py` / `streamlit_app.py` | 根因 R1/R2/R3 | — | 完成 |
| T2 | 修复 ETF 调仓触发 | ETF 池 + `topk_drop` 不得关闭偏离度再平衡 | `_deviation_rebal` 覆盖 ETF | T1 | 完成 |
| T3 | 换标的资金闭环 | 买单预算 = 现金 + 当日卖出回款 | `on_bar` 统一走 `_build_rebalance(extra_cash=proceeds)` | T2 | 完成 |
| T4 | 撮合先卖后买 | 与入队顺序解耦 | `_execute_orders` 卖出阶段/买入阶段 | T1 | 完成 |
| T5 | 资金实时校验 | 买入扣款 ≤ 可用现金 | `apply_fill` 整手收缩 + 费用缩放 + 拒单 | T1 | 完成 |
| T6 | 流水可审计 | 同日双向可核验 | `fills` 增加 `time`/`seq`/`cash_before` | T4/T5 | 完成 |
| T7 | 明细展示与原因 | 交易明细表 | 增加「时间」列；补 `abs_mom_exit` 文案 | T6 | 完成 |
| T8 | 补绝对动量参数 | 界面已传参但引擎缺参 | 构造器参数 + 建仓过滤 + 清仓原因 | T1 | 完成 |
| T9 | 回归测试 | 3 年 ETF 三配置 + 单测 | 新增 4 个用例；106→110 全绿 | T2–T8 | 完成 |
| T10 | 文档同步 | 6A 与策略规范 | `docs/04-strategy-spec.md` 6.3.3 | T9 | 完成 |
| T11 | 向量化内核同口径 | 股票快速回测共用 `Portfolio` | `fast_backtest.py` 先卖后买 + 回款预算 + 拒单返回值 | T4/T5 | 完成 |
