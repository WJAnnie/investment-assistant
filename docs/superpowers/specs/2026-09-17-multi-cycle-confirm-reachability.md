# 多周期缠论「CONFIRMED 不可达」：实测证据与判定

状态：**已解除（2026-09-18 分支 codex/multi-cycle-confirm-p2）**。P1/P2/P3 已按「装配而非放宽」路线全部解决：P2 跨日 120m 装配（app/market/minute/history.py）；P3 真实买点标志生产者（app/chan/pipeline.py derive_buy_flags）；P1 两阶段 core_signal 接线（app/portfolio/analysis.py）。全量 1319 tests 通过。原始判定证据保留如下。

## 结论

在真实生产路径上 `ConfirmOutcome.CONFIRMED` 目前**不可达**。这不是数据缺失，而是三个**互相独立**的结构性阻塞：

| # | 阻塞 | 位置 | 性质 |
|---|---|---|---|
| P1 | `core_signal` 恒为 `None` | `app/portfolio/analysis.py:665` | 硬编码 |
| P2 | `120m` 要求 ≥3 根，但单日最多只能产生 2 根 | `app/portfolio/structure.py:31` + `app/market/minute/resample.py` | 算术不可能 |
| P3 | `first_buy/second_buy/third_buy/class_second` 在生产代码中从未被赋值 | `app/chan/signal.py:20-49` + 唯一调用点 `analysis.py:470` | 无生产者 |

三者必须同时解除才可能出现 `CONFIRMED`；任一项存在，`ready` 恒为 `False`、`limit` 恒为 `unavailable`。

## P1：`core_signal` 硬编码

`analysis.py:660-667` 调用 `build_structure_evidence(..., core_signal=None, ...)`。
`multi_cycle_confirm.py:309` 计算 `core_signal_valid = (norm_core_signal in BUY_SIGNALS) and (trend_confirm is True)`；
`None` 不在 `BUY_SIGNALS` 内，因此 `blocked_by` 恒含 `"core_signal"`，`outcome` 恒为 `WAIT`。

这是**刻意**的（`bab9f66`、`e6d2377` 系列提交的方向是 fail-closed），本文不主张直接放开。

## P2：`120m` 的算术不可能（本轮新发现）

实测（2026-09-17 15:00，Asia/Shanghai，构造一个完整交易日 48 根 5m 闭合线）：

```
周期    闭合根数   门禁要求  满足？
120m         2          3      否
30m          8          4      是
15m         16          8      是
5m          48         12      是
```

根因：`resample_minutes` 对 `day.sessions` 逐段重采样，**不做跨 session 合并**（`resample.py:226-249`，
该模块 docstring 明确写「No sorting, filling, skipping, or cross-session merging is performed」）。
CN 一个交易日的 120m 桶恰好是 2 个（上午 09:30–11:30、下午 13:00–15:00），
而 `DEFAULT_MIN_BARS_BY_CYCLE[Timeframe.MIN_120] == 3`。

同时 `load_minute_context` 只接受**当天**证据：
`_snapshot_context` 要求 `snapshot.day.market_date == now.date()`，否则 `分钟快照校验失败`；
`resample._validate_lines:107` 要求 `timestamp.date() == day.market_date`。

因此只要走「当天分钟快照」这条生产路径，`120m` 必然 `insufficient`，
`blocked_by` 恒含 `"120m"`，`reason_code` 为 `STRUCTURE_DATA_MISSING`。

反证：把同样 5 个周期**手工喂成跨 2 日**的 120m（4 根）后，同一次调用直接得到
`outcome=CONFIRMED, ready=True, blocked_by=()`。所以引擎本身是可用的，
不可达来自**输入装配**，而非判定逻辑。

## P3：买点标志位没有生产者

`ChanSignalEngine.evaluate` 的四个买入分支分别要求
`first_buy`、`third_buy`+`zhongshu_breakout`、`second_buy`+`zhongshu`、`class_second`。
而全仓 `git grep` 显示生产代码里唯一组装 `buy_setup` 的位置是 `analysis.py:470`，
它只传 `trend_confirm` 与 `multi_cycle_confirm` 两个键。
其余键从未被任何生产模块计算过。

因此从组合路径出发，`ChanSignalEngine` 只可能返回 `WAIT` 或 `SELL_RISK`；
即使 P1 解除，也没有可用于 `core_signal` 的真实买点信号。

## 判定与下一步

1. **本轮不解除任何门禁**。放开 P1/P2 会让报告出现「看起来确认了」的结构，
   而在 P3 没有真实买点生产者的情况下，这正是最危险的伪信号。
2. **先补真实数据**：目前 4 个分钟周期在生产路径上全部是 `missing`，
   根因是 `minute_snapshot_loader` 从未被任何组合根注入。
   先接入真实 5m 数据，把 30m/15m/5m 变成可审计的 `closed`，是解除 P2/P3 的前提。
3. **P2 的解法是装配而非放宽**：让 `120m` 能取到 ≥3 根（跨 session/跨日），
   而不是把阈值从 3 降到 2。降阈值会削弱「核心买点」这一最重的周期，明确不采纳。
4. **P3 需要一次显式的建模决策**：由谁计算 `second_buy` 等标志位、在哪个周期上计算、
   以及它与 `multi_cycle_confirm` 的先后关系（当前两者互相依赖，存在循环依赖风险）。

## 明确不采纳

- **把 `min_bars_by_cycle[120m]` 从 3 降到 2**：`120m` 是 `CORE_CYCLE`（核心买点），
  1 天 2 根不足以构成中枢/线段级别的结构证据，降阈值等于用更少的证据下更重的结论。
- **把 `core_signal` 直接写成 `ChanSignal.SECOND_BUY`**：会在没有买点生产者的前提下
  让 `CONFIRMED` 可达，等于凭空制造买点。
- **用 5m 或本地时间伪造 120m 历史**：违背 `58c06b3`「采集源自身的时间，而不是凭空编造」的既定决策。

## 与安全边界的关系

以上全部只影响**分析完备度与展示**，不触及交易。系统仍为
`分析=自动 / 提醒=自动 / 交易=人工确认`，`auto_execute` 恒为 `False`。
`CONFIRMED` 不可达**不会**产生错误交易指令，只会让「多周期证据」长期停在
「待补齐」而不是给出一个未经验证的确认。

