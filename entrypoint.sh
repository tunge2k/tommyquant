#!/bin/sh
set -e

echo "========================================"
echo " 托米量化 · TommyQuant 启动"
echo " 盘后补全: 交易日 ${CLOSE_HOUR:-15}:${CLOSE_MINUTE:-35} | 实时采样: ${RT_INTERVAL_SEC:-60}s"
echo "========================================"

# 1. 后台启动每日更新 worker
python update_worker.py &
WORKER_PID=$!
echo "worker PID=$WORKER_PID"

# 2. 前台启动 Streamlit (主进程)
exec streamlit run streamlit_app.py \
    --server.port=8501 \
    --server.address=0.0.0.0 \
    --server.headless=true \
    --browser.gatherUsageStats=false
