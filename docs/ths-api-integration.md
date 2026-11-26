# 同花顺金融数据 API 接入说明

> 数据源官方文档：<https://fuyao.aicubes.cn/docs/>
> 接入代码：`ths_source.py`　测试：`tests/test_ths_source.py`
> 文档与代码均按 2026-11-26 的真实文档 + 真实 Key 探针核对

---

## 1. 能力梳理

### 1.1 已接入能力

| 能力 | 端点 | 本模块函数 | 项目用途 |
|---|---|---|---|
| A 股历史 K 线（批量） | `GET /api/a-share/prices/historical-batch` | `fetch_daily_bars()` | 回补 `daily_bar` 股票日线 |
| ETF 历史日线（批量） | `GET /api/fund/market/historical-batch` | `fetch_daily_bars()` | 回补 `daily_bar` ETF 日线 |
| A 股 / ETF 快照 | `/api/a-share/prices/snapshot`、`/api/fund/market/snapshot` | `fetch_snapshot()` | 写 `realtime_quote`，支撑模拟交易 |
| 标的列表 | `GET /api/meta/tickers/list` | `list_tickers()`、`ticker_index()` | 名称解析、股票/ETF 类型判定 |
| 标的检索 | `GET /api/meta/tickers/search` | `search_tickers()` | 按名称找代码（如“纳指”） |
| 交易日历 | `GET /api/a-share/calendar/trading-days` | `fetch_trading_days()` | 校验/对齐交易日 |
| 财务报表 | `/api/a-share/financials/{income-statements,balance-sheets,cash-flow-statements}` | `fetch_financials()` | 写 `fundamental` |
| 财务指标 | `GET /api/a-share/financials/indicators` | `fetch_financial_indicators()` | 毛利率/ROE/负债率/同比 |
| 估值快照 | `GET /api/a-share/valuations/snapshot` | `fetch_valuations()` | PE-TTM / PB / PS / PCF |

### 1.2 未接入（官方未开放或与项目无关）

- 主力资金、高频动向、资讯事件库：官方注明仅同花顺 AI 客户端可用。
- 期货 / 期权 / 港股：项目只做 A 股与场内基金。
- Market Dumps（Parquet 批量下载）：与逐标的接口重复，暂不引入。

---

## 2. 鉴权与密钥管理

| 项 | 规范 |
|---|---|
| 请求头 | `X-api-key: <your-api-key>` |
| 密钥来源 | 环境变量 `THS_API_KEY`，其次项目根目录 `.env` |
| 硬编码 | **禁止**；`.env` 已在 `.gitignore` 中 |
| 日志脱敏 | 统一用 `ths_source.mask_key()`，只显示 `sk-fuyao***b48L` |
| 失效处理 | `code=2001` 未认证 / `code=2003` 无权限，立即失败不重试 |

`.env` 写法（不要提交）：

```ini
THS_API_KEY=sk-fuyao-xxxxxxxxxxxxxxxxxxxx
```

NAS 部署时通过容器环境变量注入，或使用与 `nas_conf.py` 同级的本地私密文件，不进入镜像。

---

## 3. 请求与响应规范

### 3.1 统一信封

```json
{
  "code": 0,
  "message": "success",
  "request_id": "a1b2c3d4",
  "data": {"timestamp": 1716105600000, "item": []}
}
```

- 业务结果看 `code`，HTTP 状态码通常恒为 200；限流时可能直接返回 HTTP 429。
- 时间戳一律毫秒级 Unix，时区 `Asia/Shanghai`。
- 标的必须使用带后缀的 `thscode`（`600519.SH` / `513100.SH`），不接受纯 6 位代码。

### 3.2 错误码映射（`ths_source` 已内置）

| 官方 code | 异常类型 | 是否重试 |
|---|---|---|
| `1001`—`1004` 参数错误 | `ThsParamError` | 否 |
| `2001` 未认证 / `2003` 无权限 | `ThsAuthError` | 否 |
| `3001` 标的不存在 / `3002` 未就绪 / `3004` 类型不支持 | `ThsNoDataError` | 否 |
| `4001` 限流 / HTTP 429 | `ThsRateLimitError` | 是（退避更保守） |
| `5001`—`5003` 服务端或上游异常 | `ThsUpstreamError` | 是 |
| 网络超时 / DNS 失败 | `ThsUpstreamError` | 是 |
| 超出可提供区间（如 ETF 请求 10 年） | `ThsRangeError` | 否，需调用方改区间 |

重试策略：最多 3 次，指数退避 `1.5s → 3s → 6s`；鉴权/参数类错误不重试，避免浪费配额。

### 3.3 关键限制（实测确认）

| 数据 | 限制 | 本模块处理 |
|---|---|---|
| A 股历史 K 线 | 单次窗口 ≤ 10 自然年，批量 ≤ 10 个代码 | 自动按 10 年切段、按 10 个分批 |
| ETF 历史日线 | 单次窗口 ≤ 5 自然年，**且只能取到近 5 年** | `strict=True` 抛 `ThsRangeError`；落库时自动截断并回报 `clipped` |
| 快照 | 单次 ≤ 100 个代码 | 自动分批 |
| 财报 | 单只标的；`limit` ≤ 20 与 `start/end` 互斥 | 参数互斥校验 |
| 调用频率 | 不限累计次数，但要求控制并发 | 0.2s 最小间隔 + 指数退避 |

> **项目影响**：ETF 2014—2020 年的日线无法从该源获取，必须继续由现有源（akshare / 交易所 / 华泰柏瑞）回补。该源适合做**近年交叉校验**与**增量更新**。

---

## 4. 接入架构

```
业务层（data_manager / streamlit / 策略）
        │  sync_daily_bars / sync_snapshot / sync_fundamentals
        ▼
接入层 ths_source.py
  ├─ 鉴权层   get_api_key / mask_key      密钥只从 env/.env 读, 日志脱敏
  ├─ 请求层   _http_get_json             限速 + 重试 + 信封校验 + 错误码映射
  ├─ 领域层   fetch_daily_bars / fetch_snapshot / fetch_financials ...
  ├─ 转换层   _bars_frame / _norm_snapshot / build_fundamental_rows
  └─ 落库层   sync_*  → data_store.upsert_bars / save_realtime / upsert_fundamentals
        ▼
存储层 data_store（daily_bar / realtime_quote / fundamental / data_coverage）
```

设计要点：

1. **统一请求封装**：所有端点只走 `_http_get_json`，重试/限速/错误映射只实现一次。
2. **数据格式转换层**：官方字段（`date_ms`、`open_price`、`turnover`）统一转成项目字段（`date`、`open`、`amount`），上层无感知。
3. **失败隔离**：`sync_daily_bars` / `sync_fundamentals` 单标的失败只记入 `errors`，不影响整批。
4. **不臆造字段**：接口没有的口径（BPS、商誉、单期 PE）留空，不用估算值污染库。

---

## 5. 调用示例

### 5.1 连通性自检

```bash
py -3.13 ths_source.py --check
```

```json
{
  "base_url": "https://fuyao.aicubes.cn",
  "api_key": "sk-fuyao***b48L",
  "ok": true,
  "trading_days": 243,
  "last_trading_day": "2026-11-26"
}
```

### 5.2 取日线（内存，不落库）

```python
import ths_source as ts

# 股票: 自动按 10 年切段 + 每 10 只分批
bars = ts.fetch_daily_bars(["sh600519", "sz000001"], "2024-01-01", "2024-02-01")
print(bars["sh600519"].tail())

# ETF: 超出近 5 年会抛 ThsRangeError
bars = ts.fetch_daily_bars(["sh513100"], "2021-12-10", "2026-11-25")
```

### 5.3 落库

```python
report = ts.sync_daily_bars(["sh513100", "sh513500"], "2024-01-01", "2026-11-25")
# {"symbols": 2, "rows": 1400, "written": 2, "empty": [], "clipped": [], "errors": []}
```

### 5.4 实时快照

```python
ts.sync_snapshot(["sh513100", "sh600519"])   # 写入 realtime_quote
```

### 5.5 财务与估值

```python
rows = ts.build_fundamental_rows("sh600519", quarters=4)  # 可直接 upsert_fundamentals
ind = ts.fetch_financial_indicators("sh600519", "2025-4")
val = ts.fetch_valuations(["sh600519"])                  # pe_ttm / pb_mrq / ...
```

### 5.6 按名称找代码

```python
ts.search_tickers("纳指", asset_type="fund-etf", limit=5)
# [{'thscode': '159501.SZ', 'name': '纳指ETF嘉实'}, ...]
```

---

## 6. 常见问题排查

| 现象 | 原因 | 处理 |
|---|---|---|
| `ThsAuthError: code=2001` | 密钥缺失/写错/被撤销 | 检查 `.env` 的 `THS_API_KEY`，在官方后台重新签发 |
| `ThsAuthError: code=2003` | 当前 Key 无该能力权限 | 找服务方开通对应 capability |
| `ThsParamError: code=1003` | 窗口越界或参数非法 | A 股 ≤10 年、ETF ≤5 年、批量 ≤10 个 |
| `ThsRangeError: ETF 历史日线只能回溯到 ...` | 请求了 5 年以前的 ETF | 改用其他源回补，或 `strict=False` 只取可得区间 |
| `ThsRateLimitError: HTTP 429` | 并发过高或瞬时集中调用 | 降低并发，本模块已自动退避，不要手动立即重试 |
| `ThsNoDataError: code=3002` | 标的当天无报价（停牌/未开盘） | 跳过该标的，收盘后重试 |
| `ThsUpstreamError: HTTP 502/504` | 上游数据源抖动 | 已自动重试 3 次；仍失败则稍后再跑 |
| 返回 `code=0` 但 `item=[]` | 该区间确实无数据（未上市/已退市） | 核对 `list_date`，不要当异常 |
| 财务同比字段为空 | 文档名与真实字段名不一致 | 已用 `_pick()` 兼容 `calculate_*` 前缀别名 |
| 快照 `turnover` 为空 | A 股快照接口不返回换手率 | 仅 ETF 有 `turnover_ratio_pct`，属正常 |
| 落库后回测结果变化 | 该源为**前复权**，与其他源复权基准可能不同 | 同一标的不混用多源复权价；全量替换而不是拼接 |

---

## 7. 测试

```bash
py -3.13 -m pytest tests/test_ths_source.py -q
```

覆盖点（26 项，全部离线，用假响应）：

- 代码互转（股票 / ETF / 北交所 / 大小写 / 非法输入）
- 密钥读取与脱敏、缺失时报错
- 错误码映射、鉴权不重试、限流重试后成功、网络异常重试、非 JSON 响应
- A 股 10 年切段、10 只分批、跨段去重（后取覆盖先取）
- ETF 5 年越界（严格抛错 / 非严格截断）、LOF 拒绝
- 快照归一化（ETF 换手率、时间戳时区）
- 财务映射与真实字段别名兼容
- 落库写入 `daily_bar` 与覆盖索引、超区间回报 `clipped`
