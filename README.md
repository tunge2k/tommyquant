# 托米量化（TommyQuant）

> A 股多策略量化研究平台：数据采集 → 因子计算 → 策略回测 → 每日选股 → 模拟持仓 → 资讯聚合。
> 自托管，单 Docker 容器即可在 NAS 上长期运行。

**定位说明**：本项目是个人研究工具，不接券商接口、不下实盘单；所有回测与选股结果仅供学习研究，不构成投资建议。

---

## 文档

项目文档以 `docs/` 为唯一权威来源：

| 文档 | 内容 |
|---|---|
| [文档索引](docs/README.md) | 文档体系、事实基线、代码地图、维护规则 |
| [项目计划](docs/01-project-plan.md) | 定位、范围、里程碑、路线图、风险、决策记录 |
| [系统架构](docs/02-architecture.md) | 模块职责、数据流、数据库模型、缓存、实时行情、新闻 |
| [开发规范](docs/03-development-guide.md) | 环境、编码规范、新增策略流程、测试、发版、常见坑 |
| [策略规格](docs/04-strategy-spec.md) | 三个策略的完整规则、成本模型、绩效定义、回测局限 |
| [部署运维](docs/05-operations.md) | 部署、环境变量、Worker 时间线、备份、排障、安全 |
| [开源借鉴](docs/06-open-source-references.md) | 问题→成熟方案对照、可借用项目清单、平台技能用法、调研 SOP |

---

## 功能

导航为一级菜单 + 二级菜单，共 8 个可达页面：

| 一级 | 页面 | 说明 |
|---|---|---|
| 量化大师 | 首页 | 全球主要市场行情、资讯摘要、快捷入口、策略绩效对比 |
| 量化策略 | 策略回测 | 策略与参数、资金曲线、回撤、逐笔交易、交易/持仓日历 |
| 量化策略 | 今日选股 | 基于最新交易日因子输出 Top N，自动入库跟踪 |
| 量化策略 | 投资组合 | 以最新选股等权模拟建仓，浏览器直连计算浮盈 |
| 资讯 | 资讯中心 | 境内 4 源 + 境外 9 个 RSS 源，标题哈希去重、最早来源优先 |
| 更多 | 参数设置 | 语言、交易成本、回测区间、代理模式、新闻源开关 |
| 更多 | 数据管理 | 数据库概览、覆盖状态、任务队列、手动更新 |
| 更多 | 帮助 | 上手、功能说明、策略原理、FAQ、免责声明 |

其他能力：

- **双回测引擎**：向量化内核优先，不支持时回退事件驱动；T 日信号 / T+1 开盘成交。
- **真实成本模型**：佣金、印花税、过户费、滑点、冲击成本。
- **数据自动化**：启动自举、盘后补全、交易时段快照、财务周五刷新、策略按需补数。
- **实时行情**：页面由浏览器直连东方财富 `push2`，1–2 秒刷新，不经过后端轮询。
- **避免未来函数**：财务与分红按公告日 as-of 加载，缺失时策略自动退化并有提示。

---

## 策略

| 策略 | 类型 | 核心逻辑 | 默认持仓 |
|---|---|---|---|
| 小市值轮动·戴维斯双击 | 函数式 | 小市值 + 高成长 + 低估值 + 财务健康 + 趋势确认 | 5（界面可调） |
| 动量+均线+小市值 | 函数式 | 20 日动量 + MA20 + 市值上限 + 换手区间 | 5（界面可调） |
| 市值策略 | 有状态类 | 15–200 亿主板，75% 资金等比例，单标的偏离再平衡 | 5（界面可调） |

完整参数、评分公式、再平衡规则与四项不良标的过滤见 [策略规格](docs/04-strategy-spec.md)。

> 绩效数据以应用内「最近回测」为准。仓库中的 `backtest_report.txt` 是 2026-11-12 的 **198 只抽样**报告（seed=42），不代表当前全量结果。

---

## 架构

```text
Docker 容器（NAS）
├── entrypoint.sh
│   ├── update_worker.py（后台）
│   │   ├── 空库自举：全部 A 股 + ETF + 财务
│   │   ├── 交易日 15:35 → 补当日 K 线 + 新闻 + 因子预热
│   │   ├── 交易时段 → 持仓实时快照入库
│   │   ├── 每 120 分钟 → 新闻抓取
│   │   └── 消费 data_task 补数队列
│   └── streamlit_app.py（前台，8501）
│       ├── 页面 / 导航 / i18n
│       ├── backtest_engine（事件驱动）← fast_backtest（向量化，优先）
│       └── data_store / data_manager / news_fetcher
└── SQLite (WAL)  data/stock.db      cache/（因子矩阵）
```

数据库共 12 张业务表（另有 SQLite 内部表 `sqlite_sequence`）：`daily_bar`、`data_coverage`、`fundamental`、`dividend`、`realtime_quote`、`data_task`、`strategy_run`、`pick_record`、`backtest_cache`、`news`、`download_log`、`app_settings`。字段与索引见 [系统架构](docs/02-architecture.md) 第 5 节。

---

## 快速开始

### 本地运行

```powershell
py -3.13 -m pip install -r requirements.txt

# 准备数据（写入本地 data/stock.db）
py -3.13 update_worker.py --once

# 启动界面
py -3.13 -m streamlit run streamlit_app.py --server.port 8501

# 运行调仓回归测试
py -3.13 -m unittest discover -s tests -v
```

本机数据库目前只有 3 只标的的样本数据，仅用于联调界面；**绩效结论必须来自全量库**。

### Docker

```bash
docker compose up -d --build
# http://localhost:8501
```

### 部署到 NAS

```powershell
py -3.13 deploy_update.py          # 上传 + 重建 + 健康检查
py -3.13 deploy_update.py --check  # 只验证
```

---

## 配置

常用环境变量：

| 变量 | 默认 | 说明 |
|---|---|---|
| `AUTO_DOWNLOAD_COUNT` | `0` | 0 = 全量自举 |
| `CLOSE_HOUR` / `CLOSE_MINUTE` | `15` / `35` | 盘后补全时刻 |
| `RT_INTERVAL_SEC` | `5`（compose 设 60） | 实时快照入库间隔 |
| `NEWS_INTERVAL_MIN` | `120` | 新闻抓取间隔 |
| `CHECK_INTERVAL` | `5` | Worker 主循环间隔 |
| `FUNDAMENTAL_WEEKDAY` | `4` | 财务刷新日（4 = 周五） |

完整列表、代理模式与数据源说明见 [部署运维](docs/05-operations.md)。

---

## 目录结构

```text
├── streamlit_app.py      # Web UI（8 个页面）
├── backtest_engine.py    # 引擎、选股函数、成本与绩效
├── fast_backtest.py      # 向量化内核与网格寻优
├── data_store.py         # SQLite 数据层
├── data_manager.py       # 交易日历、缺口规划、财务/全球市场
├── news_fetcher.py       # 多源新闻、去重、翻译、代理
├── update_worker.py      # 后台调度
├── doh_patch.py          # DoH 兜底
├── docs/                 # 项目文档（入口：docs/README.md）
├── data/                 # SQLite（持久化，不入库）
├── cache/                # 因子矩阵与 CSV 兼容（不入库）
├── Dockerfile
├── docker-compose.yml
├── entrypoint.sh
└── deploy_update.py      # NAS 增量部署
```

---

## 已知限制

- 回测未建模涨跌停无法成交、停牌排队与完整退市样本，收益偏乐观。
- 财务覆盖不足的早期区间，基本面策略会退化为纯动量，前后期口径不完全一致。
- 已有市值策略调仓回归测试（`tests/`），但其余模块仍缺自动化测试；当前无版本控制。
- 部署脚本仍包含明文凭据，使用前请先按 P0-3 外置。

---

## 免责声明

股市有风险，历史回测不代表未来收益。本项目仅供量化研究与学习交流，据此实盘操作，盈亏自负。
