# FINAL — ETF 交易明细修复

## 1. 修复结论

三类问题全部定位并修复，3 年 ETF 回测通过「明细完整 / 现金非负 / 先卖后买」验收。

| 问题 | 根因 | 修复 |
|---|---|---|
| 3 年仅 3 条记录 | ETF 池 `topk_drop` 关闭了偏离度再平衡；ETF 无市值、排名长期不变 | ETF 池始终保留偏离度再平衡（价格口径） |
| 资金余额 -1,139,112.26 | 买单先于卖单撮合；预算含卖出回款但执行时尚未到账；旧守卫费用未随数量缩放 | 两阶段撮合 + 含费整手收缩 + 费用等比缩放 + 不足一手拒单 |
| 同一时间点先买后卖 | `_execute_orders` 按入队顺序单循环撮合 | 09:30 卖出阶段 → 09:31 买入阶段，与入队顺序解耦 |

## 2. 代码变更

`backtest_engine.py`

- `Portfolio`：新增 `rejected_buys` / `cash_floor_hits`；`apply_fill` 买入实时资金校验、整手收缩、费用缩放、拒单返回；流水新增 `time`/`seq`/`cash_before`；
- `EventDrivenBacktest._execute_orders`：卖单/买单拆分两阶段执行；
- `EventDrivenBacktest.run`：标准买入通道预算加入当日卖出回款，取消强制 100 股；
- `MarketCapStrategy`：
  - `_deviation_rebal` 覆盖 ETF 池；
  - 换标的统一走 `_build_rebalance(extra_cash=卖出回款)`，目标为 `remaining + new` 等权；
  - 补齐 `etf_abs_mom_days` / `etf_abs_mom_min` 参数、建仓过滤与 `abs_mom_exit` 清仓；
- 回测结果新增 `rejected_buys` / `cash_floor_hits`。

`fast_backtest.py`

- 向量化内核与事件驱动引擎同口径：先卖后买两阶段、买入预算含当日卖出回款、
  `apply_fill` 拒单时不再调用 `on_trade`。

`streamlit_app.py`
- 交易明细增加「时间」列（09:30 卖 / 09:31 买）；
- 新增 `abs_mom_exit → 绝对动量清仓` 文案。

`tests/test_market_cap_rebalance.py`

- 新增 4 个回归用例（ETF 调仓完整性、资金收缩、拒单、先卖后买）。

`docs/04-strategy-spec.md`

- 6.3 补充 ETF 池例外；新增 6.3.3 撮合时序与资金实时校验。

## 3. 验收

- 3 年三配置：383 / 108 / 318 条成交；最小现金 141,310.31；`rejected_buys=0`、`cash_floor_hits=0`；
- 同日换标的均为先卖后买（卖 09:30 → 买 09:31）；
- 全量测试 **110 passed**。

## 4. 残留风险与后续

1. ETF 池开启偏离度再平衡会显著提高换手，实盘需结合佣金/冲击成本复核参数（`cap_change_threshold` / `rebalance_threshold`）；
2. 回测采用「卖出回款当日可用」口径；若未来接入真实交收（T+1 资金到账），需在 `_execute_orders` 增加资金可用日字段；
3. 卖出因跌停/停牌顺延时，对应买入会被资金校验收缩/拒单，属预期行为；
4. 界面未展示 `rejected_buys` / `cash_floor_hits`，如需运营监控可在回测结果区增加提示。
