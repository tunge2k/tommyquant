# DESIGN — ETF 策略独立与持仓规则

## 1. 文档结构

```
04-strategy-spec.md
├── 6. 策略三：市值策略（股票，有状态）
└── 7. 策略四：ETF 轮动策略（pool_type="etf"）   ← 新增独立章节
    ├── 7.1 为什么独立
    ├── 7.2 选品与排序
    ├── 7.3 持仓与调仓规则
    ├── 7.4 风控因子
    ├── 7.5 参数表
    └── 7.6 引擎选择与向量化边界
```

原 6.7 内容迁入第 7 章，原第 7/8/9 章顺延为 8/9/10。

## 2. 代码设计

`MarketCapStrategy.on_bar` 的 `topk_drop` 分支：

```python
ranked = ...
keep_limit = n_slots + rank_buffer
if pool_type != "etf":
    # 股票: 保留排名跌出缓冲即换出
    for code in remaining:
        if rank_map.get(code) is None or rank_of(code) > keep_limit:
            sell(code, reason="rank_drop")
# ETF: 不做排名换出, remaining 保持原持仓
need_fill = n_slots - len(remaining)
if need_fill > 0:
    candidates = ranked[:need_fill]   # 只补空槽
```

- ETF 的卖出只来自 MA30 走弱、绝对动量转弱、资金流风控；
- 卖出后空出的槽位在下一个 `check_freq` 检查日按排序补足；
- 持仓数已满时，`_deviation_rebal` 继续每日按价格/权重偏离调仓；
- 股票池分支不动。

## 3. 向量化边界

| 内核 | 适用 | 原因 |
|---|---|---|
| `fast_backtest.py` 向量化 | 函数式策略的截面选品 | 选品可表达为「因子矩阵 → 掩码 → 排序」 |
| 事件驱动内核 | 市值/ETF 有状态策略 | 每日读持仓、判断 MA30、决定买卖数量与时序，属状态机 |

事件驱动的效率来源：MA30 全局缓存、`BarIndex.searchsorted` 定点取行、因子预计算。向量化并非唯一提速手段。

## 4. 缓存与部署

- 持仓规则影响回测结果 → 缓存键 `v15` 升至 `v16`，旧结果自动失效；
- 记忆新增硬约束：每次变更后部署 NAS 容器并验证健康；
- 部署脚本 `deploy_update.py` 上传引擎/界面依赖文件并重建容器。

## 5. 异常与兼容

- ETF 排名数据为空 → 不补槽，不卖出；
- MA30 数据不足 → 按原约定放行，不误杀；
- 持仓数大于目标数（异常）→ 不主动减仓，交由偏离度调仓处理。
