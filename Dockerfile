# ============================================================
# 涨停猎手 Pro —— Docker 镜像 (最简版)
# 去掉所有 apt-get, 全部用 pip + python
# ============================================================
FROM python:3.13-slim

LABEL maintainer="tommy-quant"
LABEL description="托米量化 —— A股多策略量化平台"

WORKDIR /app

# pip 清华源
ENV PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
ENV PIP_TRUSTED_HOST=pypi.tuna.tsinghua.edu.cn
ENV PIP_DEFAULT_TIMEOUT=180

# 直接 pip 装所有东西 (无 apt)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY streamlit_app.py .
COPY backtest_engine.py .
COPY etf_rebalance.py .
COPY fast_backtest.py .
COPY price_limits.py .
COPY robust_backtest.py .
COPY data_store.py .
COPY bt_best_state.py .
COPY data_manager.py .
COPY news_fetcher.py .
COPY update_worker.py .
COPY perf_report.py .
COPY doh_patch.py .
COPY htbr_source.py .
COPY exchange_source.py .
COPY ths_source.py .
COPY world_map_countries.py .
COPY vectorbt_etf_backtest.py .
COPY docs/vectorbt_etf_strategy.md ./docs/vectorbt_etf_strategy.md
COPY vendor ./vendor
COPY entrypoint.sh .
COPY .streamlit ./.streamlit

RUN chmod +x entrypoint.sh && mkdir -p cache data

ENV AUTO_DOWNLOAD_COUNT=0
ENV CLOSE_HOUR=15
ENV CLOSE_MINUTE=35
ENV RT_INTERVAL_SEC=60
ENV NEWS_INTERVAL_MIN=120
ENV CHECK_INTERVAL=5
ENV FUNDAMENTAL_WEEKDAY=4

EXPOSE 8501

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8501/_stcore/health').read()" || exit 1

CMD ["./entrypoint.sh"]
