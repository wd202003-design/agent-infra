# 0005：MVP 完整设计基线

- 日期：2026-10-12。
- 状态：接受为设计基线；集成、隔离与容量验收仍须通过后才能启用生产能力。
- 授权背景：用户要求完成完整设计并合并；本 ADR 不把设计合并等同于生产发布。

## 决策

| 领域 | 本期设计选择 | 理由与约束 |
|---|---|---|
| 控制面/Runner | Python 3.12、FastAPI、Linux 首发 | 沿用已验证模块；不增加 Go 与 Python 的双协议实现 |
| 状态权威 | PostgreSQL 17；控制面唯一写入入口 | 事务承载任务、租约、事件和额度；后续规范化表替代全量 JSON 扫描/全局锁 |
| 持久协调 | Temporal + Python Worker | 用 Workflow 表达等待、超时和安全重试；Runner 不直接成为 Temporal Worker，仍使用统一节点协议 |
| Agent Runtime | 本项目有限 JSON Runtime | 单 Agent、有限步骤、动态 Python/Shell/浏览器；本期不引入 LangGraph/AgentScope/ADK 或上传任意 Runtime 插件 |
| 模型网关 | 内置 model-access 模块 + OpenAI-compatible 上游适配 | 统一权限、额度池、预留/核对；本期不额外部署 LiteLLM/Bifrost；服务端配置 1～2 个上游 |
| 沙箱 | OpenSandbox 为首发集成基线；CubeSandbox 保留受门槛控制的服务端候选 | 真实隔离/回收不通过则阻断上线；不把 SDK 集成当作生产资格 |
| 文件产物 | S3 API 对象存储接口，独立私有 bucket | 兼容部署已有服务，供应商 endpoint/凭证是部署参数；必须通过本设计所用的校验和、版本/不可变读写语义测试 |
| 身份 | 单组织、用户访问令牌；节点一次性注册票据 + 独立可轮换令牌 | MVP 不增加统一登录产品；令牌哈希保存、TLS、细粒度权限与审计必需 |
| 部署 | Linux VM，控制面/Worker/网关逻辑与沙箱管理面分区 | 不依赖 Kubernetes/KubeEdge；第三方服务私网部署、版本与 digest 由发布清单锁定 |

这里选择 Temporal 的依据是所需的持久等待、故障恢复和 Python SDK 接口契合，不是已有性能比较结果。不能声称它已经在本项目中优于 Hatchet。引擎集成前必须完成原 ADR 0003 指定的故障窗口验证；失败则通过后续 ADR 调整方案，不能默默回退并宣布通过。

Temporal Workflow 只保存协调引用；模型、数据库及网络 I/O 放在 Activity/受控服务中。Temporal Python 的 workflow sandbox 用于确定性约束，不承担恶意代码隔离；Agent 代码仍进执行沙箱。

## 替代与延续

ADR 0004 的数据库队列是已实现过渡版本，继续可运行。设计选择关闭“引擎/Runtime/网关/OS 未定”的产品设计问题，但不提前迁移现有任务，不宣称代码已经切换。ADR 0003 的状态边界纳入本基线，原故障验证门槛保留；历史记录保持可追溯。

不引入外部网关产品的代价是自行维护小范围上游协议、限流和用量核对。本期只接受明确返回用量、可约束最大输出的上游；不支持的计费项必须标记软预算或拒绝硬预算任务。

## 设计交付与实现门槛

[设计入口](../design/README.md) 链接协调、执行、数据、API、运维和逐需求追踪。设计完成要求每项需求有协议、默认边界、异常语义和验收映射；生产完成要求对应实现与实测全部满足。这两种完成状态分别维护。

未通过真实沙箱隔离、宿主停止证明、三类节点数据控制、恢复与容量门槛时，仅允许受控集成测试。合并不触发部署，不解除这些门槛。

## 外部接口依据

- [Temporal Workflow 定义与确定性](https://docs.temporal.io/workflow-definition)
- [Temporal Python SDK](https://docs.temporal.io/develop/python)
- [Temporal Python workflow sandbox](https://docs.temporal.io/develop/python/best-practices/python-sdk-sandbox)
- [PostgreSQL 17 锁](https://www.postgresql.org/docs/17/explicit-locking.html)
- [S3 预签名 URL](https://docs.aws.amazon.com/AmazonS3/latest/userguide/using-presigned-url.html)

这些来源说明组件接口和边界，不构成本项目完成验收的证据。
