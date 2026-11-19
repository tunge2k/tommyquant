# DESIGN_ETF数据源接入

> 6A 阶段 2（Architect）· 决策依据：用户选择「全量落库增强」+ 首批四类数据集 + 每日覆盖「持仓 + 动量前 60 只」+ 历史成交额仅近期使用

## 1. 总体架构

```
┌────────────────────────── NAS 容器 tommyquant (Python 3.13) ──────────────────────────┐
│                                                                                      │
│  update_worker.after_close_sync                                                       │
│      └─ data_manager.ensure_etf_extras()          ← 编排：池子 / 配额 / 顺序 / 落库     │
│             ├─ htbr_source.invoke(api_id, params) ← 限流 3s / 配额计数 / 错误语义       │
│             │      └─ subprocess: vendor/htbr/scripts/api_client.py                    │
│             │             └─ HTTPS → esa.huatai-pb.com/api/v1                          │
│             └─ data_store.upsert_etf_*()          ← 归一化后写 SQLite                   │
│                                                                                      │
│  Streamlit 数据管理页 ← data_store.etf_ext_stats()                                    │
└──────────────────────────────────────────────────────────────────────────────────────┘
        ▲ 凭据挂载: ./secrets → /root/.htbr:ro （不进 git / 不进镜像）
```

## 2. 模块划分与职责

| 模块 | 新增/修改 | 职责 |
|---|---|---|
| `vendor/htbr/` | 新增（技能运行时代码，原样保留） | `config.py` 鉴权/凭据、`guardrails.py`、`scripts/api_client.py`、`SKILL.md`（版本号）、`config.local.json`（网关地址） |
| `htbr_source.py` | 新增 | 唯一对外入口：`available()` / `invoke()` / `resolve_inner_code()` / `quota_status()`；串行 3 秒限流、429 退避、配额硬闸、stdout JSON 解析 |
| `data_store.py` | 修改 | 建表 + upsert/query：`etf_meta` / `etf_quote_ext` / `etf_market_flow` / `etf_margin` |
| `data_manager.py` | 修改 | `ensure_etf_extras()`：组池 → 逐接口取数 → 归一化写库 → 返回统计 |
| `update_worker.py` | 修改 | 盘后调用 `ensure_etf_extras`；`run_task` 支持 `dataset="etf_extra"` |
| `streamlit_app.py` | 修改 | 数据管理页新增 ETF 增强数据覆盖指标；手动补数按钮 |
| `deploy_update.py` | 修改 | 上传 `vendor/` 目录与 `htbr_source.py`；一次性同步凭据到 NAS `secrets/` |
| `docker-compose.yml` | 修改 | 增加 `./secrets:/root/.htbr:ro` 挂载 |
| `tests/test_htbr_source.py` | 新增 | 解析/归一化/配额闸/失败降级的单元测试 |

## 3. 数据表设计（单位统一为「元 / 份」）

```sql
CREATE TABLE etf_meta (
  symbol TEXT PRIMARY KEY,      -- sh510300
  secu_code TEXT,               -- 510300
  inner_code TEXT,              -- 16930（来自 fund-daily-quote.fundobject，免额外 resolve）
  fund_name TEXT,
  updated_at TEXT
);

CREATE TABLE etf_quote_ext (            -- fund-daily-quote
  symbol TEXT, date TEXT,
  turnover_value REAL,                  -- 成交额（元）= turnovervalue(万元)*1e4
  turnover_volume REAL,                 -- 成交量（股）= turnovervolume(亿股)*1e8
  turnover_rate REAL,                   -- 换手率（%）
  avg_price REAL, change_pct REAL, range_pct REAL,
  discount_ratio REAL, premium_ratio REAL,
  is_suspend INTEGER,
  updated_at TEXT, PRIMARY KEY (symbol, date)
);

CREATE TABLE etf_capital (              -- etf-capital-report（unit=3 固定）
  symbol TEXT, date TEXT,
  unit_nv REAL, close_price REAL,
  exchange_shares REAL,                 -- 场内份额（份）= exchangeshares*1e4
  market_scale REAL,                    -- 规模（元）= marketscale*1e4
  net_sub_shares REAL,                  -- 净申购份额（份）
  finance_net_buy REAL, finance_balance REAL,
  near3_netbuy REAL, near5_netbuy REAL, near20_netbuy REAL, near60_netbuy REAL,
  updated_at TEXT, PRIMARY KEY (symbol, date)
);

CREATE TABLE etf_margin (               -- etf-margin（indexCycle=1, unit=3）
  symbol TEXT, date TEXT,
  finance_buy REAL, finance_refund REAL, finance_net_buy REAL,
  security_sell_vol REAL, security_refund_vol REAL, security_net_sell_vol REAL,
  finance_balance REAL, security_balance REAL, security_volume REAL,
  finance_pct REAL, security_pct REAL,
  updated_at TEXT, PRIMARY KEY (symbol, date)
);

CREATE TABLE etf_market_flow (          -- etf-capital-flow-stats（全市场）
  date TEXT, category TEXT,             -- all / stock / bond / commodity / monetary / qdii
  buy_value REAL, sell_value REAL, net_value REAL, etf_count INTEGER,
  PRIMARY KEY (date, category)
);
```

## 4. 取数编排（每日盘后，配额预算 ≤ 450/日）

| 步骤 | 接口 | 入参 | 调用次数 |
|---|---|---|---|
| 1 | `etf-capital-flow-stats` | `beginDate`=近 5 日（yyyyMMdd）、`indexCycle=1`、`unit=1` | 1 |
| 2 | `fund-daily-quote` | `secuCode`、`beginDate`=近 5 日、`endDate`=当日、`adjustingMethod=3` | ≤60 |
| 3 | `etf-capital-report` | `innerCode`、`beginDate`=近 5 日、`unit=3` | ≤60 |
| 4 | `etf-margin` | `innerCode`、`beginDate`=近 5 日、`indexCycle=1`、`unit=3` | ≤60 |
| 5 | `fund-valuation` | `innerCode`、近 1 年报告期 | 每标的**每月 1 次**（缓存判定） |

- 池子 = 近 30 日 `pick_record` 中的 ETF ∪ DFS 动量排名前 `etf_extras_pool`（默认 60，UI 可调）。
- `innerCode` 缓存于 `etf_meta`；首次用 `fund-daily-quote.fundobject` 回填，缺失才调 `fund-resolve`。
- 顺序执行，间隔 ≥3s；本地配额计数存 `app_settings['htbr_calls_YYYYMMDD']`，达到 450 即停止并标记任务 `failed(配额保护)`。
- 429 `RATE_LIMITED`：等待 6s 重试 1 次，仍失败则跳过该标的并计数。
- 429 `QUOTA_EXCEEDED` / 退出码 3（凭据问题）：立即终止整批，保留已写入数据。

## 5. 接口契约（`htbr_source.py`）

```python
class HtbrError(Exception)            # 基类
class HtbrNoCredential(HtbrError)     # 凭据缺失/失效（退出码 3）
class HtbrQuotaExceeded(HtbrError)    # 当日配额耗尽
class HtbrRateLimited(HtbrError)      # 频率超限（重试后仍失败）
class HtbrUnavailable(HtbrError)      # 网关/接口不可用（503/超时）

def available() -> tuple[bool, str]                  # (是否可用, 原因)
def invoke(api_id: str, payload: dict) -> dict       # 返回完整统一响应体
def quota_status() -> dict                           # {used, limit, date}
def resolve_inner_code(symbol: str) -> str | None    # 读缓存 → 调 fund-resolve
```

不变式：
- 任何异常都不向外抛 traceback，`ensure_etf_extras` 统一捕获并写任务日志。
- Key/Token 不进日志；`subprocess` 的 stdout/stderr 分离，只解析 stdout。
- 返回为 `{"success": true, "data": ...}`；`meta.dataSource` 记录到日志用于溯源。

## 6. 数据流与幂等

- 所有写入使用 `INSERT OR REPLACE`，主键 `(symbol, date)` → 重复跑不产生脏数据。
- 增量窗口固定「近 5 个交易日」，覆盖上次遗漏；历史回补为独立慢任务（每日配额剩余部分推进）。
- 字段缺失（`null`）写 `NULL`，不写 0；查询层用 `COALESCE` 处理展示。

## 7. 异常处理矩阵

| 情况 | 行为 |
|---|---|
| `vendor/` 缺失或无凭据 | `available()` 返回 False；盘后任务记 warning 跳过；UI 显示「未启用」 |
| 网关不可达 / 503 | 该标的跳过，任务最终 `failed`（含原因），不阻塞行情主链路 |
| 429 频率超限 | 等待 6s 重试 1 次，仍失败跳过 |
| 429 配额耗尽 | 立即终止整批，已写入数据保留 |
| 返回空记录 | 记 0 条，不报错（正常语义） |
| 单标的字段异常（字符串数值） | 统一 `_to_float()` 转换，非法值置 `None` |
| 技能提示新版本 | 仅写日志，不自动更新（人工决定） |

## 8. 部署与安全

1. `vendor/htbr/` 随 `deploy_update.py` 上传到 `/app/vendor/htbr`（只传 `*.py` + `SKILL.md` + `config.local.json`）。
2. `docker-compose.yml` 增加只读挂载 `./secrets:/root/.htbr:ro`；`secrets/htbr/credentials.json` 由部署脚本从本机 `~/.htbr/credentials.json` 上传一次，权限 600。
3. `.gitignore` 增加 `secrets/`；`vendor/` 可入库（不含凭据）。
4. 容器重建后凭据随挂载保留；`config.py status` 可在容器内验证。

## 9. 验收口径（供 Assess 阶段核对）

1. 容器内 `config.py status` = configured；`etf_meta` 覆盖池内全部标的。
2. `etf_quote_ext` 最近交易日行数 ≥ 池子规模 × 90%；成交额与东财/新浪量级一致（误差 <5%）。
3. `etf_capital` 的 `market_scale` 与「单位净值 × 场内份额」自洽（相对误差 <1e-6）。
4. `etf_market_flow` 每个交易日 6 类齐全（无数据的类别可为 0 行，需在日志说明）。
5. 配额计数与实际调用次数一致；达到 450 时任务停止且状态为 `failed`。
6. 新字段全部缺失时，ETF 策略回测结果与接入前一致（不变式回归）。
