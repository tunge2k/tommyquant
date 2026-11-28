"""回测「当前结果 vs 历史最优」状态机

纯逻辑模块: 不依赖 streamlit / pandas, 便于单元测试。
streamlit_app 只负责把这里算出的 state 渲染成提示 / 按钮。

口径:
  - 比较指标默认 total_return, 与 best_backtest.metric_name 保持一致
  - 浮点容差 EPS=1e-9: 差值 <= EPS 视为「一致」, 不重复保存
  - 自动保存只在「尚无最优」或「严格更优」时触发, 因此天然幂等:
    不会重复保存 (一致时跳过), 也不会漏掉更优结果 (任何来源的结果都会经过这里)
"""
EPS = 1e-9

NO_BEST = "no_best"        # 尚无最优记录
NEW_BEST = "new_best"      # 当前严格优于最优
SAME = "same"              # 与最优一致
WORSE = "worse"            # 劣于最优
DISCARDED = "discarded"    # 用户已确认放弃本次, 展示最优

REL_LABELS = {
    NO_BEST: "尚无最优",
    NEW_BEST: "新最优",
    SAME: "与最优一致",
    WORSE: "低于最优",
    DISCARDED: "低于最优（已放弃本次）",
}

ORIGIN_FRESH = "fresh"       # 本次点击「开始回测」刚算完
ORIGIN_LOADED = "loaded"     # 参数未变, 直接载入库内结果
ORIGIN_HISTORY = "history"   # 参数/区间已变, 展示的是上次结果


def metric_value(perf: dict, metric_name: str = "total_return") -> float:
    """从 performance 取比较指标; 缺失 / None / 非数值 → 0.0"""
    try:
        return float((perf or {}).get(metric_name, 0) or 0)
    except (TypeError, ValueError):
        return 0.0


def best_value(best) -> float:
    """已保存最优的指标值; 无记录/坏数据 → None"""
    if not best:
        return None
    try:
        return float(best.get("metric_value"))
    except (TypeError, ValueError, AttributeError):
        return None


def classify(current: float, best, eps: float = EPS) -> str:
    """当前值 vs 最优记录 → 关系枚举"""
    b = best_value(best) if not isinstance(best, (int, float)) else float(best)
    if b is None:
        return NO_BEST
    if current > b + eps:
        return NEW_BEST
    if abs(current - b) <= eps:
        return SAME
    return WORSE


def should_autosave(current: float, best, eps: float = EPS) -> bool:
    """尚无最优 / 严格更优 → 需要保存; 一致或更差 → 不写 (靠 EPS 防重复)"""
    return classify(current, best, eps) in (NO_BEST, NEW_BEST)


def should_prompt(relation: str, discarded: bool = False) -> bool:
    """只有「劣于最优且用户还没选过」才弹确认, 避免每次 rerun 重复打扰"""
    return relation == WORSE and not discarded


def origin_of(meta: dict) -> str:
    """从结果 _meta 判断来源: fresh / loaded / history"""
    meta = meta or {}
    if meta.get("stale_params"):
        return ORIGIN_HISTORY
    if meta.get("origin") == "fresh":
        return ORIGIN_FRESH
    return ORIGIN_LOADED


def origin_caption(meta: dict) -> str:
    """结果来源的说明文案 (fresh 不需要额外提示, 返回空串)"""
    meta = meta or {}
    if meta.get("stale_params"):
        return "参数/区间已变更，展示上次结果；点「🚀 开始回测」按新参数重算"
    if meta.get("stale"):
        return "行情数据已更新，展示上次结果；点「🚀 开始回测」按最新数据刷新"
    if meta.get("origin") == "fresh":
        return ""
    return "已载入保存结果"


def _fmt(v: float) -> str:
    return f"{v:+.2f}%"


def build_state(current_perf: dict, best: dict, meta: dict = None,
                discarded: bool = False, metric_name: str = None) -> dict:
    """生成展示状态。

    返回:
      relation  关系枚举
      autosave  是否需要落库 (尚无最优 / 严格更优)
      prompt    是否需要「放弃本次 / 保留本次」确认
      display   "result" 展示当前结果 / "best" 展示历史最优
      level     success / info / warning (决定提示框颜色)
      message   一句完整说明
      summary   一行「当前 vs 最优」摘要 (始终展示)
      current / best / origin / metric_name
    """
    mname = metric_name or (best or {}).get("metric_name") or "total_return"
    cur = metric_value(current_perf, mname)
    bval = best_value(best)
    rel = classify(cur, bval)
    origin = origin_of(meta)
    ts = str((best or {}).get("created_at", "")).replace("T", " ")
    if discarded and rel in (WORSE, SAME):
        rel = DISCARDED

    subject = {ORIGIN_FRESH: "本次回测",
               ORIGIN_LOADED: "当前载入的结果",
               ORIGIN_HISTORY: "库内最近一次结果"}.get(origin, "当前结果")

    if rel == NO_BEST:
        level = "success"
        message = (f"{subject}总收益 {_fmt(cur)}，尚无可比的最优记录，"
                   "已自动保存为最优。")
    elif rel == NEW_BEST:
        level = "success"
        message = (f"{subject}总收益 {_fmt(cur)}，优于原最优 {_fmt(bval)}"
                   f"（保存于 {ts}），已自动保存为新最优。")
    elif rel == SAME:
        level = "info"
        message = f"{subject}总收益 {_fmt(cur)}，与已保存最优一致（保存于 {ts}）。"
    elif rel == WORSE:
        level = "warning"
        message = (f"{subject}总收益 {_fmt(cur)}，低于历史最优 {_fmt(bval)}"
                   f"（保存于 {ts}）。是否放弃本次结果、保留最优？")
    else:  # DISCARDED
        level = "info"
        message = f"已放弃本次结果，当前展示历史最优 {_fmt(bval)}（保存于 {ts}）。"

    summary = (f"当前 {_fmt(cur)} · 已保存最优 "
               f"{_fmt(bval) if bval is not None else '—'}")
    if bval is not None and ts:
        summary += f"（{ts}）"
    summary += f" · {REL_LABELS[rel]}"

    return {
        "relation": rel,
        "autosave": rel in (NO_BEST, NEW_BEST),
        "prompt": should_prompt(rel) and origin != ORIGIN_HISTORY,
        "display": "best" if rel == DISCARDED else "result",
        "level": level,
        "message": message,
        "summary": summary,
        "current": cur,
        "best": bval,
        "origin": origin,
        "metric_name": mname,
    }


def ensure_best_saved(store, strategy: str, current_perf: dict, params: dict,
                      fingerprint: str, settings: dict, payload: dict,
                      metric_name: str = None):
    """唯一自动保存入口: 当前更优 / 尚无最优 → 落库。

    返回 (saved, prev_best)。prev_best 是保存前的记录, 供界面描述「原最优」。
    连续调用不会重复保存: 保存后 best 已等于当前值, classify 变为 SAME。
    """
    best = store.get_best_backtest(strategy)
    mname = metric_name or (best or {}).get("metric_name") or "total_return"
    cur = metric_value(current_perf, mname)
    if not should_autosave(cur, best):
        return False, best
    store.save_best_backtest(strategy, mname, cur, params, fingerprint, payload,
                             settings=settings)
    return True, best
