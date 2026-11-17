# 托米量化 · 文档中心

> 本文档体系用于回答三个问题：**项目要做什么（01）**、**系统怎么搭的（02/04）**、**怎么改和怎么运维（03/05）**。
>
> 文档基线：2026-11-17
> 代码基线：工作区 `d:\py_lezrn\stock_a\888`，8 个核心 Python 模块约 8.9k 行

---

## 1. 文档清单

| 文档 | 解决的问题 | 什么情况下必须更新 |
|---|---|---|
| [01-project-plan.md](./01-project-plan.md) | 定位、范围、里程碑、路线图、风险、决策记录 | 新增/删除里程碑、调整优先级、做出架构级决策 |
| [02-architecture.md](./02-architecture.md) | 模块职责、数据流、数据库模型、缓存、实时行情、新闻 | 改表结构、改数据流、新增外部数据源、改缓存机制 |
| [03-development-guide.md](./03-development-guide.md) | 环境搭建、编码规范、新增策略流程、测试、发版、已知坑 | 改开发流程、改缓存版本、改部署方式 |
| [04-strategy-spec.md](./04-strategy-spec.md) | 三个策略的完整规格、成本模型、绩效指标定义、回测口径 | 改选股条件/参数/交易规则/成本模型 |
| [05-operations.md](./05-operations.md) | 部署、环境变量、Worker 时间线、备份、故障排查、安全 | 改部署拓扑、改环境变量、改定时任务 |
| [06-open-source-references.md](./06-open-source-references.md) | 开源方案借鉴清单、问题→成熟做法对照、平台技能用法、调研 SOP | 引入新依赖、改变回测口径、新增可复用模块 |

---

## 2. 阅读路径

- **第一次接触项目**：01（知道在做什么）→ 02（知道代码怎么分层）→ 03（把环境跑起来）
- **做策略研究**：04（口径和规则）→ 02 第 5、7 节（数据怎么取、缓存怎么失效）→ 06（先看别人怎么解）
- **遇到新问题**：先读 06 第 2、6 节，再决定自研还是借用
- **改界面/加页面**：03 第 2、4、6 节 → 02 第 4 节（导航结构）
- **部署/排障**：05 → 02 第 3、10、11 节

---

## 3. 事实基线

排障或写文档时，以下前提默认成立：

| 项 | 值 |
|---|---|
| 生产实例 | `http://<NAS_IP>:8501`，容器名 `tommyquant`（NAS 地址见 `nas_conf.py`，不入库） |
| 远端部署目录 | `/volume1/docker/limit-up-hunter` |
| 持久化目录 | `data/`（SQLite）与 `cache/`（因子矩阵、CSV 兼容） |
| 本地开发库 | `data/stock.db`，当前仅 3 只标的 / 2076 根 K 线，**不代表生产规模** |
| 生产库规模 | 全量 A 股 + ETF，统计以「数据管理」页为准 |
| 回测数据要求 | 绩效结论必须来自全量库；本地样本库的结果无参考价值 |

> 本地库只用于联调界面和验证流程，不要用它得出的收益/回撤做任何判断。

---

## 4. 文档与代码不一致时

**以代码为准。** 发现不一致时：

1. 在 [03-development-guide.md](./03-development-guide.md) 第 11 节「已知偏差与技术债」登记；
2. 当次改动顺手修正文档，不积压。

---

## 5. 维护规则

1. **行为变更必带文档**：影响回测结果、数据写入、定时任务的改动，必须同步更新 02/04/05 对应章节。
2. **缓存版本必须记录**：改 `bt_cache_key` 的版本号（当前 `v14`）或 `fast_backtest.MATRIX_VER`（当前 `m1`）时，在 03 第 6 节和 01 第 11 节同时记录原因。
3. **新增环境变量必须进表**：05 第 3 节的环境变量表是唯一权威清单。
4. **策略增删必须双向同步**：`backtest_engine.STRATEGIES`、`data_manager.STRATEGY_REQUIREMENTS`、UI 参数区、04 文档四处保持一致。

---

## 6. 代码地图

| 文件 | 行数（约） | 职责 | 关键入口 |
|---|---|---|---|
| `streamlit_app.py` | 3473 | Web UI、i18n、导航、图表、页面路由 | `if page == "home"` 起的 9 个渲染分支 |
| `backtest_engine.py` | 1682 | 因子、选股函数、事件驱动引擎、成本/绩效 | `STRATEGIES`、`run_backtest`、`calc_performance` |
| `fast_backtest.py` | 673 | 向量化选股内核、因子矩阵磁盘缓存、网格寻优 | `supports_fast`、`run_fast` |
| `data_store.py` | 1002 | SQLite 读写、下载、缓存、任务队列 | `init_db`、`load_feat_cached` |
| `data_manager.py` | 959 | 交易日历、缺口规划、财务/分红、全球市场、基准 | `plan_bars`、`ensure_bars`、`after_close_sync` |
| `news_fetcher.py` | 677 | 多源新闻抓取、去重、翻译、代理分流 | `fetch_all_news`、`get_recent_news` |
| `update_worker.py` | 361 | 后台常驻：自举、盘后补全、实时采样、任务消费 | `main` |
| `doh_patch.py` | 72 | DNS over HTTPS 兜底 | `install` |
| `deploy_update.py` / `deploy_to_nas.py` / `push_run.py` | — | NAS 部署与远端执行 | `--check` 仅验证 |
