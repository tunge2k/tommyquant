"""
托米量化 —— A股多策略量化平台 Streamlit 应用
启动: streamlit run streamlit_app.py
"""
import streamlit as st
import pandas as pd
import numpy as np
import plotly.graph_objects as go
from plotly.subplots import make_subplots
from datetime import datetime, timedelta
import os
import json
import sys

# DoH 兜底: NAS 路由器可能劫持 UDP 53, 系统 DNS 失败时走阿里 DoH
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    import doh_patch
    doh_patch.install()
except Exception:
    pass

# 导入本地回测引擎
from backtest_engine import (
    STRATEGIES, run_backtest, get_today_picks,
    bt_cache_key, serialize_result, deserialize_result,
    MarketCapStrategy,
)
import data_store
import news_fetcher

try:
    import fast_backtest as fastbt
except Exception:      # 向量化内核缺失时自动退回旧引擎
    fastbt = None


def run_backtest_auto(feat, select_fn, strategy_params=None, **kwargs):
    """向量化引擎优先 (约 70x), 不支持时回退到事件驱动引擎"""
    if fastbt is not None and fastbt.supports_fast(select_fn):
        try:
            return fastbt.run_fast(feat, select_fn, strategy_params, **kwargs)
        except Exception:
            pass
    return run_backtest(feat, select_fn, strategy_params, **kwargs)


data_store.init_db()   # 幂等建表, 防止首次启动时表不存在

# 首页「策略绩效概览」并排对比的策略
DASH_STRATEGIES = ["小市值轮动·戴维斯双击", "动量+均线+小市值"]

# 参数寻优: 每个策略可扫描的数值参数
SWEEP_TUNABLES = {
    "小市值轮动·戴维斯双击": {
        "cap_max": "市值上限(亿)", "ret_20_min": "20日涨幅下限(%)",
        "ret_60_min": "60日涨幅下限(%)", "turn_min": "换手率下限(%)",
        "turn_max": "换手率上限(%)", "volume_ratio_min": "量比下限",
    },
    "动量+均线+小市值": {
        "ret_20_min": "20日涨幅下限(%)", "cap_max": "市值上限(亿)",
        "turn_min": "换手率下限(%)", "turn_max": "换手率上限(%)",
    },
}


def _parse_series(txt: str) -> list:
    """'3,5,8' / '3 5 8' -> [3, 5, 8]"""
    out = []
    for tok in str(txt).replace(",", " ").replace("，", " ").split():
        try:
            v = float(tok)
        except ValueError:
            continue
        out.append(int(v) if v.is_integer() else v)
    return out

CLOSE_TIME_DISPLAY = f"{os.environ.get('CLOSE_HOUR', '15')}:{os.environ.get('CLOSE_MINUTE', '35')}"

# 成交流水「原因」的中文展示 (引擎内部代号 → 用户可读说明)
FILL_REASON_LABELS = {
    "mc_new_entry": "市值建仓",
    "mc_rebalance": "市值调仓",
    "rebalance": "调仓换股",
    "ma30_exit": "MA30走弱清仓",
    "rank_drop": "排名跌出换股",
    "hold_expire": "持有到期",
    "limit_up": "涨停卖出",
}


def _parse_codes(txt: str) -> list:
    """'159509, 600719' -> ['sz159509', 'sh600719'] (6位数字自动补交易所前缀)"""
    out = []
    for tok in str(txt).replace(",", " ").replace("，", " ").split():
        c = tok.strip().lower()
        if c[:2] in ("sh", "sz") and len(c) >= 8:
            out.append(c)
            continue
        if len(c) == 6 and c.isdigit():
            out.append(("sh" if c[0] in "569" else "sz") + c)
    return out


# ==================== 国际化 (i18n) ====================
# 界面语言在「参数设置」里切换, 全站只显示一种语言
# 参考「量化大师」的导航结构: 一级菜单 + 子菜单
# 扁平导航: 所有功能直接平铺在侧边栏, 用空行 / st.divider 分组
# 元组: (page_key, zh_label, en_label, group_after)
#   group_after: 若 True, 该项下方会多一条分隔线 (视觉分组)
FLAT_NAV = [
    ("home",       "🏠 量化大师",   "🏠 Home",        False),
    ("backtest",   "🔬 策略回测",   "🔬 Backtest",    False),
    ("picks",      "🎯 今日选股",   "🎯 Daily Picks", False),
    ("portfolio",  "💼 模拟交易",   "💼 Sim Trade",   True),   # 空一行, 策略区结束
    ("news",       "📰 财经资讯",   "📰 News",        True),   # 空一行
    ("data",       "💾 数据管理",   "💾 Data Center", False),
    ("settings",   "⚙️ 参数设置",   "⚙️ Settings",    False),
    ("help",       "❓ 帮助",       "❓ Help",        False),
]
_PAGE_LABEL = {}
for k, zh, en, _ in FLAT_NAV:
    _PAGE_LABEL[k] = (zh, en)

# 保留 MAIN_PAGES / STRATEGY_SUBPAGES / MORE_SUBPAGES 仅为文档 / 回退用途
MAIN_PAGES = [("home", "🏠 量化大师", "🏠 Home")]
STRATEGY_SUBPAGES = [
    ("backtest", "🔬 策略回测", "🔬 Backtest"),
    ("picks", "🎯 今日选股", "🎯 Daily Picks"),
    ("portfolio", "💼 模拟交易", "💼 Sim Trade"),
]
MORE_SUBPAGES = [
    ("settings", "⚙️ 参数设置", "⚙️ Settings"),
    ("data", "💾 数据管理", "💾 Data Center"),
    ("help", "❓ 帮助", "❓ Help"),
]

I18N = {
    "brand": ("📈 托米量化", "📈 TommyQuant"),
    "brand_sub": ("A股多策略量化平台", "A-Share Quant Platform"),
    "nav": ("导航", "Navigation"),
    "data_status": ("📈 数据状态", "📈 Data Status"),
    "symbols": ("库内股票", "Symbols"),
    "bars": ("K线", "Bars"),
    "range": ("区间", "Range"),
    "cur_params": ("📐 当前参数", "📐 Current Parameters"),
    "initial_capital": ("初始资金", "Capital"),
    "hold_days": ("持有天数", "Hold Days"),
    "top_n": ("选股数量", "Positions"),
    "limit_up_pct": ("涨停阈值", "Limit-up"),
    # 首页 (量化大师)
    "home_title": ("量化大师", "Quant Master"),
    "home_sub": ("策略研究 · 模拟交易 · 全球市场", "Strategy Research · Paper Trading · Global Markets"),
    "quick_actions": ("快捷入口", "Quick Actions"),
    "global_markets": ("🌍 全球主要市场", "🌍 Global Markets"),
    "news_summary": ("📰 资讯摘要", "📰 News Summary"),
    "strat_perf": ("💎 策略绩效概览", "💎 Strategy Performance"),
    "dash_range": ("回测区间", "Backtest range"),
    "dash_sample": ("样本", "Universe"),
    "dash_sample_unit": ("只主板非ST股", "main-board stocks"),
    "perf_compare": ("核心绩效对比", "Performance Comparison"),
    "total_return": ("总收益", "Total Return"),
    "max_drawdown": ("最大回撤", "Max Drawdown"),
    "sharpe": ("夏普比率", "Sharpe"),
    "final_value": ("期末资金", "Final Equity"),
    "hit_rate": ("涨停命中", "Limit-up Hit"),
    "equity_curve": ("📈 资金曲线对比", "📈 Equity Curves"),
    "equity_axis": ("净值 (基准=100)", "Net Value (base=100)"),
    "drawdown_curve": ("📉 回撤对比", "📉 Drawdown"),
    "drawdown_axis": ("回撤 (%)", "Drawdown (%)"),
    "monthly_ret": ("🗓️ 动量策略月度收益分布", "🗓️ Monthly Returns (Momentum)"),
    "running_bt": ("🔄 运行回测...", "🔄 Running backtest..."),
    "news_flash": ("📰 市场快讯", "📰 Market News"),
    "news_flash_sub": ("多源实时滚动 · 最早来源优先保留 · 点击下方按钮查看详情",
                       "Multi-source live feed · earliest source kept · click for details"),
    "news_list": ("🔍 最新资讯列表", "🔍 Latest News"),
    # 子菜单分组
    "group_strategy": ("量化策略", "Strategies"),
    "group_more": ("更多", "More"),
    # 其他页
    "news_title": ("📰 资讯中心", "📰 News Center"),
    "news_sub": ("多源聚合 · 最早来源保留 · 点击查看完整内容",
                 "Multi-source aggregation · earliest source kept · click to read"),
    "bt_title": ("🔬 策略回测", "🔬 Strategy Backtest"),
    "bt_sub": ("选择策略与参数 · 完整资金曲线模拟 · 交易明细 · 交易日历",
               "Pick strategy & params · full equity simulation · trades · calendar"),
    "picks_title": ("🎯 今日涨停潜力股", "🎯 Today's Picks"),
    "picks_sub": ("基于最新交易日因子 · 涨停潜力 Top 15 · 选股记录自动入库跟踪",
                  "Based on latest factors · Top 15 · picks auto-recorded"),
    "settings_title": ("⚙️ 参数设置", "⚙️ Settings"),
    "settings_sub": ("所有参数实时生效 · 切换页面后自动应用",
                     "All parameters apply immediately"),
    "lang_label": ("界面语言", "Interface Language"),
    "lang_help": ("仅显示所选语言", "Only the selected language is shown"),
    "data_title": ("💾 数据管理", "💾 Data Center"),
    "data_sub": ("后台自动补全 · 策略按需触发 · 日线/财务/实时三层数据",
                 "Auto backfill · strategy-driven · daily/fundamental/realtime layers"),
    "help_title": ("❓ 帮助中心", "❓ Help Center"),
    "help_sub": ("快速上手 · 功能说明 · 策略原理 · 常见问题",
                 "Quick start · features · strategy logic · FAQ"),
    # 设置页
    "capital_pos": ("💰 资金与持仓", "💰 Capital & Holding"),
    "pick_threshold": ("📈 选股与阈值", "📈 Picks & Threshold"),
    "bt_range": ("🗓️ 回测区间", "🗓️ Backtest Range"),
    "pick_range": ("选择回测日期范围", "Select backtest date range"),
    "param_preview": ("🔍 当前参数预览", "🔍 Current Parameters"),
    "restore_default": ("↩️ 恢复默认参数", "↩️ Restore Defaults"),
    "wan": ("万", "×10k"),
    "tian": ("天", "d"),
    "zhi": ("只", ""),
}


def lang() -> str:
    return st.session_state.get("lang", "zh")


def t(key: str, **kw) -> str:
    pair = I18N.get(key)
    if pair is None:
        return key
    s = pair[1] if lang() == "en" else pair[0]
    return s.format(**kw) if kw else s


def page_label(key: str) -> str:
    zh, en = _PAGE_LABEL.get(key, (key, key))
    return en if lang() == "en" else zh


def hero(title_key: str, sub_key: str):
    st.markdown(
        f'<div class="hero-banner"><h1>{t(title_key)}</h1><p>{t(sub_key)}</p></div>',
        unsafe_allow_html=True)


def fmt_money(v: float) -> str:
    """金额展示: 中文用「万」, 英文用千分位"""
    return f"{v/10000:.0f}万" if lang() == "zh" else f"{v:,.0f}"

# ==================== 页面配置 ====================
st.set_page_config(
    page_title="托米量化 · TommyQuant",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="expanded",
)

# 自定义 CSS —— 金融量化专业版
# 设计系统: 8px 栅格 + WCAG AA 对比度 + 金融语义化颜色 + 专业字体栈
st.markdown("""
<style>
    /* ============ 1. 基础字体与渲染 ============ */
    html, body, [class*="css"] {
        font-family: 'Inter', -apple-system, BlinkMacSystemFont,
                     'PingFang SC', 'Hiragino Sans GB', 'Microsoft YaHei',
                     'Noto Sans SC', 'Helvetica Neue', Arial, sans-serif;
        -webkit-font-smoothing: antialiased;
        -moz-osx-font-smoothing: grayscale;
        font-feature-settings: "cv02", "cv03", "cv04", "cv11";
        letter-spacing: 0;
    }

    /* ============ 2. 主题变量 (设计 Token) ============ */
    :root {
        /* 主色 —— 翡翠绿系 */
        --primary-50: #e6f7f2;
        --primary-100: #ccefe5;
        --primary-200: #99dfca;
        --primary-300: #66cfaf;
        --primary-400: #33bf94;
        --primary-500: #1FA879;
        --primary-600: #188661;
        --primary-700: #146549;
        /* 涨跌颜色 (A股红涨绿跌) */
        --up-color: #dc2626;
        --up-bg: #fef2f2;
        --up-border: #fecaca;
        --down-color: #16a34a;
        --down-bg: #f0fdf4;
        --down-border: #bbf7d0;
        /* 中性色阶 (满足 WCAG AA) */
        --text-primary: #1a1f2e;      /* 对比度 15:1 @ #fff */
        --text-secondary: #475569;    /* 对比度 7.3:1 @ #fff, WCAG AA+ */
        --text-tertiary: #64748b;     /* 对比度 4.6:1 @ #fff, WCAG AA */
        --text-quaternary: #94a3b8;   /* 仅用于非关键辅助信息 */
        /* 背景与边框 */
        --bg-page: #f7f8fa;
        --bg-card: #ffffff;
        --bg-elevated: #f1f5f9;
        --border-subtle: #e8ecf1;
        --border-default: #d9dee6;
        /* 阴影 (从轻到重) */
        --shadow-xs: 0 1px 2px rgba(17,24,39,0.04);
        --shadow-sm: 0 1px 3px rgba(17,24,39,0.06), 0 1px 2px rgba(17,24,39,0.04);
        --shadow-md: 0 4px 12px rgba(17,24,39,0.08), 0 2px 4px rgba(17,24,39,0.04);
        --shadow-lg: 0 10px 28px rgba(17,24,39,0.10), 0 4px 10px rgba(17,24,39,0.04);
        /* 圆角 (8px 栅格) */
        --radius-sm: 6px;
        --radius-md: 8px;
        --radius-lg: 12px;
        --radius-xl: 16px;
        --radius-2xl: 20px;
        --radius-full: 999px;
    }

    /* ============ 3. 页面背景 ============ */
    .stApp {
        background: var(--bg-page);
    }

    /* ============ 4. 标题层级 ============ */
    h1 { font-size: 1.5rem !important; font-weight: 700; color: var(--text-primary) !important;
         letter-spacing: -0.01em; line-height: 1.3; }
    h2 { font-size: 1.2rem !important; font-weight: 600; color: var(--text-primary) !important;
         margin-top: 1.5rem !important; letter-spacing: -0.005em; line-height: 1.35; }
    h3 { font-size: 1.0625rem !important; font-weight: 600; color: var(--text-secondary) !important;
         margin-top: 1.25rem !important; line-height: 1.4; }
    h4 { font-size: 0.9375rem !important; font-weight: 600; color: var(--text-secondary) !important; }
    p, span, li, label { color: var(--text-secondary); }

    /* ============ 5. 侧边栏 ============ */
    section[data-testid="stSidebar"] {
        background: #ffffff;
        border-right: 1px solid var(--border-subtle);
    }
    /* Streamlit 默认 360px 太宽, 我们收窄一点给主内容更多空间 */
    section[data-testid="stSidebar"] > div:first-child {
        padding-top: 0.75rem !important;
        padding-left: 0.75rem !important;
        padding-right: 0.75rem !important;
    }
    section[data-testid="stSidebar"] label { color: var(--text-secondary) !important; }

    /* 侧边栏导航项 —— 扁平一层, 所有功能平铺 */
    section[data-testid="stSidebar"] [data-testid="stRadioGroup"] > div {
        border-radius: 6px;
        margin: 1px 0;
        transition: background-color 0.12s ease, color 0.12s ease;
    }
    section[data-testid="stSidebar"] [data-testid="stRadioGroup"] > div:hover {
        background: var(--bg-elevated);
    }
    section[data-testid="stSidebar"] [data-testid="stRadioGroup"] > div[data-selected="true"] {
        background: var(--primary-50);
    }
    section[data-testid="stSidebar"] [data-testid="stRadioOption"] {
        /* 加大左内边距让短词 ("📰 资讯") 和长词视觉上左边对齐 */
        padding: 0.4rem 0.875rem 0.4rem 1rem !important;
    }
    section[data-testid="stSidebar"] [data-testid="stRadioOption"] > div > div:first-child {
        display: none !important;        /* 隐藏 radio 圆点 */
    }
    section[data-testid="stSidebar"] [data-testid="stRadioGroup"] > div[data-selected="true"] p {
        color: var(--primary-600) !important;
        font-weight: 600 !important;
    }
    section[data-testid="stSidebar"] [data-testid="stRadioGroup"] p {
        font-size: 0.875rem !important;
        color: var(--text-secondary);
        margin: 0 !important;
    }
    /* 分组分隔线: FLAT_NAV 里 portfolio 之后 (第 4 个 = nth-child(4))、
       news 之后 (第 5 个 = nth-child(5)) 加一条灰色 1px 分隔线 */
    section[data-testid="stSidebar"] [data-testid="stRadioGroup"] > div:nth-child(4) {
        margin-bottom: 0.5rem;
    }
    section[data-testid="stSidebar"] [data-testid="stRadioGroup"] > div:nth-child(4)::after,
    section[data-testid="stSidebar"] [data-testid="stRadioGroup"] > div:nth-child(5)::after {
        content: "";
        display: block;
        height: 1px;
        background: var(--border-subtle);
        margin-top: 0.5rem;
        margin-left: 0.25rem;
    }

    /* 侧边栏收起控件 */
    [data-testid="stSidebarCollapsedControl"],
    [data-testid="collapsedControl"] {
        background: transparent !important;
        border: none !important;
        box-shadow: none !important;
        top: 0.5rem !important;
        left: 0.5rem !important;
        z-index: 99999 !important;
    }
    [data-testid="stSidebarCollapsedControl"] button,
    [data-testid="collapsedControl"] button {
        background: transparent !important;
        border: none !important;
        width: 32px !important;
        height: 32px !important;
        padding: 0 !important;
        color: var(--text-quaternary) !important;
        display: flex !important;
        align-items: center;
        justify-content: center;
        border-radius: var(--radius-sm) !important;
        transition: all 0.15s ease;
    }
    [data-testid="stSidebarCollapsedControl"] button:hover,
    [data-testid="collapsedControl"] button:hover {
        color: var(--primary-500) !important;
        background: var(--bg-elevated) !important;
    }
    [data-testid="stSidebarCollapsedControl"] svg,
    [data-testid="collapsedControl"] svg { width: 18px !important; height: 18px !important; }

    /* 侧边栏内部收起按钮 */
    section[data-testid="stSidebar"] [data-testid="stSidebarCollapseButton"] {
        background: transparent !important;
        border: none !important;
        box-shadow: none !important;
    }
    section[data-testid="stSidebar"] [data-testid="stSidebarCollapseButton"] button {
        color: var(--text-quaternary) !important;
        min-height: 32px !important;
        min-width: 32px !important;
        padding: 0 !important;
        border-radius: var(--radius-sm) !important;
        transition: all 0.15s ease;
    }
    section[data-testid="stSidebar"] [data-testid="stSidebarCollapseButton"] button:hover {
        color: var(--primary-500) !important;
        background: var(--bg-elevated) !important;
    }

    /* 品牌按钮 */
    section[data-testid="stSidebar"] button[kind="secondary"]:first-of-type {
        background: transparent !important;
        border: none !important;
        box-shadow: none !important;
        font-size: 1.25rem !important;
        font-weight: 800 !important;
        color: var(--text-primary) !important;
        letter-spacing: -0.01em !important;
        padding: 0.375rem 0.5rem !important;
        text-align: left !important;
        border-radius: var(--radius-md) !important;
        transition: background-color 0.15s ease !important;
    }
    section[data-testid="stSidebar"] button[kind="secondary"]:first-of-type:hover {
        background: var(--bg-elevated) !important;
    }

    /* ============ 6. Metric 卡片 ============ */
    div[data-testid="stMetric"] {
        background: var(--bg-card);
        border-radius: var(--radius-lg);
        padding: 0.875rem 1rem;
        border: 1px solid var(--border-subtle);
        box-shadow: var(--shadow-xs);
        transition: box-shadow 0.2s ease, border-color 0.2s ease;
    }
    div[data-testid="stMetric"]:hover {
        box-shadow: var(--shadow-sm);
        border-color: var(--border-default);
    }
    div[data-testid="stMetric"] label {
        color: var(--text-tertiary) !important;
        font-weight: 500;
        font-size: 0.75rem !important;
        text-transform: none;
        letter-spacing: 0;
    }
    div[data-testid="stMetric"] [data-testid="stMetricValue"] {
        color: var(--text-primary) !important;
        font-weight: 700;
        font-variant-numeric: tabular-nums;
    }
    div[data-testid="stMetric"] [data-testid="stMetricDelta"] {
        font-weight: 600;
    }

    /* ============ 7. 表格优化 ============ */
    [data-testid="stDataFrame"] {
        border: 1px solid var(--border-subtle);
        border-radius: var(--radius-lg);
        overflow: hidden;
        background: var(--bg-card);
        box-shadow: var(--shadow-xs);
    }
    /* Streamlit Dataframe 内部表格样式覆盖 */
    [data-testid="stDataFrame"] table {
        border-collapse: collapse !important;
    }
    [data-testid="stDataFrame"] th {
        background: var(--bg-elevated) !important;
        color: var(--text-secondary) !important;
        font-weight: 600 !important;
        font-size: 0.75rem !important;
        padding: 0.625rem 0.75rem !important;
        border-bottom: 1px solid var(--border-subtle) !important;
        white-space: nowrap;
    }
    [data-testid="stDataFrame"] td {
        font-size: 0.8125rem !important;
        padding: 0.5rem 0.75rem !important;
        border-bottom: 1px solid var(--border-subtle) !important;
        font-variant-numeric: tabular-nums;
    }
    [data-testid="stDataFrame"] tbody tr:hover {
        background: var(--bg-elevated) !important;
    }
    /* 斑马纹 */
    [data-testid="stDataFrame"] tbody tr:nth-child(even) {
        background: rgba(248, 250, 252, 0.5);
    }

    /* ============ 8. 按钮系统 ============ */
    .stButton > button {
        background: var(--bg-card);
        border: 1px solid var(--border-default);
        border-radius: var(--radius-md);
        padding: 0.5rem 1.125rem;
        font-weight: 600;
        font-size: 0.8125rem;
        color: var(--text-secondary);
        box-shadow: var(--shadow-xs);
        transition: all 0.18s ease;
        font-family: inherit;
    }
    .stButton > button:hover:not(:disabled) {
        background: var(--bg-elevated);
        border-color: var(--primary-300);
        color: var(--primary-600);
        box-shadow: var(--shadow-sm);
        transform: translateY(-1px);
    }
    .stButton > button:active:not(:disabled) {
        transform: translateY(0);
        box-shadow: var(--shadow-xs);
    }
    .stButton > button[kind="primary"] {
        background: linear-gradient(135deg, var(--primary-400), var(--primary-500)) !important;
        border: none !important;
        color: #ffffff !important;
        box-shadow: 0 2px 6px rgba(31,168,121,0.3);
    }
    .stButton > button[kind="primary"]:hover:not(:disabled) {
        background: linear-gradient(135deg, var(--primary-500), var(--primary-600)) !important;
        box-shadow: 0 4px 12px rgba(31,168,121,0.4);
        color: #ffffff !important;
    }
    .stButton > button[kind="secondary"] {
        background: var(--bg-card);
        border: 1px solid var(--border-default);
        border-radius: var(--radius-md);
        color: var(--text-secondary);
        box-shadow: var(--shadow-xs);
    }
    .stButton > button[kind="secondary"]:hover:not(:disabled) {
        background: var(--bg-elevated);
        border-color: var(--primary-300);
        color: var(--primary-600);
    }
    .stDownloadButton > button {
        background: var(--bg-card);
        border: 1px solid var(--border-default);
        border-radius: var(--radius-md);
        color: var(--text-secondary);
        font-weight: 600;
        font-size: 0.8125rem;
        box-shadow: var(--shadow-xs);
    }

    /* ============ 9. 顶部快捷入口 ============ */
    .stHorizontalBlock .stButton > button,
    div[data-testid="stHorizontalBlock"] .st-key-tn_ .stButton > button {
        border-radius: var(--radius-md) !important;
        padding: 0.5rem 0.875rem !important;
        font-size: 0.8125rem !important;
        font-weight: 500 !important;
        background: var(--bg-card) !important;
        border: 1px solid var(--border-subtle) !important;
        color: var(--text-secondary) !important;
        box-shadow: var(--shadow-xs) !important;
        transition: all 0.2s ease !important;
    }
    .stHorizontalBlock .stButton > button:hover:not([disabled]),
    div[data-testid="stHorizontalBlock"] .st-key-tn_ .stButton > button:hover:not(:disabled) {
        border-color: var(--primary-300) !important;
        color: var(--primary-600) !important;
        background: var(--primary-50) !important;
        box-shadow: var(--shadow-sm) !important;
        transform: translateY(-1px) !important;
    }
    /* 激活态 */
    .stHorizontalBlock .stButton > button[disabled],
    .stHorizontalBlock .stButton > button:disabled,
    div[data-testid="stHorizontalBlock"] .st-key-tn_ .stButton > button:disabled {
        background: linear-gradient(135deg, var(--primary-400), var(--primary-500)) !important;
        border: none !important;
        color: #ffffff !important;
        font-weight: 600 !important;
        box-shadow: 0 4px 14px rgba(31,168,121,0.35) !important;
        opacity: 1 !important;
        cursor: default !important;
        transform: none !important;
    }

    /* ============ 10. 输入控件 ============ */
    .stNumberInput input,
    .stTextInput input,
    .stDateInput input,
    [data-baseweb="select"] > div,
    [data-baseweb="input"] > div {
        background: var(--bg-elevated) !important;
        border: 1px solid transparent !important;
        border-radius: var(--radius-md) !important;
        color: var(--text-primary) !important;
        min-height: 40px !important;
        font-family: inherit !important;
        font-size: 0.875rem !important;
        transition: border-color 0.15s ease, box-shadow 0.15s ease;
    }
    .stNumberInput input:focus,
    .stTextInput input:focus {
        border-color: var(--primary-400) !important;
        box-shadow: 0 0 0 3px rgba(31,168,121,0.12) !important;
        background: var(--bg-card) !important;
    }
    .stNumberInput label,
    .stTextInput label,
    .stDateInput label,
    .stSelectbox label,
    .stMultiSelect label,
    .stSlider label {
        color: var(--text-secondary) !important;
        font-weight: 600 !important;
        font-size: 0.8125rem !important;
    }

    /* Slider 轨道 */
    [data-testid="stSlider"] > div > div > div > div {
        background: var(--primary-500) !important;
    }

    /* ============ 11. 滚动条 ============ */
    ::-webkit-scrollbar { width: 8px; height: 8px; }
    ::-webkit-scrollbar-track { background: transparent; }
    ::-webkit-scrollbar-thumb {
        background: #cbd5e1;
        border-radius: var(--radius-full);
        border: 2px solid transparent;
        background-clip: content-box;
    }
    ::-webkit-scrollbar-thumb:hover { background: #94a3b8; background-clip: content-box; }

    /* ============ 12. 提示框 ============ */
    div.stSuccess {
        background: var(--down-bg);
        border: 1px solid var(--down-border);
        border-radius: var(--radius-lg);
        color: #14532d;
    }
    div.stError {
        background: var(--up-bg);
        border: 1px solid var(--up-border);
        border-radius: var(--radius-lg);
        color: #991b1b;
    }
    div.stInfo {
        background: #eff6ff;
        border: 1px solid #bfdbfe;
        border-radius: var(--radius-lg);
        color: #1e40af;
    }
    div.stWarning {
        background: #fffbeb;
        border: 1px solid #fde68a;
        border-radius: var(--radius-lg);
        color: #92400e;
    }

    /* ============ 13. Streamlit 原生元素隐藏 ============ */
    #MainMenu { visibility: hidden; }
    footer { visibility: hidden; }
    header [data-testid="stDecoration"] { display: none; }

    /* ============ 14. 顶部 Header 压缩 ============ */
    header[data-testid="stHeader"] {
        background: transparent !important;
        height: 2.5rem !important;
        min-height: 2.5rem !important;
    }
    [data-testid="stAppViewContainer"] .block-container {
        padding-top: 1rem !important;
        padding-bottom: 1.5rem !important;
        max-width: 1400px !important;
    }

    /* ============ 15. Hero 横幅 ============ */
    .hero-banner {
        background: var(--bg-card);
        border: 1px solid var(--border-subtle);
        border-radius: var(--radius-lg);
        padding: 0.875rem 1rem;
        margin-bottom: 0.75rem;
        box-shadow: var(--shadow-xs);
    }
    .hero-banner h1 { margin: 0 0 0.125rem 0; font-size: 1.125rem !important; }
    .hero-banner p { margin: 0; color: var(--text-tertiary); font-size: 0.8125rem; }

    /* ============ 16. 新闻卡片 (class 化, 替换内联 style) ============ */
    .news-card {
        background: var(--bg-card);
        border: 1px solid var(--border-subtle);
        border-radius: var(--radius-md);
        padding: 0.875rem 1rem;
        box-shadow: var(--shadow-xs);
        height: 100%;
        display: flex;
        flex-direction: column;
        gap: 0.5rem;
    }
    .news-card-head {
        display: flex;
        align-items: center;
        gap: 0.625rem;
    }
    .news-card-icon {
        width: 32px; height: 32px;
        border-radius: 8px;
        display: flex; align-items: center; justify-content: center;
        font-size: 1rem; flex-shrink: 0;
    }
    .news-card-meta { line-height: 1.3; }
    .news-card-meta .src {
        font-size: 0.75rem; font-weight: 600;
    }
    .news-card-meta .time {
        font-size: 0.6875rem; color: var(--text-quaternary);
    }
    .news-card-title {
        font-size: 0.9375rem; font-weight: 600;
        color: var(--text-primary); line-height: 1.45;
        display: -webkit-box;
        -webkit-line-clamp: 2; -webkit-box-orient: vertical;
        overflow: hidden; text-decoration: none;
    }
    .news-card-content {
        font-size: 0.8125rem; color: var(--text-tertiary); line-height: 1.6;
        flex: 1;
        display: -webkit-box;
        -webkit-line-clamp: 3; -webkit-box-orient: vertical;
        overflow: hidden;
    }
    .sym-tag {
        background: #eef2ff; color: #4f46e5;
        padding: 1px 7px; border-radius: 6px;
        font-size: 0.6875rem; margin-left: 4px;
    }

    /* ============ 17. 帮助页卡片 ============ */
    .help-card {
        background: var(--bg-card);
        border: 1px solid var(--border-subtle);
        border-radius: var(--radius-lg);
        padding: 0.875rem 1rem;
        margin-bottom: 0.75rem;
        height: 100%;
        box-shadow: var(--shadow-xs);
        transition: box-shadow 0.2s ease, border-color 0.2s ease;
    }
    .help-card:hover {
        box-shadow: var(--shadow-sm);
        border-color: var(--border-default);
    }
    .help-card h4 {
        color: var(--primary-600);
        margin: 0 0 0.375rem 0;
        font-size: 0.875rem;
        font-weight: 600;
    }
    .help-card p { color: var(--text-secondary); font-size: 0.8125rem; margin: 0; line-height: 1.5; }

    /* ============ 17. 分割线 ============ */
    hr {
        border: none;
        height: 1px;
        background: var(--border-subtle);
        margin: 1rem 0;
    }

    /* ============ 18. Tags ============ */
    .tag {
        display: inline-block;
        padding: 0.125rem 0.5rem;
        border-radius: var(--radius-sm);
        font-size: 0.6875rem;
        font-weight: 600;
        letter-spacing: 0;
    }
    .tag-up { background: var(--up-bg); color: var(--up-color); }
    .tag-down { background: var(--down-bg); color: var(--down-color); }
    .tag-neutral { background: var(--bg-elevated); color: var(--text-tertiary); }

    /* ============ 19. 折叠面板 ============ */
    [data-testid="stExpander"] {
        border: 1px solid var(--border-subtle) !important;
        border-radius: var(--radius-lg) !important;
        background: var(--bg-card) !important;
        box-shadow: var(--shadow-xs);
        transition: box-shadow 0.2s ease, border-color 0.2s ease;
    }
    [data-testid="stExpander"]:hover {
        border-color: var(--border-default);
    }
    [data-testid="stExpander"] summary {
        padding: 0.625rem 0.875rem;
        font-weight: 600;
        color: var(--text-primary);
        font-size: 0.875rem;
    }
    [data-testid="stExpander"] summary:hover {
        background: var(--bg-elevated) !important;
        border-radius: var(--radius-md);
    }

    /* ============ 20. Tabs ============ */
    .stTabs [data-baseweb="tab-list"] { gap: 4px; }
    .stTabs [data-baseweb="tab"] {
        border-radius: var(--radius-md);
        padding: 0.375rem 0.875rem;
        color: var(--text-tertiary);
        font-weight: 500;
        font-size: 0.8125rem;
        transition: all 0.15s ease;
    }
    .stTabs [data-baseweb="tab"]:hover {
        color: var(--text-secondary);
        background: var(--bg-elevated);
    }
    .stTabs [aria-selected="true"] {
        background: var(--primary-50) !important;
        color: var(--primary-600) !important;
        font-weight: 600 !important;
    }

    /* ============ 21. Plotly 图表容器 ============ */
    .js-plotly-plot {
        border: 1px solid var(--border-subtle);
        border-radius: var(--radius-lg);
        background: var(--bg-card);
        box-shadow: var(--shadow-xs);
        padding: 4px;
    }

    /* ============ 22. Popover ============ */
    [data-testid="stPopover"] > div > div {
        border-radius: var(--radius-md) !important;
    }

    /* ============ 23. Checkbox & Radio ============ */
    label[data-baseweb="checkbox"],
    label[data-baseweb="radio"] {
        font-size: 0.8125rem !important;
        color: var(--text-secondary) !important;
    }

    /* ============ 24. Streamlit columns 移动端强制堆叠 ============ */
    @media (max-width: 768px) {
        div[data-testid="stColumn"] {
            flex: 0 0 100% !important;
            max-width: 100% !important;
            min-width: 0 !important;
        }
    }

    /* ============ 25. 响应式 —— 平板 (768-1024px) ============ */
    @media (max-width: 1024px) {
        [data-testid="stAppViewContainer"] .block-container {
            max-width: 100% !important;
            padding-left: 1rem !important;
            padding-right: 1rem !important;
        }
    }

    /* ============ 26. 响应式 —— 手机 (≤768px) ============ */
    @media (max-width: 768px) {
        /* 标题字号收缩 */
        h1 { font-size: 1.25rem !important; }
        h2 { font-size: 1.0625rem !important; }
        h3 { font-size: 0.9375rem !important; }
        h4 { font-size: 0.875rem !important; }

        /* Hero 横幅收缩 */
        .hero-banner { padding: 0.75rem 0.875rem; margin-bottom: 0.75rem; }
        .hero-banner h1 { font-size: 1.0625rem !important; }
        .hero-banner p { font-size: 0.75rem; }

        /* 侧边栏变成全屏抽屉 */
        section[data-testid="stSidebar"] {
            position: fixed !important;
            z-index: 2147483001 !important;
            width: 88vw !important;
            max-width: 360px !important;
            height: 100vh !important;
            box-shadow: 8px 0 32px rgba(0,0,0,0.12);
        }
        section[data-testid="stSidebar"] > div:first-child {
            padding-top: 3rem;
        }
        [data-testid="stAppViewContainer"] { padding-left: 0 !important; }

        /* 侧边栏收起按钮放大 (touch target) */
        [data-testid="stSidebarCollapsedControl"],
        [data-testid="collapsedControl"] {
            top: 0.25rem !important;
            left: 0.25rem !important;
        }
        [data-testid="stSidebarCollapsedControl"] button,
        [data-testid="collapsedControl"] button {
            width: 40px !important;
            height: 40px !important;
        }

        /* 品牌按钮缩小 */
        section[data-testid="stSidebar"] button[kind="secondary"]:first-of-type {
            font-size: 1.0625rem !important;
            padding: 0.25rem 0.375rem !important;
        }

        /* 导航项 touch target 放大 */
        section[data-testid="stSidebar"] [data-testid="stRadioOption"] {
            padding: 0.75rem 1rem !important;
        }
        section[data-testid="stSidebar"] [data-testid="stRadioGroup"] p {
            font-size: 0.9375rem !important;
        }

        /* Metric 卡片适配 */
        div[data-testid="stMetric"] {
            padding: 0.625rem 0.75rem !important;
            border-radius: var(--radius-md) !important;
        }
        div[data-testid="stMetric"] [data-testid="stMetricValue"] {
            font-size: 1.125rem !important;
        }
        div[data-testid="stMetric"] label {
            font-size: 0.6875rem !important;
        }

        /* 按钮全宽 + touch target */
        .stButton > button {
            width: 100% !important;
            min-height: 44px !important;
            font-size: 0.875rem !important;
            padding: 0.625rem 1rem !important;
        }
        .stDownloadButton > button {
            width: 100% !important;
            min-height: 44px !important;
        }

        /* 顶部快捷入口 — 手机端两行布局 */
        div[data-testid="stHorizontalBlock"] {
            flex-wrap: wrap !important;
            gap: 6px !important;
        }
        div[data-testid="stHorizontalBlock"] > div {
            flex: 1 1 32% !important;
            min-width: 0 !important;
        }
        .stHorizontalBlock .stButton > button,
        div[data-testid="stHorizontalBlock"] .st-key-tn_ .stButton > button {
            padding: 0.5rem 0.375rem !important;
            font-size: 0.75rem !important;
            min-height: 40px !important;
        }
        /* 分割线 */
        div[data-testid="stHorizontalBlock"] + hr,
        div[data-testid="stVerticalBlock"] > hr:last-child {
            margin-top: 0.75rem;
            margin-bottom: 0.5rem;
        }

        /* 表格横向滚动 + 字号缩小 */
        [data-testid="stDataFrame"] {
            overflow-x: auto !important;
            max-width: 100% !important;
            -webkit-overflow-scrolling: touch;
        }
        [data-testid="stDataFrame"] th {
            font-size: 0.6875rem !important;
            padding: 0.5rem 0.5rem !important;
        }
        [data-testid="stDataFrame"] td {
            font-size: 0.75rem !important;
            padding: 0.375rem 0.5rem !important;
        }

        /* 输入控件 touch target */
        .stNumberInput input,
        .stTextInput input,
        .stDateInput input,
        [data-baseweb="select"] > div {
            min-height: 44px !important;
            font-size: 0.875rem !important;
        }
        .stSelectbox label,
        .stNumberInput label,
        .stTextInput label {
            font-size: 0.75rem !important;
        }

        /* Tabs 紧凑 */
        .stTabs [data-baseweb="tab-list"] {
            gap: 2px;
            flex-wrap: wrap;
        }
        .stTabs [data-baseweb="tab"] {
            padding: 0.375rem 0.625rem !important;
            font-size: 0.75rem !important;
        }

        /* Expander 紧凑 */
        [data-testid="stExpander"] summary {
            padding: 0.75rem 0.875rem !important;
            font-size: 0.875rem !important;
        }

        /* Plotly 图表高度缩减 */
        .js-plotly-plot .plotly {
            height: auto !important;
        }
        div[data-testid="stPlotlyChart"] > div {
            height: auto !important;
        }

        /* 折叠面板内图表 */
        [data-testid="stExpander"] .js-plotly-plot {
            padding: 2px;
        }

        /* 页面主容器 */
        [data-testid="stAppViewContainer"] .block-container {
            padding-top: 0.75rem !important;
            padding-bottom: 1rem !important;
            padding-left: 0.75rem !important;
            padding-right: 0.75rem !important;
        }

        /* 提示框 touch target */
        div.stSuccess, div.stError, div.stInfo, div.stWarning {
            padding: 0.75rem 0.875rem !important;
            font-size: 0.8125rem !important;
        }
    }

    /* ============ 27. 响应式 —— 小屏手机 (≤480px) ============ */
    @media (max-width: 480px) {
        /* 顶部快捷入口 — 三列 */
        div[data-testid="stHorizontalBlock"] > div {
            flex: 1 1 31% !important;
        }

        /* 侧边栏更窄 */
        section[data-testid="stSidebar"] {
            width: 92vw !important;
        }

        /* Hero 横幅更紧凑 */
        .hero-banner { padding: 0.625rem 0.75rem; }

        /* 分割线 */
        hr { margin: 0.75rem 0; }
    }

    /* ============ 28. 响应式 —— 横屏手机 ============ */
    @media (max-height: 500px) and (orientation: landscape) {
        section[data-testid="stSidebar"] {
            width: 70vw !important;
            max-width: 320px !important;
        }
        [data-testid="stAppViewContainer"] .block-container {
            padding-top: 0.5rem !important;
        }
        header[data-testid="stHeader"] {
            height: 2rem !important;
            min-height: 2rem !important;
        }
    }
</style>
""", unsafe_allow_html=True)

# ==================== session_state 默认参数 ====================
# 回测结束日默认取「全市场有数据的最后交易日」(而不是 MAX(date)):
# 个别标的先补了单根K线会把 MAX(date) 抬高, 用它当终点会让持仓取不到行情。
try:
    _db_last = data_store.db_stats().get("coverage_last_date") or ""
except Exception:
    _db_last = ""
DEFAULT_END = _db_last if _db_last not in ("", "-") else datetime.now().strftime("%Y-%m-%d")

DEFAULTS = {
    "initial_capital": 1_000_000,   # 100万
    "hold_days": 5,
    "top_n": 5,
    "limit_up_pct": 9.5,
    "backtest_start": "2023-01-01",
    "backtest_end": DEFAULT_END,
    "lang": "zh",                   # zh / en
}
for k, v in DEFAULTS.items():
    if k not in st.session_state:
        st.session_state[k] = v

# 语言选择写入 URL (?lang=en), 刷新页面后仍然保持
try:
    _q_lang = st.query_params.get("lang")
    if _q_lang in ("zh", "en"):
        st.session_state["lang"] = _q_lang
except Exception:
    pass

# ==================== 侧边栏 (扁平一层) ====================
with st.sidebar:
    # 品牌区
    brand_text = "托米量化" if lang() == "zh" else "TommyQuant"
    if st.button(brand_text, key="brand_home", use_container_width=True):
        st.session_state["page"] = "home"
        st.rerun()
    st.caption(t("brand_sub"))

    # 所有功能平铺 (一个 radio, 分组通过 CSS margin + divider 实现)
    _labels = [p[1] if lang() == "zh" else p[2] for p in FLAT_NAV]
    _keys = [p[0] for p in FLAT_NAV]
    _group_after = [p[3] for p in FLAT_NAV]

    _cur = st.session_state.get("page", "home")
    _idx = _keys.index(_cur) if _cur in _keys else 0
    _sel = st.radio("nav", _labels, index=_idx, label_visibility="collapsed",
                    key="__flat_nav")
    _new_idx = _labels.index(_sel)
    _new_key = _keys[_new_idx]

    # 分组视觉: 选中 portfolio 之后 / news 之后 多一条 divider
    # Streamlit radio 没法在选项之间插分隔线, 所以额外用 CSS 伪元素实现
    st.session_state["page"] = _new_key

    st.divider()

    # 数据状态
    try:
        _stats = data_store.db_stats()
        st.caption(f"📈 {t('data_status')}")
        st.caption(f"{_stats['symbols']} {t('zhi')}  ·  {_stats['bars']:,} bars")
        st.caption(f"{_stats['first_date']} ~ {_stats['last_date']}")
    except Exception:
        st.caption(f"📈 {t('data_status')}")
        st.caption("Worker loading..." if lang() == "en" else "Worker 自动补全中...")

page = _new_key  # 供后续路由使用

# 从 session_state 取出参数 (供页面使用)
initial_capital = st.session_state["initial_capital"]
hold_days = st.session_state["hold_days"]
top_n = st.session_state["top_n"]
limit_up_pct = st.session_state["limit_up_pct"]
backtest_start = pd.Timestamp(st.session_state["backtest_start"])
backtest_end = pd.Timestamp(st.session_state["backtest_end"])

# ==================== 数据加载 (惰性: 只在需要计算的页面加载) ====================
# 数据指纹: 用于缓存失效判断 (数据更新→指纹变→重新加载/重算)
DATA_FINGERPRINT = data_store.data_fingerprint()


@st.cache_resource(show_spinner=False, max_entries=1)
def get_feat_data(fingerprint: str = ""):
    """行情 + 因子。磁盘缓存命中直接读 (数秒), 未命中才重算 (~80s)。"""
    return data_store.load_feat_cached(fingerprint)


def require_data() -> dict:
    """需要计算的页面调用: 首次加载时提示, 无数据则中止"""
    with st.spinner("📚 加载行情与因子..." if lang() == "zh" else "📚 Loading bars & factors..."):
        feat = get_feat_data(DATA_FINGERPRINT)
    if not feat:
        st.warning("📭 数据库正在初始化，Worker 已在后台自动自举全A股 + ETF 日线（首次约 20-40 分钟）。请稍后刷新页面，或到「💾 数据管理」页查看任务进度。"
                   if lang() == "zh" else
                   "📭 Database is initializing. The worker is bootstrapping all A-share + ETF daily bars (20-40 min on first run). Please refresh later.")
        st.stop()
    return feat

# ==================== 页面路由 ====================
def request_strategy_data(strategy_key: str, symbols: list, start, end) -> dict:
    """
    策略触发数据补全:
      只把"该策略声明需要 + 库内缺失"的区间写成后台任务, 由 update_worker 消费。
      不在请求线程里下载, 避免阻塞页面。
    """
    import data_manager
    out = {"bars": 0, "fundamental": False, "ranges": 0}
    try:
        p = data_manager.plan_bars(list(symbols), str(start)[:10], str(end)[:10])
        # Worker 侧 ensure_bars 会再按标的算一次缺口, 所以合并成一个任务即可
        need = sorted({s for syms in p["plan"].values() for s in syms})
        if p["plan"] and not any(
                t["dataset"] == "daily_bar"
                for t in data_store.task_list(limit=20, status="pending")):
            data_store.task_create(
                "daily_bar",
                {"symbols": need,
                 "start": str(start)[:10].replace("-", ""),
                 "end": str(end)[:10].replace("-", "")},
                f"{strategy_key} 需要补 {len(need)} 只标的的缺失区间")
            out["bars"] = len(need)
            out["ranges"] = len(p["plan"])
    except Exception:
        pass
    try:
        req = data_manager.requirements_for(strategy_key)
        if "fundamental" in req["datasets"]:
            fst = data_store.fundamental_stats()
            if fst["symbols"] < len(symbols) * 0.5:
                data_store.task_create("fundamental", {"quarters": 4},
                                       f"{strategy_key} 需要财务季报回填")
                out["fundamental"] = True
    except Exception:
        pass
    return out


def get_cost_params() -> dict:
    """从 app_settings 读取交易成本参数 (佣金/印花税/过户费/滑点)"""
    return {
        "commission_rate": float(data_store.get_setting("cost_commission_rate", 0.00025)),
        "commission_min": float(data_store.get_setting("cost_commission_min", 5.0)),
        "stamp_tax_rate": float(data_store.get_setting("cost_stamp_tax_rate", 0.001)),
        "transfer_fee_rate": float(data_store.get_setting("cost_transfer_fee_rate", 0.00001)),
        "slippage_rate": float(data_store.get_setting("cost_slippage_rate", 0.001)),
    }


def _cost_str() -> str:
    """成本参数的指纹串, 参与缓存键"""
    cp = get_cost_params()
    return f"{cp['commission_rate']}|{cp['commission_min']}|{cp['stamp_tax_rate']}|{cp['transfer_fee_rate']}|{cp['slippage_rate']}"


def cached_backtest(strategy_key, _feat_data, initial, hold, top, lu_pct,
                    start_d, end_d, fingerprint=""):
    """
    双层缓存:
      L1: st.cache_data (内存, 24h)
      L2: SQLite backtest_cache (持久, 数据指纹变化自动失效)
    参数/数据均未变化时, 重启容器也能秒开
    """
    cost_str = _cost_str()
    key_raw, cache_key = bt_cache_key(strategy_key, initial, hold, top, lu_pct,
                                      start_d, end_d, fingerprint, cost_str)

    # L2: SQLite 命中 → 直接返回
    hit = data_store.get_backtest_cache(cache_key)
    if hit:
        return deserialize_result(hit)

    # 未命中 → 跑回测 (向量化引擎优先)
    select_fn = STRATEGIES[strategy_key]
    result = run_backtest_auto(
        _feat_data, select_fn, strategy_params={"top_n": top},
        initial_capital=initial, hold_days=hold, limit_up_pct=lu_pct,
        start_date=start_d, end_date=end_d, cost_params=get_cost_params(),
    )

    # 回写 L2
    if result.get("curve") is not None and len(result.get("curve", [])) > 0:
        try:
            data_store.save_backtest_cache(cache_key, strategy_key, key_raw,
                                           fingerprint, serialize_result(result))
        except Exception:
            pass
    return result


def _goto(main_key: str, sub_key: str = None):
    """通过设置 session_state 下标跳转到指定页 (用于快捷入口按钮)"""
    for i, p in enumerate(MAIN_PAGES):
        if p[0] == main_key:
            st.session_state["main_idx"] = i
            break
    if sub_key is not None:
        subs = STRATEGY_SUBPAGES if main_key == "strategy" else MORE_SUBPAGES
        for i, p in enumerate(subs):
            if p[0] == sub_key:
                st.session_state["sub_idx"] = i
                break
    st.rerun()


# --- 🏠 量化大师 (首页) ---
# 回测页策略参数键 (收起/展开时用于快照与恢复)
_CFG_KEYS = ("sd_cap_max", "sd_ret20", "sd_ma20", "sd_ma60",
             "sd_turn_min", "sd_turn_max",
             "ret_20_min", "cap_max", "require_ma20",
             "mc_cap_min", "mc_cap_max", "mc_weight_frac", "mc_cap_chg_th",
             "mc_rebal_th", "mc_listed_yrs", "mc_ma30_lookback", "mc_universe",
             "mc_ma30_exit",
             "mc_ex_st", "mc_ex_ocf", "mc_ex_nonop", "mc_ex_nodiv", "mc_div_years")


def _render_top_nav(cur_page: str, subtitle: str = None, cfg_toggle=None):
    """页面顶部: 页面标题 h3 + (可选) subtitle caption + (可选) 工具按钮 + 分隔线.

    所有页面用同一套头部, 取代之前「_render_top_nav 胶囊 + hero-banner」双重头部。
    """
    page_title_map = {}
    for k, zh, _ in MAIN_PAGES:
        page_title_map[k] = zh
    for k, zh, _ in STRATEGY_SUBPAGES + MORE_SUBPAGES:
        page_title_map[k] = zh
    title = page_title_map.get(cur_page, page_label(cur_page))

    cols = st.columns([1, 1]) if cfg_toggle else st.columns([1])
    with cols[0]:
        st.markdown(f"### {title}")
        if subtitle:
            st.caption(subtitle)
    if cfg_toggle:
        with cols[1]:
            if st.button("⚙️ 设置", key="top_cfg", use_container_width=False):
                cfg_toggle()
    st.markdown("---")


# ==================== 各页面渲染 ====================

_MARKET_CARDS_HTML = r"""
<!DOCTYPE html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><style>
  * { box-sizing: border-box; }
  body { margin:0; font-family: 'Inter', -apple-system, BlinkMacSystemFont,
                   'PingFang SC', 'Hiragino Sans GB', 'Microsoft YaHei', sans-serif; }
  .grid { display:grid; grid-template-columns:repeat(4, 1fr); gap:8px; }
  .card { background:#fff; border:1px solid #e8ecf1; border-radius:12px;
          padding:8px 12px; box-shadow:0 1px 2px rgba(17,24,39,.04);
          transition: box-shadow 0.15s ease, border-color 0.15s ease; }
  .card:hover { box-shadow:0 2px 8px rgba(17,24,39,.08); border-color:#d9dee6; }
  .nm { font-size:12px; color:#475569; font-weight:500;
        white-space:nowrap; overflow:hidden; text-overflow:ellipsis;
        display:flex; align-items:center; gap:2px; }
  .gp { color:#94a3b8; font-size:10px; margin-left:auto; font-weight:400; }
  .px { font-size:17px; font-weight:700; line-height:1.35; color:#1a1f2e;
        font-variant-numeric: tabular-nums; letter-spacing:-0.01em; }
  .pc { font-size:11.5px; font-weight:600; margin-left:2px; }
  .meta { margin-top:8px; font-size:11px; color:#94a3b8; font-variant-numeric: tabular-nums; }
  /* 响应式 */
  @media (max-width: 1024px) { .grid { grid-template-columns:repeat(3, 1fr); } }
  @media (max-width: 768px) {
    .grid { grid-template-columns:repeat(2, 1fr); gap:6px; }
    .card { padding:6px 8px; border-radius:10px; }
    .nm { font-size:11px; }
    .px { font-size:15px; }
    .pc { font-size:10.5px; }
  }
  @media (max-width: 480px) {
    .grid { grid-template-columns:1fr 1fr; gap:5px; }
    .px { font-size:14px; }
    .gp { display:none; }
  }
</style></head><body>
<div class="grid" id="grid"></div>
<div class="meta" id="meta">连接行情中...</div>
<script>
const ITEMS = __ITEMS__;
const UP="#dc2626", DOWN="#16a34a", FLAT="#64748b";
function fmtPrice(v){ return v>=10000 ? v.toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2}) : v.toFixed(2); }
function arrow(p){ return p>0?"🔺":(p<0?"🔻":"➖"); }
function colorOf(p){ return p>0?UP:(p<0?DOWN:FLAT); }
function init(){
  const g = document.getElementById('grid');
  g.innerHTML = '';
  ITEMS.forEach(function(it){
    const d = document.createElement('div');
    d.className = 'card';
    d.innerHTML = '<div class="nm">'+it.flag+' '+it.name+'<span class="gp">'+it.group+'</span></div>'+
                  '<div class="px" id="px_'+it.code+'">'+
                  '<span class="v">'+(it.price?fmtPrice(it.price):'-')+'</span> '+
                  '<span class="pc">'+(it.price?arrow(it.pct)+Math.abs(it.pct).toFixed(2)+'%':'-')+'</span></div>';
    if(it.price){
      d.querySelector('.v').style.color = colorOf(it.pct);
      d.querySelector('.pc').style.color = colorOf(it.pct);
    }
    g.appendChild(d);
  });
}
function apply(code, price, pct){
  const box = document.getElementById('px_'+code);
  if(!box || !price) return;
  const c = colorOf(pct);
  box.querySelector('.v').textContent = fmtPrice(price);
  box.querySelector('.v').style.color = c;
  const pc = box.querySelector('.pc');
  pc.textContent = arrow(pct)+Math.abs(pct).toFixed(2)+'%';
  pc.style.color = c;
}
function setMeta(txt){ document.getElementById('meta').textContent = txt; }
function pull(){
  const ids = ITEMS.map(function(i){return i.code;}).join(',');
  if(!ids) return;
  const url = 'https://push2.eastmoney.com/api/qt/ulist.np/get?secids='+ids+
              '&fields=f1,f2,f3,f12,f13,f14&_='+Date.now();
  fetch(url, {cache:'no-store'})
    .then(function(r){ return r.json(); })
    .then(function(j){
      const rows = (j && j.data && j.data.diff) || [];
      if(!rows.length){ setMeta('⚠️ 行情暂无数据，自动重试中'); return; }
      rows.forEach(function(q){
        if(q.f2 === '-' || q.f2 === undefined) return;
        const dec = Number(q.f1) || 2;
        const p = Number(q.f2) / Math.pow(10, dec);
        const pct = (Number(q.f3) || 0) / 100;
        apply(q.f13 + '.' + q.f12, p, pct);
      });
      const t = new Date().toLocaleTimeString('zh-CN', {hour12:false});
      setMeta('🕒 '+t+' 更新 · 每秒自动刷新（与东方财富同一数据源）');
    })
    .catch(function(){ setMeta('⚠️ 行情接口连接失败，自动重试中'); });
}
init(); pull();
setInterval(pull, 1000);
</script></body></html>
"""


_POSITIONS_HTML = r"""
<!DOCTYPE html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><style>
  * { box-sizing: border-box; }
  body { margin:0; font-family:'Inter',-apple-system,BlinkMacSystemFont,'PingFang SC','Hiragino Sans GB','Microsoft YaHei',sans-serif; color:#1a1f2e; }
  .sum { display:grid; grid-template-columns:repeat(4,1fr); gap:10px; margin-bottom:10px; }
  .m { background:#fff; border:1px solid #e8ecf1; border-radius:12px; padding:8px 14px;
       box-shadow:0 1px 2px rgba(17,24,39,.04); }
  .m .l { font-size:11px; color:#64748b; font-weight:500; text-transform:none; }
  .m .v { font-size:17px; font-weight:700; font-variant-numeric:tabular-nums; letter-spacing:-0.01em; }
  table { width:100%; border-collapse:collapse; font-size:12.5px; background:#fff;
          border-radius:12px; overflow:hidden; box-shadow:0 1px 2px rgba(17,24,39,.04); }
  th { background:#f1f5f9; color:#475569; font-weight:600; font-size:11px;
       padding:8px 10px; border-bottom:1px solid #e8ecf1; text-align:right; white-space:nowrap; }
  th:nth-child(-n+2), td:nth-child(-n+2) { text-align:left; }
  td { padding:7px 10px; border-bottom:1px solid #f1f5f9; text-align:right;
       font-variant-numeric:tabular-nums; font-size:12.5px; }
  tr:nth-child(even) { background:#f8fafc; }
  tr:hover { background:#f1f5f9; }
  .meta { font-size:11px; color:#94a3b8; margin-top:8px; font-variant-numeric:tabular-nums; }
  /* 响应式 */
  @media (max-width: 768px) {
    .sum { grid-template-columns:repeat(2,1fr); gap:6px; margin-bottom:8px; }
    .m { padding:6px 8px; border-radius:10px; }
    .m .l { font-size:10.5px; }
    .m .v { font-size:15px; }
    table { font-size:11px; display:block; overflow-x:auto; -webkit-overflow-scrolling:touch;
            border-radius:10px; }
    th, td { padding:5px 6px; font-size:10.5px; }
  }
  @media (max-width: 480px) {
    .sum { gap:5px; }
    .m .v { font-size:14px; }
  }
</style></head><body>
<div class="sum" id="sum"></div>
<table id="tbl"></table>
<div class="meta" id="meta"></div>
<script>
const ROWS = __ROWS__;
const RT = {};
const UP="#dc2626", DOWN="#16a34a", FLAT="#64748b";
function money(v){ return v.toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2}); }
function colorOf(v){ return v>0?UP:(v<0?DOWN:FLAT); }
function build(){
  let cost=0, mv=0, rt=0;
  const hdr='<thead><tr><th>代码</th><th>选股日</th><th>买入价</th><th>最新价</th>'+
            '<th>股数</th><th>市值</th><th>浮盈</th><th>浮盈%</th></tr></thead>';
  const body = ROWS.map(function(r){
    const p = RT[r.q] || r.last;
    if(RT[r.q]) rt++;
    const c = r.shares*r.buy, m = r.shares*p, pnl = m-c, pct = (p/r.buy-1)*100;
    cost += c; mv += m;
    const col = colorOf(pnl);
    return '<tr><td>'+r.c+'</td><td>'+r.date+'</td><td>'+r.buy.toFixed(2)+'</td>'+
           '<td style="font-weight:600;color:'+col+'">'+p.toFixed(2)+'</td>'+
           '<td>'+r.shares+'</td><td>'+money(m)+'</td>'+
           '<td style="color:'+col+'">'+money(pnl)+'</td>'+
           '<td style="color:'+col+'">'+pct.toFixed(2)+'%</td></tr>';
  }).join('');
  document.getElementById('tbl').innerHTML = hdr+'<tbody>'+body+'</tbody>';

  const totalPnl = mv-cost, totalPct = cost ? totalPnl/cost*100 : 0;
  document.getElementById('sum').innerHTML =
    '<div class="m"><div class="l">持仓数量</div><div class="v">'+ROWS.length+' 只</div></div>'+
    '<div class="m"><div class="l">投入成本</div><div class="v">'+money(cost)+'</div></div>'+
    '<div class="m"><div class="l">当前市值</div><div class="v">'+money(mv)+'</div></div>'+
    '<div class="m"><div class="l">总浮盈</div><div class="v" style="color:'+colorOf(totalPnl)+'">'+
    money(totalPnl)+' <span style="font-size:12px">'+totalPct.toFixed(2)+'%</span></div></div>';

  const t = new Date().toLocaleTimeString('zh-CN',{hour12:false});
  document.getElementById('meta').textContent = rt
    ? '🟢 '+rt+' 只实时行情 · '+t+' 更新 · 每秒自动刷新（与东方财富同一数据源）'
    : '⚪ 非交易时段或行情未连接 · 显示最新收盘价';
}
function pull(){
  const ids = ROWS.map(function(r){return r.q;}).join(',');
  if(!ids) return;
  fetch('https://push2.eastmoney.com/api/qt/ulist.np/get?secids='+ids+
        '&fields=f1,f2,f3,f12,f13,f14&_='+Date.now(), {cache:'no-store'})
    .then(function(r){ return r.json(); })
    .then(function(j){
      const rows = (j && j.data && j.data.diff) || [];
      rows.forEach(function(q){
        if(q.f2 === '-' || q.f2 === undefined) return;
        const dec = Number(q.f1) || 2;
        RT[q.f13 + '.' + q.f12] = Number(q.f2) / Math.pow(10, dec);
      });
      try { build(); } catch(e) {}
    })
    .catch(function(){});
}
build(); pull(); setInterval(pull, 1000);
</script></body></html>
"""


_PICKS_HTML = r"""
<!DOCTYPE html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><style>
  * { box-sizing: border-box; }
  body { margin:0; font-family:'Inter',-apple-system,BlinkMacSystemFont,'PingFang SC','Hiragino Sans GB','Microsoft YaHei',sans-serif; color:#1a1f2e; }
  table { width:100%; border-collapse:collapse; font-size:12.5px; background:#fff;
          border-radius:12px; overflow:hidden; box-shadow:0 1px 2px rgba(17,24,39,.04); }
  th { background:#f1f5f9; color:#475569; font-weight:600; font-size:11px;
       padding:8px 10px; border-bottom:1px solid #e8ecf1; text-align:right; white-space:nowrap; }
  th:nth-child(-n+2), td:nth-child(-n+2), th:nth-last-child(-n+2), td:nth-last-child(-n+2) { text-align:left; }
  td { padding:7px 10px; border-bottom:1px solid #f1f5f9; text-align:right;
       font-variant-numeric:tabular-nums; white-space:nowrap; font-size:12.5px; }
  tr:nth-child(even) { background:#f8fafc; }
  tr:hover { background:#f1f5f9; }
  .pos { color:#dc2626; font-weight:600; } .neg { color:#16a34a; font-weight:600; } .flat { color:#64748b; }
  .meta { font-size:11px; color:#94a3b8; margin-top:8px; font-variant-numeric:tabular-nums; }
  tbody tr[data-q] { cursor:pointer; }
  tbody tr.sel { background:#e0edff !important; box-shadow:inset 3px 0 0 #2563eb; }
  /* K线面板 */
  .chart { margin-top:10px; background:#fff; border:1px solid #eef2f7; border-radius:12px;
           padding:8px 10px 4px; box-shadow:0 1px 2px rgba(17,24,39,.04); }
  .chart-head { display:flex; align-items:center; justify-content:space-between;
                gap:8px; margin-bottom:2px; }
  .chart-title { font-size:12.5px; font-weight:600; color:#334155;
                 white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
  .tabs { display:flex; gap:4px; flex:0 0 auto; }
  .tabs button { border:1px solid #e2e8f0; background:#f8fafc; color:#475569;
                 font-size:11px; padding:3px 9px; border-radius:6px; cursor:pointer;
                 font-family:inherit; }
  .tabs button.on { background:#2563eb; border-color:#2563eb; color:#fff; }
  canvas { display:block; width:100%; }
  /* 响应式 */
  @media (max-width: 768px) {
    table { display:block; overflow-x:auto; -webkit-overflow-scrolling:touch;
            border-radius:10px; font-size:11px; }
    th, td { padding:5px 6px; font-size:10.5px; white-space:nowrap; }
  }
</style></head><body>
<table id="tbl"></table>
<div class="meta" id="meta"></div>
<div class="chart">
  <div class="chart-head">
    <div class="chart-title" id="chart-title">📈 点击上方任意标的，查看其K线走势</div>
    <div class="tabs">
      <button data-klt="101" class="on">日线</button>
      <button data-klt="102">周线</button>
      <button data-klt="103">月线</button>
    </div>
  </div>
  <canvas id="cv" style="height:330px"></canvas>
</div>
<script>
const ROWS = __ROWS__;
const RT = {};
const KL = {};                 // K线缓存: secid|klt -> {name, arr}
let SEL = null, KLT = 101, CUR = null, HOV = -1;

function money(v){ return (v==null||!isFinite(v)) ? '-' : v.toFixed(2); }
function cls(v){ return v>0?'pos':(v<0?'neg':'flat'); }

function build(){
  const hdr = '<thead><tr><th>代码</th><th>选股价</th><th>现价</th><th>涨跌%</th>'+
              '<th>5日%</th><th>20日%</th><th>换手%</th><th>市值(亿)</th>'+
              '<th>PE</th><th>ROE%</th><th>行业</th><th>信号</th></tr></thead>';
  let rt = 0;
  const body = ROWS.map(function(r){
    const q = RT[r.q];
    if(q !== undefined) rt++;
    const px = q !== undefined ? q : r.c0;
    const chg = q !== undefined && r.c0 ? (q/r.c0-1)*100 : null;
    const selCls = (SEL && SEL.q === r.q) ? ' class="sel"' : '';
    return '<tr data-q="'+r.q+'" data-c="'+r.c+'"'+selCls+'>'+
      '<td>'+r.c+'</td><td>'+money(r.c0)+'</td>'+
      '<td class="'+(chg===null?'flat':cls(chg))+'" style="font-weight:600">'+money(px)+'</td>'+
      '<td class="'+(chg===null?'flat':cls(chg))+'">'+(chg===null?'-':(chg>0?'+':'')+chg.toFixed(2)+'%')+'</td>'+
      '<td class="'+cls(r.r5)+'">'+(r.r5==null?'-':r.r5.toFixed(2)+'%')+'</td>'+
      '<td class="'+cls(r.r20)+'">'+(r.r20==null?'-':r.r20.toFixed(2)+'%')+'</td>'+
      '<td>'+money(r.to)+'</td><td>'+money(r.cap)+'</td><td>'+money(r.pe)+'</td>'+
      '<td>'+money(r.roe)+'</td><td>'+r.ind+'</td><td>'+r.sig+'</td></tr>';
  }).join('');
  document.getElementById('tbl').innerHTML = hdr+'<tbody>'+body+'</tbody>';
  const t = new Date().toLocaleTimeString('zh-CN',{hour12:false});
  document.getElementById('meta').textContent = (rt
    ? '🟢 '+rt+' 只实时报价 · '+t+' 更新 · 每秒自动刷新（与东方财富同一数据源）'
    : '⚪ 非交易时段或行情未连接 · 现价显示选股日收盘价') + ' · 点击行看K线';
}

function pull(){
  const ids = ROWS.map(function(r){return r.q;}).join(',');
  if(!ids) return;
  fetch('https://push2.eastmoney.com/api/qt/ulist.np/get?secids='+ids+
        '&fields=f1,f2,f3,f12,f13,f14&_='+Date.now(), {cache:'no-store'})
    .then(function(r){ return r.json(); })
    .then(function(j){
      const rows = (j && j.data && j.data.diff) || [];
      rows.forEach(function(q){
        if(q.f2 === '-' || q.f2 === undefined) return;
        const dec = Number(q.f1) || 2;
        RT[q.f13 + '.' + q.f12] = Number(q.f2) / Math.pow(10, dec);
      });
      try { build(); } catch(e) {}
    })
    .catch(function(){});
}

/* ---------- K线 ---------- */
const PERIODS = {101:'日线', 102:'周线', 103:'月线'};

function setTitle(txt){ document.getElementById('chart-title').textContent = txt; }

function select(q, c){
  if(SEL && SEL.q === q && CUR){ return; }   // 已选中同一标的
  SEL = {q:q, c:c}; HOV = -1; CUR = null;
  build(); draw();
  loadKline();
}

function loadKline(){
  if(!SEL) return;
  const key = SEL.q + '|' + KLT;
  if(KL[key]){ CUR = KL[key]; renderTitle(); draw(); return; }
  setTitle('⏳ ' + PERIODS[KLT] + ' K线加载中 · ' + SEL.c + ' ...');
  const url = 'https://push2his.eastmoney.com/api/qt/stock/kline/get?secid='+SEL.q+
    '&fields1=f1,f2,f3,f4,f5,f6&fields2=f51,f52,f53,f54,f55,f56,f57&klt='+KLT+
    '&fqt=1&end=20500101&lmt=260&_='+Date.now();
  fetch(url, {cache:'no-store'})
    .then(function(r){ return r.json(); })
    .then(function(j){
      const d = j && j.data;
      if(!d || !d.klines || !d.klines.length){
        if(SEL && SEL.q+'|'+KLT === key){ CUR = null; setTitle('⚠️ ' + SEL.c + ' 暂无K线数据'); draw(); }
        return;
      }
      const arr = d.klines.map(function(s){
        const p = s.split(',');
        return {d:p[0], o:+p[1], c:+p[2], h:+p[3], l:+p[4], v:+p[5]};
      });
      KL[key] = {name:d.name || '', arr:arr};
      if(!SEL || SEL.q+'|'+KLT !== key) return;   // 用户已切换标的/周期
      CUR = KL[key]; renderTitle(); draw();
    })
    .catch(function(){
      if(SEL && SEL.q+'|'+KLT === key){ setTitle('⚠️ ' + SEL.c + ' K线加载失败，请稍后重试'); }
    });
}

function renderTitle(){
  if(!CUR || !SEL) return;
  setTitle('📈 ' + (CUR.name ? CUR.name : '') + ' ' + SEL.c + ' · ' + PERIODS[KLT] +
           ' · 前复权 · 共 ' + CUR.arr.length + ' 根');
}

function maOf(a, n){
  const out = new Array(a.length).fill(null);
  let s = 0;
  for(let i=0;i<a.length;i++){
    s += a[i];
    if(i>=n) s -= a[i-n];
    if(i>=n-1) out[i] = s/n;
  }
  return out;
}

function draw(){
  const cv = document.getElementById('cv');
  if(!cv) return;
  const W = cv.clientWidth || 760, H = 330;
  const dpr = window.devicePixelRatio || 1;
  cv.width = Math.round(W*dpr); cv.height = Math.round(H*dpr);
  const g = cv.getContext('2d');
  g.setTransform(dpr,0,0,dpr,0,0);
  g.clearRect(0,0,W,H);
  g.font = '11px Inter,-apple-system,"PingFang SC","Microsoft YaHei",sans-serif';

  if(!CUR || !CUR.arr || CUR.arr.length < 2){
    g.fillStyle = '#94a3b8'; g.textAlign = 'center'; g.textBaseline = 'middle';
    g.fillText(SEL ? '暂无K线数据' : '👆 点击上方任一标的，查看其K线走势', W/2, H/2);
    return;
  }

  const arr = CUR.arr, n = arr.length;
  const PL = 6, PR = 52, PT = 22, PB = 18;
  const volH = Math.round((H - PT - PB) * 0.20);
  const priceH = H - PT - PB - volH - 10;
  const plotW = W - PL - PR;
  const step = plotW / n;
  const bw = Math.max(1, Math.min(9, step * 0.64));
  const xOf = function(i){ return PL + i*step + step/2; };

  let hi = -Infinity, lo = Infinity;
  for(let i=0;i<n;i++){ if(arr[i].h > hi) hi = arr[i].h; if(arr[i].l < lo) lo = arr[i].l; }
  const padP = (hi-lo)*0.06 || Math.max(hi*0.01, 0.01);
  hi += padP; lo -= padP;
  const yOf = function(p){ return PT + (hi-p)/(hi-lo)*priceH; };

  /* 网格 + 右侧价格刻度 */
  g.strokeStyle = '#eef2f7';
  g.fillStyle = '#94a3b8'; g.textAlign = 'left'; g.textBaseline = 'middle';
  for(let i=0;i<=4;i++){
    const y = PT + priceH*i/4;
    g.beginPath(); g.moveTo(PL,y); g.lineTo(PL+plotW,y); g.stroke();
    g.fillText((hi - (hi-lo)*i/4).toFixed(2), PL+plotW+6, y);
  }

  /* 蜡烛 (红涨绿跌) */
  for(let i=0;i<n;i++){
    const k = arr[i], x = xOf(i);
    const col = k.c >= k.o ? '#ef4444' : '#16a34a';
    g.strokeStyle = col; g.fillStyle = col;
    g.beginPath(); g.moveTo(x, yOf(k.h)); g.lineTo(x, yOf(k.l)); g.lineWidth = 1; g.stroke();
    const y1 = yOf(Math.max(k.o, k.c)), y2 = yOf(Math.min(k.o, k.c));
    g.fillRect(x - bw/2, y1, bw, Math.max(1, y2-y1));
  }

  /* 成交量 */
  let maxV = 0;
  for(let i=0;i<n;i++) if(arr[i].v > maxV) maxV = arr[i].v;
  const yV = function(v){ return H - PB - (maxV ? v/maxV*volH : 0); };
  for(let i=0;i<n;i++){
    const k = arr[i], x = xOf(i);
    g.fillStyle = k.c >= k.o ? 'rgba(239,68,68,.5)' : 'rgba(22,163,74,.5)';
    g.fillRect(x - bw/2, yV(k.v), bw, (H-PB) - yV(k.v));
  }

  /* 均线 */
  const closes = arr.map(function(k){ return k.c; });
  const MAS = [[5,'#f59e0b'],[10,'#3b82f6'],[20,'#a855f7'],[60,'#0f766e']];
  const maData = MAS.map(function(m){ return {n:m[0], col:m[1], v:maOf(closes, m[0])}; });
  maData.forEach(function(item){
    g.strokeStyle = item.col; g.lineWidth = 1.2;
    g.beginPath();
    let started = false;
    for(let i=0;i<n;i++){
      if(item.v[i] == null) continue;
      const y = yOf(item.v[i]);
      if(!started){ g.moveTo(xOf(i), y); started = true; } else { g.lineTo(xOf(i), y); }
    }
    g.stroke();
  });
  g.lineWidth = 1;

  /* 均线图例 */
  let lx = PL + 2;
  g.textAlign = 'left'; g.textBaseline = 'top';
  maData.forEach(function(item){
    const last = item.v[n-1];
    const txt = 'MA' + item.n + ' ' + (last==null ? '-' : last.toFixed(2));
    g.fillStyle = item.col;
    g.fillText(txt, lx, 4);
    lx += g.measureText(txt).width + 12;
  });

  /* 底部日期 */
  g.fillStyle = '#94a3b8'; g.textBaseline = 'top';
  const cnt = Math.min(6, n);
  for(let i=0;i<cnt;i++){
    const idx = cnt > 1 ? Math.round(i*(n-1)/(cnt-1)) : 0;
    g.textAlign = i === 0 ? 'left' : (i === cnt-1 ? 'right' : 'center');
    const x = Math.max(PL, Math.min(xOf(idx), PL+plotW));
    g.fillText(arr[idx].d, x, H-PB+4);
  }

  /* 最新价标签 */
  const lk = arr[n-1], ly = yOf(lk.c);
  g.fillStyle = lk.c >= lk.o ? '#ef4444' : '#16a34a';
  g.fillRect(PL+plotW+2, ly-8, PR-4, 16);
  g.fillStyle = '#fff'; g.textAlign = 'left'; g.textBaseline = 'middle';
  g.fillText(lk.c.toFixed(2), PL+plotW+6, ly);

  /* 十字光标 + 信息框 */
  if(HOV >= 0 && HOV < n){
    const k = arr[HOV], x = xOf(HOV);
    g.save();
    g.strokeStyle = '#94a3b8'; g.setLineDash([3,3]);
    g.beginPath(); g.moveTo(x, PT); g.lineTo(x, H-PB); g.stroke();
    g.restore();
    const pc = HOV > 0 ? arr[HOV-1].c : k.o;
    const pct = pc ? (k.c/pc - 1)*100 : 0;
    const lines = [k.d,
      '开 ' + k.o.toFixed(2) + '   收 ' + k.c.toFixed(2),
      '高 ' + k.h.toFixed(2) + '   低 ' + k.l.toFixed(2),
      '涨跌 ' + (pct>0?'+':'') + pct.toFixed(2) + '%   量 ' + (k.v/10000).toFixed(2) + '万手'];
    const boxW = 148, boxH = 4*15 + 8;
    let bx = x + 12;
    if(bx + boxW > W - 4) bx = x - 12 - boxW;
    let by = PT + 6;
    g.fillStyle = 'rgba(15,23,42,.88)';
    g.beginPath();
    if(g.roundRect){ g.roundRect(bx, by, boxW, boxH, 6); } else { g.rect(bx, by, boxW, boxH); }
    g.fill();
    g.fillStyle = '#fff'; g.textAlign = 'left'; g.textBaseline = 'top';
    lines.forEach(function(s, i){
      g.fillStyle = i === 0 ? '#cbd5e1' : (i === 3 ? (pct>=0?'#f87171':'#4ade80') : '#e2e8f0');
      g.fillText(s, bx + 8, by + 5 + i*15);
    });
  }
}

/* ---------- 交互 ---------- */
document.getElementById('tbl').addEventListener('click', function(e){
  const tr = e.target && e.target.closest ? e.target.closest('tr[data-q]') : null;
  if(!tr) return;
  select(tr.getAttribute('data-q'), tr.getAttribute('data-c'));
});

Array.prototype.forEach.call(document.querySelectorAll('.tabs button'), function(b){
  b.addEventListener('click', function(){
    const v = Number(b.getAttribute('data-klt'));
    if(v === KLT) return;
    KLT = v;
    Array.prototype.forEach.call(document.querySelectorAll('.tabs button'), function(x){
      if(x === b){ x.classList.add('on'); } else { x.classList.remove('on'); }
    });
    HOV = -1;
    if(SEL){ CUR = null; loadKline(); } else { draw(); }
  });
});

const cvEl = document.getElementById('cv');
cvEl.addEventListener('mousemove', function(e){
  if(!CUR || !CUR.arr || CUR.arr.length < 2) return;
  const rect = this.getBoundingClientRect();
  const W = this.clientWidth, PL = 6, PR = 52, n = CUR.arr.length;
  const step = (W - PL - PR) / n;
  let idx = Math.floor((e.clientX - rect.left - PL) / step);
  if(idx < 0) idx = 0;
  if(idx > n-1) idx = n-1;
  if(idx !== HOV){ HOV = idx; draw(); }
});
cvEl.addEventListener('mouseleave', function(){ if(HOV !== -1){ HOV = -1; draw(); } });
window.addEventListener('resize', function(){ draw(); });

build(); pull(); setInterval(pull, 1000); draw();
</script></body></html>
"""


_LIVE_HOLDINGS_HTML = r"""
<!DOCTYPE html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><style>
  * { box-sizing: border-box; }
  body { margin:0; font-family:'Inter',-apple-system,BlinkMacSystemFont,'PingFang SC','Hiragino Sans GB','Microsoft YaHei',sans-serif; color:#1a1f2e; }
  table { width:100%; border-collapse:collapse; font-size:12.5px; background:#fff;
          border-radius:12px; overflow:hidden; box-shadow:0 1px 2px rgba(17,24,39,.04); }
  th { background:#f1f5f9; color:#475569; font-weight:600; font-size:11px;
       padding:8px 10px; border-bottom:1px solid #e8ecf1; text-align:right; }
  th:nth-child(-n+2), td:nth-child(-n+2) { text-align:left; }
  td { padding:7px 10px; border-bottom:1px solid #f1f5f9; text-align:right;
       font-variant-numeric:tabular-nums; font-size:12.5px; }
  tr:nth-child(even) { background:#f8fafc; }
  tr:hover { background:#f1f5f9; }
  .pos { color:#dc2626; font-weight:600; } .neg { color:#16a34a; font-weight:600; } .flat { color:#64748b; }
  .meta { font-size:11px; color:#94a3b8; margin-top:8px; font-variant-numeric:tabular-nums; }
  /* 响应式 */
  @media (max-width: 768px) {
    table { display:block; overflow-x:auto; -webkit-overflow-scrolling:touch;
            border-radius:10px; font-size:11px; }
    th, td { padding:5px 6px; font-size:10.5px; white-space:nowrap; }
  }
</style></head><body>
<table id="tbl"></table>
<div class="meta" id="meta"></div>
<script>
const ROWS = __ROWS__;
const RT = {};
function money(v){ return (v==null||!isFinite(v)) ? '-' : v.toFixed(2); }
function cls(v){ return v>0?'pos':(v<0?'neg':'flat'); }
function build(){
  const hdr='<thead><tr><th>代码</th><th>选股日</th><th>买入价</th><th>现价</th><th>涨跌%</th></tr></thead>';
  let rt=0;
  const body = ROWS.map(function(r){
    const q = RT[r.q];
    if(q !== undefined) rt++;
    const px = q !== undefined ? q : r.buy;
    const chg = r.buy ? (px/r.buy-1)*100 : 0;
    return '<tr><td>'+r.c+'</td><td>'+r.date+'</td><td>'+money(r.buy)+'</td>'+
      '<td class="'+cls(chg)+'" style="font-weight:600">'+money(px)+'</td>'+
      '<td class="'+cls(chg)+'">'+(chg>0?'+':'')+chg.toFixed(2)+'%</td></tr>';
  }).join('');
  document.getElementById('tbl').innerHTML = hdr+'<tbody>'+body+'</tbody>';
  const t = new Date().toLocaleTimeString('zh-CN',{hour12:false});
  document.getElementById('meta').textContent = rt
    ? '🟢 '+rt+' 只实时报价 · '+t+' 更新 · 每秒自动刷新（与东方财富同一数据源）'
    : '⚪ 非交易时段或行情未连接 · 显示选股日收盘价';
}
function pull(){
  const ids = ROWS.map(function(r){return r.q;}).join(',');
  if(!ids) return;
  fetch('https://push2.eastmoney.com/api/qt/ulist.np/get?secids='+ids+
        '&fields=f1,f2,f3,f12,f13,f14&_='+Date.now(), {cache:'no-store'})
    .then(function(r){ return r.json(); })
    .then(function(j){
      const rows = (j && j.data && j.data.diff) || [];
      rows.forEach(function(q){
        if(q.f2 === '-' || q.f2 === undefined) return;
        const dec = Number(q.f1) || 2;
        RT[q.f13 + '.' + q.f12] = Number(q.f2) / Math.pow(10, dec);
      });
      try { build(); } catch(e) {}
    })
    .catch(function(){});
}
build(); pull(); setInterval(pull, 1000);
</script></body></html>
"""


def _em_secid(code) -> str:
    """代码 → 东方财富 secid (沪 1. / 深与北 0.), 与东财网页数据源一致"""
    c = str(code).strip().lower()
    if "." in c:
        return c
    if c.startswith("sh"):
        return "1." + c[2:]
    if c.startswith(("sz", "bj")):
        return "0." + c[2:]
    if c.startswith(("6", "9")):
        return "1." + c
    return "0." + c


# ==================== 回测结果展示 (参考掘金量化: 左图右指标 + 双 Tab) ====================

def _pc(v):
    """涨红跌绿"""
    if v is None:
        return "#64748b"
    return "#dc2626" if v > 0 else ("#16a34a" if v < 0 else "#64748b")


def _benchmark_align(curve):
    """策略净值 与 沪深300 对齐, 统一以起始日 = 100"""
    import data_manager
    c = curve[["date", "value"]].copy()
    c["date"] = pd.to_datetime(c["date"])
    c["strat"] = c["value"] / float(c["value"].iloc[0]) * 100
    try:
        bm = data_manager.get_benchmark_curve()
    except Exception:
        bm = None
    if bm is None or len(bm) == 0:
        return c, None
    b = bm.copy()
    b["date"] = pd.to_datetime(b["date"])
    m = c.merge(b[["date", "close"]], on="date", how="left")
    m["close"] = m["close"].ffill().bfill()
    if pd.isna(m["close"].iloc[0]) or float(m["close"].iloc[0]) == 0:
        return c, None
    m["bench"] = m["close"] / float(m["close"].iloc[0]) * 100
    return m, b


def _bench_extra(m):
    """基准相关指标: 基准收益/年化/波动/回撤/Beta/信息比率"""
    out = {}
    if m is None or "bench" not in getattr(m, "columns", []):
        return out
    if len(m) < 5:
        return out
    rs = m["strat"].pct_change().dropna().reset_index(drop=True)
    rb = m["bench"].pct_change().dropna().reset_index(drop=True)
    n = int(min(len(rs), len(rb)))
    if n < 5:
        return out
    rs, rb = rs.iloc[:n], rb.iloc[:n]
    last = float(m["bench"].iloc[-1]) / 100
    years = max(n / 252.0, 1e-6)
    out["bench_return"] = (last - 1) * 100
    out["bench_cagr"] = (last ** (1 / years) - 1) * 100
    var_b = rb.var()
    if var_b and var_b > 0:
        out["beta"] = float(rs.cov(rb) / var_b)
    diff = rs - rb
    sd = diff.std()
    out["ir"] = float(diff.mean() / sd * (252 ** 0.5)) if sd and sd > 0 else None
    out["vol"] = float(rs.std() * (252 ** 0.5) * 100)
    bn = m["bench"] / 100
    out["bench_mdd"] = float((bn / bn.cummax() - 1).min() * 100)
    return out


def _metric_panel(p, e):
    """掘金风格指标面板 (22 项, 单栏紧凑)"""
    def _m(v):
        return "-" if v is None else f"{float(v):,.2f}"

    def _p(v):
        return "-" if v is None else f"{float(v):+.2f}%"

    init_v = float(p.get("initial_capital", 0) or 0)
    final_v = float(p.get("final_value", 0) or 0)
    pnl = final_v - init_v
    ret = p.get("total_return")
    cagr = p.get("cagr")
    bench_ret = e.get("bench_return")
    beta = e.get("beta")
    alpha = None
    treynor = None
    if beta is not None and e.get("bench_cagr") is not None and cagr is not None:
        alpha = cagr - beta * e["bench_cagr"]
    if beta not in (None, 0) and cagr is not None:
        treynor = cagr / beta
    excess = None if (ret is None or bench_ret is None) else ret - bench_ret

    rows = [
        ("期初资产", _m(init_v), "#1f2430"),
        ("期末资金", _m(final_v), "#1f2430"),
        ("累计盈亏", _m(pnl), _pc(pnl)),
        ("累计手续费", _m(p.get("total_cost", 0)), "#7b8494"),
        ("基准收益率", _p(bench_ret), _pc(bench_ret)),
        ("策略收益率", _p(ret), _pc(ret)),
        ("年化收益率", _p(cagr), _pc(cagr)),
        ("超额收益率", _p(excess), _pc(excess)),
        ("最大回撤", _p(p.get("max_drawdown")), "#16a34a"),
        ("基准最大回撤", _p(e.get("bench_mdd")), "#16a34a"),
        ("胜率", "-" if p.get("win_rate") is None else f"{float(p['win_rate']):.2f}%", "#1f2430"),
        ("盈亏比", "-" if not p.get("profit_factor") else f"{float(p['profit_factor']):.2f}", "#1f2430"),
        ("夏普比率", "-" if p.get("sharpe") is None else f"{float(p['sharpe']):.2f}", "#1f2430"),
        ("索提诺比率", "-" if p.get("sortino") is None else f"{float(p['sortino']):.2f}", "#1f2430"),
        ("卡玛比率", "-" if p.get("calmar") is None else f"{float(p['calmar']):.2f}", "#1f2430"),
        ("收益波动率", _p(e.get("vol")), "#1f2430"),
        ("贝塔 Beta", "-" if beta is None else f"{beta:.2f}", "#1f2430"),
        ("阿尔法 Alpha", _p(alpha), _pc(alpha)),
        ("信息比率", "-" if e.get("ir") is None else f"{e['ir']:.2f}", "#1f2430"),
        ("特雷诺比率", _p(treynor), _pc(treynor)),
        ("交易天数", f"{int(p.get('days', 0) or 0)}", "#1f2430"),
        ("成交笔数", f"{int(p.get('trades_count', 0) or 0)}", "#1f2430"),
    ]
    html = "".join(
        '<div style="display:flex;justify-content:space-between;align-items:baseline;'
        'padding:5px 4px;border-bottom:1px solid #f1f5f9;font-size:12.5px;'
        'font-family:Inter,-apple-system,"PingFang SC","Microsoft YaHei",sans-serif">'
        f'<span style="color:#64748b;font-weight:500">{k}</span>'
        f'<span style="font-weight:600;color:{c};font-variant-numeric:tabular-nums;'
        f'letter-spacing:-0.005em">{v}</span></div>'
        for k, v, c in rows)
    st.markdown(
        '<div style="background:#fff;border:1px solid #e8ecf1;border-radius:12px;'
        'padding:10px 14px;box-shadow:0 1px 2px rgba(17,24,39,.04)">' + html + '</div>',
        unsafe_allow_html=True)


def _chart_equity(m):
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=m["date"], y=m["strat"], name="策略收益",
                             line=dict(width=2.2, color="#1FA879")))
    if "bench" in m.columns:
        fig.add_trace(go.Scatter(x=m["date"], y=m["bench"], name="沪深300",
                                 line=dict(width=1.6, color="#3b82f6")))
        fig.add_trace(go.Scatter(x=m["date"], y=m["strat"] - m["bench"], name="超额收益",
                                 line=dict(width=1.3, color="#f59e0b", dash="dot")))
    fig.update_layout(
        height=260, margin=dict(l=10, r=10, t=30, b=10),
        paper_bgcolor="#ffffff", plot_bgcolor="#ffffff",
        yaxis_title=dict(text="累计收益 (起点=100)", font=dict(size=11, color="#64748b")),
        font=dict(family="Inter,-apple-system,'PingFang SC','Microsoft YaHei',sans-serif",
                  size=11, color="#475569"),
        hovermode="x unified",
        hoverlabel=dict(bgcolor="#1a1f2e", font_size=12, font_family="Inter,-apple-system,'PingFang SC','Microsoft YaHei',sans-serif"),
        legend=dict(orientation="h", y=1.18, x=0, font=dict(size=11, color="#475569")),
        xaxis=dict(showgrid=False, showline=True, linecolor="#e8ecf1",
                   tickfont=dict(size=10, color="#94a3b8")),
        yaxis=dict(showgrid=True, gridcolor="#f1f5f9", showline=False, zeroline=False,
                   tickfont=dict(size=10, color="#94a3b8")),
    )
    st.plotly_chart(fig, use_container_width=True)


def _chart_drawdown(m):
    fig = go.Figure()
    ds = (m["strat"] / m["strat"].cummax() - 1) * 100
    fig.add_trace(go.Scatter(x=m["date"], y=ds, name="策略回撤",
                             line=dict(width=1.6, color="#dc2626"),
                             fill="tozeroy", fillcolor="rgba(220,38,38,0.10)"))
    if "bench" in m.columns:
        db = (m["bench"] / m["bench"].cummax() - 1) * 100
        fig.add_trace(go.Scatter(x=m["date"], y=db, name="沪深300回撤",
                                 line=dict(width=1.4, color="#3b82f6")))
    fig.update_layout(
        height=200, margin=dict(l=10, r=10, t=30, b=10),
        paper_bgcolor="#ffffff", plot_bgcolor="#ffffff",
        yaxis_title=dict(text="回撤 %", font=dict(size=11, color="#64748b")),
        font=dict(family="Inter,-apple-system,'PingFang SC','Microsoft YaHei',sans-serif",
                  size=11, color="#475569"),
        hovermode="x unified",
        hoverlabel=dict(bgcolor="#1a1f2e", font_size=12, font_family="Inter,-apple-system,'PingFang SC','Microsoft YaHei',sans-serif"),
        legend=dict(orientation="h", y=1.22, x=0, font=dict(size=11, color="#475569")),
        xaxis=dict(showgrid=False, showline=True, linecolor="#e8ecf1",
                   tickfont=dict(size=10, color="#94a3b8")),
        yaxis=dict(showgrid=True, gridcolor="#f1f5f9", showline=False, zeroline=False,
                   tickfont=dict(size=10, color="#94a3b8"), tickformat=".1f"),
    )
    st.plotly_chart(fig, use_container_width=True)


def _chart_daily(m):
    rs = m["strat"].pct_change() * 100
    fig = go.Figure()
    fig.add_trace(go.Bar(x=m["date"], y=rs, name="策略日收益",
                         marker_color=["#dc2626" if v > 0 else "#16a34a" for v in rs.fillna(0)],
                         marker_line_width=0))
    if "bench" in m.columns:
        rb = m["bench"].pct_change() * 100
        fig.add_trace(go.Scatter(x=m["date"], y=rb, name="沪深300日收益",
                                 line=dict(width=1.2, color="#3b82f6")))
    fig.update_layout(
        height=200, margin=dict(l=10, r=10, t=30, b=10),
        paper_bgcolor="#ffffff", plot_bgcolor="#ffffff",
        yaxis_title=dict(text="日收益 %", font=dict(size=11, color="#64748b")),
        font=dict(family="Inter,-apple-system,'PingFang SC','Microsoft YaHei',sans-serif",
                  size=11, color="#475569"),
        hovermode="x unified",
        hoverlabel=dict(bgcolor="#1a1f2e", font_size=12, font_family="Inter,-apple-system,'PingFang SC','Microsoft YaHei',sans-serif"),
        bargap=0.15,
        legend=dict(orientation="h", y=1.22, x=0, font=dict(size=11, color="#475569")),
        xaxis=dict(showgrid=False, showline=True, linecolor="#e8ecf1",
                   tickfont=dict(size=10, color="#94a3b8")),
        yaxis=dict(showgrid=True, gridcolor="#f1f5f9", showline=False, zeroline=True,
                   zerolinecolor="#e8ecf1", zerolinewidth=1,
                   tickfont=dict(size=10, color="#94a3b8"), tickformat=".1f"),
    )
    st.plotly_chart(fig, use_container_width=True)


if page == "home":
    _render_top_nav(page)

    # ---- 全球主要市场: 浏览器端直连腾讯行情 (2 秒刷新, 不经 Streamlit 重绘) ----
    import data_manager
    import streamlit.components.v1 as _comp

    _JS_MARKETS = [
        ("上证指数", "A股", "🇨🇳", "1.000001"),
        ("深证成指", "A股", "🇨🇳", "0.399001"),
        ("沪深300", "A股", "🇨🇳", "1.000300"),
        ("创业板指", "A股", "🇨🇳", "0.399006"),
        ("恒生指数", "港股", "🇭🇰", "100.HSI"),
        ("恒生科技", "港股", "🇭🇰", "124.HSTECH"),
        ("标普500", "美股", "🇺🇸", "100.SPX"),
        ("纳斯达克", "美股", "🇺🇸", "100.NDX"),
        ("道琼斯", "美股", "🇺🇸", "100.DJIA"),
        ("COMEX黄金", "商品", "🥇", "101.GC00Y"),
        ("WTI原油", "商品", "🛢️", "102.CL00Y"),
        ("美元/人民币", "汇率", "💱", "133.USDCNH"),
    ]
    _fb_map = {}
    try:
        for _m in (data_manager.get_global_markets() or []):
            _fb_map[_m["name"]] = _m
    except Exception:
        pass
    _items = []
    for _nm, _gp, _fl, _secid in _JS_MARKETS:
        _fb = _fb_map.get(_nm) or {}
        _items.append({
            "name": _nm, "group": _gp, "flag": _fl, "code": _secid,
            "price": _fb.get("price") or 0,
            "pct": _fb.get("pct") if _fb.get("pct") is not None else 0,
        })

    st.markdown(f"### {t('global_markets')}")
    _comp.html(
        _MARKET_CARDS_HTML.replace("__ITEMS__", json.dumps(_items, ensure_ascii=False)),
        height=252, scrolling=False)

    st.divider()

    # ---- 新闻摘要 (默认折叠, 进「📰 资讯中心」看全部) ----
    with st.expander(f"📰 {t('news_summary')}", expanded=False):
        try:
            all_news = news_fetcher.get_recent_news(limit=10)
            _perr = news_fetcher.last_proxy_error()
            if _perr:
                st.warning(f"⚠️ {_perr}（可在「⚙️ 参数设置 → 网络/代理」修改）")
            if all_news:
                # 来源配色 & 图标 (卡片圆形标识)
                _src_meta = {
                    "东方财富": ("📰", "#dc2626"),
                    "财联社": ("🔔", "#0ea5e9"),
                    "新浪财经": ("📊", "#ca8a04"),
                    "同花顺": ("💡", "#16a34a"),
                    "百度财经": ("💡", "#16a34a"),
                    "百度财经日历": ("💡", "#16a34a"),
                }
                _default_icon, _default_color = ("📌", "#64748b")

                def _rel_time(tm: str) -> str:
                    try:
                        from datetime import datetime
                        dt = datetime.strptime(tm[:19], "%Y-%m-%d %H:%M:%S")
                        delta = datetime.now() - dt
                        mins = int(delta.total_seconds() / 60)
                        if mins < 1:
                            return "刚刚"
                        if mins < 60:
                            return f"{mins}分钟前"
                        hrs = mins // 60
                        if hrs < 24:
                            return f"{hrs}小时前"
                        days = hrs // 24
                        return f"{days}天前"
                    except Exception:
                        return tm[:16] if tm else ""

                news_items = all_news[:6]

                # 中文模式下, 把英文新闻的标题+正文翻译成中文 (并发, 带缓存)
                if lang() == "zh":
                    try:
                        from news_fetcher import translate_to_zh
                        from concurrent.futures import ThreadPoolExecutor

                        def _trans_one(n):
                            n["title"] = translate_to_zh(n.get("title", ""))
                            n["content"] = translate_to_zh(n.get("content", ""))
                            return n

                        with ThreadPoolExecutor(max_workers=6) as ex:
                            news_items = list(ex.map(_trans_one, news_items))
                    except Exception:
                        pass

                n_cols = 2
                cols = st.columns(n_cols)
                for i, n in enumerate(news_items):
                    title = " ".join((n.get("title") or "").strip().split())
                    src = n.get("source") or ""
                    tm = n.get("source_time") or ""
                    content = " ".join((n.get("content") or "").split())
                    if len(content) > 110:
                        content = content[:110] + "…"
                    symbol = (n.get("symbol") or "").strip()
                    link = (n.get("link") or "").strip()
                    icon, color = _src_meta.get(src, (_default_icon, _default_color))
                    rel = _rel_time(tm)

                    sym_tag = f'<span class="sym-tag">{symbol}</span>' if symbol else ""

                    title_class = "news-card-title"
                    if link:
                        title_html = (f'<a href="{link}" target="_blank" rel="noopener noreferrer"'
                                      f' class="{title_class}">{title}</a>')
                    else:
                        title_html = f'<div class="{title_class}">{title}</div>'

                    card = f'''
<div class="news-card">
  <div class="news-card-head">
    <div class="news-card-icon" style="background:{color}15;color:{color};">{icon}</div>
    <div class="news-card-meta">
      <div class="src" style="color:{color};">{src}{sym_tag}</div>
      <div class="time">{rel}</div>
    </div>
  </div>
  {title_html}
  <div class="news-card-content">{content}</div>
</div>'''
                    with cols[i % n_cols]:
                        st.markdown(card, unsafe_allow_html=True)
            else:
                st.caption("暂无新闻，Worker 每 2 小时抓取一次。" if lang() == "zh"
                           else "No news yet. Worker fetches every 2 hours.")
        except Exception:
            pass

    st.divider()

    # ---- 策略绩效概览 (可展开, 数据来自缓存) ----
    with st.expander(f"📊 {t('strat_perf')}", expanded=False):
        st.caption(f"{t('dash_range')}: {backtest_start.strftime('%Y-%m-%d')} ~ "
                   f"{backtest_end.strftime('%Y-%m-%d')}")

        _miss = [k for k in DASH_STRATEGIES
                 if not data_store.get_backtest_cache(
                     bt_cache_key(k, initial_capital, hold_days, top_n, limit_up_pct,
                                  backtest_start, backtest_end, DATA_FINGERPRINT,
                                  _cost_str())[1])]
        if _miss:
            feat_data = require_data()
            _n = len(feat_data)
        else:
            feat_data = {}
            try:
                _n = data_store.coverage_stats("daily_bar")["symbols"]
            except Exception:
                _n = 0

        with st.spinner(t("running_bt")):
            results = {}
            for key in DASH_STRATEGIES:
                try:
                    results[key] = cached_backtest(key, feat_data, initial_capital, hold_days, top_n, limit_up_pct,
                                                     backtest_start, backtest_end, DATA_FINGERPRINT)
                except Exception as e:
                    st.error(f"{key} 回测失败: {e}")

        st.subheader(t("perf_compare"))
        col_perf = st.columns(len(results))
        for i, (name, r) in enumerate(results.items()):
            p = r["performance"]
            with col_perf[i]:
                st.markdown(f"**{name}**")
                m1, m2 = st.columns(2)
                m1.metric(t("total_return"), f"{p['total_return']:+.1f}%")
                m2.metric("CAGR", f"{p['cagr']:+.1f}%")
                m3, m4 = st.columns(2)
                m3.metric(t("max_drawdown"), f"{p['max_drawdown']:.1f}%")
                m4.metric(t("sharpe"), f"{p['sharpe']:.2f}")
                m5, m6 = st.columns(2)
                m5.metric(t("final_value"), fmt_money(p['final_value']))
                m6.metric(t("hit_rate"), f"{p['hit_rate']:.1f}%")

        # 资金曲线
        st.subheader(t("equity_curve"))
        fig = go.Figure()
        for name, r in results.items():
            curve = r["curve"]
            init_val = curve["value"].iloc[0]
            curve_norm = curve["value"] / init_val * 100
            fig.add_trace(go.Scatter(
                x=curve["date"], y=curve_norm, mode="lines", name=name, line=dict(width=2)))
        fig.update_layout(template="plotly_white", height=350, margin=dict(l=0, r=20, t=20, b=0),
                          yaxis_title=t("equity_axis"), xaxis_title="", hovermode="x unified")
        fig.update_xaxes(showgrid=False)
        fig.update_yaxes(showgrid=True, gridcolor="#eef0f4")
        st.plotly_chart(fig, use_container_width=True)


# --- 📰 资讯详情页 (独立页面) ---
elif page == "news":
    _render_top_nav("news", subtitle=t("news_sub"))

    try:
        all_news = news_fetcher.get_recent_news(limit=200)
    except Exception:
        all_news = []

    if not all_news:
        st.info("暂无新闻数据。Worker 每 120 分钟抓取一次，首次使用请稍等。")
    else:
        # 侧边筛选
        sources = sorted(set(n.get("source", "") for n in all_news))
        with st.sidebar:
            src_filter = st.multiselect("来源筛选", sources, default=sources)
        filtered = [n for n in all_news if n.get("source", "") in src_filter] if src_filter else all_news

        st.caption(f"共 {len(filtered)} 条资讯 · 来自 {len(set(n.get('source','') for n in filtered))} 个源")

        # 中文模式下, 翻译英文新闻标题 (仅标题, 避免每日翻译额度超限; 正文保留原文)
        if lang() == "zh" and filtered:
            try:
                from news_fetcher import translate_to_zh
                from concurrent.futures import ThreadPoolExecutor
                # 只翻译前 60 条英文标题, 兼顾速度与额度
                to_trans = [n for n in filtered[:60]
                            if n.get("title") and sum(1 for c in n["title"] if '\u4e00' <= c <= '\u9fff') == 0]
                with ThreadPoolExecutor(max_workers=8) as ex:
                    list(ex.map(lambda n: n.update(title=translate_to_zh(n["title"])), to_trans))
            except Exception:
                pass

        # 列表 + 展开详情
        for i, n in enumerate(filtered):
            title = n.get("title", "(无标题)")
            src = n.get("source", "")
            tm = (n.get("source_time") or n.get("date") or "")[:16]
            with st.expander(f"**{title}**  \n  `{src}` · {tm}"):
                c1, c2 = st.columns([3, 1])
                with c1:
                    content = n.get("content") or "(无正文)"
                    st.markdown(content)
                    link = (n.get("link") or "").strip()
                    if link:
                        st.link_button("🔗 查看原文", link, use_container_width=True)
                with c2:
                    st.markdown(f"🗂️ **来源**: {src}")
                    st.markdown(f"🕐 **时间**: {tm}")
                    if n.get("symbol"):
                        st.markdown(f"📈 **关联标的**: {n['symbol']}")
                    st.markdown(f"🆔 **ID**: {n.get('id')}")


# --- 🔬 策略回测 ---
elif page == "backtest":
    _render_top_nav("backtest", subtitle=t("bt_sub"))
    feat_data = require_data()

    # ---------- session_state 初始化 ----------
    for _k, _v in {
        "sd_cap_max": 30, "sd_ret20": 5, "sd_ma20": True, "sd_ma60": True,
        "sd_turn_min": 3, "sd_turn_max": 20,
        "ret_20_min": 5, "cap_max": 200, "require_ma20": True,
        "mc_cap_min": 15, "mc_cap_max": 200, "mc_weight_frac": 0.75,
        "mc_cap_chg_th": 0.04, "mc_rebal_th": 0.05, "mc_listed_yrs": 3.0,
        "mc_ma30_lookback": 5, "mc_universe": "", "mc_ma30_exit": True,
        "mc_ex_st": True, "mc_ex_ocf": True, "mc_ex_nonop": True,
        "mc_ex_nodiv": True, "mc_div_years": 3,
    }.items():
        st.session_state.setdefault(_k, _v)

    if "bt_strategy_choice" not in st.session_state:
        st.session_state["bt_strategy_choice"] = list(STRATEGIES.keys())[0]
    if "bt_open_settings" not in st.session_state:
        st.session_state["bt_open_settings"] = False

    if "_pending_pick" in st.session_state:
        st.session_state["bt_strategy_choice"] = st.session_state.pop("_pending_pick")

    strategy_choice = st.session_state["bt_strategy_choice"]

    # ---------- 实时行情 expander (保留) ----------
    try:
        _pk = data_store.get_recent_picks(limit=200)
        if not _pk.empty:
            _ld = _pk["pick_date"].iloc[0]
            _h = _pk[_pk["pick_date"] == _ld]
            _rows = [{"c": str(_r["symbol"]), "q": _em_secid(_r["symbol"]),
                      "date": str(_r["pick_date"])[:10],
                      "buy": round(float(_r["close"]), 2)} for _, _r in _h.iterrows()]
            import streamlit.components.v1 as _comp
            with st.expander(f"📈 当前持仓实时行情（{len(_rows)} 只 · 2 秒刷新）", expanded=False):
                _comp.html(_LIVE_HOLDINGS_HTML.replace(
                    "__ROWS__", json.dumps(_rows, ensure_ascii=False)),
                    height=44 + 29 * len(_rows), scrolling=False)
    except Exception:
        pass

    # ---------- 预计算策略概览缓存 ----------
    _all_names = list(STRATEGIES.keys())
    with st.spinner("加载策略概览..."):
        _cached = {}
        for name in _all_names:
            try:
                _cached[name] = cached_backtest(
                    name, feat_data, initial_capital, hold_days, top_n,
                    limit_up_pct, backtest_start, backtest_end, DATA_FINGERPRINT)
            except Exception:
                _cached[name] = None

    # ---------- 扁平 toolbar (一行: 策略胶囊 + 摘要chip + ⚙️设置 + 🚀开始回测) ----------
    _pill_cols = st.columns(len(_all_names) + 3)
    for i, name in enumerate(_all_names):
        with _pill_cols[i]:
            p = (_cached.get(name) or {}).get("performance", {})
            ret = p.get("total_return", 0) if p else 0
            dd = p.get("max_drawdown", 0) if p else 0
            _label = f"{name} · {ret:+.1f}%  回撤{dd:.1f}%"
            if name == strategy_choice:
                st.button(_label, use_container_width=True,
                          key=f"pill_{i}_{name}", disabled=True)
            else:
                if st.button(_label, use_container_width=True,
                             key=f"pill_{i}_{name}"):
                    st.session_state["bt_strategy_choice"] = name
                    st.rerun()

    _summary_parts = [f"{initial_capital // 10000}万"]
    if strategy_choice != "市值策略":
        _summary_parts.append(f"{top_n}只")
        _summary_parts.append(f"持有{hold_days}天·涨停{limit_up_pct:.1f}%")
    else:
        _mc_uni = list(st.session_state.get("mc_universe_list") or [])
        _summary_parts.append(f"标的池 {len(_mc_uni)} 只" if _mc_uni else "全市场选股")
    _summary_parts.append(
        f"{pd.Timestamp(backtest_start):%Y/%m/%d}~{pd.Timestamp(backtest_end):%Y/%m/%d}")
    _param_summary = " · ".join(_summary_parts)

    with _pill_cols[len(_all_names)]:
        st.markdown(
            f'<div style="background:#f1f5f9;border:1px solid #e2e8f0;'
            f'border-radius:6px;padding:6px 10px;font-size:.78rem;'
            f'color:#475569;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;'
            f'height:36px;display:flex;align-items:center;">🛠️ {_param_summary}</div>',
            unsafe_allow_html=True)

    with _pill_cols[len(_all_names) + 1]:
        if st.button("⚙️ 设置", use_container_width=True, key="bt_open_dlg"):
            st.session_state["bt_open_settings"] = True
            st.rerun()

    with _pill_cols[len(_all_names) + 2]:
        run_btn = st.button("🚀 开始回测", type="primary",
                            use_container_width=True, key="bt_run")

    # ---------- 设置弹窗 (st.dialog: 原生 modal, 点确定关闭) ----------
    @st.dialog("⚙️ 回测设置", width="large")
    def _bt_settings_dialog():
        st.markdown("**回测参数**")
        _c_p1, _c_p2 = st.columns(2)
        initial_capital = _c_p1.number_input(
            "初始资金(万)", 10, 10000, initial_capital // 10000, 10,
            format="%d", key="di_initial") * 10000
        if strategy_choice != "市值策略":
            top_n = _c_p2.slider("持仓数", 1, 30, top_n, key="di_topn")
            hold_days = _c_p1.slider("持有天数", 1, 30, hold_days, key="di_hold")
            limit_up_pct = _c_p2.slider("涨停阈值(%)", 5.0, 15.0, limit_up_pct, 0.1,
                                        key="di_limitup")
        else:
            _mc_uni2 = list(st.session_state.get("mc_universe_list") or [])
            if _mc_uni2:
                _c_p2.metric("持仓数（标的池决定）", len(_mc_uni2))
            else:
                _c_p2.metric("持仓数（选股决定）", "top_n")
        _dr = _c_p1.date_input("回测起点", value=backtest_start, key="di_start")
        _de = _c_p2.date_input("回测终点", value=backtest_end, key="di_end")

        st.markdown("---")
        st.markdown("**策略参数**")

        if strategy_choice == "小市值轮动·戴维斯双击":
            st.markdown("**选股条件**")
            st.slider("市值上限(亿)", 10, 100, key="sd_cap_max")
            st.slider("20日涨幅下限(%)", 2, 15, key="sd_ret20")
            st.checkbox("MA20确认", key="sd_ma20")
            st.checkbox("MA60确认", key="sd_ma60")
            st.slider("换手率下限(%)", 1, 10, key="sd_turn_min")
            st.slider("换手率上限(%)", 5, 40, key="sd_turn_max")
        elif strategy_choice == "动量+均线+小市值":
            st.markdown("**选股条件**")
            st.slider("20日涨幅下限(%)", 1, 20, key="ret_20_min")
            st.slider("市值上限(亿)", 50, 500, key="cap_max")
            st.checkbox("MA20以上", key="require_ma20")
        elif strategy_choice == "市值策略":
            st.markdown("**标的池**")
            if "_mc_symbols" not in st.session_state:
                _all = sorted(feat_data.keys())
                _ETF_PRE = ("sz159", "sh51", "sh52", "sh56", "sh58")
                _etf = [s for s in _all if any(s.startswith(p) for p in _ETF_PRE)]
                _equity = [s for s in _all if s not in _etf]
                st.session_state["_mc_symbols"] = _etf + _equity
            _opts = st.session_state["_mc_symbols"]
            st.multiselect(
                "从数据库已有行情的标的中选 (支持搜索)",
                options=_opts, key="mc_universe_list",
                placeholder="留空 → 按市值/财务条件全市场选股")
            st.markdown("**选股条件**")
            st.slider("市值下限(亿)", 5, 50, key="mc_cap_min")
            st.slider("市值上限(亿)", 100, 500, key="mc_cap_max")
            st.slider("上市年限(年)", 1.0, 10.0, key="mc_listed_yrs", step=0.5)
            st.markdown("**不良标的剔除**")
            st.checkbox("剔除 ST/*ST/退市风险", key="mc_ex_st")
            st.checkbox("剔除现金流背离", key="mc_ex_ocf")
            st.checkbox("剔除靠变卖资产等非正常经营盈利", key="mc_ex_nonop")
            st.checkbox("剔除长期不分红", key="mc_ex_nodiv")
            st.slider("分红回看年数", 1, 5, key="mc_div_years")
            st.markdown("**换股条件**")
            st.checkbox("MA30 拐头向下清仓（趋势止损）", key="mc_ma30_exit")
            st.slider("周线 MA30 斜率判断回看(周)", 2, 15, key="mc_ma30_lookback")
            st.markdown("**交易规则**")
            st.radio("调仓方式", options=["topk_drop", "deviation"],
                     format_func=lambda v: {"topk_drop": "排名缓冲（Topk-Drop，低换手）",
                                            "deviation": "偏离度再平衡（旧逻辑，高换手）"}[v],
                     key="mc_rebal_mode", horizontal=True)
            st.slider("排名缓冲(名)", 0, 10, key="mc_rank_buffer")
            st.slider("资金比例(买入占总市值)", 0.50, 1.00, key="mc_weight_frac", step=0.05)
            st.slider("市值变化调仓阈值(%)", 0.01, 0.15, key="mc_cap_chg_th", step=0.01)
            st.slider("持仓比例差再平衡阈值(%)", 0.02, 0.15, key="mc_rebal_th", step=0.01)

        # 底部按钮行
        dlg_c1, dlg_c2, dlg_c3 = st.columns([1, 1, 1])
        with dlg_c3:
            if st.button("✅ 确定", type="primary", use_container_width=True,
                         key="bt_dlg_ok"):
                st.session_state["initial_capital"] = initial_capital
                if strategy_choice != "市值策略":
                    st.session_state["top_n"] = top_n
                    st.session_state["hold_days"] = hold_days
                    st.session_state["limit_up_pct"] = limit_up_pct
                st.session_state["backtest_start"] = pd.Timestamp(_dr)
                st.session_state["backtest_end"] = pd.Timestamp(_de)
                st.session_state["bt_open_settings"] = False
                st.rerun()

    if st.session_state.get("bt_open_settings"):
        _bt_settings_dialog()

    # ---------- 结果区 (FULL WIDTH) ----------
    st.divider()

    _ss = st.session_state
    _use_strategy_class = False
    if strategy_choice == "小市值轮动·戴维斯双击":
        sp = {"cap_max": _ss["sd_cap_max"], "ret_20_min": _ss["sd_ret20"],
              "require_ma20": _ss["sd_ma20"], "require_ma60": _ss["sd_ma60"],
              "turn_min": _ss["sd_turn_min"], "turn_max": _ss["sd_turn_max"]}
    elif strategy_choice == "动量+均线+小市值":
        sp = {"ret_20_min": _ss["ret_20_min"], "cap_max": _ss["cap_max"],
              "require_ma20": _ss["require_ma20"]}
    elif strategy_choice == "市值策略":
        sp = {}
        _use_strategy_class = True
    else:
        sp = {"top_n": top_n}

    if run_btn:
        with st.spinner("🔄 运行回测..."):
            if _use_strategy_class:
                _uni = list(_ss.get("mc_universe_list") or [])
                _mc = MarketCapStrategy(
                    top_n=len(_uni) if _uni else top_n,
                    universe=_uni or None,
                    cap_min=_ss["mc_cap_min"],
                    cap_max=_ss["mc_cap_max"],
                    weight_fraction=_ss["mc_weight_frac"],
                    cap_change_threshold=_ss["mc_cap_chg_th"],
                    rebalance_threshold=_ss["mc_rebal_th"],
                    min_listed_years=_ss["mc_listed_yrs"],
                    exclude_st=_ss["mc_ex_st"],
                    exclude_ocf_diverge=_ss["mc_ex_ocf"],
                    exclude_nonop_profit=_ss["mc_ex_nonop"],
                    exclude_no_dividend=_ss["mc_ex_nodiv"],
                    dividend_years=_ss["mc_div_years"],
                    ma30_lookback=_ss.get("mc_ma30_lookback", 5),
                    ma30_exit=_ss.get("mc_ma30_exit", True),
                    rebalance_mode=_ss.get("mc_rebal_mode", "topk_drop"),
                    rank_buffer=_ss.get("mc_rank_buffer", 2),
                )
                result = run_backtest(
                    feat_data, _mc, strategy_params={},
                    initial_capital=initial_capital, top_n=_mc.n_slots,
                    hold_days=hold_days, limit_up_pct=limit_up_pct,
                    start_date=backtest_start, end_date=backtest_end,
                    cost_params=get_cost_params(), use_strategy_class=True)
            else:
                result = run_backtest_auto(
                    feat_data, STRATEGIES[strategy_choice], strategy_params=sp,
                    initial_capital=initial_capital, hold_days=hold_days, top_n=top_n,
                    limit_up_pct=limit_up_pct,
                    start_date=backtest_start, end_date=backtest_end,
                    cost_params=get_cost_params())
        st.success(f"✅ 回测完成! {result['performance'].get('trades_count', 0)} 笔交易")
        if _use_strategy_class:
            st.session_state["_mc_result"] = result
        try:
            data_store.save_run("backtest", strategy_choice, sp, result["performance"])
        except Exception:
            pass
    else:
        result = (st.session_state.get("_mc_result")
                  if _use_strategy_class else None) or _cached.get(strategy_choice)

    if result:
        p = result["performance"]
        curve = result.get("curve")
        if curve is None or len(curve) == 0:
            st.info("回测没有产生净值曲线")
        else:
            m, _bm = _benchmark_align(curve)
            e = _bench_extra(m)
            tab_perf, tab_trade, tab_hold = st.tabs(
                ["📈 策略绩效", "📋 交易明细", "📅 持仓明细"])
            with tab_perf:
                c_left, c_right = st.columns([2.3, 1])
                with c_left:
                    _chart_equity(m)
                    _chart_drawdown(m)
                    _chart_daily(m)
                with c_right:
                    _metric_panel(p, e)
                if _bm is None:
                    st.caption("⚠️ 基准指数暂不可用（沪深300 未取到），超额/Alpha/Beta 等指标留空")
            with tab_trade:
                fills = result.get("fills") or []
                if not fills:
                    st.info("本次回测没有成交记录")
                else:
                    fills_df = pd.DataFrame(fills)
                    col_order = ["date", "code", "direction", "price", "qty",
                                 "amount", "cost", "pnl", "cash_after", "reason"]
                    col_order = [c for c in col_order if c in fills_df.columns]
                    fills_show = fills_df[col_order].copy()
                    fills_show.columns = ["日期", "代码", "操作", "价格", "数量",
                                          "金额", "成本", "盈亏", "账户余额", "原因"]
                    fills_show["操作"] = fills_show["操作"].apply(
                        lambda d: f"{'买入' if d=='buy' else '卖出'}")
                    fills_show["原因"] = fills_show["原因"].apply(
                        lambda r: FILL_REASON_LABELS.get(r, r or "-"))
                    fills_show["价格"] = fills_show["价格"].apply(lambda v: f"{v:,.3f}")
                    fills_show["数量"] = fills_show["数量"].apply(lambda v: f"{v:,}")
                    fills_show["金额"] = fills_show["金额"].apply(lambda v: f"{v:,.2f}")
                    fills_show["成本"] = fills_show["成本"].apply(lambda v: f"{v:,.2f}")
                    fills_show["盈亏"] = fills_show["盈亏"].apply(
                        lambda v: f"{v:+,.2f}" if v != 0 else "-")
                    fills_show["账户余额"] = fills_show["账户余额"].apply(lambda v: f"{v:,.2f}")
                    fills_show = fills_show.sort_values("日期").reset_index(drop=True)
                    st.dataframe(fills_show, use_container_width=True,
                                 hide_index=True, height=440)
                    st.caption(f"共 {len(fills_show)} 笔成交 · 按时间顺序排列")
            with tab_hold:
                holds = result.get("holdings_log") or []
                if not holds:
                    st.info("本次回测没有持仓记录")
                else:
                    holds_df = pd.DataFrame(holds)
                    holds_df = holds_df.sort_values(
                        ["date", "market_value"], ascending=[False, False])
                    holds_df = holds_df.rename(columns={
                        "date": "日期", "code": "代码", "qty": "持股量",
                        "cost_price": "成本价", "close": "收盘价",
                        "market_value": "市值", "pnl": "浮盈"})
                    holds_df["浮盈"] = holds_df["浮盈"].apply(lambda v: f"{v:+,.2f}")
                    st.dataframe(holds_df, use_container_width=True,
                                 hide_index=True, height=500)
                    st.caption(f"共 {len(holds_df)} 条持仓快照 · 按日期倒序排列")
    else:
        st.info("👈 选好策略、点 ⚙️ 设置调参数，然后点 🚀 开始回测")

# --- 🎯 今日选股 ---
elif page == "picks":
    _render_top_nav("picks", subtitle=t("picks_sub"))
    feat_data = require_data()

    strategy_choice = st.selectbox("选择选股策略", list(STRATEGIES.keys()), index=0, key="today_strategy")
    select_fn = STRATEGIES[strategy_choice]

    # 策略触发: 该策略需要但库内缺失的数据, 交给后台 Worker 补
    _need = request_strategy_data(strategy_choice, list(feat_data.keys()),
                                 backtest_start, datetime.now())
    if _need["bars"] or _need["fundamental"]:
        st.caption(
            f"🧩 已向后台提交数据任务: K线 {_need['bars']} 只"
            + (" · 财务季报回填" if _need["fundamental"] else "")
            + " —— Worker 补完后刷新页面生效")

    with st.spinner("🔍 筛选中..."):
        picks = get_today_picks(feat_data, select_fn, strategy_params={"top_n": 50}, top_n=15)

    # 选股记录落库 (SQLite pick_record, 供后续跟踪胜率)
    if not picks.empty:
        try:
            _pid = data_store.save_run("pick", strategy_choice,
                                       {"top_n": 15}, {"count": len(picks)})
            data_store.save_picks(_pid, picks)
        except Exception:
            pass

    if picks.empty:
        st.warning("⚠️ 当前条件下没有找到符合要求的股票。试试调整策略参数或策略类型。")
    else:
        latest_date = picks["date"].iloc[0]
        st.info(f"📅 数据截止: {latest_date}  |  共找到 {len(picks)} 只候选")

        # 带样式的表格
        display = picks.copy()
        display = display[["code", "close", "ret_5", "ret_20", "turnover_pct", "cap_yi",
                          "above_ma20", "vol_expand", "industry", "pe_ttm",
                          "profit_yoy", "roe"]]
        display.columns = ["代码", "收盘", "5日涨幅%", "20日涨幅%", "换手%", "市值(亿)",
                          "MA20上方", "量能放大", "行业", "PE", "净利同比%", "ROE%"]

        # 格式化数值
        for col in ["5日涨幅%", "20日涨幅%", "换手%", "PE", "净利同比%", "ROE%"]:
            display[col] = pd.to_numeric(display[col], errors="coerce").round(2)
        display["市值(亿)"] = display["市值(亿)"].round(1)
        display["MA20上方"] = display["MA20上方"].map({1: "✅", 0: "—"})
        display["量能放大"] = display["量能放大"].map({1: "🔥", 0: "—"})

        # 实时行情表 (浏览器直连腾讯行情, 2 秒刷新; 涨红跌绿)
        _rows = []
        for _, _r in display.iterrows():
            def _num(_v):
                return None if pd.isna(_v) else float(_v)
            _rows.append({
                "c": str(_r["代码"]), "q": _em_secid(_r["代码"]), "c0": _num(_r["收盘"]),
                "r5": _num(_r["5日涨幅%"]), "r20": _num(_r["20日涨幅%"]),
                "to": _num(_r["换手%"]), "cap": _num(_r["市值(亿)"]),
                "pe": _num(_r["PE"]), "roe": _num(_r["ROE%"]),
                "ind": str(_r["行业"]) if pd.notna(_r["行业"]) else "-",
                "sig": (str(_r["MA20上方"]) + str(_r["量能放大"])).strip(),
            })
        import streamlit.components.v1 as _comp
        # 表格 + K线面板 (点击行在下方就地显示K线)
        _comp.html(_PICKS_HTML.replace("__ROWS__", json.dumps(_rows, ensure_ascii=False)),
                   height=48 + 29 * len(_rows) + 390, scrolling=False)

        # 市值分布可视化
        col1, col2 = st.columns(2)
        with col1:
            st.subheader("📊 市值分布")
            fig_cap = go.Figure(go.Bar(
                x=display["代码"], y=display["市值(亿)"],
                marker_color="#3b82f6",
            ))
            fig_cap.update_layout(template="plotly_white", height=300, margin=dict(l=0, r=20, t=20, b=0))
            st.plotly_chart(fig_cap, use_container_width=True)

        with col2:
            st.subheader("📊 动量分布 (20日涨幅)")
            colors = ["#ef4444" if v > 20 else "#f59e0b" if v > 0 else "#22c55e" for v in display["20日涨幅%"]]
            fig_ret = go.Figure(go.Bar(
                x=display["代码"], y=display["20日涨幅%"],
                marker_color=colors, text=[f"{v:.1f}%" for v in display["20日涨幅%"]],
                textposition="outside",
            ))
            fig_ret.update_layout(template="plotly_white", height=300, margin=dict(l=0, r=20, t=20, b=0))
            st.plotly_chart(fig_ret, use_container_width=True)


# --- 💼 投资组合 (模拟交易) ---
elif page == "portfolio":
    _render_top_nav("portfolio",
                    subtitle="模拟交易 · 以最新一次选股为基准建仓 · 跟踪浮盈浮亏")

    picks_df = data_store.get_recent_picks(limit=60)

    if picks_df.empty:
        st.info("📭 还没有选股记录。请到「🎯 今日选股」运行一次，选股结果会自动入库，这里会生成模拟持仓。")
    else:
        # 最新一次选股 = 当前模拟持仓 (等权分配资金)
        latest_date = picks_df["pick_date"].iloc[0]
        holdings = picks_df[picks_df["pick_date"] == latest_date].copy()
        latest_strategy = holdings["strategy"].iloc[0] if "strategy" in holdings else "-"

        st.markdown(f"#### 📌 当前模拟持仓  `选股日: {latest_date}`  ·  策略: `{latest_strategy}`")

        # 持仓浮盈: 浏览器端直连腾讯行情, 2 秒刷新 (不经 Streamlit 重绘)
        import streamlit.components.v1 as _comp
        _syms = holdings["symbol"].tolist()
        _latest = data_store.get_latest_closes(_syms)
        _cap_per = initial_capital / max(1, len(holdings))
        _rows = []
        for _, r in holdings.iterrows():
            _buy = float(r["close"])
            _info = _latest.get(r["symbol"])
            _last = float(_info[1]) if _info else _buy
            _shares = max(int(_cap_per / _buy / 100) * 100, 100)
            _rows.append({
                "c": str(r["symbol"]), "q": _em_secid(r["symbol"]),
                "date": str(r["pick_date"])[:10],
                "buy": round(_buy, 2), "last": round(_last, 2), "shares": int(_shares),
            })
        _comp.html(
            _POSITIONS_HTML.replace("__ROWS__", json.dumps(_rows, ensure_ascii=False)),
            height=340, scrolling=False)

        st.divider()

        # 历史选股记录
        st.markdown("#### 📜 历史选股记录 (最近 30 条)")
        hist = picks_df.head(30)[["pick_date", "symbol", "close", "ret_20",
                                   "cap_yi", "turnover_pct", "strategy"]].copy()
        hist.columns = ["选股日", "代码", "选股价", "20日涨幅%", "市值(亿)", "换手%", "策略"]
        for col in ["20日涨幅%", "换手%"]:
            hist[col] = pd.to_numeric(hist[col], errors="coerce").round(2)
        hist["市值(亿)"] = pd.to_numeric(hist["市值(亿)"], errors="coerce").round(1)
        st.dataframe(hist, use_container_width=True, hide_index=True, height=360)


# --- ✍️ 编写策略 (策略编辑器) ---
elif page == "author":
    _render_top_nav("author",
                    subtitle="自定义选股函数 · 基于因子数据编写策略 · 一键回测验证")

    st.info("💡 在下方编辑器中编写选股函数 `select(df)`，返回布尔 Series 表示是否选中。"
            "完成后点击「验证并回测」运行。")

    _tpl = (
        "import pandas as pd\n\n"
        "def select(df: pd.DataFrame) -> pd.Series:\n"
        "    # df 包含: code,close,ret_5,ret_20,turnover_pct,cap_yi,\n"
        "    #         above_ma20,vol_expand,pe_ttm,profit_yoy,roe 等列\n"
        "    cond = (\n"
        "        (df['ret_20'] > 5) &          # 20日涨幅 > 5%\n"
        "        (df['above_ma20'] == 1) &     # 站上 MA20\n"
        "        (df['cap_yi'] < 200) &        # 市值 < 200亿\n"
        "        (df['turnover_pct'].between(1, 15))  # 换手 1-15%\n"
        "    )\n"
        "    return cond\n"
    )

    code = st.text_area("策略代码", value=_tpl, height=320,
                        placeholder="def select(df) -> pd.Series: ...",
                        key="author_code")

    col_a, col_b = st.columns([1, 4])
    with col_a:
        run_it = st.button("▶️ 验证并回测", type="primary", use_container_width=True)
    with col_b:
        st.caption("策略会被加入临时策略库并运行回测；如有语法/运行错误会在下方显示。")

    if run_it:
        try:
            ns: dict = {}
            exec(code, ns)  # noqa: S102 — 用户自定义策略研究环境
            sel = ns.get("select")
            if not callable(sel):
                st.error("❌ 代码中未定义可调用的 `select(df)` 函数")
            else:
                # 注册为临时策略并运行回测
                from backtest_engine import STRATEGIES as _S
                _S["__custom__"] = sel
                feat_data = require_data()
                with st.spinner("🔄 运行自定义策略回测..."):
                    res = run_backtest(feat_data, sel,
                                       initial_capital=initial_capital,
                                       hold_days=hold_days, top_n=top_n,
                                       limit_up_pct=limit_up_pct,
                                       start_date=backtest_start,
                                       end_date=backtest_end)
                st.success("✅ 回测完成")
                _m = res.get("performance", {})
                mc1, mc2, mc3, mc4 = st.columns(4)
                mc1.metric("总收益", f"{_m.get('total_return', 0):.2f}%")
                mc2.metric("年化收益", f"{_m.get('cagr', 0):.2f}%")
                mc3.metric("最大回撤", f"{_m.get('max_drawdown', 0):.2f}%")
                mc4.metric("夏普比率", f"{_m.get('sharpe', 0):.2f}")
                # 资金曲线
                curve = res.get("curve")
                if curve is not None and not curve.empty:
                    st.line_chart(curve.set_index("date")["value"], height=300)
        except Exception as e:
            st.error(f"❌ 策略运行失败: {e}")
            st.exception(e)


# --- ⚙️ 参数设置 ---
elif page == "settings":
    _render_top_nav("settings", subtitle=t("settings_sub"))

    # ---- 界面语言 (全站只显示所选语言) ----
    st.subheader(f"🌐 {t('lang_label')}")
    _lang_options = {"zh": "中文", "en": "English"}
    _cur = lang()
    _pick = st.radio(t("lang_label"), list(_lang_options.keys()),
                     index=list(_lang_options.keys()).index(_cur),
                     format_func=lambda k: _lang_options[k],
                     horizontal=True, label_visibility="collapsed",
                     key="lang_radio")
    st.caption(t("lang_help"))
    if _pick != _cur:
        st.session_state["lang"] = _pick
        try:
            st.query_params["lang"] = _pick
        except Exception:
            pass
        st.rerun()

    st.divider()

    # ---- 交易成本 (全局, 影响所有策略回测) ----
    st.subheader("💰 交易成本")
    st.caption("A 股真实交易成本模型, 所有策略回测统一使用。调整后缓存自动失效。")

    # 读取当前值
    _cr = float(data_store.get_setting("cost_commission_rate", 0.00025))
    _cmin = float(data_store.get_setting("cost_commission_min", 5.0))
    _sr = float(data_store.get_setting("cost_stamp_tax_rate", 0.001))
    _tr = float(data_store.get_setting("cost_transfer_fee_rate", 0.00001))
    _sl = float(data_store.get_setting("cost_slippage_rate", 0.001))

    _tc1, _tc2 = st.columns(2)
    with _tc1:
        new_cr = st.number_input("佣金费率", value=_cr, min_value=0.0, max_value=0.01,
                                  step=0.0001, format="%.5f",
                                  help="默认 0.00025 (万2.5), 双向收取")
        new_sr = st.number_input("印花税率", value=_sr, min_value=0.0, max_value=0.01,
                                  step=0.0005, format="%.5f",
                                  help="默认 0.001 (0.1%), 仅卖出时收取")
        new_tr = st.number_input("过户费率", value=_tr, min_value=0.0, max_value=0.001,
                                  step=0.00001, format="%.6f",
                                  help="默认 0.00001 (0.001%), 双向收取")
    with _tc2:
        new_cmin = st.number_input("佣金最低(元)", value=_cmin, min_value=0.0, max_value=50.0,
                                    step=0.5, help="默认 5 元, 单笔佣金不足此值按此值收")
        new_sl = st.number_input("滑点率", value=_sl, min_value=0.0, max_value=0.05,
                                  step=0.0005, format="%.5f",
                                  help="默认 0.001 (0.1%), 单边滑点")

    # 保存 (任一变化)
    _cost_changed = any([
        new_cr != _cr, new_cmin != _cmin, new_sr != _sr,
        new_tr != _tr, new_sl != _sl,
    ])
    if _cost_changed:
        data_store.set_setting("cost_commission_rate", new_cr)
        data_store.set_setting("cost_commission_min", new_cmin)
        data_store.set_setting("cost_stamp_tax_rate", new_sr)
        data_store.set_setting("cost_transfer_fee_rate", new_tr)
        data_store.set_setting("cost_slippage_rate", new_sl)
        st.success("✅ 交易成本已保存, 回测将使用新参数")

    st.divider()
    st.subheader(t("bt_range"))
    date_range = st.date_input(
        t("pick_range"),
        value=(pd.Timestamp(st.session_state["backtest_start"]).date(),
               pd.Timestamp(st.session_state["backtest_end"]).date()),
        min_value=pd.Timestamp("2020-01-01").date(),
        max_value=datetime.now().date(),
        key="date_range_picker",
    )
    if isinstance(date_range, tuple) and len(date_range) == 2:
        st.session_state["backtest_start"] = pd.Timestamp(date_range[0]).strftime("%Y-%m-%d")
        st.session_state["backtest_end"] = pd.Timestamp(date_range[1]).strftime("%Y-%m-%d")

    # ---- 网络 / 代理 ----
    st.divider()
    st.subheader("🌐 网络 / 代理")
    cur_proxy = data_store.get_setting("http_proxy", "") or ""
    _mode_now = news_fetcher.get_proxy_mode()
    _mode_opts = ["auto", "always", "off"]
    _mode_label = {
        "auto": "🔄 自动（推荐）· 境内直连 / 仅境外走代理",
        "always": "🌍 全部走代理",
        "off": "⚪ 全部直连（不用代理）",
    }
    proxy_mode = st.radio(
        "代理模式", options=_mode_opts,
        format_func=lambda k: _mode_label[k],
        index=_mode_opts.index(_mode_now), horizontal=True, key="proxy_mode_input",
    )
    if proxy_mode != _mode_now:
        data_store.set_setting("proxy_mode", proxy_mode)
        st.success(f"代理模式已切换：{_mode_label[proxy_mode]}")
    st.caption(
        "自动模式下：东方财富/新浪/腾讯/同花顺/财联社等境内源（含 A 股行情与资讯）"
        "始终直连，不占用代理带宽；BBC/CNN/Reuters 等境外源才走代理。"
        "代理只作用于新闻抓取，内置行情与回测数据从不经过代理。"
    )
    _pc1, _pc2 = st.columns([5, 1])
    proxy_input = _pc1.text_input(
        "HTTP 代理 (仅在「自动」的境外源 或「全部走代理」时生效, 留空=直连)",
        value=cur_proxy, placeholder="http://192.168.1.100:7890",
        help="示例: http://127.0.0.1:7890 或 socks5://user:pass@proxy:1080",
        key="proxy_input",
    )
    if _pc2.button("测试连通", key="proxy_test"):
        try:
            from news_fetcher import proxy_alive, proxy_works
            _ok, _msg = proxy_alive(proxy_input, timeout=3.0)
            if not _ok:
                st.warning(f"⚠️ {_msg}")
            else:
                _ok2, _msg2 = proxy_works(proxy_input, timeout=6.0)
                st.success(_msg2) if _ok2 else st.warning(f"⚠️ {_msg2}")
        except Exception as e:
            st.warning(f"⚠️ 检测失败: {e}")
    if proxy_input != cur_proxy:
        data_store.set_setting("http_proxy", proxy_input.strip())
        try:
            from news_fetcher import proxy_works
            ok, msg = proxy_works(proxy_input.strip(), timeout=6.0)
        except Exception as e:
            ok, msg = True, f"代理已保存: {proxy_input or '(直连)'}（未能检测: {e}）"
        if ok:
            st.success(msg)
        else:
            st.warning(f"⚠️ {msg} —— 已保存但当前不可用，境外新闻会自动回退直连")

    # ---- 新闻源开关 ----
    st.divider()
    st.subheader("📰 新闻源管理")
    try:
        from news_fetcher import ALL_SOURCES, _load_enabled_sources, fetch_all_news
        enabled = _load_enabled_sources()

        st.caption("勾选启用的源, 点击「立即抓取」可测试。新增源只需在 news_fetcher.GLOBAL_RSS_SOURCES 添加一行。")

        # 国内源
        st.markdown("**🇨🇳 国内源**")
        dom_cols = st.columns(4)
        dom_keys = []
        for i, src in enumerate([s for s in ALL_SOURCES if s["type"].startswith("ak_")]):
            key = src["key"]
            val = dom_cols[i % 4].checkbox(
                src["name"], value=enabled.get(key, True),
                key=f"ns_{key}")
            dom_keys.append((key, val))

        # 全球源
        st.markdown("**🌍 全球 RSS 源**")
        gl_cols = st.columns(4)
        gl_keys = []
        for i, src in enumerate([s for s in ALL_SOURCES if s["url"]]):
            key = src["key"]
            val = gl_cols[i % 4].checkbox(
                f"{src['name']}", value=enabled.get(key, True),
                help=src["url"], key=f"ns_{key}")
            gl_keys.append((key, val))

        # 保存开关
        cfg = {k: v for k, v in dom_keys + gl_keys}
        import json as _json
        if _json.dumps(cfg, sort_keys=True) != _json.dumps(enabled, sort_keys=True):
            data_store.set_setting("news_sources", _json.dumps(cfg))
            st.success("新闻源开关已保存")

        # 立即抓取
        if st.button("🔄 立即抓取新闻", type="secondary"):
            from news_fetcher import proxy_alive
            _pok, _pmsg = proxy_alive()
            if not _pok:
                st.warning(f"⚠️ {_pmsg} —— 本次境外源将自动改直连抓取")
            with st.spinner("抓取中..."):
                counts = fetch_all_news(symbols=data_store.list_symbols()[:10],
                                        per_source_timeout=12)
            total = counts.pop("total", 0)
            good = {k: v for k, v in counts.items() if v > 0}
            bad = [k for k, v in counts.items() if v == 0]
            st.success(f"共 {total} 条 · 成功 {len(good)} 源 · 失败/空 {len(bad)} 源")
            if bad:
                st.caption(f"失败源: {', '.join(bad)} (若为境外源请检查代理配置)")
    except ImportError:
        st.caption("news_fetcher 模块不可用")
    except Exception as e:
        st.caption(f"新闻源加载失败: {e}")

    st.divider()
    st.subheader(t("param_preview"))
    pc1, pc2, pc3, pc4 = st.columns(4)
    pc1.metric(t("initial_capital"), fmt_money(st.session_state['initial_capital']))
    pc2.metric(t("hold_days"), f"{st.session_state['hold_days']}{t('tian')}")
    pc3.metric(t("top_n"), f"{st.session_state['top_n']}{t('zhi')}")
    pc4.metric(t("limit_up_pct"), f"{st.session_state['limit_up_pct']}%")
    st.caption(f"{t('dash_range')}: {st.session_state['backtest_start']} ~ {st.session_state['backtest_end']}")

    st.info("💡 修改参数后，切换到「量化大师」首页或「策略回测」页即可生效。"
            if lang() == "zh" else
            "💡 Changes take effect on the Home / Backtest page.")

    if st.button(t("restore_default"), type="secondary"):
        st.session_state.pop("lang_radio", None)
        for k, v in DEFAULTS.items():
            st.session_state[k] = v
        st.success("已恢复默认参数" if lang() == "zh" else "Defaults restored")
        st.rerun()


# --- 💾 数据管理 ---
elif page == "data":
    _render_top_nav("data", subtitle=t("data_sub"))

    # ---- 数据库概览 ----
    stats = data_store.db_stats()
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("股票数", stats["symbols"])
    c2.metric("K线条数", f"{stats['bars']:,}")
    c3.metric("策略运行", stats["runs"])
    c4.metric("选股记录", stats["picks"])
    st.caption(f"数据范围: {stats['first_date']} ~ {stats['last_date']} · WAL 模式 · 容器重建不丢数据")

    st.divider()
    st.subheader("📦 数据资产状态")

    cov = data_store.coverage_stats("daily_bar")
    fst = data_store.fundamental_stats()
    rt = data_store.load_realtime_latest()
    rt_latest = max((v.get("ts") or "" for v in rt.values()), default="")
    tasks = data_store.task_list(limit=50)
    pending_n = sum(1 for t in tasks if t["status"] in ("pending", "running"))

    a1, a2, a3, a4 = st.columns(4)
    a1.metric("日线覆盖标的", f"{cov['symbols']:,}", delta=f"{stats['bars']:,} 条K线")
    a2.metric("财务覆盖标的", f"{fst['symbols']:,}",
              delta=f"最新报告期 {fst['latest_report'] or '-'}")
    a3.metric("实时快照标的", f"{len(rt):,}", delta=rt_latest or "尚无")
    a4.metric("数据任务", f"{len(tasks)}", delta=f"{pending_n} 待处理")

    import data_manager
    req_lines = "\n".join(
        f"| {k} | {' + '.join(v['datasets'])} | {v.get('lookback_days', 250)} |"
        for k, v in data_manager.STRATEGY_REQUIREMENTS.items())
    st.markdown(f"""
**策略数据需求声明**（策略需要什么数据，系统就补什么数据）

| 策略 | 数据集 | 回看天数 |
|---|---|---|
{req_lines}
""")

    # ---- 任务队列 ----
    st.divider()
    st.subheader("🧩 后台数据任务队列")
    q1, q2 = st.columns([3, 1])
    with q2:
        if st.button("⚡ 立即补全缺失数据", use_container_width=True, type="primary"):
            n = 0
            try:
                pending = data_store.task_list(limit=50, status="pending")
                if not any(t["dataset"] == "daily_bar" for t in pending):
                    p = data_manager.plan_bars(data_store.list_symbols(),
                                               "20200101", datetime.now().strftime("%Y%m%d"))
                    need = sorted({s for syms in p["plan"].values() for s in syms})
                    if need:
                        data_store.task_create("daily_bar",
                                               {"symbols": need, "start": "20200101",
                                                "end": datetime.now().strftime("%Y%m%d")},
                                               f"手动补全 {len(need)} 只标的")
                        n += 1
                if fst["symbols"] < max(1, stats["symbols"]) * 0.5 \
                        and not any(t["dataset"] == "fundamental" for t in pending):
                    data_store.task_create("fundamental", {"quarters": 4}, "财务季报回填")
                    n += 1
                st.success(f"已提交 {n} 个任务，Worker 会自动执行" if n else "没有检测到数据缺口")
                st.rerun()
            except Exception as e:
                st.error(f"提交失败: {e}")
    with q1:
        if tasks:
            df_task = pd.DataFrame(tasks)[["id", "dataset", "status", "progress", "message", "created_at"]]
            df_task.columns = ["ID", "数据集", "状态", "进度", "说明", "创建时间"]
            st.dataframe(df_task.head(15), use_container_width=True, hide_index=True, height=280)
        else:
            st.caption("暂无任务。Worker 会在交易日盘后自动补全，策略运行时也会自动提交缺口任务。")

    # ---- 手动下载 (折叠) ----
    with st.expander("⬇️ 手动下载（一般无需使用，后台已自动补全）"):
        dl_col1, dl_col2 = st.columns([1, 2])
        with dl_col1:
            n_stocks = st.selectbox("下载股票数量", ["样本200", "样本500", "样本1000", "全部A股(~5400)"], index=0)
            n_map = {"样本200": 200, "样本500": 500, "样本1000": 1000, "全部A股(~5400)": None}
            dl_start = st.text_input("开始日期", "20200101")
            dl_end = st.text_input("结束日期", datetime.now().strftime("%Y%m%d"))
            dl_btn = st.button("🚀 下载A股", type="primary", use_container_width=True)
            etf_btn = st.button("🧧 下载全部ETF", use_container_width=True)
            csv_btn = st.button("📥 导入本地CSV缓存", use_container_width=True)

        with dl_col2:
            if dl_btn:
                limit = n_map[n_stocks]
                codes = [c for c, _ in data_store.get_main_board_codes(limit=limit)]
                st.info(f"开始下载 {len(codes)} 只A股 (含主板/创业板/科创板, 排除ST)...")
                prog = st.progress(0.0, text="准备中...")
                log_area = st.empty()

                def cb(done, total, symbol, rows):
                    prog.progress(done / total, text=f"{done}/{total} · {symbol} (+{rows}条)")
                    if done % 100 == 0 or done == total:
                        log_area.caption(f"最近完成: {symbol} ({rows} 条)")

                result = data_store.download_batch(codes, dl_start, dl_end, workers=8, progress_cb=cb)
                prog.progress(1.0, text="完成!")
                st.success(f"✅ 下载完成: {result['done']} 只 · 共入库 {result['ok_rows']:,} 条K线")
                st.cache_data.clear()
                st.rerun()

            if etf_btn:
                etfs = [s for s, _ in data_store.get_etf_codes()]
                st.info(f"开始下载 {len(etfs)} 只场内ETF...")
                prog = st.progress(0.0, text="准备中...")

                def etf_cb(done, total, symbol, rows):
                    prog.progress(done / total, text=f"{done}/{total} · {symbol}")

                result = data_store.download_batch(
                    etfs, dl_start, dl_end, workers=6, progress_cb=etf_cb,
                    fetch_fn=lambda s: data_store.download_etf_one(s, dl_start, dl_end))
                prog.progress(1.0, text="完成!")
                st.success(f"✅ ETF下载完成: {result['done']} 只 · 入库 {result['ok_rows']:,} 条")
                st.cache_data.clear()
                st.rerun()

            if csv_btn:
                n = data_store.import_csv_cache()
                st.success(f"✅ 从 CSV 导入 {n} 只股票到 SQLite")
                st.cache_data.clear()
                st.rerun()

    st.divider()
    st.subheader("📋 下载日志 (最近20)")
    try:
        conn = data_store.get_conn()
        df_log = pd.read_sql(
            "SELECT symbol AS 股票, status AS 状态, rows AS 条数, "
            "first_date AS 起始, last_date AS 截止, updated_at AS 更新时间 "
            "FROM download_log ORDER BY updated_at DESC LIMIT 20", conn)
        conn.close()
        if not df_log.empty:
            st.dataframe(df_log, use_container_width=True, hide_index=True)
        else:
            st.caption("暂无下载记录")
    except Exception as e:
        st.caption(f"无日志: {e}")

    st.divider()
    st.subheader("📰 新闻资讯")

    news_col1, news_col2 = st.columns([1, 4])
    with news_col1:
        news_btn = st.button("🔄 立即抓取新闻", type="primary", use_container_width=True)
        kline_btn = st.button("📈 增量更新K线", use_container_width=True)

    with news_col2:
        if news_btn:
            with st.spinner("抓取新闻中..."):
                res = news_fetcher.fetch_all_news(symbols=data_store.list_symbols()[:10])
                st.success(f"抓取完成: 东方财富 {res['eastmoney']} / 百度 {res['baidu']} / 财联社 {res['cls']} 条")
                st.rerun()

        if kline_btn:
            with st.spinner("更新K线中..."):
                end = datetime.now().strftime("%Y%m%d")
                prog = st.progress(0.0, text="准备...")
                def cb(d, t, s, n):
                    prog.progress(d/t if t else 0, text=f"{d}/{t} {s} (+{n})")
                res = data_store.update_all_bars("20200101", end, workers=8, progress_cb=cb)
                prog.progress(1.0)
                st.success(f"更新完成: {res['done']} 只股票, 新增 {res['rows']} 条K线")
                st.cache_data.clear()
                st.rerun()

    news = news_fetcher.get_recent_news(30)
    if news:
        df_news = pd.DataFrame(news)
        df_news = df_news[["date", "source", "symbol", "title"]]
        df_news.columns = ["日期", "来源", "个股", "标题"]
        st.dataframe(df_news, use_container_width=True, hide_index=True, height=300)
    else:
        st.caption("暂无新闻，点击「立即抓取新闻」")

    st.caption("💡 Worker 常驻: 交易日 15:35 后自动补当日K线 · 交易时段每 60 秒采样实时快照 · 周五刷新财务 · 新闻每 2 小时")

    st.divider()
    st.subheader("⏱️ 实时行情快照（模拟交易数据源）")
    if rt:
        rt_rows = []
        for sym, v in list(rt.items())[:200]:
            rt_rows.append({
                "代码": sym, "时间": v.get("ts"), "最新价": v.get("price"),
                "涨跌幅%": v.get("pct"), "换手%": v.get("turnover"),
                "成交额": v.get("amount"),
            })
        st.caption(f"共 {len(rt):,} 只标的 · 最新采集 {rt_latest} · 交易时段每 60 秒采样一次")
        st.dataframe(pd.DataFrame(rt_rows), use_container_width=True, hide_index=True, height=260)
    else:
        st.caption("尚无实时快照。交易时段 (9:15-15:05) Worker 会自动采集，无需手动操作。")

    st.divider()
    st.subheader("🧱 数据库分层设计")
    st.markdown("""
| 层 | 表 | 用途 |
|---|---|---|
| **行情历史** | `daily_bar` | 日线行情(前复权), 回测引擎的数据源 |
| **覆盖索引** | `data_coverage` | 每个标的已同步的区间, 只补缺口不重复下载 |
| **基本面** | `fundamental` | 季报: 营收/净利同比、ROE、毛利率、负债率、行业、PE/PB/市值 |
| **实时** | `realtime_quote` | 交易时段快照, 供模拟交易撮合 |
| **调度** | `data_task` | 策略提交的数据需求队列, Worker 消费执行 |
| **策略** | `strategy_run` / `pick_record` | 回测参数绩效 / 选股跟踪 |
| **资讯面** | `news` | 新闻/公告 + 多源去重 |
| **缓存** | `backtest_cache` | 回测结果持久化, 数据指纹变化自动失效 |

> 数据流: 策略声明需求 → `data_task` 入队 → Worker 按覆盖索引补缺口 → 落库 → 回测/选股读取。
    """)

    st.divider()
    st.subheader("🛠️ 危险操作")
    if st.button("🗑️ 清空全部数据", type="secondary"):
        import shutil
        shutil.rmtree(data_store.DATA_DIR, ignore_errors=True)
        data_store.init_db()
        st.success("✅ 数据库已清空重建")
        st.cache_data.clear()
        st.rerun()


# --- ❓ 帮助 ---
elif page == "help":
    _render_top_nav("help", subtitle=t("help_sub"))

    # 导航结构说明
    st.subheader("🧭 导航结构")
    st.markdown("""
侧边栏采用**一级菜单 + 二级菜单**结构（参考「量化大师」App）：

| 一级菜单 | 二级菜单 | 说明 |
|---|---|---|
| 🏠 **量化大师** | — | 首页：全球市场、资讯摘要、快捷入口、策略绩效概览 |
| 📊 **量化策略** | 🔬 策略回测 / 🎯 今日选股 / 💼 投资组合 | 策略研究与模拟交易 |
| 📰 **资讯** | — | 多源聚合新闻 |
| ⋯ **更多** | ⚙️ 参数设置 / 💾 数据管理 / ❓ 帮助 | 系统配置与运维 |
    """)

    st.divider()
    st.subheader("🚀 三分钟上手")
    st.markdown("""
| 步骤 | 操作 | 说明 |
|:---:|---|---|
| 1 | 等待数据就绪 | 容器启动后 Worker 自动自举全部A股 + ETF 日线（库为空时全量，之后只补缺口） |
| 2 | **🏠 量化大师** | 首页看全球市场实时行情、Top 资讯摘要，通过快捷入口直达各功能 |
| 3 | **🔬 策略回测** | 选定策略跑详细回测：资金曲线、交易明细、交易日历 |
| 4 | **🎯 今日选股** | 基于最新交易日因子，输出涨停潜力 Top 15，自动入库 |
| 5 | **💼 投资组合** | 以最新选股为基准模拟建仓，跟踪浮盈浮亏 |
| 6 | **⚙️ 参数设置** | 调整初始资金/持有天数/回测区间，实时生效 |
    """)

    st.divider()
    st.subheader("📖 功能页说明")
    hc1, hc2, hc3 = st.columns(3)
    with hc1:
        st.markdown('<div class="help-card"><h4>🏠 量化大师</h4><p>首页聚合全球主要市场（A股/港股/美股/商品/汇率）实时行情与涨跌，带国旗图标与红绿箭头；Top 资讯以可展开摘要呈现；顶部快捷入口直达选股/回测/组合等模块；策略绩效概览可折叠查看。</p></div>', unsafe_allow_html=True)
    with hc2:
        st.markdown('<div class="help-card"><h4>🔬 策略回测</h4><p>可微调单个策略的专属参数（如市值上限、换手率阈值）。回测完成后展示逐笔交易明细和按月交易日历——红=涨停命中日、橙=有交易、蓝=选股日。</p></div>', unsafe_allow_html=True)
    with hc3:
        st.markdown('<div class="help-card"><h4>🎯 今日选股</h4><p>用最近交易日数据跑一遍选股因子，输出最看好的 Top 15。每次选股自动写入 pick_record 表，供投资组合页跟踪胜率。</p></div>', unsafe_allow_html=True)

    hc4, hc5 = st.columns(2)
    with hc4:
        st.markdown('<div class="help-card"><h4>💼 投资组合</h4><p>以最新一次选股结果作为模拟持仓，等权分配资金（100 股取整），用库内最新收盘价计算浮盈浮亏。未计手续费/滑点，仅供研究。</p></div>', unsafe_allow_html=True)
    with hc5:
        st.markdown('<div class="help-card"><h4>🌍 全球市场</h4><p>首页自动抓取上证/深证/创业板/沪深300/恒生/恒生科技/道琼斯/纳斯达克/标普500/黄金/原油/美元人民币等 12 个标的，10 分钟缓存，单源失败不影响整体。</p></div>', unsafe_allow_html=True)

    st.divider()
    st.subheader("🧠 策略原理")
    with st.expander("💎 小市值轮动·戴维斯双击（基本面增强）", expanded=True):
        st.markdown("""
**戴维斯双击** = 盈利(EPS)提升 × 估值(PE)扩张，两者同时发生带来股价戴维斯双击。

- **高成长（第一击）** — 净利同比 > 20% 且营收同比 > 10%
- **低估值（第二击）** — 0 < PE_TTM < 所属行业中位 PE（有估值修复空间）
- **财务健康** — 资产负债率 < 70%，避免高杠杆暴雷
- **行业竞争力** — ROE 高于所属行业中位数
- **小市值弹性** — 市值上限默认 30 亿
- **趋势确认** — 20日涨幅 > 5%、站上 MA20/MA60、换手 3-20%、不缩量

财务数据按**公告日**取用（ann_date ≤ 回测日），避免未来函数；某日无可用财报时自动退化为纯动量选股。
        """)
    with st.expander("🏆 动量+均线+小市值（回测最优，推荐）"):
        st.markdown("""
- **20日涨幅 > 5%** — 已经有上涨趋势（动量启动）
- **价格在 MA20 上方** — 均线确认趋势有效
- **市值 < 200亿** — 保持弹性
- **换手率 1-15%** — 有人气但不过热

关键教训：**命中率 ≠ 赚钱**，这层的价值在于"涨停后不容易跌回去"。实际收益/回撤请以本页回测结果为准（随参数与区间变化）。
        """)
    with st.expander("小市值+高换手"):
        st.markdown("""
- **市值 < 100亿** — 极致小盘
- **换手率 > 3%** — 资金活跃

涨停命中率不低（17%），但回测亏钱（-16%）：选出的多是散户情绪票，追高后跌回去。
保留它作为对照组，警示"容易涨停"和"能赚钱"是两回事。
        """)
    with st.expander("📉 随机对照"):
        st.markdown("随机买入同数量股票作为基准线（命中率约 7%）。任何策略都必须显著跑赢它才有意义。")

    st.divider()
    st.subheader("❔ 常见问题")
    faqs = {
        "页面显示'数据库正在初始化'？": "Worker 正在后台自动下载 200 只股票，约 1-2 分钟。可到「💾 数据管理」页看下载日志，完成后刷新页面即可。",
        "数据多久更新一次？": f"每个交易日 {CLOSE_TIME_DISPLAY} 后自动补全当日K线；交易时段每 60 秒采集实时快照；每周五刷新财务季报；策略运行时还会自动提交缺失数据任务。",
        "回测结果可信吗？": "回测基于真实历史日线（前复权），采用事件驱动架构（行情→信号→订单→成交→持仓），T+1 开盘集合竞价成交（选股日收盘后产生信号，次一交易日 09:30 执行，消除前视偏差）。成本模型采用 A 股真实费率：佣金万2.5（双向、最低5元）、印花税卖出0.1%、过户费0.001%（双向）、滑点0.1%（买卖各单边）、冲击成本（订单占当日成交额>5%时按ratio^1.5计提）。涨跌停无法成交等极端约束仍未完全模拟，结果仅供研究，不构成投资建议。",
        "新闻源有失败怎么办？": "三个新闻源独立容错，单源失败不影响整体。东方财富个股新闻偶尔限流、财联社接口偶有变更，可在数据管理页重试。",
        "怎么改盘后补全时间？": "编辑 docker-compose.yml 中 CLOSE_HOUR / CLOSE_MINUTE（默认 15:35），然后 docker compose up -d 重建。",
        "策略需要什么数据？": "每个策略在 data_manager.STRATEGY_REQUIREMENTS 中声明数据集与回看天数。例如「小市值轮动·戴维斯双击」需要日线 + 财务季报（成长/估值/负债/ROE/行业），系统按需自动补全。",
        "财务数据怎么避免未来函数？": "回测时只使用公告日（ann_date）早于当前回测日期的财报，确保不会用到当时还没披露的业绩。",
        "换仓频率是什么逻辑？": "每周一（或首个交易日）收盘后选股，次一交易日 09:30 开盘换仓；持有满 hold_days 个交易日到期次日开盘卖出；中途涨停按 limit_up_pct 阈值识别后次日开盘止盈卖出。所有交易以开盘价加减 0.1% 滑点成交，并按 A 股真实费率扣除佣金/印花税/过户费。",
    }
    for q, a in faqs.items():
        with st.expander(q):
            st.markdown(a)

    st.divider()
    st.subheader("⚠️ 免责声明")
    st.warning("本系统仅用于量化研究与学习交流。股市有风险，历史回测不代表未来收益，据此实盘操作盈亏自负。")

st.divider()
st.caption("🔬 数据来自 akshare (新浪财经) · 仅供研究参考，不构成投资建议")
