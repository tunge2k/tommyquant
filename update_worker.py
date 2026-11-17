"""
数据调度 Worker —— 容器内后台常驻进程

职责 (对应 5 项数据要求):
  1. 启动自举: 库为空时自动补全全A股 + 全部ETF
  2. 策略驱动: 消费 data_task 队列, 按策略声明的数据需求补全
  3. 盘后补全: 每个交易日 15:35 自动补齐当日K线 (+ 周五刷新财务)
  4. 实时落库: 交易时段每 60 秒采集全市场快照, 支撑模拟交易
  5. 新闻滚动: 每 NEWS_INTERVAL_MIN 分钟抓取去重新闻

运行:
  python update_worker.py          # 常驻
  python update_worker.py --once   # 立即执行一次盘后补全
"""
import os
import sys
import time
import logging
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# DoH 兜底: NAS 路由器可能劫持 UDP 53, 系统 DNS 失败时走阿里 DoH
try:
    import doh_patch
    doh_patch.install()
except Exception:
    pass

import data_store
import data_manager
import news_fetcher

CLOSE_HOUR = int(os.environ.get("CLOSE_HOUR", "15"))       # 盘后补全开始时间
CLOSE_MINUTE = int(os.environ.get("CLOSE_MINUTE", "35"))
RT_INTERVAL_SEC = int(os.environ.get("RT_INTERVAL_SEC", "5"))        # 实时快照间隔
GM_INTERVAL_SEC = int(os.environ.get("GM_INTERVAL_SEC", "5"))        # 全球市场刷新间隔(交易时段)
GM_INTERVAL_IDLE = int(os.environ.get("GM_INTERVAL_IDLE", "300"))    # 全球市场刷新间隔(非交易时段)
NEWS_INTERVAL_MIN = int(os.environ.get("NEWS_INTERVAL_MIN", "120"))  # 新闻间隔
CHECK_INTERVAL = int(os.environ.get("CHECK_INTERVAL", "5"))          # 主循环间隔
AUTO_DOWNLOAD_COUNT = os.environ.get("AUTO_DOWNLOAD_COUNT", "0")     # 0=全量
FUNDAMENTAL_WEEKDAY = int(os.environ.get("FUNDAMENTAL_WEEKDAY", "4"))  # 周五刷新财务

logger = logging.getLogger("update_worker")
if not logger.handlers:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s %(message)s")


def _end_date() -> str:
    return datetime.now().strftime("%Y%m%d")


def bootstrap():
    """库为空时全量自举: 全部A股 + 全部ETF, 并写入覆盖索引"""
    if AUTO_DOWNLOAD_COUNT in (0, "0", "all"):
        limit = None
    else:
        try:
            limit = int(AUTO_DOWNLOAD_COUNT)
        except Exception:
            limit = 200

    start, end = "20200101", _end_date()
    try:
        stocks = data_store.get_main_board_codes(limit=limit)
        symbols = [f"sh{c}" if c.startswith("6") else f"sz{c}" for c, _ in stocks]
        logger.info(f"自举 A股 {len(symbols)} 只 ...")
        res = data_manager.ensure_bars(symbols, start, end, workers=8)
        logger.info(f"A股完成: {res}")
    except Exception as e:
        logger.error(f"A股自举失败: {e}")

    try:
        etfs = [s for s, _ in data_store.get_etf_codes()]
        logger.info(f"自举 ETF {len(etfs)} 只 ...")
        res = data_manager.ensure_bars(etfs, start, end, workers=6)
        logger.info(f"ETF完成: {res}")
    except Exception as e:
        logger.error(f"ETF自举失败: {e}")

    # 财务数据 (戴维斯双击等策略需要)
    try:
        logger.info("自举财务数据 ...")
        r = data_manager.ensure_fundamentals(quarters=4)
        logger.info(f"财务数据完成: {r}")
    except Exception as e:
        logger.error(f"财务数据自举失败: {e}")


# 网页端仪表盘默认跑这三个策略 (参数与 streamlit_app.DEFAULTS 保持一致)
DASH_STRATEGIES = ["小市值轮动·戴维斯双击", "动量+均线+小市值"]
WEB_DEFAULTS = {"initial": 1_000_000, "hold": 5, "top": 5, "lu_pct": 9.5, "start": "2023-01-01"}


def warm_web_backtests(feat: dict):
    """盘后把仪表盘策略的回测结果算好写进 SQLite 缓存,
    这样第二天打开页面直接命中缓存 (不用等 3×20s 的回测)"""
    from backtest_engine import run_backtest, STRATEGIES, bt_cache_key, serialize_result
    try:
        import fast_backtest as _fb
    except Exception:
        _fb = None
    fp = data_store.data_fingerprint()
    end_d = data_store.db_stats().get("coverage_last_date") or data_store.db_stats()["last_date"]
    start_s = f"{WEB_DEFAULTS['start']} 00:00:00"
    end_s = f"{end_d} 00:00:00"
    done = 0
    t_all = time.time()

    # 成本参数 (与 UI 保持一致, 参与缓存键)
    cost_params = {
        "commission_rate": float(data_store.get_setting("cost_commission_rate", 0.00025)),
        "commission_min": float(data_store.get_setting("cost_commission_min", 5.0)),
        "stamp_tax_rate": float(data_store.get_setting("cost_stamp_tax_rate", 0.001)),
        "transfer_fee_rate": float(data_store.get_setting("cost_transfer_fee_rate", 0.00001)),
        "slippage_rate": float(data_store.get_setting("cost_slippage_rate", 0.001)),
    }
    cost_str = "|".join(str(cost_params[k]) for k in
                        ("commission_rate", "commission_min", "stamp_tax_rate",
                         "transfer_fee_rate", "slippage_rate"))

    for name in DASH_STRATEGIES:
        key_raw, ck = bt_cache_key(name, WEB_DEFAULTS["initial"], WEB_DEFAULTS["hold"],
                                   WEB_DEFAULTS["top"], WEB_DEFAULTS["lu_pct"],
                                   start_s, end_s, fp, cost_str)
        if data_store.get_backtest_cache(ck):
            continue
        try:
            _fn = STRATEGIES[name]
            if _fb is not None and _fb.supports_fast(_fn):
                r = _fb.run_fast(feat, _fn, strategy_params={"top_n": WEB_DEFAULTS["top"]},
                                 initial_capital=WEB_DEFAULTS["initial"],
                                 hold_days=WEB_DEFAULTS["hold"],
                                 limit_up_pct=WEB_DEFAULTS["lu_pct"],
                                 start_date=start_s, end_date=end_s,
                                 cost_params=cost_params)
            else:
                r = run_backtest(feat, _fn, strategy_params={"top_n": WEB_DEFAULTS["top"]},
                                 initial_capital=WEB_DEFAULTS["initial"],
                                 hold_days=WEB_DEFAULTS["hold"],
                                 limit_up_pct=WEB_DEFAULTS["lu_pct"],
                                 start_date=start_s, end_date=end_s,
                                 cost_params=cost_params)
            if len(r.get("curve", [])) > 0:
                data_store.save_backtest_cache(ck, name, key_raw, fp, serialize_result(r))
                done += 1
        except Exception as e:
            logger.error(f"回测预热失败 {name}: {e}")
    logger.info(f"网页回测预热: 新算 {done} 个策略 / {round(time.time() - t_all, 1)}s")


def after_close(fundamentals: bool = False):
    """盘后增量补全"""
    logger.info("=== 盘后补全开始 ===")
    try:
        r = data_manager.after_close_sync(fundamentals=fundamentals)
        logger.info(f"盘后补全完成: {r}")
    except Exception as e:
        logger.error(f"盘后补全异常: {e}")
    try:
        symbols = data_store.list_symbols()[:10]
        logger.info(f"新闻: {news_fetcher.fetch_all_news(symbols=symbols)}")
    except Exception as e:
        logger.error(f"新闻异常: {e}")
    # 预热因子磁盘缓存: 网页端打开「回测/选股」时无需现算 (~80s)
    try:
        t0 = time.time()
        data_store.invalidate_stats()          # 保证指纹反映刚补完的数据
        feat = data_store.load_feat_cached()
        logger.info(f"因子缓存预热完成: {len(feat)} 只 / {round(time.time() - t0, 1)}s")
        warm_web_backtests(feat)
    except Exception as e:
        logger.error(f"因子缓存预热异常: {e}")
    logger.info("=== 盘后补全结束 ===")


def run_task(t: dict):
    """消费策略提交的数据任务"""
    import json
    params = {}
    try:
        params = json.loads(t.get("params") or "{}")
    except Exception:
        pass
    tid = t["id"]
    data_store.task_update(tid, "running", "0%", "开始")
    try:
        ds = t["dataset"]
        if ds == "fundamental":
            r = data_manager.ensure_fundamentals(quarters=params.get("quarters", 4))
        elif ds == "realtime":
            n = data_manager.ensure_realtime(params.get("symbols"))
            r = {"rows": n}
        else:
            r = data_manager.ensure_bars(
                params.get("symbols") or data_store.list_symbols(),
                params.get("start", "20200101"),
                params.get("end", _end_date()))
        data_store.task_update(tid, "done", "100%", str(r)[:300])
        logger.info(f"任务 #{tid} 完成: {r}")
    except Exception as e:
        data_store.task_update(tid, "failed", "0%", str(e)[:300])
        logger.error(f"任务 #{tid} 失败: {e}")


def main():
    if "--once" in sys.argv:
        data_store.init_db()
        after_close()
        return

    data_store.init_db()

    # 升级兼容: 老库已有K线但无覆盖索引 → 回填, 避免被判定为"全部缺失"而重复全量下载
    try:
        n_cov = data_manager.backfill_coverage()
        if n_cov:
            logger.info(f"覆盖索引回填 {n_cov} 只标的")
    except Exception as e:
        logger.error(f"覆盖索引回填失败: {e}")

    if not data_store.list_symbols():
        logger.info("数据库为空, 开始自举 ...")
        bootstrap()
    else:
        s = data_store.db_stats()
        f = data_store.fundamental_stats()
        logger.info(f"库内已有 {s['symbols']} 只标的 / {s['bars']} 条K线 / "
                    f"财务 {f['symbols']} 只 (最新报告期 {f['latest_report']})")
        # 财务数据缺失 → 立即排队回填 (戴维斯双击等基本面策略依赖)
        if f["symbols"] == 0:
            data_store.task_create("fundamental", {"quarters": 4}, "首次财务数据回填")
            logger.info("财务数据为空, 已提交回填任务")

    last_close_date = None
    last_news_time = None
    last_rt_time = None
    last_fund_week = None
    last_global_mkt_time = None
    logger.info(f"Worker 启动 | 盘后 {CLOSE_HOUR}:{CLOSE_MINUTE:02d} 补全 | "
                f"实时 {RT_INTERVAL_SEC}s | 新闻 {NEWS_INTERVAL_MIN}min")

    # 启动时数据落后于最近交易日 → 立即补一次 (比如容器在盘后重启)
    try:
        s = data_store.db_stats()
        ltd = data_manager.last_trading_day()
        have = s.get("coverage_last_date") or s.get("last_date")
        if have and have < ltd:
            logger.info(f"数据落后 ({have} < {ltd}), 启动即补全")
            after_close()
            last_close_date = datetime.now().date()
    except Exception as e:
        logger.error(f"启动补全失败: {e}")

    # 启动时若仪表盘回测缓存缺失 → 后台线程预热 (不阻塞主循环)
    def _bg_warm():
        try:
            from backtest_engine import bt_cache_key
            fp = data_store.data_fingerprint()
            s = data_store.db_stats()
            start_s = f"{WEB_DEFAULTS['start']} 00:00:00"
            end_s = f"{s.get('coverage_last_date') or s['last_date']} 00:00:00"
            miss = False
            for name in DASH_STRATEGIES:
                _, ck = bt_cache_key(name, WEB_DEFAULTS["initial"], WEB_DEFAULTS["hold"],
                                     WEB_DEFAULTS["top"], WEB_DEFAULTS["lu_pct"],
                                     start_s, end_s, fp)
                if not data_store.get_backtest_cache(ck):
                    miss = True
                    break
            if miss:
                logger.info("仪表盘回测缓存缺失, 后台预热中 ...")
                warm_web_backtests(data_store.load_feat_cached())
        except Exception as e:
            logger.error(f"后台预热异常: {e}")

    # 启动时后台刷新全球市场数据 (耗时数分钟, 不阻塞主循环)
    def _bg_global_mkt():
        try:
            logger.info("后台刷新全球市场数据 ...")
            m = data_manager.refresh_global_markets()
            logger.info(f"全球市场数据刷新完成: {len(m)} 条")
        except Exception as e:
            logger.error(f"全球市场刷新异常: {e}")

    try:
        import threading
        threading.Thread(target=_bg_warm, daemon=True).start()
        threading.Thread(target=_bg_global_mkt, daemon=True).start()
    except Exception as e:
        logger.error(f"启动预热线程失败: {e}")

    while True:
        try:
            now = datetime.now()
            today = now.date()
            today_str = now.strftime("%Y-%m-%d")

            # 1. 交易时段: 实时快照落库 (模拟交易数据源)
            if data_manager.is_trading_time(now):
                if last_rt_time is None or (now - last_rt_time).total_seconds() >= RT_INTERVAL_SEC:
                    try:
                        # 只抓当前模拟持仓 (最新一次选股) 的标的, 走新浪直连避免东财限流
                        picks = data_store.get_recent_picks(limit=200)
                        syms = []
                        if not picks.empty:
                            _latest = picks["pick_date"].iloc[0]
                            syms = sorted(set(
                                picks[picks["pick_date"] == _latest]["symbol"].tolist()))
                        if syms:
                            n = data_manager.ensure_realtime(syms)
                            logger.info(f"实时快照入库 {n} 条 (标的 {len(syms)} 只)")
                        else:
                            logger.info("实时快照跳过: 暂无选股持仓")
                    except Exception as e:
                        logger.error(f"实时快照异常: {e}")
                    last_rt_time = now

            # 2. 盘后: 当日K线补全
            hm = now.hour * 100 + now.minute
            if (hm >= CLOSE_HOUR * 100 + CLOSE_MINUTE and last_close_date != today
                    and data_manager.is_trading_day(today_str)):
                need_fund = (today.weekday() == FUNDAMENTAL_WEEKDAY and last_fund_week != today.isocalendar()[1])
                after_close(fundamentals=need_fund)
                if need_fund:
                    last_fund_week = today.isocalendar()[1]
                last_close_date = today
                last_news_time = now

            # 3. 新闻滚动
            if last_news_time is None or (now - last_news_time).total_seconds() >= NEWS_INTERVAL_MIN * 60:
                try:
                    symbols = data_store.list_symbols()[:10]
                    news_fetcher.fetch_all_news(symbols=symbols, per_source_timeout=12)
                except Exception as e:
                    logger.error(f"新闻异常: {e}")
                last_news_time = now

            # 4. 全球市场数据定时刷新 (交易时段 30 秒, 非交易时段 5 分钟)
            _gm_iv = GM_INTERVAL_SEC if data_manager.is_trading_time(now) else GM_INTERVAL_IDLE
            if last_global_mkt_time is None or (now - last_global_mkt_time).total_seconds() >= _gm_iv:
                try:
                    m = data_manager.refresh_global_markets()
                    logger.info(f"全球市场数据刷新: {len(m)} 条")
                except Exception as e:
                    logger.error(f"全球市场刷新异常: {e}")
                # 以本次刷新完成时刻为基准, 避免抓取耗时长时连续触发
                last_global_mkt_time = datetime.now()

            # 5. 消费策略提交的数据任务
            t = data_store.task_next_pending()
            if t:
                run_task(t)

        except Exception as e:
            logger.error(f"Worker 异常: {e}")

        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
