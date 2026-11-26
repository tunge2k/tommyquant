# -*- coding: utf-8 -*-
"""同花顺金融数据 API 接入层 (https://fuyao.aicubes.cn)

职责边界
--------
只做「取数 + 字段归一化 + 落库」, 不参与策略逻辑。落库统一走 `data_store`,
与 akshare / 交易所 / 华泰柏瑞等数据源共用 `daily_bar` / `realtime_quote` / `fundamental`。

官方规范要点 (2026-11 按官方文档核对, 并用真实 Key 探针验证)
-----------------------------------------------------------
- Base URL `https://fuyao.aicubes.cn`, 请求头 `X-api-key`; 密钥缺失/无效返回 `code=2001`。
- 业务响应恒 HTTP 200, 结果由信封 `code` 表达; 限流可能直接返回 HTTP 429 或 `code=4001`。
- 成功信封: `{"code":0,"message","request_id","data":{"timestamp","item":[...]}}`。
- 时间戳统一毫秒级 Unix, 时区 `Asia/Shanghai`; 标的必须用带后缀 thscode (`600519.SH`)。
- A 股历史 K 线: 批量最多 10 个 thscode, 单次 `[start,end]` 窗口 ≤ 10 自然年。
- ETF 历史日线: 仅支持 ETF, 批量最多 10 个, 单次窗口 ≤ 5 自然年, **且只能取到近 5 年**。
  这是本数据源相对项目的硬限制: 2014—2020 的 ETF 日线必须由其他数据源回补。
- 财报: 单只标的, `limit`(最近 N 期, ≤20) 与 `start/end`(闭区间, ≤10 年) 互斥。
- 当天没有「累计调用次数」上限, 但要求控制并发; 本模块默认 0.2s 最小间隔 + 指数退避重试。

安全约定
--------
密钥只从环境变量 `THS_API_KEY` 或项目根目录 `.env` 读取, 不写入代码;
异常与日志一律用 `mask_key()` 脱敏, 不打印完整密钥。
"""
from __future__ import annotations

import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Iterable, Sequence

import pandas as pd
import requests

import data_store

BASE_URL = "https://fuyao.aicubes.cn"
API_KEY_ENV = "THS_API_KEY"

TIMEOUT = 30                     # 单次请求超时(秒)
MAX_RETRIES = 3                  # 网络/限流/上游错误最大重试次数
BACKOFF_BASE = 1.5               # 指数退避基数(秒): 1.5 / 3 / 6
MIN_INTERVAL = 0.2               # 两次请求的最小间隔(秒), 防止瞬时高并发
CODE_BATCH = 10                  # 历史 K 线批量接口单次代码上限
SNAPSHOT_BATCH = 100             # 快照接口单次代码上限
STOCK_MAX_YEARS = 10             # A 股历史 K 线单次窗口上限(自然年)
ETF_MAX_YEARS = 5                # ETF 历史日线单次窗口上限(自然年)
ETF_FLOOR_BUFFER_DAYS = 7        # ETF 5 年边界留出的安全余量

BAR_COLUMNS = ["date", "open", "high", "low", "close", "volume", "amount"]

# 需要重试的业务码: 限流 + 上游超时/不可用/内部错误
_RETRY_CODES = {4001, 5001, 5002, 5003}


# ==================== 异常体系 ====================

class ThsError(RuntimeError):
    """同花顺数据源异常基类。code 为官方业务码, request_id 用于向服务方追溯。"""

    def __init__(self, message: str, code: int | None = None, request_id: str | None = None):
        super().__init__(message)
        self.code = code
        self.request_id = request_id

    def __str__(self) -> str:      # 便于排障日志直接带出业务码
        base = super().__str__()
        extra = []
        if self.code is not None:
            extra.append(f"code={self.code}")
        if self.request_id:
            extra.append(f"request_id={self.request_id}")
        return f"{base} ({', '.join(extra)})" if extra else base


class ThsAuthError(ThsError):
    """2001 未认证 / 2003 权限不足"""


class ThsParamError(ThsError):
    """1001—1004 参数错误"""


class ThsRateLimitError(ThsError):
    """HTTP 429 / 4001 触发限流"""


class ThsNoDataError(ThsError):
    """3001 标的不存在 / 3002 数据未就绪 / 3004 标的类型不支持"""


class ThsUpstreamError(ThsError):
    """5001—5003 服务端或上游异常、网络异常"""


class ThsRangeError(ThsParamError):
    """请求区间超出数据源可提供范围 (如 ETF 只能取近 5 年)"""


_CODE_EXC = {
    2001: ThsAuthError, 2003: ThsAuthError,
    1001: ThsParamError, 1002: ThsParamError, 1003: ThsParamError, 1004: ThsParamError,
    4001: ThsRateLimitError,
    3001: ThsNoDataError, 3002: ThsNoDataError, 3004: ThsNoDataError,
    5001: ThsUpstreamError, 5002: ThsUpstreamError, 5003: ThsUpstreamError,
}


# ==================== 密钥与鉴权 ====================

_ENV_LOADED = False


def _load_env_file(path: str | None = None) -> None:
    """极简 .env 加载器: 只补充缺失的环境变量, 不覆盖已有值。

    项目未引入 python-dotenv, 这里手写解析避免新增依赖。
    """
    global _ENV_LOADED
    if _ENV_LOADED:
        return
    _ENV_LOADED = True
    path = path or os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.exists(path):
        return
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, val = line.split("=", 1)
                key, val = key.strip(), val.strip().strip('"').strip("'")
                if key and not os.environ.get(key):
                    os.environ[key] = val
    except OSError:
        pass


def get_api_key() -> str:
    """读取 API Key: 环境变量优先, 其次项目 .env。缺失时抛 ThsAuthError。"""
    _load_env_file()
    key = (os.environ.get(API_KEY_ENV) or "").strip()
    if not key:
        raise ThsAuthError(f"未配置 {API_KEY_ENV}, 请在 .env 或环境变量中设置")
    return key


def mask_key(key: str | None = None) -> str:
    """密钥脱敏, 用于健康检查/日志。任何情况下都不返回完整密钥。"""
    try:
        key = key or get_api_key()
    except ThsError:
        return "(未配置)"
    if len(key) <= 12:
        return "***"
    return f"{key[:8]}***{key[-4:]}"


# ==================== 请求封装 (限速 + 重试) ====================

_throttle_lock = threading.Lock()
_last_call = [0.0]


def _throttle() -> None:
    with _throttle_lock:
        gap = time.time() - _last_call[0]
        if gap < MIN_INTERVAL:
            time.sleep(MIN_INTERVAL - gap)
        _last_call[0] = time.time()


def _raise_for_code(code: int, message: str, request_id: str | None) -> None:
    exc = _CODE_EXC.get(code, ThsUpstreamError)
    raise exc(f"同花顺接口返回失败: {message or '-'}", code=code, request_id=request_id)


def _http_get_json(path: str, params: dict | None = None) -> dict:
    """GET 并返回完整响应信封。

    重试策略: 网络异常 / HTTP 5xx / HTTP 429 / code∈{4001,5001,5002,5003} 走指数退避;
    鉴权(2001/2003)、参数(1001—1004)、无数据(3001/3002/3004) 立即失败, 不浪费配额。
    """
    url = BASE_URL + path
    headers = {"X-api-key": get_api_key(), "Accept": "application/json"}
    last_err = ""

    for attempt in range(MAX_RETRIES + 1):
        _throttle()
        retryable = False
        try:
            resp = requests.get(url, params=params, headers=headers, timeout=TIMEOUT)
        except requests.RequestException as ex:          # 连接/超时/DNS
            last_err = f"{type(ex).__name__}: {ex}"
            retryable = True
            if attempt == MAX_RETRIES:
                raise ThsUpstreamError(f"{path} 网络异常: {last_err}") from ex
        else:
            if resp.status_code == 429:
                last_err = "HTTP 429 触发限流"
                retryable = True
                if attempt == MAX_RETRIES:
                    raise ThsRateLimitError(f"{path} 触发限流, 请降低并发/频率")
            elif resp.status_code >= 500:
                last_err = f"HTTP {resp.status_code}"
                retryable = True
                if attempt == MAX_RETRIES:
                    raise ThsUpstreamError(f"{path} 服务端异常: {last_err}")
            elif resp.status_code != 200:
                raise ThsUpstreamError(f"{path} HTTP {resp.status_code}: {resp.text[:120]}")
            else:
                try:
                    payload = resp.json()
                except ValueError as ex:
                    raise ThsUpstreamError(f"{path} 响应不是合法 JSON: {resp.text[:120]}") from ex
                if not isinstance(payload, dict) or "code" not in payload:
                    raise ThsUpstreamError(f"{path} 响应缺少统一信封: {str(payload)[:120]}")
                code = payload.get("code")
                if code == 0:
                    return payload
                msg = str(payload.get("message") or "")
                rid = payload.get("request_id")
                if code in _RETRY_CODES:
                    last_err = f"code={code} {msg}"
                    retryable = True
                    if attempt == MAX_RETRIES:
                        _raise_for_code(code, msg, rid)
                else:
                    _raise_for_code(code, msg, rid)

        if retryable and attempt < MAX_RETRIES:
            # 限流场景退避更保守, 避免"立即连续重试"再次被限
            factor = 2 if "限流" in last_err else 1
            time.sleep(BACKOFF_BASE * factor * (2 ** attempt))

    raise ThsUpstreamError(f"{path} 请求失败: {last_err}")     # 理论不可达


def _data(payload: dict) -> dict:
    data = payload.get("data")
    return data if isinstance(data, dict) else {}


# ==================== 代码与类型转换 ====================

_SH_PREFIX = ("5", "6", "9")
_BJ_PREFIX = ("4", "8")


def to_thscode(symbol: str) -> str:
    """项目内部代码 -> 同花顺 thscode。

    'sh600519' / '600519' / '600519.sh' -> '600519.SH'; ETF 'sz159915' -> '159915.SZ'。
    """
    s = str(symbol).strip().lower()
    if not s:
        raise ThsParamError("空代码")
    if "." in s:
        code, suffix = s.split(".", 1)
        return f"{code.strip().upper()}.{suffix.strip().upper()}"
    if s[:2] in ("sh", "sz", "bj") and len(s) >= 7:
        return f"{s[2:].upper()}.{s[:2].upper()}"
    if len(s) != 6 or not s.isdigit():
        raise ThsParamError(f"无法识别的标的代码: {symbol}")
    if s[0] in _SH_PREFIX:
        return f"{s}.SH"
    if s[0] in _BJ_PREFIX:
        return f"{s}.BJ"
    return f"{s}.SZ"


def from_thscode(thscode: str) -> str:
    """同花顺 thscode -> 项目内部代码: '600519.SH' -> 'sh600519'。"""
    s = str(thscode or "").strip().upper()
    if "." not in s:
        raise ThsParamError(f"缺少交易所后缀: {thscode}")
    code, suffix = s.split(".", 1)
    return f"{suffix.lower()}{code}"


def _guess_asset_type(thscode: str) -> str:
    """按代码段推断资产类型 (无法访问元数据表时的兜底)。"""
    code, suffix = thscode.split(".", 1)
    if suffix in ("SH", "SZ") and code[:2] in ("51", "52", "56", "58", "50", "15", "16"):
        return "etf"
    return "stock"


_TICKER_CACHE: dict = {"ts": 0.0, "map": {}}
_TICKER_TTL = 3600.0            # 元数据表缓存 1 小时


def list_tickers(asset_type: str | None = None, page_size: int = 10000,
                 max_pages: int = 50, progress_cb=None) -> list[dict]:
    """翻页拉取标的代码表。asset_type 支持逗号分隔多值。"""
    page_size = max(1, min(int(page_size), 10000))
    rows: list[dict] = []
    offset = 0
    for page in range(max_pages):
        params = {"limit": page_size, "offset": offset}
        if asset_type:
            params["asset_type"] = asset_type
        item = _data(_http_get_json("/api/meta/tickers/list", params)).get("item") or []
        rows.extend(item)
        if progress_cb:
            progress_cb(page + 1, len(rows))
        if len(item) < page_size:
            break
        offset += page_size
    return rows


def ticker_index(refresh: bool = False) -> dict[str, dict]:
    """thscode -> 标的信息。用于名称解析与资产类型判定, 带 1 小时缓存。"""
    now = time.time()
    if not refresh and _TICKER_CACHE["map"] and now - _TICKER_CACHE["ts"] < _TICKER_TTL:
        return _TICKER_CACHE["map"]
    rows = list_tickers("a-share,fund-etf,fund-lof")
    mapping = {r["thscode"]: r for r in rows if r.get("thscode")}
    _TICKER_CACHE.update(ts=now, map=mapping)
    return mapping


def resolve_asset_type(symbol: str) -> str:
    """返回 'stock' / 'etf' / 'lof'。元数据不可用时按代码段推断, 不阻断取数。"""
    ths = to_thscode(symbol)
    try:
        row = ticker_index().get(ths)
    except ThsError:
        row = None
    if row and row.get("asset_type"):
        return {"fund-etf": "etf", "fund-lof": "lof"}.get(row["asset_type"], "stock")
    return _guess_asset_type(ths)


def search_tickers(keyword: str, asset_type: str | None = None,
                   limit: int = 10) -> list[dict]:
    """按 thscode / 纯代码 / 中英文名称检索标的。"""
    if not str(keyword or "").strip():
        raise ThsParamError("q 不能为空")
    params = {"q": str(keyword).strip(), "limit": max(1, min(int(limit), 50))}
    if asset_type:
        params["asset_type"] = asset_type
    return _data(_http_get_json("/api/meta/tickers/search", params)).get("item") or []


# ==================== 时间与分片工具 ====================

def _norm_day(d) -> str:
    """任意日期写法 -> 'YYYY-MM-DD'。"""
    s = str(d).strip().replace("/", "-")
    if len(s) == 8 and s.isdigit():
        return f"{s[:4]}-{s[4:6]}-{s[6:]}"
    return pd.Timestamp(s).strftime("%Y-%m-%d")


def _to_ms(day: str) -> int:
    """'YYYY-MM-DD' -> Asia/Shanghai 当日零点的毫秒戳。"""
    return int(pd.Timestamp(day, tz="Asia/Shanghai").value // 10 ** 6)


def _from_ms(ms) -> str:
    """毫秒戳 -> Asia/Shanghai 的 'YYYY-MM-DD'。"""
    return pd.to_datetime(ms, unit="ms", utc=True).tz_convert("Asia/Shanghai").strftime("%Y-%m-%d")


def _chunked(seq: Sequence, size: int) -> Iterable[Sequence]:
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


def _split_window(start: str, end: str, years: int) -> list[tuple[str, str]]:
    """把 [start, end] 切成若干跨度不超过 years 自然年的闭区间。"""
    segs: list[tuple[str, str]] = []
    cur = pd.Timestamp(start)
    stop = pd.Timestamp(end)
    while cur <= stop:
        seg_end = min(cur + pd.DateOffset(years=years) - pd.Timedelta(days=1), stop)
        segs.append((cur.strftime("%Y-%m-%d"), seg_end.strftime("%Y-%m-%d")))
        cur = seg_end + pd.Timedelta(days=1)
    return segs


def etf_history_floor() -> str:
    """ETF 历史日线可回溯的最早日期 (近 5 年, 留 7 天安全余量)。"""
    floor = pd.Timestamp.now().normalize() - pd.DateOffset(years=ETF_MAX_YEARS)
    return (floor + pd.Timedelta(days=ETF_FLOOR_BUFFER_DAYS)).strftime("%Y-%m-%d")


# ==================== 行情: 日线 ====================

def _empty_bars() -> pd.DataFrame:
    return pd.DataFrame(columns=BAR_COLUMNS)


def _bars_frame(items: list[dict]) -> pd.DataFrame:
    """K 线记录 -> 项目标准日线 DataFrame (date 为 'YYYY-MM-DD')。"""
    if not items:
        return _empty_bars()
    df = pd.DataFrame(items)
    if "date_ms" not in df.columns or "close_price" not in df.columns:
        return _empty_bars()
    df["date"] = (pd.to_datetime(df["date_ms"], unit="ms", utc=True)
                  .dt.tz_convert("Asia/Shanghai").dt.strftime("%Y-%m-%d"))
    df = df.rename(columns={"open_price": "open", "high_price": "high",
                            "low_price": "low", "close_price": "close",
                            "turnover": "amount"})
    for col in BAR_COLUMNS:
        if col not in df.columns:
            df[col] = None
    df = df[BAR_COLUMNS].dropna(subset=["close"])
    return df.sort_values("date").reset_index(drop=True)


def fetch_daily_bars(symbols: Iterable[str], start: str, end: str,
                     adjust: str = "forward", strict: bool = True
                     ) -> dict[str, pd.DataFrame]:
    """批量取日线, 返回 {项目代码: DataFrame}。

    - 股票走 `/api/a-share/prices/historical-batch` (adjust 可选 forward/backward/none);
    - ETF 走 `/api/fund/market/historical-batch` (固定前复权, 只能取近 5 年);
    - 超出 ETF 5 年上限时: strict=True 抛 ThsRangeError, strict=False 静默截断。
    """
    syms = [s for s in dict.fromkeys(str(x).strip() for x in symbols) if s]
    if not syms:
        return {}
    start, end = _norm_day(start), _norm_day(end)
    if start > end:
        raise ThsParamError(f"起始日期({start})晚于结束日期({end})")

    groups: dict[str, list[str]] = {}
    for sym in syms:
        groups.setdefault(resolve_asset_type(sym), []).append(sym)

    out: dict[str, pd.DataFrame] = {}
    for kind, group in groups.items():
        if kind == "lof":
            raise ThsParamError(f"同花顺历史日线接口仅支持 ETF, 不支持 LOF: {group[:3]}")

        if kind == "etf":
            floor = etf_history_floor()
            eff_start = max(start, floor)
            if eff_start != start and strict:
                raise ThsRangeError(
                    f"ETF 历史日线只能回溯到 {floor} (近 {ETF_MAX_YEARS} 年), "
                    f"请求起点 {start} 超出范围; 更早历史请改用其他数据源")
            if eff_start > end:
                for sym in group:
                    out[sym] = _empty_bars()
                continue
            path = "/api/fund/market/historical-batch"
            segs = _split_window(eff_start, end, ETF_MAX_YEARS)
        else:
            path = "/api/a-share/prices/historical-batch"
            segs = _split_window(start, end, STOCK_MAX_YEARS)

        for chunk in _chunked(group, CODE_BATCH):
            thscodes = ",".join(to_thscode(s) for s in chunk)
            for seg_start, seg_end in segs:
                params = {"thscodes": thscodes, "interval": "1d",
                          "start": _to_ms(seg_start), "end": _to_ms(seg_end)}
                if kind == "stock":
                    params["adjust"] = adjust
                data = _data(_http_get_json(path, params))
                for block in data.get("item") or []:
                    try:
                        sym = from_thscode(block.get("thscode") or "")
                    except ThsParamError:
                        continue
                    df = _bars_frame(block.get("item") or [])
                    if df.empty:
                        continue
                    prev = out.get(sym)
                    out[sym] = (df if prev is None or prev.empty
                                else pd.concat([prev, df], ignore_index=True)
                                .drop_duplicates("date", keep="last")
                                .sort_values("date")
                                .reset_index(drop=True))

    return {sym: out.get(sym, _empty_bars()) for sym in syms}


# ==================== 行情: 快照 ====================

def _norm_snapshot(item: list[dict], ts: str, with_turnover: bool) -> list[dict]:
    rows = []
    for it in item:
        ths = it.get("thscode")
        if not ths:
            continue
        try:
            sym = from_thscode(ths)
        except ThsParamError:
            continue
        rows.append({
            "symbol": sym, "ts": ts,
            "price": it.get("last_price"), "pct": it.get("price_change_ratio_pct"),
            "open": it.get("open_price"), "high": it.get("high_price"),
            "low": it.get("low_price"), "pre_close": it.get("prev_price"),
            "volume": it.get("volume"), "amount": it.get("turnover"),
            "turnover": it.get("turnover_ratio_pct") if with_turnover else None,
        })
    return rows


def fetch_snapshot(symbols: Iterable[str]) -> list[dict]:
    """实时快照, 返回 realtime_quote 可直接落库的行。"""
    syms = [s for s in dict.fromkeys(str(x).strip() for x in symbols) if s]
    if not syms:
        return []
    etf, stock = [], []
    for sym in syms:
        (etf if resolve_asset_type(sym) in ("etf", "lof") else stock).append(sym)

    rows: list[dict] = []
    for chunk in _chunked(etf, SNAPSHOT_BATCH):
        data = _data(_http_get_json("/api/fund/market/snapshot",
                                    {"thscode": ",".join(to_thscode(s) for s in chunk)}))
        rows += _norm_snapshot(data.get("item") or [], _snapshot_ts(data), True)
    for chunk in _chunked(stock, SNAPSHOT_BATCH):
        data = _data(_http_get_json("/api/a-share/prices/snapshot",
                                    {"thscodes": ",".join(to_thscode(s) for s in chunk)}))
        rows += _norm_snapshot(data.get("item") or [], _snapshot_ts(data), False)
    return rows


def _snapshot_ts(data: dict) -> str:
    ms = data.get("timestamp")
    if not ms:
        return pd.Timestamp.now().strftime("%Y-%m-%d %H:%M:%S")
    return pd.to_datetime(ms, unit="ms", utc=True).tz_convert("Asia/Shanghai") \
        .strftime("%Y-%m-%d %H:%M:%S")


# ==================== 交易日历 ====================

def fetch_trading_days() -> list[str]:
    """A 股近一年交易日 (官方固定窗口 [今日-1年, 今日])。"""
    item = _data(_http_get_json("/api/a-share/calendar/trading-days")).get("item") or []
    out = []
    for it in item:
        d = it.get("date")
        if d and len(str(d)) == 8:
            out.append(f"{d[:4]}-{d[4:6]}-{d[6:]}")
    return out


# ==================== 财务数据 ====================

_FIN_PATHS = {
    "income": "/api/a-share/financials/income-statements",
    "balance": "/api/a-share/financials/balance-sheets",
    "cashflow": "/api/a-share/financials/cash-flow-statements",
}


def fetch_financials(symbol: str, kind: str = "income", period: str = "quarterly",
                     limit: int | None = 4, start: str | None = None,
                     end: str | None = None) -> list[dict]:
    """财务报表多期序列。

    kind: income / balance / cashflow; period: annual / quarterly。
    `limit`(最近 N 期, 1—20) 与 `start`+`end`(闭区间, ≤10 年) 互斥, 传区间时 limit 忽略。
    """
    path = _FIN_PATHS.get(kind)
    if not path:
        raise ThsParamError(f"未知报表类型: {kind}")
    if period not in ("annual", "quarterly"):
        raise ThsParamError(f"period 只能是 annual/quarterly: {period}")

    params = {"thscode": to_thscode(symbol), "period": period}
    if start and end:
        params.update(start=_to_ms(_norm_day(start)), end=_to_ms(_norm_day(end)))
    elif start or end:
        raise ThsParamError("start 与 end 必须同时提供")
    else:
        params["limit"] = max(1, min(int(limit or 4), 20))
    return _data(_http_get_json(path, params)).get("item") or []


def fetch_financial_indicators(symbol: str, report: str) -> dict:
    """单期财务指标 -> {index_id: value}; report 形如 '2025-1' (一季报)。"""
    if not report or "-" not in str(report):
        raise ThsParamError(f"report 需形如 '2025-1': {report}")
    data = _data(_http_get_json("/api/a-share/financials/indicators",
                                {"thscode": to_thscode(symbol), "report": str(report)}))
    out: dict[str, float | None] = {}
    for ability in data.get("abilities") or []:
        for ind in ability.get("indicators") or []:
            raw = ind.get("value")
            try:
                out[ind.get("index_id")] = float(raw) if raw not in (None, "") else None
            except (TypeError, ValueError):
                out[ind.get("index_id")] = None
    return out


_FISCAL_NO = {"Q1": 1, "Q2": 2, "Q3": 3, "Q4": 4, "FY": 4}


def _pick(ind: dict, *keys):
    """按顺序取第一个非空指标。

    官方文档与真实响应存在别名差异 (如 net_profit_yoy_growth_ratio 实测为
    calculate_parent_holder_net_profit_yoy_growth_ratio), 这里同时兼容。
    """
    for k in keys:
        v = ind.get(k)
        if v is not None:
            return v
    return None


def build_fundamental_rows(symbol: str, quarters: int = 4,
                           with_indicators: bool = True) -> list[dict]:
    """合并利润表/资产负债表/财务指标 -> fundamental 表行。

    只映射字段语义明确的项目: 营收、归母净利、EPS、营业利润、毛利率、ROE、
    资产负债率、营收/净利同比。商誉、BPS、PE/PB 等接口未提供或口径不一致的字段留空,
    避免把错口径数据写进库。
    """
    rows: list[dict] = []
    income = fetch_financials(symbol, "income", "quarterly", limit=quarters)
    if not income:
        return rows
    balance = {(r.get("period_end_ms")): r
               for r in fetch_financials(symbol, "balance", "quarterly", limit=quarters)}
    sym = from_thscode(income[0].get("thscode") or to_thscode(symbol))

    for inc in income:
        report_date = _from_ms(inc.get("period_end_ms")).replace("-", "")
        if len(report_date) != 8:
            continue
        ann = inc.get("report_date_ms")
        row = {
            "symbol": sym,
            "report_date": report_date,
            "ann_date": _from_ms(ann) if ann else None,
            "eps": inc.get("basic_eps"),
            "revenue": inc.get("operating_income"),
            "net_profit": inc.get("parent_holder_net_profit"),
            "operating_profit": inc.get("operating_profit"),
        }
        bal = balance.get(inc.get("period_end_ms")) or {}
        if bal:
            # 资产负债率可由此口径直接算 (总负债 / 总资产), 与指标接口互为校验
            assets, debt = bal.get("assets_total"), bal.get("total_debt")
            if assets and debt is not None:
                row["debt_ratio"] = round(float(debt) / float(assets) * 100, 4)

        if with_indicators:
            fy = inc.get("fiscal_year")
            no = _FISCAL_NO.get(str(inc.get("fiscal_period") or ""))
            if fy and no:
                try:
                    ind = fetch_financial_indicators(sym, f"{fy}-{no}")
                except ThsError:
                    ind = {}
                if ind:
                    row["gross_margin"] = ind.get("sale_gross_margin")
                    row["roe"] = _pick(ind, "index_weighted_avg_roe",
                                       "index_deduct_weighted_avg_roe")
                    row["profit_yoy"] = _pick(
                        ind, "net_profit_yoy_growth_ratio",
                        "calculate_parent_holder_net_profit_yoy_growth_ratio",
                        "calculate_net_profit_yoy_growth_ratio")
                    row["revenue_yoy"] = _pick(
                        ind, "operating_income_yoy_growth_ratio",
                        "calculate_operating_income_yoy_growth_ratio")
                    if row.get("debt_ratio") is None:
                        row["debt_ratio"] = ind.get("assets_debt_ratio")
        rows.append(row)
    return rows


def fetch_valuations(symbols: Iterable[str]) -> list[dict]:
    """A 股最新估值快照 (PE-TTM/PE-MRQ/PB/PS/PCF), 单次最多 100 个代码。"""
    syms = [s for s in dict.fromkeys(str(x).strip() for x in symbols) if s]
    out: list[dict] = []
    for chunk in _chunked(syms, SNAPSHOT_BATCH):
        data = _data(_http_get_json("/api/a-share/valuations/snapshot",
                                    {"thscodes": ",".join(to_thscode(s) for s in chunk)}))
        out += data.get("item") or []
    return out


# ==================== 落库 (data_store 集成) ====================

def sync_daily_bars(symbols: Iterable[str], start: str, end: str,
                    adjust: str = "forward", progress_cb=None) -> dict:
    """批量取日线并写入 daily_bar, 同步更新覆盖索引。

    ETF 超出近 5 年的部分会被自动截断 (不阻断股票部分), 结果里通过 clipped 说明。
    """
    syms = [s for s in dict.fromkeys(str(x).strip() for x in symbols) if s]
    report = {"symbols": len(syms), "rows": 0, "written": 0, "empty": [],
              "clipped": [], "errors": []}
    if not syms:
        return report

    try:
        frames = fetch_daily_bars(syms, start, end, adjust=adjust, strict=False)
    except ThsError as ex:
        report["errors"].append(str(ex))
        return report

    floor = etf_history_floor()
    has_etf = any(_guess_asset_type(to_thscode(s)) == "etf" for s in syms)
    if has_etf and _norm_day(start) < floor:
        report["clipped"].append({"start": _norm_day(start), "etf_floor": floor})

    done = 0
    for sym, df in frames.items():
        done += 1
        if df is None or df.empty:
            report["empty"].append(sym)
            if progress_cb:
                progress_cb(done, len(frames), sym, 0)
            continue
        asset_type = "etf" if resolve_asset_type(sym) == "etf" else "stock"
        try:
            n = data_store.upsert_bars(sym, df, asset_type=asset_type)
        except Exception as ex:                       # noqa: BLE001 - 单标的失败不影响整批
            report["errors"].append(f"{sym}: {type(ex).__name__}: {ex}")
            continue
        data_store.coverage_set("daily_bar", sym, str(df["date"].iloc[0]),
                                str(df["date"].iloc[-1]), n)
        report["rows"] += n
        report["written"] += 1
        if progress_cb:
            progress_cb(done, len(frames), sym, n)
    return report


def sync_snapshot(symbols: Iterable[str]) -> dict:
    """拉一次快照并写入 realtime_quote。"""
    rows = fetch_snapshot(symbols)
    if not rows:
        return {"rows": 0}
    return {"rows": data_store.save_realtime(rows)}


def sync_fundamentals(symbols: Iterable[str], quarters: int = 4,
                      workers: int = 4, progress_cb=None) -> dict:
    """批量同步财务数据到 fundamental 表 (单标的失败不中断整批)。"""
    syms = [s for s in dict.fromkeys(str(x).strip() for x in symbols) if s]
    report = {"symbols": len(syms), "rows": 0, "failed": []}
    if not syms:
        return report

    def _one(sym: str) -> tuple[str, list[dict] | None, str | None]:
        try:
            return sym, build_fundamental_rows(sym, quarters=quarters), None
        except Exception as ex:                        # noqa: BLE001
            return sym, None, f"{type(ex).__name__}: {ex}"

    done = 0
    with ThreadPoolExecutor(max_workers=max(1, min(workers, 8))) as pool:
        for sym, rows, err in pool.map(_one, syms):
            done += 1
            if err:
                report["failed"].append(f"{sym}: {err}")
            elif rows:
                report["rows"] += data_store.upsert_fundamentals(rows)
            if progress_cb:
                progress_cb(done, len(syms), sym, len(rows or []))
    return report


# ==================== 自检 ====================

def health_check() -> dict:
    """连通性自检: 校验密钥有效并取一次交易日历。不打印完整密钥。"""
    out = {"base_url": BASE_URL, "api_key": mask_key(), "ok": False}
    try:
        days = fetch_trading_days()
        out.update(ok=True, trading_days=len(days),
                   last_trading_day=days[-1] if days else None)
    except ThsError as ex:
        out.update(error=str(ex), code=getattr(ex, "code", None))
    return out


if __name__ == "__main__":       # py ths_source.py --check
    import json
    import sys

    if "--check" in sys.argv:
        print(json.dumps(health_check(), ensure_ascii=False, indent=2))
    else:
        print("用法: py ths_source.py --check")
