# Investment Assistant

个人 AI 投资研究助手。

上线状态（2026-09-16）：公开 GitHub 仅运行公共行情与固定合成账户，不读取真实账户。
旧账户导出工作流已停用；公开 main 已替换为经扫描的无父根历史，旧账户数据分支、
已识别旧运行日志及缓存已清理。服务器悬空提交仍可能访问，需 GitHub Support 后续处理。

本地已实现四时点调度、首份阶段证据、同日晨报比较和私有五日观察账本；没有启动常驻进程。
隔夜全球八标的公共观测已接入；行业、基本面及完整多周期来源仍未齐，整体仍为降级分析。
账本已接入可选的私有观察，真实验收未开始。
观察仅记录已知事实和失败，不认证数据/回执，也不因软件测试通过而宣布五日验收通过。
此会话没有 ChatGPT Tasks 管理工具，没有创建自动提醒或验证 ChatGPT/设备收件。
详见[当前实施记录](docs/live-completion-and-privacy-plan.md)和[验收契约](docs/live-acceptance-contract.md)。

## 项目定位

Investment Assistant 用于辅助个人投资研究，不进行自动交易。

研究能力目标（以下并非全部已接通，实际就绪状态见下文）：

- 多周期行情分析
- 技术指标分析
- 缠论结构分析
- 行业轮动分析
- 基本面分析
- 新闻事件影响分析
- 持仓管理
- 风险控制
- 飞书消息推送
- 交易复盘记录

## 目标架构

```
行情数据
  ↓
技术分析
  ↓
缠论分析
  ↓
基本面 / 行业分析
  ↓
风险过滤
  ↓
投资决策
  ↓
飞书通知
```

## 当前版本

v1.2.0 research pipeline

当前已打通：

- 行情主源/备用源和内存缓存；
- EMA、SMA、MACD、RSI、KDJ、布林带和趋势评分；
- K 线包含关系→分型→笔→线段→中枢候选→背驰→统一信号；
- 技术评分与风险优先的人工复核决策；
- 日报格式化和可选飞书通知；
- 独立新浪实时指数快照和命令行一键入口；
- 可注入的 A 股分钟闭合证据：5m 会话内聚合为 15m/30m/120m，并给出下一人工复核条件；
- 九时点人工复核清单、无凭据离线预览和显式启用的全日提醒模式；
- 全流程的离线可重复测试。

## 运行和测试

项目默认不发起外部行情请求，也不会自动交易。实际数据源通过
`MarketCollector` 的 `primary`/`fallback` 注入，便于离线测试和故障降级。

```bash
# 单标的一键运行日报（默认输出 JSON，不自动交易）
python -m app.main --code 000001 --start 20260101 --end 20260911

# 多标的运行；不发送飞书通知
python -m app.main --code 000001 --code 510300 --no-notify

# 只取当前 A 股主要指数实时快照，不依赖历史 K 线
python -m app.main --snapshot --no-notify

# 常驻运行工作日定时发送五类不同报告（06:30、09:00、11:30、14:30、16:10）
python -m app.main --schedule

# 私有常驻设备：四时点报告＋首份阶段证据（不是 ChatGPT Tasks，也不启动五日账本）
python -m app.main --schedule --schedule-profile four-stage --private-state-dir .private/stages

# 已事前冻结私有账本后，才可显式接入观察；这不是启动验收的快捷方式
python -m app.main --schedule --schedule-profile four-stage --private-state-dir .private/stages --live-ledger .private/trial.jsonl

# 只读查看既有私有账本（不采集行情、不通知、不启动调度）
python -m app.integration.live_trial status --ledger .private/trial.jsonl

# 仅预览人工复核清单：不读取凭据/持仓，不采集行情，不发送、不注册任务
python -m app.main --reminder-plan --at "2026-09-14T14:30:00+08:00"

# 显式选择九时点清单提醒（仅示例；需自行启用进程及通知配置）
python -m app.main --schedule --schedule-profile full-day

# 手动运行一次 A/B/合并账户收盘报告；不发送飞书
python -m app.main --portfolio --report-kind closing --no-notify

# 盘中分钟数据（1/5/15/30/60 分钟，取决于数据源权限）
python -m app.main --code 000001 --period 5 --no-notify

python -m unittest discover -s tests -v
python -m compileall -q app tests
```

## GitHub 公开测试与私有运行边界

`.github/workflows/public-trial.yml` 是当前公开测试入口：四时点工作日调度、固定国内指数、
晨报八项全球公共观测和合成账户，产物写入独立的 `public-test-data` 分支。它不读取真实账户或私有仓库，
不代表完整分析或真实账户验收。GitHub cron 可能延迟，不能保证 14:30 准点送达。

旧 `publish-chatgpt-data.yml` 工作流已停用；其代码只允许经过验证的私有执行和私有导出，
不得为了绕过额度限制在公开 runner 注入真实持仓。Secrets 加密保存不保护程序输出、
公开日志或计算后的衍生结果。公开测试说明见[运行手册](docs/public-github-trial.md)。

私有 Windows 必须在运行窗口持续开机联网。ChatGPT Tasks/私有数据授权与设备收件
尚未打通；飞书 API 接受消息不能作为 ChatGPT 已读或用户设备显示的证明。
接入边界见[私有接入说明](docs/chatgpt-github-automation.md)。

命令退出码为 `0` 表示全部标的完成，`1` 表示至少一个标的失败。批量结果会保留每个
标的的错误信息，便于诊断。冻结的五日观察不重跑补账，不用成功重试掩盖失败。复制 `.env.example` 为
`.env` 后，可配置 `TUSHARE_TOKEN`、群机器人 `FEISHU_WEBHOOK`，或应用机器人直发所需的
`FEISHU_APP_ID`、`FEISHU_APP_SECRET`、`FEISHU_RECEIVE_ID` 和 `FEISHU_RECEIVE_ID_TYPE`。

日报流水线入口是 `app.workflow.daily.daily_report_job`。传入 `prices` 即可运行
技术分析；传入 `k_lines` 后会额外执行完整缠论结构流水线。通知器是可选的，优先使用
`FEISHU_WEBHOOK`；未配置 Webhook 时，如果应用机器人字段齐全，会按
`FEISHU_RECEIVE_ID_TYPE`（默认 `chat_id`）直发到目标会话，否则安全跳过发送。

多账户入口是 `app.workflow.portfolio.run_portfolio_report`。真实账户配置只保留在私有
`config/portfolio.yaml`，公开仓库仅提供 `config/portfolio.example.yaml` 合成示例。
README、公开日志与导出产物不保存真实账户数量、成本、现金或净值。
数量不明时不推算金额收益；报告中的技术辅助分不等于完整综合分。

## 行情接口接入

当前已接入 `AkShareProvider` 的快照适配层，按代码自动选择：

- 六位 A 股代码：`stock_zh_a_spot_em`；
- 六位 ETF 代码（如 `510300` 或 `SH.510300`）：`fund_etf_spot_em`。

它会统一转换成 `Quote` 所需字段，并在单次快照 TTL 内复用全市场结果；实际使用时
仍应通过 `MarketCollector` 保留缓存、校验和故障降级：

```python
from app.market.akshare import AkShareProvider
from app.market.collector import MarketCollector

collector = MarketCollector(primary=AkShareProvider())
quote = collector.get_quote("510300")
```

当前版本先不接入港股代码，聚焦 A 股和境内 ETF。AkShare 文档中的 A 股和 ETF 快照接口
适合本项目的研究型轮询。历史日线/分钟线也有对应接口，但接入缠论前仍需统一复权、交易日
和字段语义。`TushareProvider` 已接入为可选历史 K 线源：它通过 REST API 获取 A 股/ETF
日线和可选的实时分钟线，自动转换为按时间升序排列的 `KLine`；需要在 `.env` 中提供
`TUSHARE_TOKEN`。

```python
from app.market.factory import create_default_collector
from app.workflow.daily import daily_report_job

collector = create_default_collector()
result = daily_report_job(
    "000001",
    collector=collector,
    fetch_history=True,
    history_start="20260101",
    history_end="20260911",
)
```

批量研究使用 `app.workflow.batch.run_daily_reports`，传入 A 股/ETF 代码列表即可；
单个标的数据失败时不会中断整批，结果会标记为 `partial` 并保留每个标的的错误信息：

```python
from app.workflow.batch import run_daily_reports

batch = run_daily_reports(["000001", "510300"], collector=collector)
```

行情可靠性分为两种情况：AkShare 主源仍依赖外部行情站点和本地网络，无法由项目承诺
永不失败；`MarketCollector` 会按 AkShare → 新浪公开实时接口 → Tushare 的顺序降级，
并对无效数据明确失败，不会伪造价格。`--snapshot` 走独立新浪实时接口，只取当前观察指数，
不依赖历史 K 线，适合盘中临时简报和定时快照。配置有效 `TUSHARE_TOKEN` 后，Tushare
适配器会优先尝试实时分钟接口，失败时回落到最新日线收盘价。A 股实时分钟使用
`rt_min_daily`，ETF 使用 `rt_etf_min_daily`；这两类接口都受 Tushare 账户权限和交易时段
影响。Tushare 的实时分钟接口需要相应权限，Token/HTTP 接入方式见官方说明：
[`rt_min_daily` A 股实时分钟](https://tushare.pro/document/2?doc_id=457)、
[`rt_etf_min_daily` ETF 实时分钟](https://tushare.pro/document/2?doc_id=470)、
[`Token 与 HTTP 接口](https://tushare.pro/document/1?doc_id=40)。
港股个股行情目前不作为可靠实时主路径承诺；港股通科技基金、全球医疗等场外品种优先展示
有效净值日期，并在数据陈旧或失败时降级为基准值估算。恒生科技指数通过独立适配器纳入监控，
接口失败时保留失败状态，不伪造指数点位。

定时任务仅在进程持续运行时生效，工作日发送：06:30 全球市场扫描、09:00 投资晨报、11:30
午盘分析、14:30 人工交易复核提醒、16:10 A/H 股统一收盘复盘。五个阶段使用同一份 A/B/合并账户
数据，但各自呈现对应场景：隔夜缺口、开盘观察、同日参考价变化、人工复核条件与收盘检查。
午盘/收盘不再把累计成本收益当作日内贡献；没有同日基线或事前预测时明确无法比较/评价。未配置飞书 Webhook 或完整
应用机器人凭据时，行情仍会采集，通知会安全跳过。

当前的缠论识别是可审计的候选结构实现，不等同于交易所级别的生产行情服务；上线前
仍需接入经过验证的数据源、补充多周期真实 K 线校验，并由使用者人工复核。

## 分钟闭合证据与人工复核提示

`app.market.minute` 提供独立的分钟基础模块。它只接受来源明确、带时区、使用
**收盘时间戳**的 5m K 线，以及调用者核实的当日交易时段。A 股标准时段为
09:30–11:30 / 13:00–15:00；支持显式休市或半日市，不以工作日猜测开市。
聚合不跨午休、不补造缺 K；未闭合、缺失或无效的桶不输出可供确认的 OHLC。

多账户入口 `run_portfolio_report` 和 `analyze_portfolio` 均可通过可选参数
`minute_snapshot_loader` 接入。加载器签名为 `loader(holding, *, now)`，返回
`MinuteSnapshot`，明确声明标的、市场、交易日历、来源、获取时间、`base_minutes=5`
和 `timestamp_semantics="close"`。单标的加载或校验失败会隔离，并保留日线分析；
已到期的历史缺 K 不会被后续完整数据掩盖。未配置加载器时保留原有日线状态，明确提示
“分钟数据未接入”。当前命令行的 `--period 5` 不等于接入了此闭合证据契约。

报告新增 `minute_context`、各周期 `bar_status` 与 `next_trigger`：在已核实的 A 股
常规交易日，14:30 的下午 120m 仍在形成，提示 15:00 后且数据齐全再人工复核。
`next_trigger` 是报告中的提示，本身不创建定时任务或发送通知；legacy 五时点仍保留；four-stage 须显式选择。`input_ready` 只代表分钟输入
闭合且无已到期缺口，`multi_cycle_confirm` 始终为 false，不会据此生成买入许可。

本阶段尚未接通经过核实的生产分钟源、官方交易日历、港股分钟时段或完整六周期信号
引擎。接口见 [MinuteSnapshot 与上下文校验](app/market/minute/context.py)，
异常反例见 [分钟边界回归](tests/test_minute_boundary_review.py)。

## 全日人工复核提醒

`--reminder-plan` 输出上海当日的 JSON 清单预览；可用 `--at` 指定带时区的 ISO 时间，
如 `2026-09-14T14:30:00+08:00`，或等价 UTC 时间。不带时区、非法时间和冲突模式
会在配置/通知初始化前拒绝。此模式不读 `.env`、持仓、行情或新闻，不注册或发送提醒。

`--schedule` 继续使用原有五类行情报告；`--schedule --schedule-profile full-day`
才选择下列九点人工检查清单。干净 CLI 进程只注册所选模式，全日清单不调用行情报告
流水线，也不修改 GitHub 五类 `report_kind` 或 Schema V2。`--schedule --no-notify`
会明确报错，不再静默忽略“不发送”；无发送检查请使用预览模式。

| 上海时间 | 人工检查事项 |
| --- | --- |
| 06:30 | 隔夜市场、开休市状态与数据日期 |
| 08:10 | 政策、公告、业绩及消息来源 |
| 08:45 | 盘前观察项、风险预算与失效条件 |
| 10:00 | 开盘后已闭合、已到齐的数据 |
| 11:30 | 午盘变化与风险暴露 |
| 13:15 | 午后变化、周期闭合情况 |
| 14:30 | 人工条件复核与证据缺口 |
| 16:10 | A/H 收盘状态、基金净值日期与归因 |
| 20:30 | 公告、财报、行业深研及后续待核实事项 |

这些是**工作日提醒时点，不是交易日历**。工作日节假日仍会列出未验证的检查清单，
必须人工分别核对 A 股、港股、半日市和数据日期；周末清单为空，不推算下一交易日。
10:00、13:15 不保证数据已到齐；15:00 仅是 A 股常规交易日的条件提示，
不泛化至港股或半日市，也不会自动新增一个 15:00 任务。

每条清单带完整日期/时区、有效期和稳定提醒 ID。仅在时点开始后的 5 分钟内尝试发送，
到期即跳过、不补发；过期不代表已发送或已完成。发送只尝试一次且须通知器明确确认，
ID 用于追踪，不保证跨进程去重。所有阶段保留 `WAIT / NOT_READY / UNVERIFIED`
和 `auto_execute=false`，不宣称研究完成，不产生交易指令。
legacy 五类报告保留 30 分钟调度宽限；four-stage 仅允许时点后的 10 分钟内启动和发送。
调度层不因一次未确认而重复发送；这只限制单次调用，不能保证跨进程恰好一次。
九点清单采用独立的 5 分钟有效期。

本轮只完成本地实现和离线验证，**没有启用常驻进程或真实通知**。实际发送依赖进程
持续运行及有效飞书配置；生产交易日历、实时事件、周末/月季任务尚未接入。
接口见[清单生成器](app/workflow/reminders.py)；上线与验证边界见
[当前实施记录](docs/live-completion-and-privacy-plan.md)。

## 四阶段证据与五日观察账本

`--private-state-dir` 只允许搭配 `--portfolio` 或 legacy/four-stage 报告调度；
不能搭配 snapshot、单标的、reminder-plan 或 full-day 清单。路径必须是本项目未跟踪的
`.private/` 或其他 Git 工作树之外；链接、重解析点、硬链接及无法确认的路径会被拒绝。
这些保护不是加密，可信主机、目录权限和备份仍需单独配置。

四时点为上海时间 09:00、11:30、14:30、16:10。当前只注册工作日，
`calendar_status=UNVERIFIED`，不是已核实的交易日历。采集前后均检查当日 10 分钟窗口；
过期、跨日、时钟倒退时不发送、不补发。首份阶段文件不覆盖，损坏文件不自动“修复”。

同日比较要求账户/数量口径、来源、规则与时间匹配。收盘只评价事前存档、带目标期限的预测；
目前没有自动预测生成器，不能把昨日判断补填成正确。`status=completed` 仅代表流水线完成，
`full_analysis_ready` 仍为 false；缺失行业、基本面和多周期确认时保留 WAIT / +0%。

`LiveTrialLedger` 通过 `--live-ledger` 显式接入四阶段私有观察，其他模式不隐式启用。
执行锁覆盖检查、采集、发送和落账，已有结果不重发，崩溃残留锁不自动删除。
它冻结首五个连续交易日与规则，计入失败和遗漏，不允许挑日或补跑凑数。
来源与人工审查未齐时 observer 保持 `data_complete=false`。即使 20 个时点的本地字段都齐全，
也只返回 `pending_verification`，不宣称真实验收通过。详见[账本契约](docs/live-acceptance-contract.md)。

## 风险说明

本项目用于投资研究和纪律辅助，不构成投资建议，也不会自动执行交易。
