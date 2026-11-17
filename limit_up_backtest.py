"""
抓涨停策略回测 v1.2 —— 用新浪数据源(stock_zh_a_daily)

数据来源: akshare.stock_zh_a_daily (新浪财经，稳定可用)
特点:
  - 沪市 sh60xxxx  深市 sz00xxxx
  - 字段: date, open, high, low, close, volume, amount, outstanding_share, turnover
  - 无东财接口被封的问题
"""

import warnings
warnings.filterwarnings('ignore')

import akshare as ak
import pandas as pd
import numpy as np
import time
import random
import os
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

# ==================== 配置 ====================
MIN_PROFIT_GROWTH = 30
MAX_PE = 30
MAX_MARKET_CAP = 200        # 亿
TOP_N = 5
HOLD_DAYS = 5
LIMIT_UP_THRESHOLD = 9.5    # %

BACKTEST_START = '20230101'
BACKTEST_END = '20261112'

OUTPUT_DIR = r"d:\py_lezrn\stock_a\888"
CACHE_DIR = os.path.join(OUTPUT_DIR, "cache")
os.makedirs(CACHE_DIR, exist_ok=True)

# ==================== 工具函数 ====================
def to_sina_code(code):
    """600519 -> sh600519, 000001 -> sz000001"""
    if code.startswith('6'):
        return f"sh{code}"
    elif code.startswith('0'):
        return f"sz{code}"
    return None

def download_daily(sina_code, force=False):
    """下载日线，带缓存和重试"""
    cache_path = os.path.join(CACHE_DIR, f"{sina_code}.csv")
    if not force and os.path.exists(cache_path):
        try:
            df = pd.read_csv(cache_path)
            df['date'] = pd.to_datetime(df['date'])
            if len(df) > 60:
                return sina_code, df
        except:
            pass
    
    for attempt in range(3):
        try:
            df = ak.stock_zh_a_daily(
                symbol=sina_code,
                start_date=BACKTEST_START,
                end_date=BACKTEST_END,
                adjust='qfq'
            )
            if df is not None and len(df) > 0:
                df.to_csv(cache_path, index=False)
                return sina_code, df
        except Exception as e:
            time.sleep(1 + attempt * 2)
    return sina_code, None

def add_features(df):
    """计算因子 - 返回带衍生列的DataFrame"""
    if df is None or len(df) < 60:
        return None
    df = df.copy().sort_values('date').reset_index(drop=True)
    
    # 计算涨跌幅
    df['close'] = pd.to_numeric(df['close'], errors='coerce')
    df['open'] = pd.to_numeric(df['open'], errors='coerce')
    df['high'] = pd.to_numeric(df['high'], errors='coerce')
    df['low'] = pd.to_numeric(df['low'], errors='coerce')
    df['turnover'] = pd.to_numeric(df['turnover'], errors='coerce')
    df['outstanding_share'] = pd.to_numeric(df['outstanding_share'], errors='coerce')
    
    df['pct'] = df['close'].pct_change() * 100
    
    # 均线
    df['ma5'] = df['close'].rolling(5).mean()
    df['ma20'] = df['close'].rolling(20).mean()
    df['ma60'] = df['close'].rolling(60).mean()
    
    # 20日涨幅
    df['ret_20'] = df['close'].pct_change(20) * 100
    df['ret_5'] = df['close'].pct_change(5) * 100
    
    # 换手率 (%)
    df['turnover_pct'] = df['turnover'] * 100
    
    # 市值 (亿) = close * outstanding_share / 1e8
    df['market_cap_yi'] = df['close'] * df['outstanding_share'] / 1e8
    
    # 涨停标记
    df['limit_up'] = (df['pct'] >= LIMIT_UP_THRESHOLD).astype(int)
    
    # 未来5天是否出现涨停
    df['future_limit_up'] = 0
    for i in range(len(df)):
        end = min(i + HOLD_DAYS + 1, len(df))
        if i + 1 < end:
            df.at[i, 'future_limit_up'] = df.iloc[i+1:end]['limit_up'].max()
    
    # 未来5天最大涨幅
    df['future_max_gain'] = np.nan
    for i in range(len(df) - HOLD_DAYS):
        future = df.iloc[i+1:i+1+HOLD_DAYS]
        if len(future) > 0 and df.at[i, 'close'] > 0:
            max_price = future['high'].max()
            df.at[i, 'future_max_gain'] = (max_price - df.at[i, 'close']) / df.at[i, 'close'] * 100
    
    # 突破MA20
    df['above_ma20'] = (df['close'] > df['ma20']).astype(int)
    
    # 量能放大 (成交量>5日均量*1.5)
    df['vol_ma5'] = df['volume'].rolling(5).mean()
    df['vol_expand'] = (df['volume'] > df['vol_ma5'] * 1.5).astype(int)
    
    return df

# ==================== 主程序 ====================
print("=" * 60)
print("📊 抓涨停策略回测 v1.2")
print("=" * 60)

# Step 1: 股票池
print("\n[1/5] 获取主板股票列表...")
all_stocks = ak.stock_info_a_code_name()
main_board = all_stocks[
    all_stocks['code'].str.startswith(('60', '00')) &
    ~all_stocks['name'].str.contains('ST|退', na=False)
].reset_index(drop=True)
main_board['sina_code'] = main_board['code'].apply(to_sina_code)
main_board = main_board.dropna(subset=['sina_code'])
print(f"  主板非ST: {len(main_board)} 只")

# Step 2: 下载日线
print(f"\n[2/5] 下载日线数据 ({BACKTEST_START} ~ {BACKTEST_END})...")
# 抽样 200 只
random.seed(42)  # 可复现
sample_idx = random.sample(range(len(main_board)), min(200, len(main_board)))
sample = main_board.iloc[sample_idx].reset_index(drop=True)
print(f"  抽样 {len(sample)} 只")

raw_data = {}
with ThreadPoolExecutor(max_workers=8) as executor:
    futures = {executor.submit(download_daily, row.sina_code): row.sina_code
               for row in sample.itertuples()}
    for i, future in enumerate(as_completed(futures), 1):
        try:
            code, df = future.result()
            if df is not None:
                raw_data[code] = df
        except:
            pass
        if i % 25 == 0 or i == len(futures):
            print(f"    进度 {i}/{len(futures)}  ({len(raw_data)} 成功)")

print(f"  ✅ 成功下载: {len(raw_data)} 只")
if len(raw_data) < 30:
    print("  ❌ 数据太少，可能被限流了，先跑单只测试...")
    exit(1)

# Step 3: 计算因子
print("\n[3/5] 计算技术面因子...")
feature_dict = {}
for code, df in raw_data.items():
    feat = add_features(df)
    if feat is not None:
        feature_dict[code] = feat
print(f"  ✅ 有效数据: {len(feature_dict)} 只")

# Step 4: 周频回测
print("\n[4/5] 执行三种策略回测...")

def run_backtest(feature_dict, strategy_name, filter_fn):
    """通用周频回测框架"""
    all_signals = []
    
    for code, df in feature_dict.items():
        # 找出每周第一个交易日
        df = df.copy()
        df['week_idx'] = df['date'].dt.strftime('%Y-%W')
        weekly = df.drop_duplicates('week_idx', keep='first').reset_index(drop=True)
        
        for row in weekly.itertuples():
            # 跳过最后10周（没有未来数据）
            pass_idx = weekly[weekly['week_idx'] == row.week_idx].index[0]
            if pass_idx >= len(weekly) - 8:
                continue
            
            try:
                signal = filter_fn(row)
                if signal:
                    all_signals.append({
                        'code': code,
                        'date': row.date,
                        'week': row.week_idx,
                        'hit': row.future_limit_up if not np.isnan(row.future_limit_up) else 0,
                        'max_gain': row.future_max_gain if not np.isnan(row.future_max_gain) else 0,
                        'market_cap': row.market_cap_yi if not np.isnan(row.market_cap_yi) else 9999,
                        'ret_20': row.ret_20 if not np.isnan(row.ret_20) else -999,
                        'turnover': row.turnover_pct if not np.isnan(row.turnover_pct) else 0,
                    })
            except:
                pass
    
    return pd.DataFrame(all_signals)

# 策略A: 动量+均线+小市值
def filter_strategy_a(row):
    return (
        not np.isnan(row.ret_20) and row.ret_20 > 5 and
        row.above_ma20 == 1 and
        not np.isnan(row.market_cap_yi) and row.market_cap_yi < MAX_MARKET_CAP and
        not np.isnan(row.turnover_pct) and row.turnover_pct > 1 and row.turnover_pct < 15
    )

# 策略B: 纯小市值+活跃度
def filter_strategy_b(row):
    return (
        not np.isnan(row.market_cap_yi) and row.market_cap_yi < 100 and
        row.turnover_pct > 3
    )

# 策略C: 随机对照组（不做过滤，后随机抽）
def filter_strategy_c(row):
    return True

result_a = run_backtest(feature_dict, "动量+均线+小市值", filter_strategy_a)
result_b = run_backtest(feature_dict, "小市值+高换手", filter_strategy_b)
result_c_all = run_backtest(feature_dict, "随机对照", filter_strategy_c)
# 对照组: 每周随机抽 TOP_N 只
result_c = result_c_all.groupby('week').apply(lambda x: x.sample(min(TOP_N, len(x)), random_state=42)).reset_index(drop=True) if len(result_c_all) > 0 else pd.DataFrame()

# Step 5: 统计
print("\n[5/5] 统计分析...")

def summarize(df, name):
    if len(df) == 0:
        return None
    hit = int(df['hit'].sum())
    total = len(df)
    return {
        'name': name,
        'signals': total,
        'hit': hit,
        'hit_rate': hit / total * 100,
        'avg_max_gain': df['max_gain'].mean(),
        'median_cap': df['market_cap'].median() if 'market_cap' in df.columns else 0,
    }

results = []
for name, df in [("动量+均线+小市值", result_a), ("小市值+高换手", result_b), ("随机对照组", result_c)]:
    s = summarize(df, name)
    if s:
        results.append(s)

# ============ 输出 ============
print("\n" + "=" * 70)
print("📈 回测结果")
print("=" * 70)

if not results:
    print("❌ 没有产生任何交易信号，请检查策略条件是否过严")
else:
    print(f"\n{'策略':<20} {'信号':>6} {'涨停命中':>8} {'命中率':>10} {'平均最大收益':>12} {'中位市值':>10}")
    print("-" * 70)
    for r in results:
        print(f"{r['name']:<20} {r['signals']:>6} {r['hit']:>8} {r['hit_rate']:>9.2f}% {r['avg_max_gain']:>11.2f}% {r['median_cap']:>9.1f}亿")

    # 超额收益分析
    base = [r for r in results if '随机' in r['name']]
    if base and len(results) > 1:
        b = base[0]
        print(f"\n📊 超额收益 (vs 随机):")
        for r in results:
            if '随机' not in r['name']:
                print(f"  {r['name']}: 命中率 +{r['hit_rate'] - b['hit_rate']:.2f}%, 收益差 {r['avg_max_gain'] - b['avg_max_gain']:.2f}%")

print(f"\n📅 回测区间: {BACKTEST_START} ~ {BACKTEST_END}")
print(f"📊 股票池: {len(main_board)} 只主板, 抽样 {len(feature_dict)} 只")

# ============ 保存报告 ============
report_path = os.path.join(OUTPUT_DIR, "backtest_report.txt")
with open(report_path, 'w', encoding='utf-8') as f:
    f.write(f"""抓涨停策略回测报告 v1.2
{'='*60}
生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M')}
回测区间: {BACKTEST_START} ~ {BACKTEST_END}
主板非ST股票池: {len(main_board)} 只
实际回测样本: {len(feature_dict)} 只随机抽样 (seed=42)

策略条件:
  动量+均线+小市值: 20日涨幅>5%, 价格>MA20, 市值<{MAX_MARKET_CAP}亿, 换手率1-15%
  小市值+高换手: 市值<100亿, 换手率>3%
  随机对照: 每周随机选 {TOP_N} 只
  持有期: {HOLD_DAYS}天
  涨停阈值: {LIMIT_UP_THRESHOLD}%
  
结果:
  {'策略':<22} {'信号':>6} {'命中':>6} {'命中率':>8} {'平均最大收益':>10} {'中位市值':>10}
  {'-'*56}
""")
    for r in results:
        f.write(f"  {r['name']:<22} {r['signals']:>6} {r['hit']:>6} {r['hit_rate']:>7.2f}% {r['avg_max_gain']:>9.2f}% {r['median_cap']:>9.1f}亿\n")

print(f"\n📄 报告: {report_path}")

# ============ 今日选股 ============
print("\n" + "=" * 70)
print("🎯 今日主板抓涨停潜力Top 10")
print("=" * 70)

today_candidates = []
for sina_code, df in feature_dict.items():
    if df is None or len(df) < 5:
        continue
    last = df.iloc[-1]
    if np.isnan(last.get('ret_20', np.nan)):
        continue
    
    # 解析原始代码
    code = sina_code[2:]  # sh600519 -> 600519
    stock_info = main_board[main_board['code'] == code]
    name = stock_info['name'].iloc[0] if len(stock_info) > 0 else '?'
    
    today_candidates.append({
        'code': code,
        'name': name,
        'close': last['close'],
        'ret_20': last['ret_20'],
        'ma_trend': last.get('above_ma20', 0),
        'turnover%': last.get('turnover_pct', 0),
        '市值(亿)': last.get('market_cap_yi', 0),
        'score': last.get('ret_20', 0) + last.get('above_ma20', 0) * 10,
    })

if today_candidates:
    df_today = pd.DataFrame(today_candidates)
    # 过滤一下
    df_today = df_today[
        (df_today['ret_20'] > 0) &
        (df_today['ma_trend'] == 1) &
        (df_today['市值(亿)'] < MAX_MARKET_CAP)
    ].sort_values('score', ascending=False)
    print(df_today.head(10).to_string(index=False))
else:
    print("  暂无符合条件的标的")
print()
