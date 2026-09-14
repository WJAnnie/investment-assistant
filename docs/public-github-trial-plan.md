# 公开 GitHub 四时点测试：实施与验收计划

## 首轮授权与范围（2026-09-14）

本文件保留首轮合成联调的设计记录。后续用户已授权清理旧公开历史；当前执行状态以
[完整分析与隐私计划](live-completion-and-privacy-plan.md) 为准，首轮限制不覆盖后续授权。

用户确认当前电脑不能常驻，先在现有公开仓库 `WJAnnie/investment-assistant`
测试运行，以后迁移到另一台 24 小时 Windows。公开测试仅包含固定公共指数与
明确标注的合成账户；真实账户、私有仓库、凭据、原工作树其余改动不随本次发布。
自动分析、自动提醒，不自动交易。测试结果不计入五交易日真实账户验收。

## 小范围实施顺序

1. 核验远端与本地差异，停用仍 active 的旧账户导出 workflow，避免并行运行。
   首轮不改可见性、不处理历史；后续专项清理另行私有备份、精确替换引用。
2. 主控实现独立 `app.integration.public_trial`，只复用纯 `SinaProvider`/`Quote`。
   不导入账户 exporter、factory、portfolio 或 dotenv；固定指数、固定字段、
   固定错误码，不输出来源自由文本/原始异常。没有可验证来源时间就标 UNKNOWN。
3. Antigravity 异步实现 `tests/test_public_trial.py` 的合成边界/导入隔离测试；
   主控补 workflow/通知测试，先复现失败再实现。不得读取真实配置或密钥。
4. 新增独立公开 workflow：标准 `ubuntu-latest`，北京时间工作日 09:00、
   11:30、14:30、16:10；手动 `all` 一次模拟四种版式。GitHub cron 可能迟到，
   工作日不冒充交易日历；手动回放不冒充当时采集。
5. 稀疏检出仅经过审查的源码/合成测试，无账户文件、pip 缓存或 artifacts。
   公开报告存入独立无源代码历史的 `public-test-data` 分支，精确暂存文件。
   日志/报告不回显外部响应、账户信息、通知地址或凭据。
6. 通知只发送带“公开联调 / 合成账户 / 非交易信号”标签的报告；若启用飞书，
   仅在独立通知步骤注入所需 GitHub Secrets，采集步骤不接触它们。
   API 接受不等于设备可见，GitHub 发布不等于 ChatGPT 自动消费。
7. 全量离线回归、编译/差异检查、独立安全审查后，隔离目录创建最小 Lore commit，
   只推送已审查文件。实际 dispatch，检查 run、公开分支内容与通知状态。
8. 更新部署证据及未来 Windows 迁移检查表，不以本轮测试代替未完成的产品功能。

## 公开契约与主控/子代理分工

新模块公开接口：

- `STAGES`：`morning`、`midday`、`decision`、`closing` 到北京时间时点的映射。
- `PUBLIC_SYMBOLS`：固定公共指数代码到固定中文显示名的映射。
- `build_snapshot(stage, *, provider, requested_at, generated_at, execution_mode='manual_replay')`
  返回纯 dict；时间必须为有时区 datetime，完成不得早于开始。
- `validate_snapshot(payload)`：严格字段、类型和固定值校验；非法输入抛 ValueError，
  错误只含固定信息；拒绝追加 account/token/free-text/private 分类。
- `render_report(payload)`：先验证，只渲染固定标签与已验证数值/时间。
- `write_reports(snapshots, output_dir)`：验证所有快照后，只写 README.md 和
  `latest/{stage}.json`，拒绝符号链接和不安全的已有输出结构。

数据分类 `public_market_and_synthetic_test`，schema `public-trial/v1`。
`purpose=delivery_test_not_investment_advice`，`execution_mode` 仅
`manual_replay` 或 `scheduled`，`real_account_trial_day=false`。
各指数只接受同一代码、finite 正价格、finite 涨跌幅，reject bool；
provider 返回 None/异常/错码/非法数值都转固定错误码。
provider 的 name/source/timestamp/market_time 均不直接发布。
当前 Sina 指数来源时间无可靠保证：`source_as_of=null`、`freshness=UNKNOWN`。
合成账户固定为 DEMO_A 10%、DEMO_B 5%、现金 85%，不得接受外部账户输入。
所有场景 action=WAIT、仓位变动 0%、评分空；全球/行业/午盘基线/多周期/事前配对
缺口如实标注。请求日期与场景时点固定在开始时，跨午夜完成不改写原场景日期。

主控负责实现与发布工作流、通知边界、集成及最终验收。Antigravity 的写入范围
仅限 `tests/test_public_trial.py` 与指定审查报告，不得改业务实现、配置或 Git 状态。

## 验收标准与非目标

- 正常、空返回、异常含敏感哨兵、NaN/Infinity/bool/零/负数/错码、额外字段、
  私有分类、路径/链接、时区/跨午夜等回归通过；纯公共导入不触发账户模块。
- 公共 workflow 不读取私有目录/真实配置，不注入 TUSHARE 或私有仓库 token。
  不使用 pull_request_target，不把 inputs 直接拼入 shell 命令。
- 远端旧 workflow disabled，新 workflow 的实际运行/公开产物单独核验。
- 通知只有明确 API 接受证据才标 accepted；缺凭据为 not_configured 并失败，
  拒绝/网络错误为 failed；仅主动关闭或重跑抑制时 skipped。
- 剩余产品工作：真实全球/行业/完整持仓、午盘事前基线、真实多周期、复盘配对、
  交易日历、ChatGPT 消费回执、五日实盘验收。公开合成测试不声称完成这些功能。
- 首轮不迁移真实账户、不安装本机常驻服务、不新增依赖；旧公开历史后续按专项计划治理。

## 执行证据

实施完成后追加实际测试、发布与回执；计划本身不是完成证明。

### 发布前边界补充（2026-09-15）

先补失败回归，再作三处小范围修复：显式无效完成时间必须在采集前拒绝；
飞书应用模式收件人类型采用白名单，非法配置不得默默改投；最终验收门禁
只有通知步骤成功且 API 状态为 accepted 时才承认已接受。已有用户改动不整体发布。

### 本地验证

- 2026-09-15：新增边界先复现 13 个失败子测试，修复后全量 pytest 为
  527 passed、792 subtests passed；公开入口 unittest 48 项通过。
- 飞书实现复用已审查的严格业务状态码检查，对应 5 项测试也纳入云端门禁。
- compileall 与 tracked diff 空白检查通过；新文件在隔离发布目录暂存后另查。
- 未安装 Ruff、mypy 或 pip-audit；不声称完成这些工具的检查或依赖 CVE 审计。
- Antigravity 异步实现了初版测试，但最终因 API 地区限制失败；原生执行代理
  补正测试，独立安全审查未发现可复现的阻断项。主控另行补齐上述三处边界。
- 已核验旧账户导出工作流 disabled_manually，私有仓库 Actions 仍为关闭。
  真实账户未迁移到公开 runner；旧历史的后续清理不改变本测试的合成数据边界。

### 云端发布及通知验证（首轮证据已私有归档）

- 从隔离目录发布明确的公开文件集合，未提交原工作树的其他改动。
- 本地与首轮云端通过 53 个公开入口及通知 unittest；结果不证明完整账户分析。
- 仅通知步骤使用飞书所需 Secrets，未配置真实账户或私有仓库访问令牌。
- 手动 all 回放覆盖四种场景，不是四时点真实运行；每阶段 8/8 个指数为数值有效，
  source_as_of 仍为 null、freshness 为 UNKNOWN，不用于实际交易判断。
- public-test-data 为独立无父根，只含 README 与四份合成报告，严格 schema 验证通过。
- 飞书 API 当时接受一条合并合成消息，未取得设备显示、已读或 ChatGPT 消费证明。
- 原运行元数据仍能定位旧源码树，已纳入专项清理，证据只保存在私有归档；
  不在公开文档保留旧源码提交或运行链接。
- 全球/行业/真实持仓多周期、事前配对复盘、真实日历、ChatGPT 自动提醒及五日
  真实账户验收仍未完成；公开合成运行不得计入。
