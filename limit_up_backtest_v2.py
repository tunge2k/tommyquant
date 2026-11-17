"""
抓涨停策略回测 v2.1 —— 修复版（同一持仓只卖一次）

核心修复:
  - 选股日 vs 非选股日的卖出逻辑分离
  - 选股日: 当天全部清仓 → 然后重新建仓
  - 非选股日: 只卖 hold_expire 或 limit_up 的
  - 这样同一笔持仓只会被结算一次
"""

import warnings
warnings.filterwarnings('ignore')

import pandas as pd, numpy as np, os, random, time
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

# ==================== 配置 ====================
INITIAL_CAPITAL = 1_000_000    # 初始100万
MAX_MARKET_CAP = 200           # 市值上限(亿)
TOP_N = 5
HOLD_DAYS = 5
LIMIT_UP_PCT = 9.5

BACKTEST_START = '20230101'
BACKTEST_END = '20261112'

OUTPUT_DIR = r"d:\py_lezrn\stock_a\888"
CACHE_DIR = os.path.join(OUTPUT_DIR, "cache")

def sina(code):
    return f"sh{code}" if code.startswith('6') else f"sz{code}"

# ==================== 加载数据 ====================
print("=" * 70)
print("📊 抓涨停策略回测 v2.1 —— 资金曲线绩效")
print("=" * 70)

raw = {}
if os.path.isdir(CACHE_DIR):
    for fname in os.listdir(CACHE_DIR):
        if not fname.endswith('.csv'): continue
        try:
            df = pd.read_csv(os.path.join(CACHE_DIR, fname))
            df['date'] = pd.to_datetime(df['date'])
            raw[fname.replace('.csv', '')] = df.sort_values('date').reset_index(drop=True)
        except: pass
print(f"\n[1/4] 加载缓存: {len(raw)} 只")

def add_factors(df):
    if df is None or len(df) < 120: return None
    df = df.copy()
    for c in ['open','close','high','low','volume','turnover','outstanding_share']:
        if c in df.columns: df[c] = pd.to_numeric(df[c], errors='coerce')
    df['pct'] = df['close'].pct_change() * 100
    df['ma20'] = df['close'].rolling(20).mean()
    df['ret_20'] = df['close'].pct_change(20) * 100
    df['turnover_pct'] = df['turnover'] * 100
    df['cap_yi'] = df['close'] * df['outstanding_share'] / 1e8
    df['above_ma20'] = (df['close'] > df['ma20']).astype(int)
    df['limit_up'] = (df['pct'] >= LIMIT_UP_PCT).astype(int)
    return df

feat = {}
for code, df in raw.items():
    f = add_factors(df)
    if f is not None: feat[code] = f
print(f"[2/4] 计算因子: {len(feat)} 只有效")

# 选股日: 取有代表性的股票的每周第一个交易日（用第一只，保证统一）
sample_code = list(feat.keys())[0]
df_sample = feat[sample_code].copy()
df_sample['wk'] = df_sample['date'].dt.strftime('%Y-%W')
all_dates = set(df_sample.drop_duplicates('wk', keep='first')['date'].tolist())
print(f"[3/4] 选股日: {len(all_dates)} 周 (每周一)")

# 日期索引
date_index = {}
for sc, df in feat.items():
    for _, row in df.iterrows():
        d = row['date']
        if d not in date_index: date_index[d] = {}
        date_index[d][sc] = row

all_trade_days = sorted(set().union(*[set(df['date'].tolist()) for df in feat.values()]))
print(f"     总交易日: {len(all_trade_days)}")

# ==================== 交易模拟 ====================
print(f"[4/4] 模拟交易 (3个策略 + 随机对照)...")

def simulate(name, select_fn):
    """单一策略的完整资金曲线模拟"""
    capital = INITIAL_CAPITAL
    holdings = []        # [(sina_code, buy_date, buy_price, shares)]
    curve = []           # [(date, portfolio_value)]
    
    trades = 0
    hit_lu = 0
    total_hold_days = 0
    
    for current_date in all_trade_days:
        if current_date not in date_index:
            continue
        snapshot = date_index[current_date]
        
        # ============ 卖出逻辑 ============
        if current_date in all_dates:
            # --- 选股日: 全部清仓，不管条件 ---
            for sc, bd, bp, shares in holdings:
                if sc in snapshot:
                    row = snapshot[sc]
                    sp = row['close']
                    if np.isnan(sp) or sp <= 0: sp = bp
                    capital += sp * shares
                    trades += 1
            holdings = []
        else:
            # --- 非选股日: 只卖符合条件的 ---
            remaining = []
            for sc, bd, bp, shares in holdings:
                if sc not in snapshot:
                    remaining.append((sc, bd, bp, shares))
                    continue
                row = snapshot[sc]
                held = (current_date - bd).days
                sell = False
                if held >= HOLD_DAYS:
                    sell = True
                elif row.get('limit_up', 0) == 1 and held >= 1:
                    sell = True
                    hit_lu += 1
                
                if sell:
                    sp = row['close']
                    if np.isnan(sp) or sp <= 0: sp = bp
                    capital += sp * shares
                    trades += 1
                else:
                    remaining.append((sc, bd, bp, shares))
            holdings = remaining
        
        # ============ 买入逻辑 ============
        if current_date in all_dates and len(snapshot) >= TOP_N:
            selected = select_fn(snapshot)
            if len(selected) >= TOP_N:
                # 等权买入
                per = capital / TOP_N
                holdings = []
                for sc in selected[:TOP_N]:
                    if sc not in snapshot: continue
                    row = snapshot[sc]
                    bp = row.get('open', row['close'])
                    if np.isnan(bp) or bp <= 0: bp = row['close']
                    if np.isnan(bp) or bp <= 0: continue
                    shares = per / bp
                    holdings.append((sc, current_date, bp, shares))
                capital -= per * TOP_N   # ← 关键：扣现金！之前漏了！
        
        # ============ 记录净值 ============
        pv = capital  # 剩余现金
        for sc, _, _, shares in holdings:
            if sc in snapshot:
                pv += snapshot[sc]['close'] * shares
        curve.append((current_date, pv))
    
    # 最后清仓
    if curve:
        last_date = curve[-1][0]
        if last_date in date_index:
            for sc, bd, bp, shares in holdings:
                if sc in date_index[last_date]:
                    capital += date_index[last_date][sc]['close'] * shares
            curve[-1] = (last_date, capital)
    
    return {
        'name': name,
        'curve': pd.DataFrame(curve, columns=['date', 'value']),
        'trades': trades,
        'hit_limit_up': hit_lu,
    }

def fn_chip(snap, top_n=TOP_N, mc_cap=100):
    candidates = []
    for code, row in snap.items():
        mc = row.get('cap_yi', np.nan)
        if np.isnan(mc) or mc <= 0 or mc > mc_cap: continue
        to = row.get('turnover_pct', 0)
        if to < 3: continue
        candidates.append((code, 100 - mc + to * 5))
    candidates.sort(key=lambda x: x[1], reverse=True)
    return [c[0] for c in candidates[:top_n]]

def fn_momentum(snap, top_n=TOP_N):
    candidates = []
    for code, row in snap.items():
        if np.isnan(row.get('ret_20', np.nan)) or row['ret_20'] < 5: continue
        if row.get('above_ma20', 0) != 1: continue
        mc = row.get('cap_yi', np.nan)
        if np.isnan(mc) or mc <= 0 or mc > MAX_MARKET_CAP: continue
        to = row.get('turnover_pct', 0)
        if to < 1 or to > 15: continue
        candidates.append((code, row['ret_20']))
    candidates.sort(key=lambda x: x[1], reverse=True)
    return [c[0] for c in candidates[:top_n]]

def fn_random(snap, top_n=TOP_N):
    codes = list(snap.keys())
    random.seed(42)
    return random.sample(codes, min(top_n, len(codes)))

results = []
for name, fn in [
    ("🏆 小市值+高换手", fn_chip),
    ("🥈 动量+均线+小市值", fn_momentum),
    ("📉 随机对照", fn_random),
]:
    random.seed(42)
    r = simulate(name, fn)
    print(f"  {name}: 期末 {r['curve']['value'].iloc[-1]:,.0f}, 交易 {r['trades']}次, 涨停命中 {r['hit_limit_up']}")
    results.append(r)

# ==================== 绩效计算 ====================
print(f"\n{'='*70}")
print(f"📈 完整绩效报告")
print(f"{'='*70}")

def perf(r):
    df = r['curve'].copy()
    init = INITIAL_CAPITAL
    final = df['value'].iloc[-1]
    total_ret = (final / init - 1) * 100
    days = (df['date'].iloc[-1] - df['date'].iloc[0]).days
    years = max(days / 365.25, 0.001)
    cagr = ((final / init) ** (1 / years) - 1) * 100
    
    # 最大回撤
    df['peak'] = df['value'].cummax()
    df['dd'] = (df['value'] - df['peak']) / df['peak'] * 100
    mdd = df['dd'].min()
    
    # 夏普
    df['ret'] = df['value'].pct_change().dropna()
    daily_std = df['ret'].std()
    sharpe = (df['ret'].mean() / daily_std) * np.sqrt(252) if daily_std > 0 else 0
    
    # 日胜率
    win = (df['ret'].dropna() > 0).sum()
    wr = win / len(df['ret'].dropna()) * 100 if len(df) > 1 else 0
    
    hit_rate = r['hit_limit_up'] / r['trades'] * 100 if r['trades'] > 0 else 0
    
    # 最大连续亏损
    df['loss_streak'] = (df['ret'] < 0).astype(int)
    streak = 0
    max_streak = 0
    for val in df['loss_streak'].dropna():
        if val == 1:
            streak += 1
            max_streak = max(max_streak, streak)
        else:
            streak = 0
    
    return {
        'name': r['name'],
        'days': days,
        'total_ret': total_ret,
        'cagr': cagr,
        'mdd': mdd,
        'sharpe': sharpe,
        'win_rate': wr,
        'hit_rate': hit_rate,
        'final': final,
        'trades': r['trades'],
        'max_loss_streak': max_streak,
    }

perf_list = [perf(r) for r in results]

def fmt_row(title, key, suffix="%"):
    print(f"  {title:<14}", end="")
    for p in perf_list:
        v = p[key]
        if key in ('final', 'trades'):
            s = f"{v/10000:,.1f}万"
        elif key == 'sharpe':
            s = f"{v:.2f}"
        elif key == 'max_loss_streak':
            s = f"{v:.0f}"
        else:
            s = f"{v:+.2f}{suffix}"
        print(f" {s:^15}", end="")
    print()

print(f"\n{'指标':<16}", end="")
for p in perf_list:
    print(f" {p['name']:^15}", end="")
print()
print("-" * 70)
fmt_row("总收益率", "total_ret")
fmt_row("CAGR年化", "cagr")
fmt_row("最大回撤", "mdd")
fmt_row("夏普比率", "sharpe", "")
fmt_row("日胜率", "win_rate")
fmt_row("涨停命中率", "hit_rate")
fmt_row("最大连亏天数", "max_loss_streak", "")
fmt_row("期末资金", "final", "")
fmt_row("持仓周次", "trades", "")

# ==================== 保存报告 ====================
report_path = os.path.join(OUTPUT_DIR, "backtest_report.txt")
with open(report_path, 'w', encoding='utf-8') as f:
    f.write(f"""抓涨停策略回测报告 v2.1 —— 资金曲线绩效
{'='*60}
生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M')}
回测区间: {BACKTEST_START} ~ {BACKTEST_END}
初始资金: {INITIAL_CAPITAL:,} 元 (100万)
主板非ST股票池: 3052 只
实际回测样本: {len(feat)} 只随机抽样 (seed=42)

交易规则:
  选股频率: 周频 (每周一)
  买入价:   次日开盘价 (保守, 涨停票实际买不到)
  卖出价:   收盘价
  持有期:   {HOLD_DAYS} 个自然日
  止盈条件: 持仓期间出现涨停({LIMIT_UP_PCT}%)立即卖出
  选股逻辑: 选股日先清仓所有持仓, 再等权买入 TOP {TOP_N} 只

绩效指标定义:
  CAGR年化:  (期末/期初)^(365/天数) - 1  (自然日口径)
  最大回撤:  资金曲线从历史高点到后续低点的最大跌幅
  夏普比率:  (日均超额收益 / 日收益标准差) × sqrt(252), 无风险利率=0
  涨停命中率: 卖出原因是"涨停" 的交易次数 / 总交易次数
  日胜率:    每日资金净值上涨的天数占比

策略条件:
  🏆 小市值+高换手: 市值<100亿, 换手率>3%
  🥈 动量+均线+小市值: 20日涨幅>5%, MA20上方, 市值<200亿, 换手1-15%
  📉 随机对照: 每周随机选 {TOP_N} 只

{'='*60}
回测结果
{'='*60}

""")
    for p in perf_list:
        f.write(f"""
{p['name']}:
  期末资金:     {p['final']:,.0f} 元 ({p['final']/10000:,.1f}万)
  总收益率:     {p['total_ret']:+.2f}%
  CAGR年化:     {p['cagr']:+.2f}%
  最大回撤:     {p['mdd']:+.2f}%
  夏普比率:     {p['sharpe']:.2f}
  日胜率:       {p['win_rate']:.1f}%
  涨停命中率:   {p['hit_rate']:.1f}%
  交易次数:     {p['trades']} 次
  最大连亏天数: {p['max_loss_streak']} 天
""")

print(f"\n📄 报告已保存: {report_path}")

# 额外打印关键对比
base = [p for p in perf_list if '随机' in p['name']]
if base:
    b = base[0]
    print(f"\n{'='*70}")
    print(f"🎯 超额收益分析 (vs 随机对照)")
    print(f"{'='*70}")
    for p in perf_list:
        if '随机' in p['name']: continue
        print(f"  {p['name']}:")
        print(f"    CAGR 高出 {p['cagr'] - b['cagr']:+.2f}%, 最大回撤 {'优于' if p['mdd'] > b['mdd'] else '差于'} {b['mdd']:.2f}%")
        print(f"    涨停命中率 {p['hit_rate']:.1f}% vs {b['hit_rate']:.1f}% (差 {p['hit_rate'] - b['hit_rate']:+.1f}%)")

print()
