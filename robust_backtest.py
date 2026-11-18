"""K 段稳健参数评估 + 滚动样本外检验 (walk-forward)

参考 tick-stock-panel (MIT) 的 robust_optimizer / walkforward 思路:

- 不按"整段区间的冠军"选参, 而是把区间等分为 K 个互不重叠验证段,
  每组参数固定不变各跑一遍, 用 0.6*中位数 + 0.4*最差段 作为稳健分。
  中位数抗单段运气, 最差段惩罚"只在一段行情里好用"的参数。
- 主目标白名单剔除胜率/平均持仓天数等"少数样本即满分"的退化指标,
  并设置最小交易数门槛, 防止 3 笔交易的漂亮曲线夺冠。
- 附赠诊断: 各段冠军是否收敛、冠军在网格中的邻域是否为高原。
- 滚动样本外: 训练窗内选参, 测试窗从训练窗末日的次一交易日起算,
  避免前视泄漏, 输出 OOS 复利收益、退化率与胜折占比。

本模块不依赖具体回测引擎: 调用方注入 run_one(params, start, end) -> dict,
由它决定走事件驱动还是向量化引擎, 便于单元测试与 UI 复用。
"""
from __future__ import annotations

import math
from datetime import timedelta

import numpy as np
import pandas as pd

MIN_TRADES_DEFAULT = 30
ROBUST_MEDIAN_WEIGHT = 0.6
ROBUST_WORST_WEIGHT = 0.4

# 允许作为主目标 (数值越大越好); max_drawdown 为负数, 越大代表回撤越小
OBJECTIVE_WHITELIST = {
    "total_return", "cagr", "sharpe", "sortino", "calmar",
    "profit_factor", "max_drawdown",
}

# 明确拒绝: 少量交易即可接近满分的退化指标
DEGENERATE_OBJECTIVES = {
    "win_rate", "avg_hold_days", "avg_holding_days",
    "trades_count", "max_consecutive_wins",
}

_OBJECTIVE_ALIAS = {
    "annual_return": "cagr",
    "年化收益": "cagr",
    "总收益": "total_return",
    "夏普": "sharpe",
    "索提诺": "sortino",
    "卡玛": "calmar",
    "胜率": "win_rate",
    "最大回撤": "max_drawdown",
}


def normalize_objective(objective: str) -> str:
    """把用户输入规范化为白名单内指标; 退化指标直接拒绝"""
    key = str(objective or "").strip().lower()
    key = _OBJECTIVE_ALIAS.get(key, key)
    if key in DEGENERATE_OBJECTIVES:
        raise ValueError(
            f"指标 {key} 属于退化目标 (少数交易即可满分), 不能作为参数选优依据; "
            f"可选: {', '.join(sorted(OBJECTIVE_WHITELIST))}"
        )
    if key not in OBJECTIVE_WHITELIST:
        raise ValueError(
            f"不支持的主目标 {objective}; 可选: {', '.join(sorted(OBJECTIVE_WHITELIST))}"
        )
    return key


def metric_value(perf: dict, objective: str) -> float | None:
    """从绩效字典取目标值; 缺失/NaN 返回 None"""
    if not perf:
        return None
    v = perf.get(objective)
    if v is None:
        return None
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def trades_count(result: dict) -> int:
    """兼容完整回测结果与纯绩效字典"""
    if not result:
        return 0
    perf = result.get("performance", result)
    n = perf.get("trades_count")
    if n is not None:
        try:
            return int(n)
        except (TypeError, ValueError):
            pass
    return len(result.get("trades") or [])


def split_windows(start, end, k: int) -> list[tuple[pd.Timestamp, pd.Timestamp]]:
    """把 [start, end] 等分为 k 个互不重叠的日历段

    每段至少保留约 120 个自然日 (约 80 个交易日), 区间过短时自动降 K,
    避免出现"每段不足 20 个交易日、回测直接返回空"的无效评估。
    """
    s = pd.Timestamp(start).normalize()
    e = pd.Timestamp(end).normalize()
    if e <= s:
        raise ValueError("结束日期必须晚于开始日期")
    total_days = (e - s).days + 1
    k = max(2, min(int(k), max(2, total_days // 120)))
    bounds = np.linspace(0, total_days, k + 1).astype(int)
    out = []
    for i in range(k):
        ws = s + timedelta(days=int(bounds[i]))
        we = s + timedelta(days=int(bounds[i + 1]) - 1)
        if we < ws:
            continue
        out.append((ws, we))
    return out


def robust_score(values: list[float | None]) -> dict:
    """0.6*中位数 + 0.4*最差段; 空值段不参与统计"""
    vals = [float(v) for v in values if v is not None and math.isfinite(float(v))]
    if not vals:
        return {"median": None, "worst": None, "mean": None, "score": None, "n": 0}
    return {
        "median": float(np.median(vals)),
        "worst": float(np.min(vals)),
        "mean": float(np.mean(vals)),
        "score": ROBUST_MEDIAN_WEIGHT * float(np.median(vals))
                 + ROBUST_WORST_WEIGHT * float(np.min(vals)),
        "n": len(vals),
    }


def run_robust_sweep(run_one, combos: list[dict],
                     windows: list[tuple], objective: str = "calmar",
                     min_trades: int = MIN_TRADES_DEFAULT,
                     on_progress=None) -> dict:
    """K 段稳健参数评估

    Args:
        run_one: (params, start, end) -> 完整回测结果 dict (含 performance)
        combos: 参数组合列表
        windows: split_windows 的输出
        objective: 主目标 (白名单内, 越大越好)
        min_trades: 全部验证段累计最小交易数; 不足则标记不合格
        on_progress: (done, total, label) -> None, 用于 UI 进度条

    Returns:
        {"rows": 按稳健分降序, "windows": ..., "objective": ..., "min_trades": ...}
    """
    objective = normalize_objective(objective)
    windows = list(windows or [])
    if not windows:
        raise ValueError("至少需要一个验证段")
    total_runs = len(combos) * len(windows)

    rows = []
    done = 0
    for combo in combos:
        segs = []
        values = []
        n_trades = 0
        for (ws, we) in windows:
            try:
                result = run_one(combo, ws, we)
                perf = (result or {}).get("performance", result) or {}
                v = metric_value(perf, objective)
                nt = trades_count(result)
            except Exception as e:  # 单段失败不拖垮整轮
                v, nt = None, 0
                perf = {"error": str(e)}
            segs.append({
                "start": str(pd.Timestamp(ws).date()),
                "end": str(pd.Timestamp(we).date()),
                "value": v,
                "trades": nt,
            })
            values.append(v)
            n_trades += nt
            done += 1
            if on_progress:
                on_progress(done, total_runs, f"{len(rows) + 1}/{len(combos)} 组参数")

        rs = robust_score(values)
        reasons = []
        if rs["n"] < 2:
            reasons.append("有效验证段不足")
        if n_trades < min_trades:
            reasons.append(f"交易数不足 ({n_trades}<{min_trades})")
        rows.append({
            "params": dict(combo),
            "segments": segs,
            "values": values,
            "trades": n_trades,
            "disqualified": bool(reasons),
            "reason": "; ".join(reasons),
            **rs,
        })

    rows.sort(key=lambda r: (
        1 if r["disqualified"] else 0,
        0 if r["score"] is not None else 1,
        -(r["score"] if r["score"] is not None else -1e18),
    ))
    return {
        "rows": rows,
        "windows": [
            {"start": str(pd.Timestamp(a).date()), "end": str(pd.Timestamp(b).date())}
            for a, b in windows
        ],
        "objective": objective,
        "min_trades": min_trades,
        "combos": len(combos),
    }


def champion_convergence(rows: list[dict], k: int) -> dict:
    """各验证段的最优参数是否指向同一组 (unanimous / majority / scattered)"""
    if not rows or k <= 0:
        return {"label": "unknown", "per_segment": [], "agreement": 0.0}
    valid = [r for r in rows if r.get("values")]
    per_segment, champions = [], []
    for i in range(k):
        best, best_v = None, None
        for r in valid:
            vals = r["values"]
            if i >= len(vals) or vals[i] is None:
                continue
            if best_v is None or vals[i] > best_v:
                best, best_v = r, vals[i]
        name = _params_key(best["params"]) if best else ""
        champions.append(name)
        per_segment.append({"segment": i + 1, "params": best["params"] if best else None,
                            "value": best_v})
    filled = [c for c in champions if c]
    if not filled:
        return {"label": "unknown", "per_segment": per_segment, "agreement": 0.0}
    top_name = pd.Series(filled).mode().iloc[0]
    agreement = filled.count(top_name) / len(filled)
    if agreement >= 0.999:
        label = "unanimous"
    elif agreement > 0.5:
        label = "majority"
    else:
        label = "scattered"
    return {"label": label, "per_segment": per_segment, "agreement": float(agreement)}


def _params_key(params: dict) -> str:
    return ",".join(f"{k}={params[k]}" for k in sorted(params))


def plateau_ratio(rows: list[dict], champion: dict, k: int,
                  threshold: float = 0.7) -> float | None:
    """冠军的"邻域"里有多少组合也达到冠军稳健分的 70%

    只沿每个参数轴取相邻档位, 衡量这是高原还是孤峰; 1.0=全邻域可用。
    """
    if not champion or not champion.get("score") or k <= 0:
        return None
    keys = sorted(champion["params"].keys())
    axes = {}
    for key in keys:
        try:
            vals = sorted({float(r["params"][key]) for r in rows
                           if isinstance(r.get("params"), dict) and key in r["params"]})
        except (TypeError, ValueError):
            continue
        axes[key] = vals
    champ_vec = {}
    for key in axes:
        try:
            champ_vec[key] = float(champion["params"][key])
        except (TypeError, ValueError):
            return None
    neighbors, scores = [], []
    for r in rows:
        if not isinstance(r.get("params"), dict) or r.get("disqualified"):
            continue
        close, valid = 0, True
        for key in axes:
            try:
                v = float(r["params"][key])
            except (TypeError, ValueError):
                valid = False
                break
            idx = axes[key].index(champ_vec[key]) if champ_vec[key] in axes[key] else None
            if idx is None:
                valid = False
                break
            step = axes[key][idx] - (axes[key][idx - 1] if idx > 0 else axes[key][idx])
            if abs(v - champ_vec[key]) <= abs(step) + 1e-9:
                close += 1
        if valid and close >= len(axes) - 1 and r is not champion:
            neighbors.append(r)
            if r.get("score") is not None:
                scores.append(r["score"])
    if not neighbors:
        return None
    good = sum(1 for s in scores if s >= champion["score"] * threshold)
    return good / len(neighbors)


def generate_folds(start, end, train_days: int = 365,
                   test_days: int = 90, step_days: int | None = None,
                   min_train_days: int = 120) -> list[dict]:
    """滚动样本外折切分

    测试窗从训练窗末日 + 1 天开始, 严格不重叠。
    """
    s = pd.Timestamp(start).normalize()
    e = pd.Timestamp(end).normalize()
    if e <= s:
        raise ValueError("结束日期必须晚于开始日期")
    train_days = max(int(train_days), int(min_train_days))
    test_days = max(int(test_days), 20)
    step = max(int(step_days or test_days), 1)

    folds = []
    train_start = s
    while True:
        train_end = train_start + timedelta(days=train_days - 1)
        test_start = train_end + timedelta(days=1)
        test_end = test_start + timedelta(days=test_days - 1)
        if test_end > e:
            break
        folds.append({
            "train": (train_start, train_end),
            "test": (test_start, test_end),
        })
        train_start = train_start + timedelta(days=step)
    return folds


def run_walkforward(run_one, combos: list[dict], folds: list[dict],
                    objective: str = "calmar",
                    min_trades: int = MIN_TRADES_DEFAULT,
                    on_progress=None) -> dict:
    """滚动样本外检验: 每折在训练窗选参, 在紧随其后的测试窗验证

    资本按折重置; 聚合时把各折 OOS 收益率复利串联, 得到连续样本外净值。
    """
    objective = normalize_objective(objective)
    folds = list(folds or [])
    if not folds:
        raise ValueError("至少需要一折")
    total_runs = len(folds) * (len(combos) + 1)

    records = []
    done = 0
    for idx, fold in enumerate(folds):
        ts, te = fold["train"]
        vs, ve = fold["test"]
        best, best_score, best_trades = None, None, 0
        for combo in combos:
            try:
                r = run_one(combo, ts, te)
                perf = (r or {}).get("performance", r) or {}
                v = metric_value(perf, objective)
                nt = trades_count(r)
            except Exception:
                v, nt = None, 0
            done += 1
            if on_progress:
                on_progress(done, total_runs, f"第 {idx + 1}/{len(folds)} 折 · 训练窗选参")
            if v is None or nt < min_trades:
                continue
            if best_score is None or v > best_score:
                best, best_score, best_trades = combo, v, nt

        if best is None:
            records.append({
                "fold": idx + 1,
                "train": [str(pd.Timestamp(ts).date()), str(pd.Timestamp(te).date())],
                "test": [str(pd.Timestamp(vs).date()), str(pd.Timestamp(ve).date())],
                "params": None, "is_value": None, "oos_value": None,
                "oos_return": None, "trades": 0, "skipped": True,
                "reason": f"训练窗无合格参数 (交易数<{min_trades} 或指标为空)",
            })
            done += 1
            continue

        try:
            r_oos = run_one(best, vs, ve)
            perf_oos = (r_oos or {}).get("performance", r_oos) or {}
            oos_value = metric_value(perf_oos, objective)
            oos_return = metric_value(perf_oos, "total_return")
            oos_trades = trades_count(r_oos)
        except Exception as e:
            oos_value, oos_return, oos_trades = None, None, 0
            r_oos = {"error": str(e)}
        done += 1
        if on_progress:
            on_progress(done, total_runs, f"第 {idx + 1}/{len(folds)} 折 · 测试窗验证")

        records.append({
            "fold": idx + 1,
            "train": [str(pd.Timestamp(ts).date()), str(pd.Timestamp(te).date())],
            "test": [str(pd.Timestamp(vs).date()), str(pd.Timestamp(ve).date())],
            "params": dict(best),
            "is_value": best_score,
            "oos_value": oos_value,
            "oos_return": oos_return,
            "trades": oos_trades,
            "skipped": False,
            "reason": (r_oos or {}).get("error", ""),
        })

    valid = [r for r in records if not r["skipped"] and r["oos_return"] is not None]
    is_vals = [r["is_value"] for r in valid if r["is_value"] is not None]
    oos_vals = [r["oos_value"] for r in valid if r["oos_value"] is not None]
    oos_returns = [r["oos_return"] for r in valid]
    compounded = None
    if oos_returns:
        growth = 1.0
        for r in oos_returns:
            growth *= 1 + r / 100.0
        compounded = (growth - 1) * 100

    avg_is = float(np.mean(is_vals)) if is_vals else None
    avg_oos = float(np.mean(oos_vals)) if oos_vals else None
    degradation = None
    if avg_is not in (None, 0) and avg_oos is not None:
        degradation = (avg_is - avg_oos) / abs(avg_is) * 100

    return {
        "folds": records,
        "objective": objective,
        "compounded_return": compounded,
        "consistency": (sum(1 for r in oos_returns if r > 0) / len(oos_returns)
                        if oos_returns else None),
        "avg_is": avg_is,
        "avg_oos": avg_oos,
        "degradation_pct": degradation,
        "valid_folds": len(valid),
        "total_folds": len(records),
        "min_trades": min_trades,
    }
