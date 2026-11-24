# TODO_ETF数据源接入

> 后续待办（按价值排序），均未包含在本次交付内

## P0 · 策略应用（数据已就绪，只差规则）

1. ~~**溢价过滤**~~：已完成（`etf_premium_cap`，默认 1.0%）。⚠ 历史验证受限：已覆盖标的历史溢价多在 0.1%~0.3%，阈值 0.5%/1.0%/2.0% 对 2023-2026 回测**无差异**；仅 2026-11-18 出现 7.85% 的 QDII 高溢价（实盘会剔除）。需补齐 `etf_backfill` 后重跑。
   - **2026-11-20 回测确认**：溢价上限 1.0% 反而劣化收益（-7.89% vs -7.44%），**建议默认关闭**（设为 0 或 None）。NAV 回补已完成 991/993 标的，但 QDII 滞后导致覆盖仅 14~55 只/日，溢价因子实战价值有限。
2. **同指数去重**：用 `fund-index-related` 建「指数 → 跟踪基金」清单，同一指数只保留规模/成交额最大的 1 只，避免重复暴露。
3. ~~**资金流因子**~~：**已验证正向有效**。个股级净申购 ≥ -3%（`etf_flow_min=-3.0`）显著改善 2023-2026 收益：
   - 基线（无因子）：-7.44% / 回撤 -39.19% / 夏普 -0.0169
   - 资金流 -3%：-1.30% / 回撤 -37.52% / 夏普 **+0.0728**（从负转正）
   - 市场资金流风控：-3.77% / 回撤 -35.67% / 夏普 0.0261
   - **结论**：`etf_flow_min` 默认值建议由 `None` → **-3.0**；`etf_mkt_flow_guard` 暂保留默认关闭。
   - 网格搜索（`flow_min ∈ {None, -5, -3, -2, -1, 0}`）确认 -3% 为最优平衡点，更严格阈值池子过窄收益反降。
4. ~~**绝对动量过滤**~~：**已落地**。新增 `etf_abs_mom_days`（默认 60）与 `etf_abs_mom_min`（默认 0.0，即关闭）：
   - 建仓时：近 N 日涨幅 < 阈值则剔除该标的；
   - 持仓时：`ret_N < 阈值` 触发 `abs_mom_exit` 强制清仓；
   - 界面提供「动量窗口 5-120 日 / 阈值 -10%~10%」滑块，0=关闭。

## P0.5 · 数据回补（阻塞上述验证）

- 剩余约 150 只「历史交易过的 ETF」的 2022-2026 溢价率未回补（今天配额已用尽）。
- 执行：`task_create("etf_backfill", {"start_year": 2022, "max_calls": 250, "with_capital": false})`，每日跑一次，约 3 天完成。
- 完成后重跑：`premium_cap` 0.5/1.0/2.0 三档对比，再决定默认阈值。

## P1 · 数据完善

5. **全池盘后观察**：等一次 15:30 盘后任务跑完（60 只约 180 次调用），确认耗时、失败率与配额余量。
6. **`etf_valuation` 保留策略**：只保留最近 8 个报告期，避免表无限增长。
7. **历史回补慢任务**：用每日剩余配额推进「真实成交额/份额」历史（按年切窗），完成后可评估是否切换回测口径。
8. **复权校准**：用 `fund-daily-quote` 后复权价（`adjustingMethod=2`）校验现有 `_adjust_fund_splits` 的折算检测阈值。

## P2 · 工程与体验

9. **ETF 详情面板**：在 ETF 策略页展示选品的规模、5 日净申购、溢价率、两融余额趋势。
10. **配额可视化**：设置页显示今日调用曲线与剩余可用次数。
11. **技能升级流程**：`vendor/htbr` 升级需人工同步并重跑测试，建议在 `docs/05-operations.md` 增加一节「技能包升级」。

## 候选数据源：FTShare（2026-11-19 实测，暂不接入）

Key 存 `.env` 的 `FTSHARE_API_KEY`（`.gitignore` 已覆盖，不入库）。网关 `https://market.ft.tech/gateway`，鉴权头 `FTSHARE_API_KEY`。

| 接口 | 实测结果 |
|---|---|
| `api/v2/market/data/etf-candlesticks/batch`（本技能核心） | **403 `route_not_allowed`**「当前套餐无权访问该接口」——属**基础版**，当前 Key 为免费版 |
| `api/v1/market/data/etf-candlesticks`（免费版） | 有权限，但持续 502「下游服务请求失败」/503，未完成历史深度与成交额口径验证 |
| `api/v1/market/data/etf-adjust-factor`（免费版） | 200，返回 `{symbol, trade_date, adj_factor}`（510300 adj_factor=1.0） |
| `api/v1/market/data/eastmoney-etf-flow`（免费版） | 200，510300 有 156 条历史记录 |
| 限流 | 首次约 20 次请求正常，之后全 503，等待 5 分钟未恢复 → 免费版额度/限流远超预期严厉 |

结论：原评估里价值最高的「批量真实成交额 + 前/后复权快速补全历史」需要基础版套餐；免费版额度不足以支撑全市场回补。**主链路继续 akshare，增强继续华泰柏瑞**。

重新评估的前置条件（满足任一即可再谈）：

- 升级到基础版 → 重跑三项验证：批量上限（50/100/200 只）、2020 年窗口历史深度、510300 某日 `turnover` 与库内 `daily_bar.amount` 对齐；
- 或证明免费版 `etf-adjust-factor` 日常调用稳定 → 仅用它替代 `_adjust_fund_splits` 的「单日跳变 >25%」启发式（低频，只覆盖持仓 ETF）。

## 候选数据源：交易所官方 + 东财净值（2026-11-19 实测，优先级高于 FTShare）

线索来自 GitHub `unixdn/etf-flow`（宽基净申赎工具）。三个接口已在本机复测：

| 数据 | 接口 | 实测结果 |
|---|---|---|
| 沪市 ETF 份额 | `query.sse.com.cn/commonQuery.do?sqlId=COMMON_SSE_ZQPZ_ETFZL_XXPL_ETFGM_SEARCH_L&isPagination=true&pageHelp.pageSize=3000&pageHelp.pageNo=1&STAT_DATE=YYYY-MM-DD`（需 UA + `Referer: https://www.sse.com.cn/`） | 200，单次返回当日全市场：2020-01-02 → 169 只 / 2024-06-28 → 547 只 / 2026-11-18 → 925 只；字段 `SEC_CODE / SEC_NAME / TOT_VOL(万份)`；**任意历史日可回溯**，无 Key、无配额 |
| 深市 ETF 规模 | `www.szse.cn/api/report/ShowReport/data?SHOWTYPE=JSON&CATALOGID=1945&TABKEY=tab1&PAGENO=1&PAGESIZE=100`（`Referer: https://www.szse.cn/`） | 200，772 只；**仅当日快照**，须逐日累积 |
| 单位净值历史 | `api.fund.eastmoney.com/f10/lsjz?fundCode=&pageIndex=&pageSize=&startDate=&endDate=`（`Referer: http://fundf10.eastmoney.com/`） | 200，510300 可回溯到 2015，513100（QDII）到 2018，分页取全历史 |

可在本地计算，不消耗华泰柏瑞配额：

- 规模 = 份额 × 单位净值
- 净申购（亿）= (当日在册份额 − 前一日) × 当日净值
- 溢价率（%）= (收盘价 / 单位净值 − 1) × 100

与现有实现的关系：

- `etf_capital` 的 `exchange_shares / market_scale / net_sub_shares` 可由官方份额替代（沪市全历史、深市自接入日起累积）；
- `etf_quote_ext.premium_ratio` 可由「收盘价 ÷ 净值」本地计算，不再受 `etf_backfill` 配额限制；注意 QDII 净值滞后 T-1/T-2，存在系统性偏差，需按类型校准；
- 华泰柏瑞保留：两融、持仓估值、全市场资金流（交易所/东财无对应数据）。

风险与待验证：交易所接口属非正式 API（依赖 UA/Referer，可能改版）；净值口径是「单位净值」而非 IOPV；深市历史无法回溯。

其他 GitHub 候选（未实测，仅记录）：

- `1nchaos/adata`（5.4k★，活跃）：`fund.market.get_market_etf()` 免费 ETF 日/周/月 K 线（同花顺源），可作成交额/复权交叉校验；
- `xticktop/skills`（67★，2026-11 活跃）：90 个 HTTP API，含 ETF 日 K 复权与成交统计，需免费注册 Token；
- `quantskills/skill-etf-arbitrage-monitor`：IOPV/折溢价/申赎篮子三级数据源，但依赖 Pandadata（付费）。

## 已明确不做

- 不做 927 只全量历史镜像（配额不允许）。
- 不用真实成交额覆盖历史 `amount`（用户已确认仅近期使用）。
- 不接入指数类（`index-*`）与基金经理/费率展示（当前无明确需求）。
