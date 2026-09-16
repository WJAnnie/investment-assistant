# GitHub ↔ ChatGPT 数据契约 V2

> 规则基线：《股票研究与交易体系 V8.1》
> 本文件描述数据交接约束，不另行重编号交易规则；防抢跑和禁止旧数据冒充新时点是交接门槛。

## 0. 隐私前置条件

完整账户快照、失败诊断和 manifest 均属于 `private_account_data`，只允许在已验证的私有
运行/数据仓库保存，由获授权的消费端读取。密钥脱敏不等于账户信息可公开。
快照、manifest 及其 current/latest 条目的 `data_classification` 必须严格匹配该值；
字段缺失、其他值或未知可见性均拒绝，不给旧快照自动补标签。V1 仅可作为私有历史保留，
不能当作当前证据；已有无分类 V2 文件需要显式重新生成，不能静默升级。

`publishable=true` 只表示 current 通过数据交接检查，不表示允许公开，也不证明已经推送或
ChatGPT 已收到。公开入口保护已部署。私有存储仓库 `WJAnnie/investment-assistant-private`
已创建但为空、Actions 已关闭；执行设备、私有数据分支和消费端权限尚未配置。ChatGPT 任务页
已存在四个公开研究提醒，但它们不代表私有仓库读取已授权。公开旧引用已清理，
服务器悬空提交仍需 GitHub Support 处理。本文件是旧私有导出协议；它的固定 rule_version 8.1
不等于五日观察的源码/配置 SHA256，不能拿它作为冻结版本证明。
接入及验证边界见 [私有接入说明](chatgpt-github-automation.md)。

## 1. 双时钟设计

| 字段 | 含义 | 示例 |
|---|---|---|
| \`data_cutoff\` | 分析口径截止的市场时间 | \`2026-09-14T14:30:00+08:00\` |
| \`generated_at\` | GitHub 实际完成生成的时间 | \`2026-09-14T14:34:21+08:00\` |

ChatGPT 报告应写"截至 14:30"，而不是"截至 14:34"。

上表是目标语义，不是实际运行证据。时间标签本身不能证明采集完成或推送成功，
`data_cutoff` 必须与实际数据来源时间核对，不能仅凭标签宣称拥有该时点行情。
新的四阶段流程另行验证采集前、发送前和发送后的时钟，拒绝迟到补发和时钟倒退；
详见[观察账本契约](live-acceptance-contract.md)。旧导出工作流保持停用，未用于真实验收。

## 2. 快照结构（Schema V2）

\`\`\`json
{
  "schema_version": 2,
  "rule_version": "8.1",
  "data_classification": "private_account_data",
  "market_date": "2026-09-14",
  "data_cutoff": "2026-09-14T14:30:00+08:00",
  "generated_at": "2026-09-14T14:34:21+08:00",
  "execution_status": "completed",
  "analysis_state": "READY",
  "bar_status": {"daily": "CLOSED"},
  "quality": {
    "ready": true,
    "missing_sources": [],
    "stale_sources": []
  },
  "producer": {
    "type": "github_actions",
    "repository": "WJAnnie/investment-assistant-private",
    "workflow": "Publish ChatGPT market data",
    "run_id": "...",
    "run_attempt": "...",
    "source_sha": "..."
  }
}
\`\`\`

## 3. 四个分析状态

| 状态 | 含义 | 消费端动作 |
|---|---|---|
| \`READY\` | 关键数据齐全 | 允许完整分析，但只进入人工复核 |
| \`DEGRADED\` | 部分数据缺失 | 说明缺口；受影响条件保持 WAIT |
| \`NOT_READY\` | 数据尚未生成 | **只提醒补数据并保持 WAIT** |
| \`FAILED\` | 关键行情/计算失败 | **只提醒故障并保持 WAIT** |

## 4. 消费端（ChatGPT）读取流程

先确认授权的私有仓库及准确分支，再验证 manifest、current/latest 条目、目标快照的私有
分类。目标为 `WJAnnie/investment-assistant-private`；该仓库当前没有快照或数据分支，
上文只是结构示例，不是实际产物或启用私有 Actions 的声明。以下是消费端要求，不代表
公开研究提醒任务已经启用；私有 GitHub 数据的自动调用、连接器运行上下文与通知仍须独立联调。
manifest 的 `publishable` 只针对 current；若它与预期场景条目的 run_id/cutoff/哈希不一致，
保持 WAIT，不能用另一个场景的可用状态背书本次数据。

1. 读取 \`manifest.json\`
2. 校验 manifest 的 \`publishable\` 为 true
3. 校验 \`report_kind\` 与当前任务匹配
4. 校验 \`market_date\` 为预期交易日
5. 校验 \`data_cutoff\` 精确匹配报告类型的标准时刻
6. 校验 \`generated_at\` 不早于 \`data_cutoff\`
7. 校验 \`analysis_state\` ∈ {READY, DEGRADED}（NOT_READY/FAILED → 输出 DATA_NOT_READY 并保持 WAIT）
8. 校验 \`execution_status\` 为 completed、\`rule_version\` = 8.1
9. 校验 \`run_id\` 未重复消费
10. 校验 sha256 与实际文件一致
11. 校验 \`bar_status\`（任何必需周期为 FORMING/UNKNOWN/MISSING 时保持 WAIT）

生产端 manifest 还会拒绝 `generated_at` 晚于 manifest 构建时间、距构建时间超过 30 分钟，或
`market_date` 与上海当日不一致的 current 快照。closing 场景要求所有已报告周期均为 `CLOSED`；
盘中场景可携带已知的 `FORMING` 状态供研究解释，但消费端仍必须保持 WAIT。

任一步失败 → 输出 \`DATA_NOT_READY\`，**不得**用旧时点数据 + 新搜索结果拼凑当前时点结论。
即使全部校验通过，输出也仅供研究与人工复核；系统不连接券商、不自动下单，是否交易由本人
确认并手动执行。

## 5. 调度时序

\`\`\`text
14:30  行情截止（data_cutoff）
14:31+ GitHub Action 启动
14:34~ 快照生成并推送（generated_at）
14:38~ ChatGPT task 读取
\`\`\`

上面只是期望时序，不是已创建的任务或准点保证。只有消费端确实支持自动授权读取且本次
数据就绪时才能继续；未验证的 ChatGPT 定时任务/应用组合不能当作已接通。

## 6. Provenance 字段

每个快照必须携带 \`producer\`（GitHub run_id / run_attempt / source_sha / workflow），manifest 同时记录每个 latest 快照的 sha256 与 git_blob_sha，支持完整审计与回放。

## 7. 分钟证据：可研究不等于已闭合

Schema V2 保持兼容，顶层 `bar_status` 仍只使用 `CLOSED`、`FORMING`、`UNKNOWN`、
`MISSING`。其中 `UNKNOWN` 包含无效输入（内部 `invalid`）或无法识别的状态，并非仅指
“暂时不知道”；`UNKNOWN` / `MISSING` 都禁止 manifest 宣称可发布。具体错误原因和
原始状态应读取 `result.analysis.items[].minute_context`，不能将 `UNKNOWN` 推断为闭合。

以下字段含义不能互换：

| 字段 | 能证明什么 | 不能证明什么 |
| --- | --- | --- |
| `quality.ready` | 分析覆盖与本次执行结果达到报告就绪条件 | 可跳过 manifest 校验、所有分钟周期已闭合或策略条件通过 |
| manifest 的 `publishable` | current 快照通过当前场景的数据交接检查 | 公开发布许可、已推送/已通知、其他场景已就绪、所有分钟周期已闭合或策略条件通过 |
| `minute_context.status = ready` | 当前时段的时间与数据证据已知，可含 `forming` | 分钟输入已全部闭合 |
| `minute_context.input_ready = true` | 已报告的四个分钟周期当前桶闭合，且没有已开始的历史缺口 | 周线/日线/分钟多周期结构确认、买点或风险门通过 |
| `minute_context.multi_cycle_confirm` | 本阶段固定为 false | 不得从其他 ready 字段反推 true |

例如 14:30 的盘中快照可以 `quality.ready=true`、`publishable=true`，但下午 120m 为
`FORMING`、`input_ready=false`。消费端只能说明仍在形成，保持 WAIT，提醒 15:00 后
**且基础 K 线齐全**再人工复核，不能把发布时间或 ready 标志当作买点确认。收盘场景仍
要求所有已报告周期为 `CLOSED`；即便全部闭合，也不代表策略信号成立。

未配置分钟加载器时，`minute_context.configured=false`；不能因为旧版日线报告仍为
ready 就宣称拥有分钟数据。`next_trigger` 仅是报告中的人工复核条件，不创建定时任务，
`auto_execute` 始终为 false。相关回归位于 `tests/test_minute_context.py`。
