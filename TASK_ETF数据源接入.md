# TASK_ETF数据源接入

> 6A 阶段 3（Atomize）· 依赖顺序自上而下，每项可独立验证

| # | 任务 | 输入 | 输出 | 约束 | 依赖 | 验证 |
|---|---|---|---|---|---|---|
| T1 | 技能运行时入库 | `~/.trae-cn/skills/htbr-etf-skill` | `vendor/htbr/`（SKILL.md、config.py、guardrails.py、config.local.json、scripts/api_client.py、key_guide.py、guide_audit.py） | 剔除 `__pycache__`/`.audit`/`.version_check`；不含凭据 | — | `py -3.13 vendor/htbr/scripts/api_client.py invoke fund-resolve '{"secuCode":"510300"}'` 成功 |
| T2 | `htbr_source.py` | T1 | 限流/配额/异常封装（`invoke`/`available`/`quota_status`/`resolve_inner_code`） | 3 秒最小间隔；450 次硬闸；不打印 Key | T1 | 单测 mock 子进程：成功解析、429 退避、退出码 3 抛 `HtbrNoCredential` |
| T3 | `data_store.py` 建表与写入 | 设计 §3 | 4 张表 + `upsert_etf_quote_ext` / `upsert_etf_capital` / `upsert_etf_margin` / `upsert_etf_market_flow` / `etf_ext_stats()` | 主键幂等；单位统一元/份；`None` 不写 0 | — | 本地临时库写入两次行数不变 |
| T4 | `data_manager.ensure_etf_extras()` | T2/T3 | 组池 → 4 接口顺序取数 → 归一化写库 → 返回统计 | 池 = 近 30 日 pick_record ETF ∪ 动量前 N；单标的失败不中断 | T2,T3 | 干跑（`dry_run=True`）输出计划调用清单与次数 |
| T5 | 盘后调度接入 | T4 | `update_worker.after_close` 调用；`run_task` 支持 `dataset="etf_extra"` | 失败只记日志，不影响行情同步 | T4 | 手动 `task_create("etf_extra")` 后状态流转 pending→done |
| T6 | UI 接入 | T3 | 数据管理页 3 个指标（增强覆盖 / 最近同步日 / 今日调用）+ 手动补数按钮 | 无数据时显示 0，不报错 | T3 | 浏览器页面无异常 |
| T7 | 部署与凭据 | T1 | `deploy_update.py` 递归上传 vendor + 同步 secrets；`docker-compose.yml` 挂载 `/root/.htbr:ro`；`.gitignore` | 凭据不进 git/镜像 | T1 | 部署后容器内 `config.py status` = configured |
| T8 | 端到端验证 | T5,T7 | NAS 上小池试跑（3 只 ETF）→ 检查 4 张表 | 遵守配额与 3 秒间隔 | T5,T7 | 表内数据与技能直查结果一致 |
| T9 | 文档同步 | 全部 | `docs/05-operations.md` 增补数据源说明；`README.md` 一行说明 | 中文 | T8 | 文档可读、无过期截图 |

## 明确不做（本任务边界）

- 不修改 ETF 策略的选股逻辑（溢价过滤等因子留待下一任务）。
- 不用真实成交额覆盖历史 `amount`（用户已确认仅近期使用）。
- 不做全市场 927 只历史回补（配额不允许，留作可选慢任务）。
- 不接指数类接口（`index-*`）、不接基金经理/费率类展示。
