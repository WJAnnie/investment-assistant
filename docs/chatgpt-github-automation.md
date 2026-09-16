# 私有 GitHub 数据 → ChatGPT 接入（私有数据链路尚未上线）

状态：2026-09-16。完整账户数据必须私有；公开入口保护已部署，真实账户分析尚未上线。
ChatGPT 任务页已核实四个投资助手公开研究提醒已启用：Asia/Shanghai 周一至周五
09:00、11:30、14:30、16:10；这不等于私有 GitHub 数据已授权给 ChatGPT。
当前 `WJAnnie/investment-assistant` 仍为公开仓库，不能作为下述完整账户快照的发布目标。
已按授权创建 `WJAnnie/investment-assistant-private`：API 复核为 private、空仓库、
Actions disabled；未上传工作树、账户或凭据，未创建数据分支或授权 ChatGPT。
原公开账户工作流已 disabled_manually；公开 main 已改为脱敏新根、旧账户分支已删除。
已核实的旧运行/缓存已清理，服务器悬空提交仍需 GitHub Support 处理，不宣称彻底消失。

## 额度约束下的目标链路（执行设备尚待确定）

```text
公开标准 Actions → 固定公共行情（无账户、无私有仓库凭据）
                         ↓ 私有侧按需拉取并校验
可信私有设备 → 账户分析/提醒 → 私有仓库的快照与 manifest
                                    ↓
                        已授权且已验证可读取的消费端
```

用户托管 Actions 分钟额度已用完，因此不启用私有仓库的托管 runner。
[GitHub 官方计费说明](https://docs.github.com/en/billing/concepts/product-billing/github-actions)
明确：公开仓库使用标准 GitHub-hosted runner 免费；larger runner 即使在公开仓库也收费；
self-hosted runner 的 Actions 使用免费，但需要自行提供设备，设备、电力、网络和存储并非因此免费。
文档核验日期为 2026-09-14。免费托管分钟不等于 artifacts/cache 存储无限免费。

推荐在已有常驻可信设备用本机调度执行私有账户分析，私有仓库仅作存储，保持其 Actions 关闭。
私有 self-hosted runner 是可选替代方案，不是本次已安装或获准启用的部署。
不能假定用户电脑常驻在线；设备未确定前不创建第二套调度、不启动五日试运行。
公开侧使用独立 `public_trial` 导出器，仅运行固定公共行情和合成账户；
不能把现有账户 exporter 去掉标签后复用，合成运行不计入真实五日观察。

GitHub 应用授权读取与 ChatGPT 定时任务是两项能力，不能相互推定。当前已观察到公开研究
提醒任务存在并启用，但尚未证明任务能自动调用私有 GitHub 应用读取账户快照；该组合保持待
验证状态。必须在实际私有消费任务中取得读取、分析和通知回执。
GitHub PR 提交不是已验证的 ChatGPT 唤醒接口；本提示词也不会创建或唤醒任务。

GitHub Actions cron 可能延迟，不能保证 09:00、11:30、14:30、16:10 准点提醒。固定时点
提醒与数据就绪后的分析需分开设计；迟到或未就绪时只提醒 WAIT，不能把消费时间当作行情时间。
四时点目标及尚未接入的能力见[当前实施记录](live-completion-and-privacy-plan.md)。

## GitHub 侧

工作流文件：`.github/workflows/publish-chatgpt-data.yml`。这是已有私有账户保护实现，
保留作回归基线；不适用于本次公开托管 runner 方案，也没有部署到新私有仓库。它会：

1. 在 checkout 和账户采集前，通过 GitHub API 验证运行仓库为 `private`；公开、internal、
   未知或查询失败均停止，不注入行情密钥、不读取账户；
2. 保留原有五场景 cron：上海时间 06:31、09:01、11:31、14:31、16:11。它们是旧入口，
   不是新的四时点上线声明，也不能把工作日 cron 当成已实现交易日历；
3. 采集组合估值及已接入的分析，生成 `data_classification=private_account_data` 的完整快照；
4. 在同一私有仓库保存各场景 latest 文件、`current.json` 和 manifest；推送前再次验证 private；
5. 失败/部分结果若可安全序列化，也只在私有目标留存诊断。采集、校验、私有检查或推送失败，
   最终步骤均不得显示成功交付；诊断留存不代表数据可分析。

这是“运行仓库必须私有”的保护，不是从公开仓库跨仓库推送账户数据的实现。私有存储目标
已确定，私有执行设备、账户配置迁移及受控代码同步方式仍待确定。没有改变原公开仓库可见性。
公开仓库只可保留代码和专门验证过的公共/合成数据，入口为 `public-trial.yml`。
公开任务不得加载 `portfolio.yaml`、账户工作流、私人自选清单或私有仓库，不持有私有仓库 PAT/
deploy key；日志、错误文本、缓存和 artifacts 都在公开边界内，不能仅在最后推送时检查隐私。
已有 `run_market_snapshot` 可输出原始异常和来源名称，且 Sina 的 `timestamp` 是采集时刻，
不是行情来源时间，因此不能直接作为已验收的公开导出器。缺来源时间时不能宣称实时或 READY。

仅在私有执行设备与安全部署确定后，可在该设备的受保护运行配置中按需设置：

- `TUSHARE_TOKEN`：历史与备用行情接口；

不要把飞书 App Secret、Webhook 或 Token 写入输出 JSON。公开 Actions 只使用合成提醒所需的
最小通知权限，不读取或发送真实账户；私有提醒通道另行配置与验收。本轮未复制任何 secret 到新仓库。
密钥脱敏不等于账户隐私脱敏；账户净值、现金、数量、成本和完整自由文本报告同样必须私有。
本地导出只允许本项目被忽略的 `.private/` 或 Git 工作树之外的目录；忽略规则不是加密/ACL，
也不会移除已跟踪的内容。不要伪造 CI 可见性环境变量绕过保护。

## ChatGPT 侧读取草案（需要真实能力验证）

下文目标 `WJAnnie/investment-assistant-private` 已存在，但仓库为空，拟用的 `chatgpt-data`
分支尚未创建。创建仓库不等于 ChatGPT 获得权限；后续仅授权必要的私有仓库读取权限，
分别验证交互式读取和实际自动消费环境。
没有授权或自动消费能力时停止在 WAIT，不转向公开 raw URL，也不在 URL/提示词/日志中放 token。

调度方式未验证前，不配置第二套轮询任务。现有飞书接口回执也不能证明 ChatGPT 已读取，
或用户终端已经看到提醒。若最终使用支持授权读取的其他消费端，同样遵守以下数据检查。

以下是消费指令草案，不代表这些消费检查或定时触发器已经实现：

```text
每次被已验证的调度入口调用时，通过已授权的 GitHub 应用读取私有仓库
`WJAnnie/investment-assistant-private` 的 `chatgpt-data` 分支：

1. 从本次计划时点取得 expected_report_kind 和预期交易日：09:00=morning、11:30=midday、
   14:30=trading、16:10=closing（Asia/Shanghai）。global 仅是旧五场景的兼容入口。
   迟到时不改变预期时点，缺少本次数据则 WAIT。
2. 从 `chatgpt-data` 分支读取 `data/chatgpt/manifest.json`，再读取
   `manifest.latest[expected_report_kind].path` 指向的场景文件。不得从 main 分支读取同名文件，
   也不得用 current.json 中的其他场景替代。
3. 校验 manifest、current/latest 条目及场景文件的 `data_classification` 均为
   private_account_data；缺失或其他值一律拒绝，不能自动补标签。manifest 和场景文件的
   `schema_version` 必须为 2，目标条目与场景文件的 `rule_version` 必须为 8.1；GitHub
   连接器返回的场景文件 sha 必须等于对应 `manifest.latest` 条目的 `git_blob_sha`；如任务具有
   代码执行能力，再复核 SHA-256。`report_kind` 必须等于 `expected_report_kind`；`generated_at`、
   `data_cutoff`、`market_date`、`producer.run_id` 必须存在且与 manifest 条目一致。校验失败时只报告
   数据链路故障，并保持 WAIT。
4. manifest 的 `publishable` 只描述 current 的数据就绪性，不是公开发布许可。要求 current
   与目标 latest 条目的场景、run_id、cutoff 和哈希一致，不能用其他场景的就绪状态背书。
   `publishable` 不为 true，或 `execution_status` 不是 completed，或
   `analysis_state` 为 NOT_READY/FAILED 时，只提醒数据未就绪及补数据动作，不输出方向性结论。
   `analysis_state` 为 DEGRADED 时，必须逐项说明缺失来源，仍不得把缺失证据视为满足条件。
5. 以 `data_cutoff`（Asia/Shanghai）作为报告的预期证据时点，逐项核对实际来源时间。
   `generated_at` 是生产端声明时间；当前代码取采集入口时刻，不能证明采集/推送完成。
   两者不能互相替代。`data_cutoff` 必须匹配当次场景的标准时刻（06:30/09:00/11:30/14:30/16:10）。
   若日期不对、生成时间早于 cutoff，或距当前时间超过 30 分钟，则标注“数据延迟/未就绪”并保持
   WAIT。若 run_id 与本任务上一次已处理值相同，则静默结束，避免重复发送。
6. 先检查 `bar_status`、`quality.errors`、`failed_valuation_codes`、缺失来源和技术分析覆盖率。
   任一必需周期为 FORMING/UNKNOWN/MISSING 时保持 WAIT；closing 场景的所有已报告周期必须为 CLOSED。
   不得用基准市值、陈旧价格或接口失败结果
   推导即时交易结论。
7. 根据 report_kind 生成不同内容：
   - global：隔夜海外风险、港股传导、全球医疗净值日期；
   - morning：前收、缺口、强弱排序和开盘观察条件；
   - midday：仅比较同日、同口径晨报基线后的新增变化；缺少基线则说明无法比较，不将累计成本收益写成上午贡献；
   - trading：只列已触发的仓位、集中度、技术或数据质量条件；
   - closing：核对事前判断与今日结果；缺少预测记录时写无法评价，不补写正确/错误。账户估值、次日观察及基金净值日期差异另列，不把成本浮盈当今日预测命中。
8. 分别输出账户与合并视图。集中度必须依据当次 JSON 中的实际权重和 warnings，
   不固化私人持仓名称或沿用旧数字。
9. 明确区分已获得事实、模型分析和数据缺口。基本面、行业景气、新闻若没有当次可靠数据，写明
   “未接入/未核实”，不得补写。
10. 每次提醒都明确列出“现在检查什么”“什么条件下可进入人工复核”“哪些条件不满足时必须
    WAIT”。仅输出研究判断和条件化建议，不自动交易，不连接券商，不使用“立即买入”“立即卖出”等措辞。
11. 报告开头写明 report_kind、data_cutoff、generated_at、GitHub run_id 和数据状态；结尾明确：
    “本系统只做辅助提醒，不自动下单；是否交易由本人确认并手动执行。”
```

## 上线前联调门槛

1. 私有存储目标已创建；继续明确私有执行设备与部署授权，受控迁移所需配置，并处理原公开
   工作流与已有公开数据/历史的残留风险；
2. 先用合成账户验证公开/未知环境拒绝、私有环境成功、失败诊断保持私有；
3. 在受控私有部署中，分别验证 `closing` 与 `trading` 的场景、run_id、哈希和分类一致；
4. 从实际消费环境取得授权读取回执，再以明确标记的测试通知验证接收端可见、迟到、失败和去重；
5. 自动任务能力、四时点数据语义和提醒链路都验收后，才开始连续五交易日观察，不把本地测试计入。

新产生的完整账户数据必须私有，不存在“为了连通而临时公开”的回退路径；此前已公开的
服务器残留仍需单独处置。公开历史的已执行清理见[实施记录](live-completion-and-privacy-plan.md)。
私有 `--live-ledger` 已接入四阶段观察，init/status/record-receipt CLI 已具备；
它不补齐完整来源、自动事前预测或人工审查，observer 仍保持 data_complete=false。
没有创建真实账本或私有常驻进程，也没有完成私有 GitHub → ChatGPT 线上联调；公开研究
提醒任务已启用，但不计入真实五日验收。
接线语义和失败处理见[观察账本契约](live-acceptance-contract.md)。
