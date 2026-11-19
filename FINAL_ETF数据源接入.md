# FINAL_ETF数据源接入

> 6A 结项报告 · 2026-11-19

## 1. 结论

华泰柏瑞 ETF 技能**可以作为项目数据源之一**，且能补上比现有 akshare 链路更多、更权威的字段；已按「落库增强」形态完成接入并部署到 NAS，六张扩展表可用，端到端试跑通过。

## 2. 关键判断依据

| 判断 | 依据 |
|---|---|
| 数据更新更准 | 单位净值、场内份额、规模、净申购、两融、折溢价、真实成交额均为交易所/基金公司口径；现有链路只能用 `close×volume` 估算成交额，份额字段全空 |
| 历史可取 | `fund-daily-quote` 实测可取 2020 年起的逐日行情 |
| 成本可控 | 3 秒间隔、每日 500 次配额；按「持仓 + 动量前 60」组池，每日约 180 次，占配额 36% |
| 风险可隔离 | 无凭据/网关异常/配额耗尽均降级跳过，不影响行情主链路与策略运行 |

## 3. 交付物

| 类型 | 内容 |
|---|---|
| 代码 | `htbr_source.py`、`vendor/htbr/`、`data_store.py`（6 表 + 写入/统计）、`data_manager.ensure_etf_extras()`、`update_worker`（盘后 + `etf_extra` 任务）、`streamlit_app.py`（4 指标 + 手动任务） |
| 部署 | `deploy_update.py`（vendor/secrets 上传）、`docker-compose.yml`（凭据只读挂载）、`Dockerfile`、`.gitignore`/`.dockerignore` |
| 测试 | `tests/test_htbr_source.py`（13 项）+ 既有 `test_etf_strategy.py`（74 项） |
| 文档 | `ALIGNMENT_/DESIGN_/TASK_/ACCEPTANCE_/FINAL_/TODO_ETF数据源接入.md`、`docs/02`、`docs/05` |

## 4. 实测数据（2026-11-19，NAS）

- 池内 3 只样本：`rows=219`，调用 13 次，失败 0。
- 510300：成交额 76.39 亿元、换手 3.793%、溢价 0.089%；规模与「净值×份额」自洽。
- 全市场资金流：股票型 ETF 净流入 159.4 亿元（1372 只），跨境 24.6 亿元。

## 5. 与既有回测的关系

- 未改动回测引擎与选股逻辑 → 既有 ETF/股票回测结论**不变**。
- 新增字段为后续「溢价过滤 / 资金流因子」提供数据基础，需另立任务。

## 6. 后续建议

见 `TODO_ETF数据源接入.md`。
