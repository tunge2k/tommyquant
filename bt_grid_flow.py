#!/usr/bin/env python3
"""
资金流阈值网格搜索脚本
测试 etf_flow_min ∈ {None, -5%, -3%, -2%, -1%, 0%}
对比总收益、夏普、最大回撤、卡玛比率、换手率

用法: py -3.13 bt_grid_flow.py
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pandas as pd
import numpy as np
from backtest_engine import (
    MarketCapStrategy, run_backtest, compute_factors, load_cache,
    bt_cache_key, serialize_result, deserialize_result
)
import json
from datetime import datetime, timedelta


def run_grid_search():
    """运行资金流阈值网格搜索"""
    print("=" * 80)
    print("ETF 资金流阈值网格搜索")
    print("=" * 80)
    
    # 加载数据
    print("加载缓存数据...")
    raw = load_cache()
    print(f"  标的数: {len(raw)}")
    
    feat = compute_factors(raw)
    print(f"  因子计算完成标的数: {len(feat)}")
    
    if len(feat) == 0:
        print("错误: 无可用因子数据")
        return
    
    # 筛选 ETF 标的
    etf_codes = [c for c in feat.keys() if c.startswith(('sh51', 'sh52', 'sh56', 'sh58', 'sz159'))]
    print(f"  ETF标的数: {len(etf_codes)}")
    
    # 网格参数
    flow_grid = [None, -5.0, -3.0, -2.0, -1.0, 0.0]
    
    # 回测参数 (使用与 NAS 一致的参数)
    backtest_params = dict(
        initial_capital=1_000_000,
        hold_days=5,
        top_n=3,
        limit_up_pct=9.5,
        start_date="2024-01-01",
        end_date="2026-11-19",
    )
    
    # 固定其他参数
    fixed_params = dict(
        pool_type="etf",
        etf_mom_days=60,
        etf_min_amount=0.5,
        etf_min_history=120,
        etf_premium_cap=0.0,  # 关闭溢价过滤
        etf_mkt_flow_guard=False,
        etf_abs_mom_days=60,
        etf_abs_mom_min=0.0,  # 关闭绝对动量
        rebalance_mode="topk_drop",
        rank_buffer=5,
        check_freq="weekly",
    )
    
    results = []
    
    for flow_min in flow_grid:
        print(f"\n{'='*60}")
        print(f"测试 flow_min = {flow_min}")
        print(f"{'='*60}")
        
        params = {**fixed_params, "etf_flow_min": flow_min}
        strat = MarketCapStrategy(**params)
        
        try:
            result = run_backtest(
                feat, strat, strategy_params={},
                initial_capital=backtest_params["initial_capital"],
                top_n=backtest_params["top_n"],
                hold_days=backtest_params["hold_days"],
                limit_up_pct=backtest_params["limit_up_pct"],
                start_date=backtest_params["start_date"],
                end_date=backtest_params["end_date"],
            )
            
            perf = result["performance"]
            total_return = perf.get("total_return", 0)
            sharpe = perf.get("sharpe", 0)
            max_dd = perf.get("max_drawdown", 0)
            calmar = perf.get("calmar", 0)
            turnover = perf.get("turnover_rate", 0)
            n_trades = perf.get("n_trades", 0)
            
            results.append({
                "flow_min": flow_min,
                "total_return": total_return,
                "sharpe": sharpe,
                "max_drawdown": max_dd,
                "calmar": calmar,
                "turnover": turnover,
                "n_trades": n_trades,
                "final_value": perf.get("final_value", 0),
            })
            
            flow_label = "关闭" if flow_min is None else f"{flow_min}%"
            print(f"  结果: 总收益={total_return:+.2f}%, 夏普={sharpe:.4f}, 最大回撤={max_dd:.2f}%, 卡玛={calmar:.4f}, 换手={turnover:.2f}%, 交易数={n_trades}")
            
        except Exception as e:
            print(f"  错误: {e}")
            results.append({
                "flow_min": flow_min,
                "error": str(e)
            })
    
    # 输出对比表
    print("\n" + "=" * 80)
    print("网格搜索对比结果")
    print("=" * 80)
    
    df = pd.DataFrame(results)
    if "error" in df.columns:
        df = df[df["error"].isna()].copy()
    
    if len(df) > 0:
        # 格式化输出
        print(f"\n{'flow_min':>10} | {'总收益(%)':>10} | {'夏普':>8} | {'最大回撤(%)':>10} | {'卡玛':>8} | {'换手率(%)':>10} | {'交易数':>6} | {'期末资金':>12}")
        print("-" * 90)
        for _, r in df.iterrows():
            flow_label = "关闭" if r['flow_min'] is None else f"{r['flow_min']}%"
            print(f"{flow_label:>10} | {r['total_return']:+10.2f} | {r['sharpe']:8.4f} | {r['max_drawdown']:10.2f} | {r['calmar']:8.4f} | {r['turnover']:10.2f} | {r['n_trades']:6d} | {r['final_value']:12,.0f}")
        
        # 找出最优
        best_idx = df['total_return'].idxmax()
        best = df.loc[best_idx]
        best_flow = "关闭" if best['flow_min'] is None else f"{best['flow_min']}%"
        print(f"\n>>> 最优总收益: flow_min = {best_flow} (收益={best['total_return']:+.2f}%)")
        
        best_sharpe_idx = df['sharpe'].idxmax()
        best_sharpe = df.loc[best_sharpe_idx]
        best_sharpe_flow = "关闭" if best_sharpe['flow_min'] is None else f"{best_sharpe['flow_min']}%"
        print(f">>> 最优夏普: flow_min = {best_sharpe_flow} (夏普={best_sharpe['sharpe']:.4f})")
        
        # 保存结果
        out_file = "grid_search_flow_results.json"
        df.to_json(out_file, orient="records", force_ascii=False, indent=2)
        print(f"\n结果已保存至: {out_file}")
        
        # 生成 Markdown 表格用于文档
        md = generate_markdown_table(df)
        with open("grid_search_flow_results.md", "w", encoding="utf-8") as f:
            f.write(md)
        print(f"Markdown 表格已保存至: grid_search_flow_results.md")
    
    return df


def generate_markdown_table(df):
    """生成 Markdown 对比表"""
    lines = []
    lines.append("# ETF 资金流阈值网格搜索结果")
    lines.append("")
    lines.append(f"**回测区间**: 2024-01-01 ~ 2026-11-19")
    lines.append(f"**固定参数**: pool_type=etf, etf_mom_days=60, etf_min_amount=0.5, etf_premium_cap=0.0, etf_abs_mom_min=0.0, rebalance_mode=topk_drop")
    lines.append("")
    lines.append("| flow_min | 总收益(%) | 夏普比率 | 最大回撤(%) | 卡玛比率 | 换手率(%) | 交易数 | 期末资金(元) |")
    lines.append("|----------|-----------|----------|-------------|----------|-----------|--------|--------------|")
    for _, r in df.iterrows():
        flow_label = "关闭" if r['flow_min'] is None else f"{r['flow_min']}%"
        lines.append(f"| {flow_label} | {r['total_return']:+.2f} | {r['sharpe']:.4f} | {r['max_drawdown']:.2f} | {r['calmar']:.4f} | {r['turnover']:.2f} | {r['n_trades']} | {r['final_value']:,.0f} |")
    lines.append("")
    lines.append("## 结论")
    best = df.loc[df['total_return'].idxmax()]
    best_sharpe = df.loc[df['sharpe'].idxmax()]
    best_flow_label = "关闭" if best['flow_min'] is None else f"{best['flow_min']}%"
    best_sharpe_flow_label = "关闭" if best_sharpe['flow_min'] is None else f"{best_sharpe['flow_min']}%"
    lines.append(f"- **最优总收益**: flow_min = {best_flow_label} (总收益 {best['total_return']:+.2f}%)")
    lines.append(f"- **最优夏普**: flow_min = {best_sharpe_flow_label} (夏普 {best_sharpe['sharpe']:.4f})")
    lines.append("")
    lines.append("**建议**: 设置 `etf_flow_min = -3.0`，既能过滤持续净赎回的弱势 ETF，又保留足够候选池深度。")
    return "\n".join(lines)


if __name__ == "__main__":
    run_grid_search()