# GitHub 公开四时点通知测试

本入口仅验证云端调度、公开报告发布和飞书通知链路。它使用固定公共指数和
DEMO_A / DEMO_B 合成账户，不读取真实持仓、成本或现金，不提供实际交易信号，
也不包含任何下单接口。全部动作固定为 WAIT，建议仓位变动为 0%。

## 首轮合成验证（证据已私有归档）

首轮手动运行验证了四种版式、公共报告发布与飞书 API 接受，但不是四次定时触发验收。
原运行元数据可以定位旧源码树，现已纳入专项历史清理；日志和元数据仅保存在私有恢复副本。
这里不再散发旧源码提交或运行页面链接。

- 首轮四场景每次均得到 8/8 个数值有效的国内公共指数；当时全球行情尚未接入。
- public-test-data 使用独立无父根，只有 README 和四份 JSON；不继承源码历史。
- 当时只发送一条合并合成测试消息；API 接受不等于设备显示、已读或 ChatGPT 消费。
- 四时点 cron 已配置，但其准点性不能由一次手动回放证明。真实账户验收天数仍为 0。

当前治理状态与剩余条件见 [完整分析与隐私计划](live-completion-and-privacy-plan.md)。

## 时间与范围

| 北京时间（周一至周五） | 场景 | 当前公开测试内容 |
| --- | --- | --- |
| 09:00 | 晨报 | 国内公共指数；8 项隔夜全球公共观测、较前值变化与数据时间；行业评分仍未接入 |
| 11:30 | 午盘变化 | 缺少可靠晨报基线，明确标记无法比较，不用成本收益替代时段变化 |
| 14:30 | 核心决策 | 合成账户 WAIT；没有评分或多周期确认，不编造二买信号 |
| 16:10 | 收盘复盘 | 缺少事前判断配对，明确标记无法评价，不擅自调整模型 |

GitHub Actions 定时任务可能排队、延迟甚至被跳过，不保证准点；14:30 提醒
不能作为必须准时到达的交易执行工具。周一至周五不等于交易日历，节假日也可能
收到带测试标签的通知。报告分别记录场景时点、实际采集开始和完成时间；
晚于场景时点 15 分钟完成时标记 late。不得把这个容差解释为交易时效保证。

Sina 当前指数接口没有可靠的来源时间。即使请求成功，仍然保留
source_as_of=null、freshness=UNKNOWN，不能声称行情实时或可用于交易。
网络失败、空行情和非法数值使用固定错误码，不把原始响应或异常写入公开报告。

晨报全球观测固定为 `^GSPC`、`^DJI`、`^IXIC`、`^SOX`、`^TNX`、
`DX-Y.NYB`、`GC=F` 和 `CL=F`。Yahoo Chart 无密钥公共接口是主源；它未经本项目认证，
可能限流、延迟或改变结构，因此通知只写“较前值”，不冒充已确认收盘或官方实时行情。
`^TNX` 主源失败时才尝试 FRED `DGS10`；FRED 仅提供日期级日频数据，不编造盘中时分秒。
每个标的独立降级，单项失败显示“暂不可用”，不会阻断其余项目或改变 WAIT。
11:30、14:30、16:10 不重复请求全球源，快照明确记录该阶段未采集。

## 工作流与公开产物

- 工作流：.github/workflows/public-trial.yml，名称 Public synthetic reminder trial。
- 报告分支：[public-test-data](https://github.com/WJAnnie/investment-assistant/tree/public-test-data)。
- 只发布 README.md 和 latest/{morning,midday,decision,closing}.json。
- 数据分支使用独立的根提交，不继承源码分支历史；每次发布前进行严格 schema 校验。
- 各阶段保留最近一次报告，可能不是同一天；应查看各自时间，不把它们当作同日完整记录。
- 只运行标准 ubuntu-latest runner，不上传 artifact、不启用依赖缓存。

每次发布都会保留当前允许的 README 和阶段 JSON，但以无父根提交重建
`public-test-data`，不继续累积该分支的旧提交历史。新报告安全不代表旧悬空对象、
缓存或他人副本已消失；服务器残留仍需 GitHub Support 处理。

手动测试命令：

~~~text
gh workflow run public-trial.yml --repo WJAnnie/investment-assistant --ref main -f stage=all -f send_notification=false
~~~

all 是一次回放四种版式，并只发送一条合并测试消息，不代表在四个真实时点
各采集了一次。关闭 send_notification 只测试发布。重跑同一个 run 不重复发送；
新建一次手动 dispatch 会再次发送，因此不要为查看结果重复 dispatch。

## Secrets 与通知回执

只在通知步骤注入飞书凭据；采集和报告步骤不读取凭据。
应用模式使用以下 Actions Secrets：

- FEISHU_APP_ID
- FEISHU_APP_SECRET
- FEISHU_RECEIVE_ID
- FEISHU_RECEIVE_ID_TYPE（open_id / user_id / union_id / email / chat_id）

已有可信飞书机器人 webhook 的部署也支持 FEISHU_WEBHOOK，但不要同时配置
两种方式来猜测接收目标：webhook 优先。只接受官方 HTTPS webhook 地址；
禁止跟随重定向、隐式 netrc 凭据和环境代理。非法收件人类型直接失败，不自动改投。

Actions Secrets 是加密保存，不是普通 Variables。它们也不是运行沙箱：
获授权的工作流代码仍可读取密钥，日志自动遮罩不保证保护拆分、计算后的值。
真实账户即使存入 Secrets，生成的收益、金额、建议或报告仍可能泄漏。
本测试不配置账户 Secrets、不读取真实账户，不能直接改造为公开账户报告。

回执必须分开理解：

- public publication confirmed：推送后的远端提交 SHA 与本地一致。
- notification_status=accepted：飞书 API 返回明确成功，不等于手机已显示或用户已读。
- not_configured：缺少通知配置；启用通知时工作流失败，不算成功。
- failed：API 拒绝、异常或非法配置；不能当作已送达。
- skipped：手动关闭通知或重跑抑制；不是 accepted。

GitHub 发布成功不等于 ChatGPT 自动读取。本入口没有 ChatGPT Tasks 创建能力或
消费回执，尚未建立 ChatGPT 自动提醒链路。公开链接可供后续接入，但不可冒充接入完成。

## 本地验证与迁移到常驻 Windows

仅需项目已有的 requests 和 PyYAML；公开入口不依赖完整账户分析环境。

~~~text
python -m unittest discover -s tests -p "test_public_trial*.py"
python -m unittest discover -s tests -p "test_global_markets.py"
python -m unittest discover -s tests -p "test_feishu.py"
python -m app.integration.public_trial collect --stage all --output <新的空报告目录>
python -m app.integration.public_trial validate --input <报告目录>
~~~

迁移顺序：

1. 在新 Windows 上准备 Python 3.11、现有依赖、系统时区和网络；执行离线回归。
2. 将真实配置放在受 Windows ACL 保护的私有目录或凭据存储，不提交到 Git。
3. 先做合成通知测试，核对实际接收设备；公开入口不接受真实账户参数。
4. 为账户分析另设私有输出和私人通知，补齐来源时间、交易日历、午盘基线、
   多周期闭合规则与复盘配对后，再开始五个交易日的真实账户验收。
5. 使用任务计划程序安排四时点，检查休眠唤醒、断网重试、迟到提醒和去重。
   切换前停用 GitHub 测试调度，避免两端重复提醒。
6. 保持“分析自动、提醒自动、交易人工确认并手动执行”；不配置券商下单权限。

本轮公开合成测试累计的真实账户验收天数始终为 0。
