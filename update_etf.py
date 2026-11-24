#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""ETF 数据全量刷新脚本

一次性灌满 ETF 全部数据:
  1. ETF 清单同步 + 日线行情 (新浪/akshare, 无配额限制)
  2. 交易所官方份额 (上交所, 无配额)
  3. 华泰柏瑞增强数据 (溢价/规模/资金流/两融/估值, 受每日配额限制)
  4. (可选) 历史回补: 溢价率 + 资金流按年切窗

用法:
  python update_etf.py                    # 全量刷新 (日线 + 当日增强)
  python update_etf.py --backfill         # 额外回补历史增强数据
  python update_etf.py --bars-only        # 只刷日线行情
"""
import os
import sys
import time
import argparse
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import data_store
import data_manager


def _ts():
    return datetime.now().strftime("%H:%M:%S")


def step_bars(start: str = "20200101"):
    """Step 1: ETF 清单 + 日线行情全量刷新"""
    print(f"\n{'='*60}")
    print(f"[{_ts()}] Step 1: ETF 清单同步 + 日线行情")
    print(f"{'='*60}")

    etfs = data_store.get_etf_codes()
    print(f"  ETF 清单: {len(etfs)} 只")

    def _cb(done, total, symbol, n):
        if done % 50 == 0 or done == total:
            print(f"  [{done}/{total}] {symbol}: {n} bars")

    r = data_manager.ensure_etf_universe(start=start, progress_cb=_cb)
    print(f"  结果: {r}")
    return r


def step_shares():
    """Step 2: 交易所官方份额 (上交所, 免配额)"""
    print(f"\n{'='*60}")
    print(f"[{_ts()}] Step 2: 交易所官方份额")
    print(f"{'='*60}")
    r = data_manager.ensure_etf_shares()
    print(f"  结果: {r}")
    return r


def step_extras():
    """Step 3: 华泰柏瑞增强数据 (溢价/规模/资金流/两融/估值)"""
    print(f"\n{'='*60}")
    print(f"[{_ts()}] Step 3: 华泰柏瑞增强数据")
    print(f"{'='*60}")

    ok, reason = _check_htbr()
    if not ok:
        print(f"  跳过: {reason}")
        return {"skipped": reason}

    def _cb(msg):
        print(f"  {msg}")

    r = data_manager.ensure_etf_extras(progress_cb=_cb)
    print(f"  结果: {r}")
    return r


def step_backfill():
    """Step 4: 历史回补 (溢价率 + 资金流, 按年切窗)"""
    print(f"\n{'='*60}")
    print(f"[{_ts()}] Step 4: 历史回补")
    print(f"{'='*60}")

    ok, reason = _check_htbr()
    if not ok:
        print(f"  跳过: {reason}")
        return {"skipped": reason}

    # 全市场资金流 (成本低, 不依赖基金内码)
    print("  [4a] 全市场 ETF 资金流历史...")
    r_flow = data_manager.backfill_etf_market_flow(start="2020-01-01")
    print(f"    {r_flow}")

    # 溢价率 + 资金报表 (按年切窗, 受 max_calls 保护)
    print("  [4b] 溢价率 + 资金报表回补...")
    r_fac = data_manager.backfill_etf_factors(start_year=2022, max_calls=380)
    print(f"    {r_fac}")

    # 净值回补 (供本地算溢价率)
    print("  [4c] ETF 单位净值回补...")
    r_nav = data_manager.backfill_etf_nav(max_symbols=60)
    print(f"    {r_nav}")

    return {"flow": r_flow, "factors": r_fac, "nav": r_nav}


def _check_htbr():
    """检查华泰柏瑞数据源是否可用"""
    try:
        import htbr_source as hs
        return hs.available()
    except Exception as e:
        return False, f"htbr_source 不可用: {e}"


def _print_stats():
    """打印当前 DB 中 ETF 数据覆盖情况"""
    print(f"\n{'='*60}")
    print(f"[{_ts()}] DB 数据覆盖概览")
    print(f"{'='*60}")
    s = data_store.etf_stats()
    print(f"  日线行情: {s['symbols']} 只, {s['bars']} 根, "
          f"{s['first_date']} ~ {s['last_date']}")
    e = data_store.etf_ext_stats()
    print(f"  元数据:   {e['meta_symbols']} 只")
    print(f"  行情扩展: {e['quote_symbols']} 只, {e['quote_bars']} 行, "
          f"末尾 {e['quote_last']}")
    print(f"  规模份额: {e['capital_symbols']} 只, {e['capital_bars']} 行, "
          f"末尾 {e['capital_last']}")
    print(f"  两融:     {e['margin_bars']} 行, 末尾 {e['margin_last']}")
    print(f"  资金流:   {e['flow_bars']} 行, 末尾 {e['flow_last']}")
    print(f"  官方份额: {e['share_symbols']} 只, {e['share_bars']} 行, "
          f"末尾 {e['share_last']}")
    print(f"  净值:     {e['nav_bars']} 行, 末尾 {e['nav_last']}")


def main():
    parser = argparse.ArgumentParser(description="ETF 数据全量刷新")
    parser.add_argument("--backfill", action="store_true",
                        help="额外回补历史增强数据 (溢价/资金流/净值)")
    parser.add_argument("--bars-only", action="store_true",
                        help="只刷日线行情, 跳过增强数据")
    parser.add_argument("--start", default="20200101",
                        help="日线起始日期 (默认 20200101)")
    args = parser.parse_args()

    data_store.init_db()
    t0 = time.perf_counter()

    print(f"[{_ts()}] ETF 数据全量刷新开始")

    # Step 1: 日线行情 (必做)
    step_bars(start=args.start)

    if not args.bars_only:
        # Step 2: 官方份额
        step_shares()

        # Step 3: 华泰柏瑞增强数据
        step_extras()

        # Step 4: 历史回补 (可选)
        if args.backfill:
            step_backfill()

    _print_stats()
    dt = time.perf_counter() - t0
    print(f"\n[{_ts()}] 全量刷新完成, 总耗时 {dt:.1f}s")


if __name__ == "__main__":
    main()