# -*- coding: utf-8 -*-
"""绩效报告 —— 基于 QuantStats 生成 HTML tearsheet

QuantStats 官方包 (0.0.64) 锁定 numpy<2.0, 在 Python 3.13 上无法安装;
这里使用其 numpy2 兼容分支 quantstats-lumi, 模块名与 API 保持一致。
"""
import os

import pandas as pd


def _import_qs():
    """量化绩效库: 官方包模块名为 quantstats, lumi 分支为 quantstats_lumi"""
    try:
        import quantstats as qs
        return qs
    except ImportError:
        import quantstats_lumi as qs
        return qs


def available() -> bool:
    """QuantStats 是否可用 (镜像未安装时界面降级提示)"""
    try:
        _import_qs()
        return True
    except Exception:
        return False


def build_tearsheet(m: pd.DataFrame, out_path: str,
                    title: str = "托米量化 · 策略绩效报告") -> tuple[bool, str]:
    """生成 HTML 绩效报告

    m: 含 date / strat(净值, 起点=100) / bench(可选) 的 DataFrame
    返回 (是否成功, 路径或错误信息)
    """
    if m is None or len(m) < 30:
        return False, "净值曲线太短，至少需要 30 个交易日"
    if "strat" not in getattr(m, "columns", []):
        return False, "缺少净值列，无法生成报告"

    try:
        import matplotlib
        matplotlib.use("Agg")          # 容器内无显示设备
        import matplotlib.pyplot as plt
        plt.rcParams["font.sans-serif"] = ["DejaVu Sans"]
        plt.rcParams["axes.unicode_minus"] = False
        qs = _import_qs()
    except Exception as exc:
        return False, f"QuantStats 不可用: {exc}"

    df = m[["date", "strat"] + (["bench"] if "bench" in m.columns else [])].copy()
    df["date"] = pd.to_datetime(df["date"])
    df = df.drop_duplicates("date").set_index("date").sort_index()
    rets = df["strat"].pct_change().dropna()
    if len(rets) < 30:
        return False, "有效收益率样本不足 30 个交易日"

    bm = None
    if "bench" in df.columns and df["bench"].notna().sum() > 30:
        bm = df["bench"].pct_change().dropna()

    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    try:
        qs.reports.html(rets, benchmark=bm, output=out_path, title=title,
                        download_filename=os.path.basename(out_path))
    except Exception as exc:
        return False, f"生成失败: {exc}"

    if not os.path.exists(out_path):
        return False, "生成失败: 未产生输出文件"
    return True, out_path
