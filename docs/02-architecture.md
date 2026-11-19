# 02 · 系统架构

> 文档基线：2026-11-17
> 阅读前提：已读 [01-project-plan.md](./01-project-plan.md) 的定位与范围。

---

## 1. 总览

```
                              ┌──────────────────────── NAS / Docker ────────────────────────┐
                              │                                                              │
 浏览器 ──HTTP 8501──────────▶│  streamlit_app.py  (前台进程, 8 个可达页面)                    │
   │                          │    ├── 页面渲染 / 导航 / i18n                                 │
   │ 直连 push2.eastmoney.com │    ├── 回测调用 run_backtest_auto()                           │
   └─────────────────────────▶│    │       ├── fast_backtest.run_fast()  (向量化, 优先)        │
                              │    │       └── backtest_engine.run_backtest() (事件驱动, 兜底)│
                              │    └── 数据读取 data_store                                   │
                              │                                                              │
                              │  update_worker.py (后台常驻)                                  │
                              │    ├── 启动自举 / 启动补全                                     │
                              │    ├── 交易日 15:35 盘后补全 + 因子缓存预热                    │
                              │    ├── 交易时段 60s 实时快照                                   │
                              │    ├── 新闻每 120 分钟                                         │
                              │    ├── 财务每周五                                              │
                              │    ├── 全球市场 5s/300s 轮询                                   │
                              │    └── 消费 data_task 队列                                     │
                              │                                                              │
                              │  SQLite (WAL)  ./data/stock.db        ./cache/               │
                              └──────────────────────────────────────────────────────────────┘
```

### 1.1 进程与启动顺序

`entrypoint.sh`：

1. 后台启动 `python update_worker.py &`；
2. 前台 `exec streamlit run streamlit_app.py --server.port=8501`。

因此 Worker 与应用共享容器生命周期；应用是 PID 1 的主进程，Worker 崩溃不会自动重启（`restart: unless-stopped` 只覆盖整个容器）。

### 1.2 网络与 DNS

- 容器显式配置公共 DNS（`223.5.5.5`、`119.29.29.29`、`8.8.8.8`），规避 NAS DNS 劫持。
- `doh_patch.py` 提供 DoH 兜底：系统 DNS 失败时走阿里 DoH（应用与 Worker 均导入）。
- 境内数据源始终直连；代理只作用于境外新闻源（见第 10 节）。

---

## 2. 模块职责

| 层 | 模块 | 职责 | 不负责 |
|---|---|---|---|
| 表现层 | `streamlit_app.py` | 导航、8 个页面、图表、i18n、session 参数、缓存调用 | 不做数据下载 |
| 应用层 | `backtest_engine.py` | 因子、选股函数、事件驱动引擎、成本模型、绩效 | 不做 I/O 调度 |
| 应用层 | `fast_backtest.py` | 向量化选股矩阵、快速撮合、网格寻优 | 不支持有状态策略类 |
| 应用层 | `price_limits.py` | 板块/ST/改革日感知的涨跌停价与撮合拦截判定 | 不读行情、不记账 |
| 应用层 | `robust_backtest.py` | K 段稳健分、冠军诊断、滚动样本外聚合 | 不直接调用具体回测引擎 |
| 数据服务 | `data_manager.py` | 交易日历、缺口规划、财务/分红、全球市场、基准指数 | 不直接渲染 |
| 存储层 | `data_store.py` | SQLite 建表/读写、下载、回测缓存、任务队列、设置 KV | 不做策略逻辑 |
| 采集层 | `news_fetcher.py` | 多源抓取、去重、翻译、代理分流 | 不做情绪分析（字段预留） |
| 采集层 | `htbr_source.py` | 华泰柏瑞 ESA 数据源封装：限流（3 秒）、配额硬闸（450/日）、统一错误语义 | 不做业务编排（由 `data_manager` 负责） |
| 采集层 | `vendor/htbr/` | 技能运行时（`api_client.py`/`config.py`/`guardrails.py`），凭据经 `/root/.htbr` 只读挂载 | 不含凭据 |
| 调度层 | `update_worker.py` | 定时任务、自举、任务消费、缓存预热 | 不提供 HTTP |
| 基础设施 | `doh_patch.py` | DoH 兜底 | — |

### 2.1 模块依赖方向

```
streamlit_app ──▶ backtest_engine ──▶ (pandas/numpy)
      │                ▲
      ├──▶ fast_backtest ┘（复用 backtest_engine 的撮合与成本）
      ├──▶ data_store ◀── data_manager ◀── update_worker
      └──▶ news_fetcher ──▶ data_store
```

约束：`data_store` 不反向依赖任何业务模块；`data_manager` 可以依赖 `data_store`；UI 不直接写库（除设置项）时应经 `data_manager`/`data_store` 的既有函数。

---

## 3. 页面与导航

一级菜单 + 二级菜单（`MAIN_PAGES` / `STRATEGY_SUBPAGES` / `MORE_SUBPAGES`）：

| 一级 | 二级 | 路由 key | 说明 |
|---|---|---|---|
| 量化大师 | — | `home` | 全球行情、资讯摘要、5 宫格快捷入口、策略绩效对比 |
| 量化策略 | 策略回测 | `backtest` | 策略与参数、资金曲线、逐笔交易、交易日历、持仓明细 |
| 量化策略 | 今日选股 | `picks` | 最新交易日 Top N，自动写入 `pick_record` |
| 量化策略 | 投资组合 | `portfolio` | 以最新选股等权模拟建仓，浏览器直连算浮盈 |
| 资讯 | — | `news` | 聚合资讯列表与详情跳转 |
| 更多 | 参数设置 | `settings` | 语言、交易成本、回测区间、代理、新闻源开关 |
| 更多 | 数据管理 | `data` | 数据库概览、覆盖状态、任务队列、手动更新 |
| 更多 | 帮助 | `help` | 上手、功能说明、FAQ、免责声明 |

额外事实：

- 顶部快捷入口在除首页外的页面固定显示，当前页按钮 disabled 并由 CSS 高亮；首页不显示该行。
- 路由中还存在 `page == "author"`（自定义策略编辑器，`exec` 用户代码），但**未注册到任何导航列表，当前不可达**。见 01 的 P1-3。

---

## 4. 数据流

### 4.1 数据自举与增量（离线链路）

```
空库启动
  └─ update_worker.bootstrap()
       ├─ data_store.get_main_board_codes()  → 全部主板股票
       ├─ data_store.get_etf_codes()         → 全部 ETF
       └─ data_manager.ensure_bars(...)      → 并发下载 → upsert_bars（幂等）→ 写 data_coverage

交易日 15:35
  └─ after_close()
       ├─ data_manager.after_close_sync()    → 只补当日缺口
       ├─ news_fetcher.fetch_all_news()
       └─ data_store.load_feat_cached()      → 预热因子缓存
```

### 4.2 策略驱动补数（按需链路）

```
页面声明需求 request_strategy_data()
  └─ data_manager.plan_bars(symbols, start, end)  → 用 data_coverage 算缺口
       ├─ 有缺口 → data_store.task_create("daily_bar", ...)
       └─ 财务覆盖率 < 50% → task_create("fundamental", ...)

update_worker 主循环
  └─ data_store.task_next_pending() → run_task() → 更新 status/progress
```

原则：**页面线程绝不下载数据**，只排队；下载全部由 Worker 完成。

**日期口径（易错点）**：`daily_bar.MAX(date)` 会被个别标的（指数、刚补过单根K线的股票）抬高，不等于全市场覆盖日期。因此：

| 用途 | 取值 |
|---|---|
| 回测默认终点（UI `DEFAULT_END`） | `db_stats.coverage_last_date`（当日 ≥1000 只标的有K线的最后一天） |
| 盘后增量补全起点 | `coverage_last_date` 的次日（不是 `MAX(date)+1`，否则中间缺口永远补不到） |
| 盘后增量补全终点 | 已收盘交易日：`CLOSE_HOUR:CLOSE_MINUTE` 之后用当天，否则退到前一交易日 |
| 持仓估值 | 当日无K线时用 `BarIndex.last_row`（最近可用收盘价），不得按 0 估值 |

### 4.3 回测链路

```
页面参数（策略/区间/成本/资金/持仓数）
  └─ bt_cache_key(v14, 参数..., 成本指纹) + data_fingerprint
       ├─ SQLite 命中 → deserialize_result → 直接渲染
       └─ 未命中 → run_backtest_auto
             ├─ fast_backtest.supports_fast(select_fn)?
             │     ├─ 是 → run_fast()（向量化选股 + 复用撮合）
             │     └─ 否 → run_backtest()（每日事件循环）
             └─ 写回 backtest_cache + 界面临时记录 strategy_run
```

### 4.4 实时行情链路（浏览器直连）

```
页面渲染一个自包含 HTML 组件（st.components / markdown HTML）
  └─ 浏览器 setInterval 1–2s
       └─ fetch push2.eastmoney.com/api/qt/ulist.np/get?secids=...
            └─ 原地更新 DOM（不触发 Streamlit rerun）
```

- `secid` 规则：`1.` + 沪市代码，`0.` + 深市代码（与东财一致）。
- 全球指数卡片、持仓浮盈、K 线迷你图均走该链路。
- 后端 `realtime_quote` 表只服务模拟撮合与历史回看，不参与页面刷新。

---

## 5. 数据库模型

SQLite 单库 `data/stock.db`，WAL 模式。共 18 张业务表 + SQLite 内部表 `sqlite_sequence`。

| 表 | 主键 | 用途 | 关键字段 |
|---|---|---|---|
| `daily_bar` | `(symbol, date)` | 前复权日线 | open/high/low/close/volume/amount/outstanding_share/turnover/asset_type |
| `data_coverage` | `(dataset, symbol)` | 已同步区间索引，只补缺口 | start_date/end_date/rows |
| `fundamental` | `(symbol, report_date)` | 季报 | ann_date/eps/bps/revenue/net_profit/revenue_yoy/profit_yoy/roe/gross_margin/debt_ratio/industry/pe_ttm/pb/market_cap/ocf_per_share/operating_profit/is_st |
| `dividend` | `(symbol, report_date)` | 分红历史（6 财年） | cash_div_ps/ann_date |
| `realtime_quote` | `(symbol, ts)` | 交易时段快照 | price/pct/open/high/low/pre_close/volume/amount/turnover |
| `data_task` | `id` | 策略驱动的补数队列 | dataset/params/status/progress/message |
| `strategy_run` | `id` | 每次回测/选股记录 | kind/strategy/params/metrics(JSON) |
| `pick_record` | `id` | 选股明细，后续验证胜率 | run_id/symbol/pick_date/close/ret_20/cap_yi/turnover_pct |
| `backtest_cache` | `cache_key` | 回测结果持久缓存 | strategy/params/fingerprint/payload(JSON) |
| `news` | `id` | 新闻与资讯 | date/source/symbol/title/content/sentiment/title_hash/source_time/link |
| `download_log` | `symbol` | 下载流水 | status/rows/first_date/last_date |
| `app_settings` | `key` | 运行时配置 KV | value/updated_at |
| `etf_meta` | `symbol` | ETF 代码映射 | secu_code/inner_code/fund_name |
| `etf_quote_ext` | `(symbol, date)` | ETF 真实行情增强（华泰柏瑞） | turnover_value(元)/turnover_rate/discount_ratio/premium_ratio/is_suspend |
| `etf_capital` | `(symbol, date)` | ETF 单位净值/份额/规模/净申购 | unit_nv/exchange_shares(份)/market_scale(元)/net_sub_shares |
| `etf_margin` | `(symbol, date)` | ETF 两融明细 | finance_buy/finance_balance/finance_pct |
| `etf_market_flow` | `(date, category)` | 全市场 ETF 资金流（6 类） | buy_value/sell_value/net_value/etf_count |
| `etf_valuation` | `(symbol, report_date)` | ETF 持仓加权估值（报告期） | pe_ttm/pb/dividend_ratio |
| `sqlite_sequence` | — | SQLite 自增序列 | — |

### 5.1 一致性约束

- 写入均为 `INSERT OR REPLACE` / upsert，**幂等**。
- 行情主键 `(symbol, date)` 保证同一标的同一天只有一行。
- 新闻以 `title_hash` 去重，并保留 `source_time` 最早的一条。
- `data_coverage` 是缺口计算的唯一依据；手工导入 CSV 后需回填覆盖索引（`backfill_coverage`）。

### 5.2 迁移策略

没有迁移框架。`init_db()` 用 `CREATE TABLE IF NOT EXISTS` + `ALTER TABLE ... ADD COLUMN` 包在 `try/except` 中做兼容升级（如 `daily_bar.asset_type`、`fundamental.bps/ocf_per_share/operating_profit/is_st`、`news.link`）。

新增字段时的约定：

1. 在 `init_db` 的建表语句中加列；
2. 同时追加一段 `ALTER TABLE ... ADD COLUMN`，兼容旧库；
3. 读取侧用 `_col(row, names, default)` 之类的方式做缺省处理。

---

## 6. 回测引擎

### 6.1 事件驱动引擎（`backtest_engine.py`）

```
MarketBarEvent ──▶ Strategy.on_bar ──▶ SignalEvent ──▶ OrderEvent ──▶ FillEvent
                                                          │
                                                 CostModel 计算成交价与费用
                                                          │
                                                     Portfolio 更新现金/持仓
```

- 时序：T 日收盘后产生信号，T+1 开盘撮合，无前视。
- 函数式策略经 `FunctionStrategyAdapter` 适配；有状态策略继承 `BaseStrategy`（如 `MarketCapStrategy`）。
- 有状态策略每日都能拿到全市场快照，可自行决定再平衡；函数式策略只在选股日拿到快照。
- 固定持有期与涨停退出只对函数式策略生效；自定义策略类自行管理退出。
- 订单支持两种来源：策略直接给出的 `rebalance_orders`（指定数量），或引擎按 `top_n` 等分买入。

### 6.2 向量化引擎（`fast_backtest.py`）

- `SelectionMatrix` 把“每个选股日的全市场截面”压成 `(S, N)` 的 numpy 矩阵；
- 选股退化为布尔掩码 + 稳定排序；
- 撮合、成本、持仓估值复用事件驱动的 `BarIndex` / `Portfolio` / `CostModel`，保证口径一致；
- 只支持 `_VEC_SELECTORS` 中注册的函数：`select_momentum`、`select_chip`、`select_breakout`、`select_random`、`select_smallcap_rotation`；
- 不在注册表中的策略（如 `MarketCapStrategy`）由 `supports_fast` 返回 False，自动回退事件驱动。

### 6.3 缓存层级

| 层 | 位置 | 键 | 失效条件 |
|---|---|---|---|
| 因子内存缓存 | Streamlit 进程（`@st.cache_resource(max_entries=1)`） | 数据指纹 | 指纹变化 / 进程重启 |
| 因子磁盘缓存 | `data/feat_cache.pkl` | 数据指纹 | 指纹变化 |
| 选股矩阵磁盘缓存 | `cache/factor_matrix/<sha1>/` | `MATRIX_VER=m1` + 指纹 + 需求列 + 选股日数 | 任一变化 |
| 回测结果缓存 | SQLite `backtest_cache` | `v14` + 策略 + 全部参数 + 成本指纹 + 数据指纹 | 任一变化 |
| 全球市场 | 内存 + `app_settings.global_markets_cache` | 时间戳 | TTL / Worker 刷新 |
| 基准指数 | 内存 + `app_settings.benchmark_sh000300` | 时间戳 | 24 小时 |

数据指纹定义：`股票数_最早日期_最新日期`（`data_store.data_fingerprint`）。

> 代码中的 `cached_backtest` 文档字符串声称存在 L1 `st.cache_data`，但当前**没有该装饰器**，实际只有 SQLite L2 与因子缓存。属于文档/实现偏差，已登记在 03 第 11 节。

---

## 7. 交易成本模型

`CostModel` 参数默认值（可在设置页覆盖，并参与缓存键）：

| 项 | 默认 | 方向 |
|---|---|---|
| 股票佣金率 | 0.0001154（万 1.154） | 双向，单笔最低 1 元 |
| ETF/LOF 佣金率 | 0.0001（万 1） | 双向，单笔最低 0.2 元 |
| 印花税 | 0.0005（0.05%） | 仅卖出，ETF/LOF 免征 |
| 过户费 | 0.00001 | 双向 |
| 滑点 | 0.001（0.1%） | 买加价、卖减价 |
| 冲击成本 | 订单额 / 当日成交额 > 5% | 触发后 `amount × ratio^1.5` |

---

## 8. 实时行情与全球市场

- 前端直连东财 `push2` 的 `ulist.np`（实时）与 `push2his`（K 线），1–2 秒轮询，DOM 原地更新。
- 全球市场由 Worker 抓取，写入内存与 `app_settings`，交易时段 5 秒刷新、非交易时段 300 秒。
- 品种：A 股指数、港股、美股、黄金/原油、汇率；无涨跌幅的品种用前后两次快照计算 tick 变动。

---

## 9. 新闻系统

| 类别 | 源 | 说明 |
|---|---|---|
| 境内 | 东方财富、同花顺、财联社、新浪财经 | 经 akshare 接口，境内域名强制直连 |
| 境外 | BBC、CNN、Al Jazeera、CNBC、Guardian、MarketWatch、Investing、Reuters(Google News)、Bloomberg | 公开 RSS，无 API Key，按需代理 |

处理流程：

```
并发抓取 → 清洗 HTML → 取各源发布时间 → 中文环境且为英文 → 翻译
        → 按标题哈希去重（保留最早来源）→ 写 news 表
```

代理分流规则：`auto`（默认）按域名后缀判断，境内直连、境外走代理；`always` 全走代理；`off` 全直连。代理探测失败时本轮自动降级直连，避免逐源超时。

---

## 10. 配置体系

两套配置并存：

1. **环境变量**：部署态参数（盘后时间、采样间隔、财务刷新日、自举数量），见 05 第 3 节。
2. **`app_settings` KV**：运行时可改项（交易成本、代理、新闻源开关、语言不落库、全球市场与基准缓存），设置页读写。

注意：`data_store.DB_PATH` 是模块常量，不读环境变量。测试隔离必须直接改这个属性，见 03 第 8 节。

---

## 11. 已知架构问题

| 问题 | 影响 | 对应计划 |
|---|---|---|
| `streamlit_app.py` 单文件约 3473 行，页面/样式/脚本混在一起 | 维护成本高、冲突概率大 | P1-1 |
| `author` 页面已实现但不可达，且使用 `exec` 执行用户代码 | 死代码 + 安全面 | P1-3 |
| `STRATEGY_REQUIREMENTS` 残留 3 个未注册策略 | 需求声明与实际策略不一致 | P0-2 |
| 3 个部署脚本重复实现且硬编码凭据 | 安全风险、改一处要改三处 | P0-3 |
| 无 `.gitignore` / 无版本控制 | 数据库与缓存可能被误提交 | P0-4 |
| i18n 覆盖不完整 | 英文模式下仍出现中文 | P1-2 |
| 测试覆盖不完整 | 成本、绩效、缓存键缺回归保护 | P0-1（调仓已完成，其余待补） |
| `cached_backtest` 注释与实际缓存层不符 | 误导维护者 | P1-6 顺带修正 |
| Worker 与 Web 同容器，Worker 崩溃不自动重启 | 数据停止更新但页面仍可访问 | P1-5 告警 |
