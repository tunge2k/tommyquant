# ACCEPTANCE_ETF数据源接入

> 6A 阶段 6（Assess）· 验收时间：2026-11-19 · 环境：NAS 容器 `tommyquant`

| # | 验收项 | 结果 | 证据 |
|---|---|---|---|
| 1 | 容器内技能可用、凭据就绪 | ✅ | `config.py status` → `configured: API Key 已配置（来源: /root/.htbr/credentials.json）`；`/root/.htbr/credentials.json` 权限 `-rw-------` |
| 2 | 数据源封装可调用、配额可计数 | ✅ | 本机 `available()=(True,'')`；`quota_status()` 调用后由 0→1，持久化在 `app_settings['htbr_calls_20261119']` |
| 3 | 六张新表建立且写入幂等 | ✅ | 临时库连写两次，`etf_capital` 行数仍为 1；`etf_ext_stats()` 正常返回 |
| 4 | 真实取数并落库 | ✅ | NAS 小池 `top_n=3, days=3`：`rows=219, calls_used=13, failed=[]`；`quote 15 / capital 15 / margin 12 / flow 30 / valuation 147` |
| 5 | 单位换算正确 | ✅ | `etf_capital.market_scale/(unit_nv×exchange_shares)=1.000000`（相对误差 <1e-6）；510300 实测成交额 76.39 亿元、溢价 0.089% |
| 6 | 配额硬闸与失败降级 | ✅ | 单测覆盖：`SAFE_LIMIT` 触发 `HtbrQuotaExceeded`；退出码 3 → `HtbrNoCredential`；「配额已用尽」→ `HtbrQuotaExceeded`；无凭据时 `ensure_etf_extras` 返回 `skipped` |
| 7 | 策略不受影响 | ✅ | ETF 策略 74 项 + 新增 13 项 = 87 项单测全部通过；策略代码未改动，新字段缺失时保持旧逻辑 |
| 8 | UI 展示 | ✅ | 浏览器验证数据管理页 4 张卡片：资金流 30 / 规模份额 3 只（15 条）/ 行情增强 3 只（15 条）/ 配额 13-500，无异常 |
| 9 | 部署可复现 | ✅ | `deploy_update.py` 输出 vendor/htbr 与 secrets 上传成功；容器 `healthy`；凭据只读挂载 |
| 10 | 文档同步 | ✅ | `docs/02-architecture.md`（模块 + 表）、`docs/05-operations.md`（数据源/凭据/配额/降级） |

## 遗留与限制

- 当前仅试跑 3 只；全池（60 只）预计每日 180 次调用、约 10 分钟，需在盘后窗口（15:30 之后）观察一次真实运行。
- `fund-valuation` 返回 49 个报告期，`etf_valuation` 行数会持续增长，暂未做保留期清理。
- 溢价率早年可能为空（`NULL`），历史因子不可用，仅近期有效。
- 真实成交额仅用于近期窗口，历史 `amount` 仍为 `close×volume` 估算（用户确认）。
