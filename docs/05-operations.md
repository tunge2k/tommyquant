# 05 · 部署与运维

> 文档基线：2026-11-17
> 配套阅读：[02-architecture.md](./02-architecture.md) 第 1、10 节。

---

## 1. 部署形态

| 形态 | 用途 | 入口 |
|---|---|---|
| 本地 Python | 开发联调 | `py -3.13 -m streamlit run streamlit_app.py` |
| 本地 Docker | 验证镜像 | `docker compose up -d --build` |
| NAS（生产） | 日常使用 | `http://<NAS_IP>:8501`（地址写在 `nas_conf.py`，不入库） |

生产事实：

- 容器名 `tommyquant`，镜像 `tommyquant:latest`。
- 远端目录 `/volume1/docker/limit-up-hunter`。
- 端口 `8501:8501`；`restart: unless-stopped`；healthcheck 访问 `/_stcore/health`。
- 卷：`./data:/app/data`（SQLite）、`./cache:/app/cache`（因子矩阵/CSV）。
- 容器 DNS：`223.5.5.5`、`119.29.29.29`、`8.8.8.8`。

---

## 2. 部署步骤

### 2.1 本地 Docker

```powershell
docker compose up -d --build
# 打开 http://localhost:8501
docker compose down          # 停止（保留数据卷）
```

### 2.2 NAS 增量部署

```powershell
# 上传改动文件 → 远端重建容器 → 轮询健康检查
py -3.13 deploy_update.py

# 只检查，不部署
py -3.13 deploy_update.py --check
```

`deploy_update.py` 上传的文件清单：`streamlit_app.py`、`backtest_engine.py`、`fast_backtest.py`、`data_store.py`、`data_manager.py`、`news_fetcher.py`、`update_worker.py`、`doh_patch.py`、`entrypoint.sh`、`Dockerfile`、`docker-compose.yml`、`requirements.txt`、`.streamlit/config.toml`。

注意：

- 新增文件必须同步加入 `FILES` 列表，否则容器里缺失。
- `deploy_to_nas.py` 是更早的全量部署脚本，`push_run.py` 是临时远端执行脚本；三者功能重叠（见 03 的 TD-04）。

### 2.3 首次启动行为

1. `data_store.init_db()` 幂等建表；
2. Worker 回填 `data_coverage`（旧库升级时）；
3. 库为空 → `bootstrap()`：全部 A 股 + 全部 ETF（2020 至今）+ 4 个季度财务；
4. 库非空但数据落后最近交易日 → 启动即补一次；
5. 后台线程预热仪表盘回测与全球市场数据。

---

## 3. 环境变量

| 变量 | 代码默认 | compose 值 | 说明 |
|---|---|---|---|
| `AUTO_DOWNLOAD_COUNT` | `0` | `0` | `0` = 全量自举；正数 = 限制股票数量 |
| `CLOSE_HOUR` | `15` | `15` | 盘后补全小时 |
| `CLOSE_MINUTE` | `35` | `35` | 盘后补全分钟 |
| `RT_INTERVAL_SEC` | `5` | `60` | 实时快照入库间隔（秒） |
| `GM_INTERVAL_SEC` | `5` | 未设置（=5） | 交易时段全球市场刷新间隔 |
| `GM_INTERVAL_IDLE` | `300` | 未设置（=300） | 非交易时段全球市场刷新间隔 |
| `NEWS_INTERVAL_MIN` | `120` | `120` | 新闻抓取间隔（分钟） |
| `CHECK_INTERVAL` | `5` | `5` | Worker 主循环间隔（秒） |
| `FUNDAMENTAL_WEEKDAY` | `4` | `4` | 每周几刷新财务（4 = 周五） |
| `TZ` | — | `Asia/Shanghai` | 容器时区 |
| `PYTHONUNBUFFERED` | — | `1` | 日志即时输出 |

约定：新增变量必须同时更新本表、`docker-compose.yml`、`Dockerfile`（如有默认值）。

> 代码中全球市场刷新注释写“交易时段 30 秒”，实际读取 `GM_INTERVAL_SEC`（默认 5 秒）。以变量为准。

---

## 4. Worker 时间线

| 触发 | 动作 | 说明 |
|---|---|---|
| 启动 | 建表、覆盖索引回填 | 幂等 |
| 启动（空库） | 全量自举 A 股 + ETF + 财务 | 耗时长，期间界面可打开但数据不全 |
| 启动（数据落后） | 立即盘后补全一次 | 如容器在盘后重启 |
| 启动（后台线程） | 预热仪表盘回测、刷新全球市场 | 不阻塞主循环 |
| 交易时段每 `RT_INTERVAL_SEC` | 最新选股持仓的实时快照入库 | 仅持仓标的，走新浪直连 |
| 交易日 ≥ 15:35 | 补当日 K 线 + 新闻 + 因子预热 | 每天一次 |
| 周五盘后 | 额外刷新财务季报 | `FUNDAMENTAL_WEEKDAY` |
| 每 `NEWS_INTERVAL_MIN` | 多源新闻抓取 | 含翻译与去重 |
| 交易时段每 `GM_INTERVAL_SEC` | 全球市场刷新 | 默认 5 秒 |
| 非交易时段每 `GM_INTERVAL_IDLE` | 全球市场刷新 | 默认 300 秒 |
| 每 `CHECK_INTERVAL` | 消费一个 `data_task` | 策略触发的补数 |

---

## 5. 数据源与网络

| 数据 | 来源 | 代理 |
|---|---|---|
| A 股/ETF 日线 | akshare → 新浪财经（前复权） | 不使用 |
| 实时行情（页面） | 浏览器直连东财 `push2` | 不使用 |
| 实时快照（入库） | 新浪直连 | 不使用 |
| 财务季报 | akshare 财务接口 | 不使用 |
| 分红历史 | `stock_fhps_em` | 不使用 |
| 全球指数/商品/汇率 | akshare 多接口候选回退 | 不使用 |
| 境内新闻 | 东财/同花顺/财联社/新浪 | 不使用 |
| 境外新闻 | 公开 RSS | 代理可配 |

代理模式（设置页）：

- `auto`（默认）：境内域名直连，境外走代理；
- `always`：全部走代理；
- `off`：全部直连。

代理探测失败时，本轮境外源自动降级直连，避免逐源超时。

DNS：容器 DNS + `doh_patch` 兜底；若 NAS 劫持 UDP 53，DoH 会接管解析。

---

## 6. 存储与备份

### 6.1 数据资产

| 路径 | 内容 | 是否可重建 |
|---|---|---|
| `data/stock.db` | 全部业务数据 | 可重建，但耗时长（全量下载 + 财务） |
| `data/feat_cache.pkl` | 因子缓存 | 可删除，自动重算 |
| `cache/factor_matrix/` | 选股矩阵缓存 | 可删除，自动重算 |
| `cache/*.csv` | 旧版 CSV 兼容 | 可删除 |

### 6.2 备份

推荐在 NAS 上停容器后复制，或用 SQLite 在线备份：

```bash
# 在线备份（推荐，不中断服务）
sqlite3 /volume1/docker/limit-up-hunter/data/stock.db \
  ".backup '/volume1/backup/stock-$(date +%F).db'"

# 或停容器后整目录复制（含 WAL，需一起复制）
docker stop tommyquant
cp -a /volume1/docker/limit-up-hunter/data /volume1/backup/data-$(date +%F)
docker start tommyquant
```

### 6.3 恢复

```bash
docker stop tommyquant
cp /volume1/backup/stock-2026-11-17.db /volume1/docker/limit-up-hunter/data/stock.db
rm -f /volume1/docker/limit-up-hunter/data/stock.db-wal \
      /volume1/docker/limit-up-hunter/data/stock.db-shm
docker start tommyquant
```

恢复后首次启动会自动补齐落后区间。

---

## 7. 监控与健康检查

| 检查项 | 方式 | 正常表现 |
|---|---|---|
| 应用存活 | `curl -sf http://localhost:8501/_stcore/health` | 返回 `ok` |
| 容器状态 | `docker ps --filter name=tommyquant` | `Up ... (healthy)` |
| Worker 心跳 | `docker logs tommyquant \| tail -20` | 有盘后/实时/新闻日志 |
| 数据新鲜度 | 数据管理页「数据范围」 | 最新日期 = 最近交易日 |
| 任务健康 | 数据管理页任务队列 | 无长期 `pending/running` |
| 新闻健康 | 设置页「立即抓取」 | 多数源有返回 |

---

## 8. 故障排查

| 症状 | 排查顺序 | 处理 |
|---|---|---|
| 页面打不开 | `docker ps` → 日志 → 端口 | 重建容器；确认 8501 未被占用 |
| healthcheck unhealthy | `docker logs` 看启动异常 | 常见为依赖缺失或 DB 损坏 |
| 数据不更新 | 看 Worker 日志、`data_task`、系统时间/时区 | 重启容器触发启动补全 |
| 盘后没补数据 | 是否交易日、是否 ≥ 15:35、`last_close_date` | 手动 `docker exec ... python update_worker.py --once` |
| 财务为空 | `fundamental_stats`、任务队列 | 提交 `fundamental` 任务或重启自举 |
| 境外新闻全失败 | 设置页测试代理、DNS | 切 `off` 或修复代理 |
| 回测结果异常/很慢 | 数据指纹、缓存版本、引擎路径 | 升缓存版本；确认数据完整 |
| `database is locked` | 并发写、WAL 文件 | 避免页面写库；重启释放 |
| 磁盘增长过快 | 检查 `realtime_quote`、`news`、矩阵缓存 | 清理历史快照/新闻；删矩阵缓存 |
| 内存占用高 | 因子缓存、矩阵 mmap | 重启容器；确认指纹稳定 |

手动一次性补全：

```bash
docker exec -w /app tommyquant python update_worker.py --once
```

---

## 9. 安全注意事项

1. **明文凭据**：`deploy_update.py`、`deploy_to_nas.py`、`push_run.py` 中硬编码了 NAS 账号密码。必须尽快外置（01 的 P0-3），并轮换密码。
2. **无鉴权**：Streamlit 没有登录机制，端口不要暴露到公网；只允许内网访问，必要时加反向代理与 Basic Auth。
3. **SSH 暴露**：限制 NAS SSH 来源 IP，优先使用密钥登录。
4. **用户代码执行**：`author` 页使用 `exec` 执行任意代码，当前不可达；启用前必须做隔离或删除（P1-3）。
5. **备份文件**：备份含完整行情与策略记录，按敏感数据管理。

---

## 10. 升级与回滚

当前没有版本控制，升级依赖文件覆盖，回滚能力弱（03 的 TD-05）。

建议流程：

1. 升级前备份 `data/stock.db`；
2. 记录本次改动的文件与缓存版本；
3. 部署后跑 `deploy_update.py --check`；
4. 若异常，恢复数据库备份 + 回退对应源码文件 + 重建容器；
5. 涉及回测语义的改动，确认 `v14` 已升级，避免新旧结果混用。

长期方案：初始化 git，每次发布打 tag，部署脚本按 tag 拉取。
