# 私有五日观察账本契约

更新：2026-09-15。对应 `app/workflow/live_acceptance.py`。
这是可审计的本地观察账本，不是独立验收机构，也不能证明策略收益。
当前已通过显式 four-stage 调度接入未认证观察；没有创建真实试验，真实五日验收未开始。
接线位于 `app/workflow/live_observation.py`，私有管理 CLI 位于 `app/integration/live_trial.py`。

## 冻结输入

`LiveTrialLedger.create()` 接收私有 `path`，以及：

- `trial_start`：计划起始 ISO 日期。
- `rule_version`：冻结的实际源码/配置版本；CLI 与报告复用 `portfolio_rule_version()`，
  包括是否启用分析，不手填“新版本”绕过失败。
- `calendar_source`：明确的日历来源标识。
- `ordered_trading_dates`、`calendar_coverage`：严格升序的日期序列，均至少五日。
  有序区间不能遗漏 coverage 中的交易日；不允许周末或重复日期。
- `calendar_verified`：兼容字段，默认 false。即使传 true 也只是调用者声明，
  不是本组件查询或认证过交易所日历。
- `clock`：默认真实上海时钟；显式注入仅用于测试或可信运行环境。

账本冻结起始日及之后的首五个交易日，不能跳过失败日，也不能拿第六天替换失败的第五天。
日历名单仍须独立核验节假日、覆盖范围与市场语义；“五个工作日”不是充分证据。
创建时间必须不晚于首个选定交易日 09:00。晚建、覆盖已有文件、修改冻结 header 都被拒绝。

## 显式私有接线

初始化不采集账户或行情、不加载 dotenv、不启动调度、不发送通知：

```text
python -m app.integration.live_trial init --ledger .private/trials/TRIAL-ID/ledger.jsonl --calendar .private/calendar.json --start YYYY-MM-DD
python -m app.integration.live_trial status --ledger .private/trials/TRIAL-ID/ledger.jsonl
```

`YYYY-MM-DD` 是待明确的将来起点，不是可直接运行的日期。日历文件须在私有路径，精确包含
`schema=private_trial_calendar.v1`、`source`、`ordered_trading_dates`、`calendar_coverage` 和布尔
`verified`；不接受重复 JSON 键、额外字段或把字符串 true 当作布尔值。名单必须来自已核实的
交易日历，不自动生成五个工作日。即便 `verified=true`，状态仍是调用者声明，不是独立认证。

私有常驻主机与通知配置就绪后，显式加载已存在的账本：

```text
python -m app.main --schedule --schedule-profile four-stage --private-state-dir .private/trials/TRIAL-ID/stages --live-ledger .private/trials/TRIAL-ID/ledger.jsonl
```

`TRIAL-ID` 是本次试验的独立目录名。一试验专用一对 ledger/StageStore，不与 one-off CLI、
回放或其他试验共用；这是运维约束，不是目录归属的代码强制隔离。
账本和目录在 dotenv、账户及通知器初始化前校验。执行锁覆盖时点检查、预约、采集、发送与落账。
采集前先在账本持久化唯一的 run/时点预约，只有本次新预约成功才继续。已有预约、首份阶段或
任意分析记录均不能用于重新授予执行权。首份阶段存在而账本缺项，可能是发送后崩溃，不能重试。
运行 ID 显式传入流水线并核对返回身份，冲突时禁发，不把别的运行结果改 ID 后纳入。

投递后重新采样真实时钟、重新读取 StageStore 证据；晚送照实记录，时间无效或倒退不能伪造
准点证据。调度、采集与阶段写入共用受检时钟；任一已采样时间倒退/无效，即使被下层捕获
或之后恢复也保持本次禁发。发送前重读实际首份阶段，校验生成/持久化时间，再采样发送时钟。
规则变化、普通采集失败或缺首份阶段时，可在时钟正常且未过期的窗口内发送安全 WAIT 提示，
明确本时点不重跑补账。时间或身份校验失败不改发 WAIT。异常只留类型，不输出私有载荷。

观察器始终保持 `data_complete=false`：完整来源和人工审查尚未实现，不能接受调用者传入的
`full_analysis_ready=true` 作为证明。它不生成 API 回执 ID，也不复制结果中自称的 ChatGPT/设备
收件标记。API 接受必须同时有本次尝试、notified 和 delivery 的严格布尔 true。
`recorded_unverified` 只表示已落账；`record_failed` 表示本次落账失败，原投递结果仍保留。

已有真实回执可通过私有 JSON 文件追加：

```text
python -m app.integration.live_trial record-receipt --ledger .private/trials/TRIAL-ID/ledger.jsonl --receipt-file .private/receipt.json
```

回执字段精确对应下节的 `ReceiptEvent`。命令不校验外部渠道真实性，不自动生成回执，
不允许覆盖真实写入时钟；退出码 0 仅表示命令完成，不表示验收通过。

## 时点预约、分析事件与回执

`reserve_attempt(run_id, trading_date, stage)` 在 append 锁内采样并 fsync 写入 `record_type=attempt`。
payload 只有 run_id、trading_date、stage；必须在冻结五日和对应窗口内，同一 run 或时点只占用一次。
预约不是分析、通知尝试、接口接受或收件证明，不能作为回执父记录。后续 analysis 必须与已有
预约的身份一致，观察时间不能早于预约。预约后无 analysis：窗口内 pending，超时 failed，绝不计 complete。

保留 schema 1 的旧 analysis/receipt 可读性；新增 attempt 由新解析器处理，不能降级后丢弃。
源码变化会改变冻结规则 hash；试验开始后不得改 header、清空失败或删预约来沿用旧试验。

`append(SlotEvent(...))` 记录一次分析，字段分为：

| 类别 | 字段 |
| --- | --- |
| 身份/来源 | run_id、trading_date、stage、rule_version、ingested_at、mode、storage_visibility、calendar_source |
| 完整性与投递 | data_complete、report_valid、api_accepted、chatgpt_received、device_received |
| 证据引用 | data_evidence_id、report_fingerprint、api_receipt_id、chatgpt_ack_id、device_receipt_id |
| 质量检查 | strategy_success、wait_reason、user_review_second_buy_ok、no_overtrading_ok、position_increment_validated |
| 质量证据引用 | strategy_evidence_id、review_evidence_id、position_evidence_id |
| 可选指标 | latency_seconds（有限、非负数字，不能是布尔值） |

证据引用必须为非空字符串或 None，布尔值/数字/容器不能冒充回执 ID。
这些引用在本组件中只是引用，尚没有外部系统验证其存在、内容或真实性。
`strategy_success` 也不代表赚钱或未来收益。

`append_receipt(ReceiptEvent(...))` 允许之后分别补充两个真实渠道的回执：
`run_id`、`trading_date`、`stage`、`receipt_observed_at`、
`chatgpt_ack_id`、`device_receipt_id`。至少提供一个渠道。

- 回执必须引用已有分析的同一 run、日期和阶段，不能早于该分析写入时间。
- run ID 不可重复；同一 run 的同一渠道不能重复追加；同一渠道的回执 ID 不可复用到其他 run。
- 分别追加 ChatGPT 和设备回执会累积，不相互清空。
- 飞书/API 返回 true 只证明接口接受，不能合成 ChatGPT 或设备回执。
- 无预约的旧/手动 analysis 中，同一时点不同 run 可保留以审计；任何失败都不能被成功样本覆盖。
  已预约时点只接受该预约的分析；手动 append 不等于获得调度执行或补发权限。

## 时间与计数

四阶段固定为上海时间 09:00、11:30、14:30、16:10。
每个阶段采用闭区间 `[阶段开始, 阶段开始 + 10 分钟]`。
分析的 ingestion 和真实写入时间、回执观察和写入时间均须匹配选定日期及窗口。
迟到/补录事件可以保留以审计，但不能成为合格证据。

写入时钟在取得 append 锁后采样，记录时间不得倒退。只有 `clock is None` 才使用系统时间；
非 callable、callable 返回 None、无时区、错误类型或超出可表示范围的值均失败，不能静默换成当前时间。
时钟异常不回显其消息。注入测试时间不是实盘证明，不允许用回放时钟启动“真实”五日统计。

只有 `mode=trusted_private_live` 且 `storage_visibility=private` 的事件满足运行分类门槛。
模拟、回放和公开合成数据不计入真实验收。输入完整、报告有效、API 接受、ChatGPT 回执、
设备回执及质量审查是彼此独立的条件。

完整证据下的 WAIT 可以是有效研究结论；缺数据而 WAIT 不能冒充策略成功。
缺事件或可补充回执在窗口内为 pending，超过窗口仍缺失则 failed，不能无限等待。
错误规则、未声明核实日历、非私有运行或明确质量失败直接进入 failed。
人工二买、过度交易和仓位合理性审查仍需实际进行；不得为了使字段齐全自动填 true。

## 汇总输出（没有 passed）

`summary(as_of=None)` 默认使用真实上海时间；显式 `as_of` 仅为本地历史视图。
只计算截至该时间已写入的记录，但会验证整个文件。输出包括：

| 字段 | 取值/含义 |
| --- | --- |
| status | pending / failed / pending_verification |
| evidence_status | pending / failed / complete |
| independently_verified | 始终 false |
| calendar_verification | unverified / caller_claim_only |
| totals | complete、failed、pending 三类时点计数，共 20 项 |
| days[date].slots[stage] | status、evidence_status、reasons |
| verification_gaps | 独立日历、数据/报告出处、渠道回执及策略/仓位审查 |

即使 20 个时点的本地字段全部齐全，最高也只是 `pending_verification`。
没有 `passed` 状态，没有可手填成功开关，也没有“一键完成五日验收”命令。
日历的调用者 true 声明不会改变 `independently_verified=false`。

## 私有存储与威胁边界

- 路径仅允许本项目未跟踪的 `.private/`，或其他 Git 工作树之外。
  公开/未知可见性的 GitHub Actions 在读取或写入账户状态前拒绝。
- 逐层拒绝符号链接、Windows 重解析点和文件硬链接；路径检查失败时关闭功能。
- 严格 JSON 拒绝重复键及非有限值；单行上限 100,000 字节，总文件上限 2,000,000 字节。
- 首写使用 O_EXCL，append 使用进程间锁、缓冲写入和 fsync；损坏文件不自动覆盖。
  header 与每个事件的 schema、hash 链和前后关系在加载/追加时重新检查。
- 本地哈希只能发现不一致，不能认证数据来源；有写权限的人可重写并重算哈希。
- 本组件不是加密、ACL 管理器或不受信任主机上的安全沙箱。Windows 的私有访问权限、
  备份及可信父目录须在迁移时配置；检查和打开间的路径竞争仍依赖可信主机/父目录。
- 锁超时或残留锁必须先确认没有正在写入的进程；不自动删锁或把坏记录丢弃以继续计数。
  `.lock` 用于追加，`.run.lock` 用于整次调度，两者不互相替代；崩溃后的执行锁保留供核查。
- 普通异常且正常释放锁后，一个时点的完整预约不阻断后续不同的时点。硬崩溃残留锁、
  半行写入或损坏账本会整体关闭功能；没有自动恢复、过期重试或可靠送达保证。

## 当前集成缺口

本地已具备 StageStore、同日比较、四时点调度及上述账本组件，但并未连成已上线的真实试验。
仍缺可信常驻私有主机、核实的交易日历、完整分析来源、事前预测生成、人工审查接线与可核验的
ChatGPT/设备回执。此会话没有 ChatGPT Tasks 管理工具，未创建任务或授权私有数据消费。

回归使用固定合成账户/日历/回执，与真实运行证据严格分开。
