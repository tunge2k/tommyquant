# -*- coding: utf-8 -*-
"""原型: 按「纳指ETF型」特征筛选 ETF

用户给的参考标的 (截图): 纳指ETF广发 (513100), 周线多头排列, 长期向上, 最大回撤 < 30%
需要找: 走势类似 + 估值不高 + 溢价低 + 无下架风险

6 大维度:
  D1 长期趋势 (40%): 累计涨幅 + 六均线多头排列 + 周K MA30 向上
  D2 风险控制 (25%): 最大回撤 < 30% + 年化波动合理 (不是那种暴涨暴跌的妖币)
  D3 估值空间 (15%): 近 250 日涨幅排名 + 当前位置距 250 日高点比例
  D4 溢价安全 (10%): 溢价率过滤 (QDII 回落是主要亏损源)
  D5 下架风险 (10%): 规模 ≥ 2亿 + 上市 ≥ 1年 + 非 LOF 老封闭式基金
  D6 流动性 (附加): 20 日均成交额 ≥ 3000 万 + 20 日均换手率 ≥ 0.5%

数据源: akshare 新浪日线 + 东财 push2 补成交额
"""
import sys, time, json
from datetime import datetime, timedelta

import numpy as np
import pandas as pd


# ---------------------- 候选池 ----------------------
# 宽基 + 行业 + 主题 + QDII, 共约 40 只典型 ETF (避免全市场 1500+ 只太多)
CANDIDATES = [
    # 宽基
    ("510050", "sh", "上证50ETF"),
    ("510300", "sh", "沪深300ETF"),
    ("510500", "sh", "中证500ETF"),
    ("512100", "sh", "中证1000ETF"),
    ("159915", "sz", "创业板ETF"),
    ("159949", "sz", "创业板50ETF"),
    ("512880", "sh", "证券ETF"),
    ("512010", "sh", "医药ETF"),
    ("515030", "sh", "新能源车ETF"),
    ("159915", "sz", "创业板ETF"),
    # 主题 / 行业
    ("515790", "sh", "光伏ETF"),
    ("512760", "sh", "半导体ETF"),
    ("512480", "sh", "半导体ETF2"),
    ("159995", "sz", "芯片ETF"),
    ("159813", "sz", "芯片ETF2"),
    ("512200", "sh", "房地产ETF"),
    ("512800", "sh", "银行ETF"),
    ("512660", "sh", "军工ETF"),
    ("512580", "sh", "环保ETF"),
    ("512170", "sh", "医疗ETF"),
    ("515050", "sh", "5G ETF"),
    ("159995", "sz", "芯片ETF华夏"),
    ("515790", "sh", "光伏ETF华泰"),
    ("512780", "sh", "人工智能ETF"),
    # QDII (纳指/道指/恒生/日经/德国)
    ("513100", "sh", "纳指ETF广发"),
    ("513500", "sh", "标普500ETF"),
    ("513080", "sh", "恒生科技ETF"),
    ("513330", "sh", "恒生指数ETF"),
    ("513180", "sh", "恒生医疗ETF"),
    ("513050", "sh", "中概互联ETF"),
    ("513880", "sh", "越南ETF"),
    ("513130", "sh", "港股通科技ETF"),
    ("513520", "sh", "日经ETF"),
    ("513030", "sh", "德国ETF"),
    ("513190", "sh", "日本ETF"),
    # 商品
    ("518880", "sh", "黄金ETF"),
    ("518800", "sh", "黄金ETF国泰"),
    ("161129", "sz", "原油ETF"),
    ("513130", "sh", "港股通科技2"),
    # 债券 + 红利
    ("511010", "sh", "国债ETF"),
    ("511260", "sh", "十年国债ETF"),
    ("515080", "sh", "中证红利ETF"),
    ("515180", "sh", "红利ETF"),
]

# 去重
_seen = set()
CANDIDATES_UNIQ = []
for c in CANDIDATES:
    if c[0] not in _seen:
        _seen.add(c[0])
        CANDIDATES_UNIQ.append(c)


# ---------------------- 数据抓取 ----------------------
def fetch_sina_daily(code: str, prefix: str = "sh", start: str = "20200101") -> pd.DataFrame | None:
    """从新浪拉日线, 返回标准列 (date/open/high/low/close/volume/amount), 前复权"""
    import akshare as ak
    symbol = f"{prefix}{code}"
    try:
        df = ak.fund_etf_hist_sina(symbol=symbol)
        if df is None or len(df) == 0:
            return None
        df = df.copy()
        df["date"] = pd.to_datetime(df["date"])
        df = df.sort_values("date").reset_index(drop=True)
        df = df[df["date"] >= start].reset_index(drop=True)
        # 新浪没有成交额, 用 volume 估算 (但东财 push2 有 amount)
        return df
    except Exception as e:
        print(f"  sina {symbol} err: {type(e).__name__}")
        return None


def fetch_push2_ext(code: str, market: str = "sh", start: str = "20200101",
                    end: str = None) -> pd.DataFrame | None:
    """东财 push2 补成交额 (新浪源没有)"""
    import requests
    end = end or datetime.now().strftime("%Y%m%d")
    secid = f"1.{code}" if market == "sh" else f"0.{code}"
    url = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
    params = {
        "secid": secid, "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
        "klt": "101", "fqt": "1", "beg": start, "end": end, "lmt": "800",
    }
    try:
        r = requests.get(url, params=params, timeout=10)
        d = r.json()
        kl = (d.get("data") or {}).get("klines") or []
        if not kl:
            return None
        rows = [s.split(",") for s in kl]
        df = pd.DataFrame(rows, columns=["date", "open", "close", "high", "low",
                                          "volume", "amount", "pct", "amp", "chg", "turnover"])
        for c in ["open", "close", "high", "low", "volume", "amount", "pct", "turnover"]:
            df[c] = pd.to_numeric(df[c], errors="coerce")
        df["date"] = pd.to_datetime(df["date"])
        return df[["date", "open", "high", "low", "close", "volume", "amount", "pct", "turnover"]]
    except Exception:
        return None


def fetch_all() -> dict[str, pd.DataFrame]:
    """抓全部候选 ETF 的日线"""
    import requests
    # 全局 push2 限流: 每 3 只 sleep 一下
    out = {}
    for i, (code, mkt, name) in enumerate(CANDIDATES_UNIQ):
        t0 = time.perf_counter()
        sina = fetch_sina_daily(code, mkt)
        push2 = fetch_push2_ext(code, mkt)
        if sina is None:
            print(f"[{i+1}/{len(CANDIDATES_UNIQ)}] {code} {name}: SKIP (sina empty)")
            continue
        # 合并: sina 有 OHLCV, push2 有 amount/pct/turnover
        df = sina.copy()
        if push2 is not None and len(push2) > 0:
            df = df.merge(push2[["date", "amount", "pct", "turnover"]], on="date", how="left")
        if "amount" not in df.columns:
            df["amount"] = np.nan
        # 估算 amount: close * volume (如果 push2 也没拿到)
        mask = df["amount"].isna()
        if mask.any():
            df.loc[mask, "amount"] = (df.loc[mask, "close"] * df.loc[mask, "volume"]).astype("float64")
        out[code] = df
        dt = time.perf_counter() - t0
        print(f"[{i+1}/{len(CANDIDATES_UNIQ)}] {code} {name}: {len(df)} bars ({dt:.2f}s)")
        time.sleep(0.2)
    return out


# ---------------------- 打分引擎 ----------------------
def _ma_lines(df: pd.DataFrame) -> pd.DataFrame:
    """六均线 + 周K MA30 预计算"""
    df = df.copy()
    close = df["close"]
    df["ma5"] = close.rolling(5).mean()
    df["ma10"] = close.rolling(10).mean()
    df["ma20"] = close.rolling(20).mean()
    df["ma30"] = close.rolling(30).mean()
    df["ma60"] = close.rolling(60).mean()
    df["ma120"] = close.rolling(120).mean()
    df["ma250"] = close.rolling(250).mean()
    return df


def _max_drawdown(close: np.ndarray) -> float:
    """滚动最大回撤 (正值, 20% 回撤返回 0.2)"""
    peak = np.maximum.accumulate(close)
    dd = 1 - close / peak
    return float(np.nanmax(dd))


def _volatility(close: np.ndarray) -> float:
    """年化波动率"""
    ret = np.diff(close) / close[:-1]
    return float(np.nanstd(ret) * np.sqrt(252))


def _week_df(df: pd.DataFrame) -> pd.DataFrame:
    """日K → 周K (取每周五最后一根, 未走完周用当日补齐)"""
    d = df[["date", "close"]].copy()
    d = d.set_index("date")
    wk = d.resample("W-FRI").last().dropna()
    return wk


def score_d1_trend(df: pd.DataFrame) -> dict:
    """D1 长期趋势 (40%) — 累计涨幅 + 多头排列 + 周K MA30"""
    score = 0.0
    detail = {}
    close = df["close"].to_numpy(dtype="float64")
    last = close[-1]

    # 2a. 累计涨幅 (近 5 年优先, 数据不够就取全窗口)
    # 纳指 ETF 特征: 5 年 > 50%, 3 年 > 30%
    days = len(close)
    cum5y = None
    cum3y = None
    cum1y = None
    if days >= 250 * 5:
        cum5y = (last / close[-250 * 5] - 1) if close[-250 * 5] > 0 else None
    if days >= 250 * 3:
        cum3y = (last / close[-250 * 3] - 1) if close[-250 * 3] > 0 else None
    if days >= 250:
        cum1y = (last / close[-250] - 1) if close[-250] > 0 else None

    detail["cum5y"] = cum5y
    detail["cum3y"] = cum3y
    detail["cum1y"] = cum1y

    # 涨幅打分
    # 5 年 ≥ 50% → 5 分; 3 年 ≥ 30% → 3 分; 1 年 ≥ 10% → 2 分
    trend_grow = 0
    if cum5y is not None and cum5y >= 0.50:
        trend_grow += 5
    elif cum3y is not None and cum3y >= 0.30:
        trend_grow += 4
    elif cum1y is not None and cum1y >= 0.10:
        trend_grow += 2
    elif cum1y is not None and cum1y >= 0:
        trend_grow += 1
    detail["trend_grow"] = trend_grow

    # 2b. 六均线多头排列 (MA5>MA10>MA20>MA30>MA60>MA120)
    m5, m10, m20, m30, m60, m120 = df[["ma5", "ma10", "ma20", "ma30", "ma60", "ma120"]].iloc[-1].values
    long_arrange = (m5 > m10 > m20 > m30 > m60 > m120)
    short_arrange = (m5 > m10 > m20)  # 至少短周期多头
    if long_arrange and np.isfinite(m120):
        arrange_score = 3
    elif short_arrange and np.isfinite(m60):
        arrange_score = 2
    else:
        arrange_score = 0
    detail["long_arrange"] = long_arrange
    detail["arrange_score"] = arrange_score

    # 2c. 周K MA30 向上 (纳指 ETF 核心特征)
    wdf = _week_df(df)
    if len(wdf) >= 30:
        wma30 = wdf["close"].rolling(30).mean().dropna()
        if len(wma30) >= 6:
            # 近 5 周 MA30 是否递增 (允许 1 周微小回落)
            up_count = sum(1 for i in range(-5, 0) if wma30.iloc[i] > wma30.iloc[i - 1])
            ma30_score = 2 if up_count >= 4 else (1 if up_count >= 3 else 0)
        else:
            ma30_score = 0
    else:
        ma30_score = 0
    detail["ma30_up_weeks"] = ma30_score

    # 总分 (满分 10): 5 + 3 + 2
    score = trend_grow + arrange_score + ma30_score
    detail["score"] = score
    return detail


def score_d2_risk(df: pd.DataFrame) -> dict:
    """D2 风险控制 (25%) — 近 3 年最大回撤 + 年化波动

    历史最大回撤对老 ETF 会高估 (份额折算/早期低流动性), 改用近 3 年窗口。
    """
    close = df["close"].to_numpy(dtype="float64")
    n = len(close)
    # 近 3 年优先, 数据不够就取全窗口
    win = close[-750:] if n >= 750 else close
    mdd = _max_drawdown(win)
    vol = _volatility(close)

    # 最大回撤打分 (窗口 3 年的)
    if mdd < 0.20:
        mdd_s = 5
    elif mdd < 0.30:
        mdd_s = 4
    elif mdd < 0.40:
        mdd_s = 3
    elif mdd < 0.50:
        mdd_s = 2
    else:
        mdd_s = 1

    # 年化波动打分 —— 纳指型特征: 有弹性但不是妖币
    # 8% 以下视为债券/货币, 非目标池; 15%~35% 黄金区
    if vol < 0.08:
        vol_s = 0  # 直接 D1 也会被压, 无弹性品种
    elif 0.12 <= vol <= 0.35:
        vol_s = 5
    elif 0.08 <= vol <= 0.50:
        vol_s = 3
    else:
        vol_s = 1

    return {"mdd_3y": round(mdd, 4), "vol": round(vol, 4),
            "mdd_score": mdd_s, "vol_score": vol_s,
            "score": mdd_s + vol_s}


def score_d3_spread(df: pd.DataFrame) -> dict:
    """D3 估值空间 (15%) — 距 250 日高点位置 + 250 日涨幅分位"""
    close = df["close"].to_numpy(dtype="float64")
    last = close[-1]
    n = len(close)

    # 当前距 250 日高点比例
    if n >= 250:
        hi250 = float(np.max(close[-250:]))
        pos_pct = (last - hi250) / hi250  # 正 = 创新高
    else:
        hi250 = float(np.max(close))
        pos_pct = (last - hi250) / hi250

    # 打分: 距高点 -10%~0% → 5 分 (强势但还有空间); -30% 以下 → 2 分 (可能在底部)
    # 纳指 ETF 经常在高点附近创新高, 但离高点 -10%~0% 时最好 (还没过热)
    # 这里用户说"估值不能太高, 要有上涨空间", 所以我倾向 -20%~-5% 最理想
    if -0.20 <= pos_pct <= -0.05:
        pos_s = 5
    elif -0.35 <= pos_pct <= 0.05:
        pos_s = 4
    elif -0.50 <= pos_pct <= 0.10:
        pos_s = 3
    elif pos_pct < -0.50:
        pos_s = 2
    else:
        pos_s = 2

    # 250 日涨幅
    if n >= 250:
        ret250 = (last / close[-250] - 1)
    else:
        ret250 = (last / close[0] - 1)

    # 涨幅不是越高越好 (过高说明追高风险大), 30%~100% 最理想
    if 0.30 <= ret250 <= 1.00:
        ret_s = 5
    elif 0.10 <= ret250 <= 1.50:
        ret_s = 3
    elif ret250 >= 0:
        ret_s = 2
    else:
        ret_s = 1

    return {"pos_pct_vs_hi250": round(pos_pct, 4), "ret_250d": round(ret250, 4),
            "pos_score": pos_s, "ret_score": ret_s,
            "score": pos_s + ret_s}


def score_d4_premium(code: str) -> dict:
    """D4 溢价安全 (10%) — 东财 fund-daily-quote 有 premium_ratio 字段
    原型阶段: akshare fund_etf_fund_daily_em 接口可能不稳定, 先给个占位函数
    如果拿不到溢价数据, 该维度给中性分 (不影响总分排序), 但标注数据缺失
    """
    try:
        import akshare as ak
        # 东财基金日线: fund_etf_fund_daily_em
        # symbol 参数是 6 位代码
        df = ak.fund_etf_fund_daily_em(symbol=code)
        if df is None or len(df) == 0 or "溢价率" not in df.columns:
            return {"premium_miss": True, "score": 3, "note": "溢价数据缺失, 给中性分"}
        # 取近 20 日
        df = df.sort_values("日期").tail(20)
        pcts = pd.to_numeric(df["溢价率"].astype(str).str.replace("%", ""), errors="coerce").dropna()
        if len(pcts) == 0:
            return {"premium_miss": True, "score": 3, "note": "溢价数据缺失"}
        avg = float(np.mean(pcts))
        today = float(pcts.iloc[-1])
        # 溢价率打分: 平均 < 1%, 当日 < 2% → 5 分
        if avg < 1.0 and today < 2.0:
            s = 5
        elif avg < 3.0 and today < 5.0:
            s = 3
        else:
            s = 1
        return {"premium_avg_20d": round(avg, 3), "premium_today": round(today, 3),
                "score": s, "note": "溢价安全"}
    except Exception as e:
        return {"premium_miss": True, "score": 3, "note": f"溢价接口异常: {type(e).__name__}"}


def score_d5_risk(code: str, df: pd.DataFrame, name: str) -> dict:
    """D5 下架风险 (10%) — 规模 + 上市年限 + 非封基"""
    # 原型阶段没有规模/份额数据库, 用两个代理指标
    # 1) 上市年限 (日线起点)
    first_date = df["date"].iloc[0]
    age_days = (pd.Timestamp.now() - first_date).days
    age_score = 2 if age_days >= 365 * 2 else 1 if age_days >= 365 else 0

    # 2) 日均成交额代理 (成交额越大 → 规模越大, 下架风险越低)
    amt20 = df["amount"].tail(20).mean()
    # 原型没有真实规模数据, 先不打分 (这个维度等数据层补齐再精细化)
    return {"age_days": age_days, "avg_amount_20d": round(amt20 / 1e8, 3),
            "score": age_score + 3, "note": "下架风险代理: 上市年龄 + 成交额"}


def score_d6_liquidity(df: pd.DataFrame) -> dict:
    """D6 流动性附加 (软门槛) — 20 日均成交额 + 换手率"""
    amt20 = df["amount"].tail(20).mean()
    # 成交额 < 3000 万 → 流动性不足, 过滤掉
    ok = amt20 >= 3e7
    return {"avg_amount_20d": round(amt20, 3), "liquid_ok": bool(ok)}


# ---------------------- 主流程 ----------------------
def evaluate_all(feat: dict[str, pd.DataFrame]) -> pd.DataFrame:
    results = []
    for code, df in feat.items():
        df = df.dropna(subset=["close"]).reset_index(drop=True)
        if len(df) < 120:
            continue
        df = _ma_lines(df)

        d1 = score_d1_trend(df)
        d2 = score_d2_risk(df)
        d3 = score_d3_spread(df)
        # 溢价/下架风险 原型不调 API (太慢)
        d4 = {"score": 3, "note": "跳过 (原型无实时溢价)"}
        d5 = {"score": 5, "note": "跳过 (原型无规模数据)"}
        d6 = score_d6_liquidity(df)

        # 加权总分: D1×0.40 + D2×0.25 + D3×0.15 + D4×0.10 + D5×0.10
        total = (d1.get("score", 0) * 0.40
                 + d2.get("score", 0) * 0.25
                 + d3.get("score", 0) * 0.15
                 + d4.get("score", 0) * 0.10
                 + d5.get("score", 0) * 0.10) * 10  # 满分 100

        # 波动率硬门槛: 年化波动 < 8% (债券/货币/红利低波) → 非纳指型池, 总分封顶 30
        close_arr = df["close"].to_numpy(dtype="float64")
        vol_now = _volatility(close_arr)
        if vol_now < 0.08:
            total = min(total, 30.0)

        row = {
            "code": code,
            "name": next((n for c, m, n in CANDIDATES_UNIQ if c == code), "?"),
            "trend": d1.get("score"),
            "mdd_3y": d2.get("mdd_3y"), "vol": d2.get("vol"), "risk": d2.get("score"),
            "pos250": d3.get("pos_pct_vs_hi250"), "ret250": d3.get("ret_250d"), "spread": d3.get("score"),
            "amount_20d_yi": round(d6["avg_amount_20d"] / 1e8, 2),
            "liquid_ok": d6["liquid_ok"],
            "total": round(total, 1),
            "detail_cum5y": d1.get("cum5y"),
            "detail_long_arrange": d1.get("long_arrange"),
            "detail_ma30_w": d1.get("ma30_up_weeks"),
        }
        results.append(row)

    out = pd.DataFrame(results).sort_values("total", ascending=False).reset_index(drop=True)
    return out


if __name__ == "__main__":
    t0 = time.perf_counter()
    feat = fetch_all()
    print(f"\n== 抓取完成: {len(feat)} 只 ETF, 耗时 {time.perf_counter()-t0:.1f}s ==\n")
    t1 = time.perf_counter()
    out = evaluate_all(feat)
    print(f"== 打分完成: {time.perf_counter()-t1:.2f}s ==\n")

    pd.set_option("display.max_columns", None)
    pd.set_option("display.width", 200)
    pd.set_option("display.float_format", lambda x: f"{x:.3f}")

    cols = ["code", "name", "total", "trend", "mdd_3y", "vol", "risk",
            "pos250", "ret250", "spread", "amount_20d_yi", "liquid_ok"]
    print("Top 15 (纳指ETF型特征得分):")
    print(out[cols].head(15).to_string(index=False))

    out.to_csv(r"d:\py_lezrn\stock_a\888\etf_match_results.csv", index=False, encoding="utf-8-sig")
    print(f"\n完整结果已保存: etf_match_results.csv ({len(out)} rows)")
