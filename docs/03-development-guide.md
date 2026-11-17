# 03 · 开发规范与流程

> 文档基线：2026-11-17
> 配套阅读：[02-architecture.md](./02-architecture.md)（分层与数据模型）、[05-operations.md](./05-operations.md)（部署）。

---

## 1. 环境准备

| 项 | 要求 | 说明 |
|---|---|---|
| Python | 3.13（生产镜像 `python:3.13-slim`） | Windows 本机建议 `py -3.13`；本机 `python` 可能未加入 PATH |
| Docker | 可选 | 只在验证生产形态时需要 |
| 依赖 | `pip install -r requirements.txt` | akshare / pandas / numpy / plotly / streamlit / PySocks |

依赖版本策略：当前为下限约束（`>=`），可复现性不足；P1-7 要求改为固定区间。

---

## 2. 本地运行

```powershell
# 1. 安装依赖
py -3.13 -m pip install -r requirements.txt

# 2. 首次准备数据（会写本地 data/stock.db，耗时取决于下载规模）
py -3.13 update_worker.py --once

# 3. 启动界面
py -3.13 -m streamlit run streamlit_app.py --server.port 8501
```

要点：

- 本地库当前只有 3 只标的的样本数据。**不要用本地库的绩效结论做判断**，只用于验证页面与流程。
- 容器内由 `entrypoint.sh` 同时拉起 Worker 与 Streamlit；本地开发通常只跑 Streamlit，Worker 用 `--once` 手动触发。
- 数据目录 `data/`、缓存目录 `cache/` 已在 `.dockerignore` 中排除进镜像，靠卷挂载持久化。

---

## 3. 目录与文件约定

```
.
├── streamlit_app.py        # UI 入口（唯一 Streamlit 入口，不要新增第二个）
├── backtest_engine.py      # 引擎与策略函数（策略函数放这里）
├── fast_backtest.py        # 向量化内核（新增向量化实现放这里）
├── data_store.py           # 唯一 SQLite 访问层
├── data_manager.py         # 数据编排（缺口/财务/全球市场）
├── news_fetcher.py         # 新闻源与代理
├── update_worker.py        # 后台调度
├── doh_patch.py
├── docs/                   # 本文档体系
├── data/                   # SQLite（不入库、不打进镜像）
├── cache/                  # 因子矩阵与 CSV 兼容（不入库）
└── .streamlit/config.toml  # 主题（主色 #1FA879）
```

---

## 4. 编码规范

### 4.1 通用

- 遵循 PEP 8；缩进 4 空格；行宽以可读为先（现有代码约 100–120）。
- 注释、日志、界面文案使用中文；英文仅出现在 i18n 字典的英文分支。
- 函数保持单一职责；数据访问必须走 `data_store`，不要在 UI 里直接 `sqlite3.connect`。
- 新增第三方依赖前先确认：能否用现有库完成；镜像体积是否可接受。

### 4.2 项目特有约定

| 约定 | 说明 |
|---|---|
| 日期格式 | 库内统一 `YYYY-MM-DD`；akshare 调用参数常用 `YYYYMMDD`，转换集中在 `data_manager._norm_day` 附近 |
| 代码格式 | 库内与 `feat` 的 key 为带前缀代码：`sh600719` / `sz159509`；UI 输入 6 位数字时用 `_parse_codes` 补前缀 |
| 幂等写入 | 一律 `INSERT OR REPLACE` / upsert；重复跑不产生重复行 |
| 不阻塞页面 | 页面线程禁止下载数据，只允许 `task_create` 排队 |
| 异常处理 | 外部数据源调用必须有回退或降级；不允许 `except: pass` 掩盖关键失败（现有代码部分存在，改动时不要扩散） |
| 成本参数 | 只从 `app_settings` 读，禁止在策略里写死费率 |
| 涨跌颜色 | 红涨绿跌，所有价格/涨跌幅/浮盈位置一致 |
| 主题色 | 只用 `#1FA879` 作为强调色 |

### 4.3 新增字段/表的流程

1. 在 `data_store.init_db` 的建表语句中加列/表；
2. 同时补 `ALTER TABLE ... ADD COLUMN`（`try/except`）兼容旧库；
3. 读取侧提供缺省值，不假设列一定存在；
4. 更新 02 第 5 节；
5. 如果该字段参与回测结果，升缓存版本（见第 6 节）。

---

## 5. 新增策略标准流程

### 5.1 函数式策略（推荐）

以 `select_momentum` 为例：

```python
# backtest_engine.py
def select_my_strategy(snap: dict, top_n: int = 5, threshold: float = 5.0) -> list:
    rows = [c for c, r in snap.items() if r.get("ret_20", 0) >= threshold]
    return rows[:top_n]
```

然后按顺序完成 5 处注册：

| 步骤 | 文件 | 内容 |
|---|---|---|
| 1 | `backtest_engine.py` | 函数实现，参数带默认值，返回代码列表 |
| 2 | `backtest_engine.py` | `STRATEGIES` 加一行：`"策略名": select_my_strategy` |
| 3 | `data_manager.py` | `STRATEGY_REQUIREMENTS` 声明所需数据集与回看天数 |
| 4 | `fast_backtest.py` | 可选：`_VEC_SELECTORS` 注册向量化实现；不注册则自动走事件驱动（正确但慢） |
| 5 | `streamlit_app.py` | 如需可调参：`SWEEP_TUNABLES`、参数控件、`_CFG_KEYS`；如需首页对比：`DASH_STRATEGIES` |

最后更新 [04-strategy-spec.md](./04-strategy-spec.md)。

注意事项：

- 参数默认值必须与 UI 默认值一致，否则回测与选股结果会不一致。
- 选股函数必须能处理 `NaN`、空快照、停牌缺行。
- 若依赖财务数据，使用 `row.get("_f")`，缺失时退化而不是直接排除全部标的。

### 5.2 有状态策略类

适用于每日再平衡、按数量下单等场景，参考 `MarketCapStrategy`：

1. 继承 `BaseStrategy`，实现 `on_init(context)` 与 `on_bar(event) -> SignalEvent`；
2. 需要每日全市场快照时，直接读 `event.snapshot`；
3. 下单用 `SignalEvent(..., rebalance_orders=[OrderEvent(...)])` 精确控制数量；
4. 在 UI 中用 `use_strategy_class=True` 调用 `run_backtest`；
5. 该策略**不会**进入 `_VEC_SELECTORS`，永远走事件驱动引擎。

调仓类策略必须遵守（来自 2026-11-16 的修复结论）：

- 判断依据是**单只标的自身**相对建仓市值/目标市值的偏离，不能用全组合平均值；
- 先卖后买，买单受可用现金约束，现金不得为负；
- 数量按 100 股取整；
- 任何改变调仓语义的修改都要升缓存版本。

---

## 6. 缓存与版本管理

| 缓存 | 版本/键 | 何时必须升级 |
|---|---|---|
| 回测结果 | `bt_cache_key` 中 `v14` | 选股逻辑、成交价、成本、调仓规则、绩效口径发生变化 |
| 选股矩阵 | `fast_backtest.MATRIX_VER = "m1"` | 矩阵列定义、财务上下文、选股日计算方式变化 |
| 因子缓存 | `data/feat_cache.pkl` 内嵌数据指纹 | 因子计算逻辑变化（需手动删除文件或改指纹策略） |

规则：

1. 升版本 = 旧缓存自动失效，不要手工 `DELETE FROM backtest_cache`（生产上容易误删）。
2. 升版本必须在 01 第 11 节记录“为什么升”。
3. `bt_cache_key` 的 `cost_str` 已包含 5 个成本参数，改默认费率无需升版本。

---

## 7. 调试

### 7.1 日志

- Worker 日志：`docker logs tommyquant`；本地直接看控制台。
- 新闻与代理问题：设置页「测试连通」「立即抓取」会显示每个源的成败。

### 7.2 数据库排查

```python
import sqlite3
conn = sqlite3.connect(r"data\stock.db")
print(conn.execute("select count(*), min(date), max(date) from daily_bar").fetchone())
print(conn.execute("select dataset, status, count(*) from data_task group by 1,2").fetchall())
print(conn.execute("select strategy, created_at from backtest_cache order by created_at desc limit 5").fetchall())
```

### 7.3 回测问题定位顺序

1. 看 `data_fingerprint()` 是否变化（决定是否命中旧缓存）；
2. 看策略是否被 `supports_fast` 命中（走的是哪套引擎）；
3. 看 `strategy_run.params` 与界面参数是否一致；
4. 看交易明细中 `reason`（`limit_up` / `hold_expired` / `rebalance` 等）；
5. 最后才怀疑因子计算。

---

## 8. 测试

### 8.1 现状

已有 `tests/test_market_cap_rebalance.py`，20 项用例覆盖市值策略调仓语义、Topk-Drop 换股、MA30 预计算口径、成本核算、缺失行情估值、数据覆盖日期、卖出原因与界面参数键名回归：

```powershell
py -3.13 -m unittest discover -s tests -v
```

### 8.2 约定（新增测试时遵守）

1. 测试目录 `tests/`，使用标准库 `unittest`（无需额外依赖）。需要参数化时再引入 pytest。
2. **涉及数据库的测试必须隔离**：`data_store.DB_PATH` 是模块常量，直接改属性：

```python
import data_store

def setUp(self):
    self._orig = data_store.DB_PATH
    data_store.DB_PATH = os.path.join(tempfile.mkdtemp(), "test.db")
    data_store.init_db()

def tearDown(self):
    data_store.DB_PATH = self._orig
```

3. 不得读写生产库 `data/stock.db`；不得依赖网络；行情/财务用内存构造。
4. 优先覆盖：
   - 市值策略：下跌补仓、上涨减仓、单标的偏离不连带、先卖后买、现金不为负（已覆盖）；
   - 界面参数键名与函数签名一致（已覆盖）；
   - 成本模型：买/卖方向、最低佣金、印花税仅卖出、冲击成本阈值；
   - 缓存键：参数/成本/数据指纹任一变化必须产生不同键；
   - 绩效：最大回撤、夏普、索提诺、卡尔玛在构造曲线上的数值。

### 8.3 当前用例清单

| 用例 | 验证点 |
|---|---|
| `test_buy_on_self_drop_and_sell_on_self_rise` | 低于目标买入、高于目标卖出、卖单排在买单前 |
| `test_buy_limited_by_cash` | 买单不超过可用现金 |
| `test_sell_proceeds_fund_buy_and_cash_not_negative` | 卖单回款可用于同日买单 |
| `test_small_diff_below_one_lot_is_skipped` | 差异不足 100 股不下单 |
| `test_drop_buys_only_that_symbol` | 下跌只补该标的 |
| `test_rise_sells_only_that_symbol` | 上涨只减该标的 |
| `test_drop_never_produces_sell_for_that_symbol` | 下跌不会产生卖单 |
| `test_ui_keys_match_smallcap_rotation_signature` | 界面参数键名与函数签名一致 |
| `test_last_row_returns_previous_close` | 缺失当日行情时取最近收盘价 |
| `test_last_row_is_none_before_first_bar` | 首根K线之前不虚构价格 |
| `test_total_value_keeps_positions_on_missing_day` | 数据缺日不得把持仓按 0 估值 |
| `test_last_coverage_date_ignores_single_symbol_day` | `coverage_last_date` 不被单行标的抬高 |
| `test_ma30_exit_sets_reason` | MA30 清仓在流水里标记 `ma30_exit` |
| `test_ui_reason_labels_cover_engine_codes` | 界面原因列覆盖全部引擎代号 |
| `test_rank_outside_buffer_is_sold` | 排名跌出 `top_n+buffer` 触发 `rank_drop` 卖出并补位买入 |
| `test_rank_inside_buffer_is_kept` | 缓冲区内不换股 |
| `test_topk_drop_skips_deviation_rebalance` | `topk_drop` 模式不再触发偏离度调仓 |
| `test_slope_up_and_insufficient_history` | MA30 斜率判断；数据不足返回 `None` |
| `test_matches_per_day_resample_on_every_day` | 预计算 MA30 与逐日 resample 逐日完全一致（含未走完周用当日收盘） |
| `test_merged_position_cost_excludes_principal` | 合并加仓后卖出，成本不得包含买入本金 |

---

## 9. 发布流程

当前没有 CI，流程靠脚本：

```powershell
# 1. 本地自检（可选）
py -3.13 -c "import streamlit_app"   # 仅验证导入

# 2. 部署到 NAS（上传改动文件 + 重建容器 + 健康检查）
py -3.13 deploy_update.py

# 3. 只验证，不部署
py -3.13 deploy_update.py --check
```

`deploy_update.py` 会：上传 `FILES` 列表中的文件与 `.streamlit/config.toml` → 远端 `docker compose up -d --build --force-recreate` → 轮询 `/_stcore/health` 直到 `ok`。

发布检查清单：

- [ ] 改了策略/成本/调仓 → 已升 `v14`
- [ ] 改了矩阵列/财务注入 → 已升 `m1`
- [ ] 新增环境变量 → 已同步 05 第 3 节与 `docker-compose.yml`
- [ ] 改了表结构 → 兼容旧库的 `ALTER TABLE` 已补
- [ ] 部署后 `--check` 健康为 `ok`，Worker 日志无连续错误

---

## 10. 常见坑

| 现象 | 原因 | 处理 |
|---|---|---|
| `No Python at ...Python37-32` | 本机 `python` 指向旧解释器 | 用 `py -3.13` |
| 页面提示数据库初始化 | 空库正在自举 | 等 Worker 下载完成；看 `data_task` |
| 改完策略但结果没变 | 命中旧 `backtest_cache` | 升 `v14` 或确认参数/指纹变化 |
| 回测很快但结果不对 | 走到了不支持的快速路径或命中旧缓存 | 检查 `supports_fast` 与 `strategy_run` |
| 境外新闻全失败 | 代理不可用/DNS 异常 | 设置页测试代理；确认 `auto` 模式下境内源不受影响 |
| 容器健康但数据不更新 | Worker 崩溃或任务卡住 | `docker logs`；看 `data_task` 状态 |
| `database is locked` | 并发写 | 避免在页面线程写库；重试；确认 WAL |
| 选股结果代码对不上 | 6 位代码未补交易所前缀 | 用 `_parse_codes` / `sina_code` |
| 跨域取不到行情 | 接口 Referer 限制 | 只使用已验证的东财 `push2`；不要随意换源 |
| 内存持续上涨 | 因子/矩阵缓存未命中导致反复构建 | 检查指纹是否每次变化；确认 `feat_cache.pkl` 生效 |
| 日志显示「补全 5029 只 / 0 行」 | akshare 升级后接口改名（如 `stock_zh_a_daily` → `stock_zh_a_daily_sina`），异常被 `except` 吞掉 | `requirements.txt` 已锁 `akshare==1.20.63`；取数统一走 `data_store._ak_stock_daily`，异常会打 warning |
| 回测末期净值突然塌成现金 | 回测终点用了 `MAX(date)`（被单行标的抬高），末日持仓取不到行情 | 终点用 `coverage_last_date`；估值走 `BarIndex.last_row` 前向取价 |

---

## 11. 已知偏差与技术债

| 编号 | 偏差/债务 | 现状 | 计划 |
|---|---|---|---|
| TD-01 | `cached_backtest` 注释写“L1 st.cache_data 24h”，实际无该装饰器 | 注释误导 | P1-6 顺带修正 |
| TD-02 | `STRATEGY_REQUIREMENTS` 有 3 个未注册策略 | 需求声明失真 | P0-2 |
| TD-03 | `author` 页不可达且 `exec` 用户代码 | 死代码 + 安全面 | P1-3 |
| TD-04 | 部署脚本硬编码 NAS 凭据、三份重复实现 | 安全 + 维护 | P0-3 |
| TD-05 | 无 `.gitignore`、无 git 历史 | 无法回滚/追溯 | P0-4 |
| TD-06 | 无测试 | 回归靠人工 | P0-1 |
| TD-07 | `streamlit_app.py` 约 3473 行 | 维护成本高 | P1-1 |
| TD-08 | i18n 未覆盖全部页面 | 英文模式有中文残留 | P1-2 |
| TD-09 | 依赖未锁版本 | 构建不可复现 | P1-7 |
| TD-10 | 本地库仅 3 只样本，与生产规模差异大 | 易得出错误结论 | 文档明示 + 本地标注数据来源 |
| TD-11 | 「小市值轮动」界面参数键名与函数参数名不一致 | 已于 2026-11-17 修复（P0-6），并有回归用例保护 | 已完成 |
