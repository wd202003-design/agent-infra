# 按目录推进的设计需求

状态：需求分解已由 [D1 完整设计](design/README.md) 细化，2026-10-12。本文是开发顺序和验收追踪入口；产品范围以 [MVP](mvp.md) 为准，可靠性约束见 [架构](architecture.md)。目标决策见 [ADR 0005](adr/0005-mvp-design-baseline.md)；当前 v1 与未实现 v2 分开维护，原 ADR 0003 的故障验证门槛继续保留。

## 当前基础与目标

已补充正式模块首轮实现，详情见 [实现状态](implementation-status.md) 和 [ADR 0004](adr/0004-modular-platform-increment.md)。现有独立 Runner、模型工具循环、用户授权与 token 预留已有自动化测试；真实后端/模型、完整预算与三类节点仍待验收。devserver 与 PoC 保留原有范围。

正式 MVP 面向单组织管理员和使用者，运行 Agent 动态生成的代码、命令和浏览器步骤。首批只允许外部只读访问或独立产物生成。每次任务固定 Agent 版本、权限策略和资源上限；源码由 Agent 生成不意味着可以扩大权限。

## 目录职责与交付顺序

下表保存原有目录规划，当前源码覆盖情况以实现状态为准。每个模块 README 给出需求编号、输入输出、失败语义与验收要求。

| 顺序 | 目录与设计入口 | 后续建议划分 | 本阶段设计交付 |
|---|---|---|---|
| 1 | [contracts](../contracts/README.md) | api、runner、events、schemas | 数据对象、状态转换、版本与错误约定 |
| 2 | [control-plane](../control-plane/README.md) | tasks、agents、nodes、scheduler、workflow、model-access、audit | 授权、任务准入、单一状态入口、模型循环及预算边界 |
| 3 | [runner](../runner/README.md) | identity、leases、executor、sandbox、events、reconcile | 三类节点统一执行、失联和回收 |
| 4 | [web](../web/README.md) | tasks、agents、nodes、usage | 提交、观测、取消、核对和费用体验 |
| 5 | [deploy](../deploy/README.md) | development、server、local、edge、runbooks | 环境前提、配置、认证、恢复和回退 |
| 6 | [examples](../examples/README.md) | server-analysis、local-workspace、edge-summary | 三个真实业务闭环及隔离负例 |
| 7 | [tests/acceptance](../tests/acceptance/README.md) | functional、faults、security、capacity、reports | 可重复验收、容量口径和证据模板 |

`devserver/` 继续用于 API/交互/适配器试验，迁移到正式目录必须按 ADR 明确。`poc/sandbox/` 负责验证真实后端，不能用模拟测试替代。`docs/` 保存范围、需求、架构和决策；不要在多个目录复制同一协议。

## 分阶段推进与依赖

| 步骤 | 对应里程碑 | 开始条件 | 退出条件 |
|---|---|---|---|
| A：真实沙箱证据 | M1 前置 | 受控 Linux 测试宿主、镜像/模板 | 代码/Shell/浏览器执行、越权拒绝、终止与宿主回收有证据；Cube 输出接收有界 |
| B：冻结最小契约 | M1 | 本文和 ADR 0003 评审、语言/引擎决策 | 版本化 schema、迁移草案、重复/乱序/取消契约测试通过 |
| C：真实 Agent 单任务 | M1 | A、B；一种 Runtime/网关决策 | 固定 Agent → 真实模型 → 受控工具 → 产物；步数/时间/预算上限可验证 |
| D：三类节点 | M2 | C；本机 OS 和节点鉴权决策 | 同协议服务端、本机、在线边缘样例通过，越权被强制拒绝 |
| E：故障与共享额度 | M3 | D；基本幂等/epoch/取消已在 C 实现 | 断网、崩溃、429、旧代次、未知副作用及多实例额度测试通过 |
| F：容量和交付 | M4 | E；负载与测量配置冻结 | 完成全部规模目标、24h 报告、恢复演练和限制说明 |

M3 是对可靠性机制的系统故障验证，不表示 M1 可以缺少基本幂等、权限、取消和执行代次。步骤 C 的模型循环可先单任务验证；分布式共享额度在 E 完成。

## 需求管理规则

- 编号前缀：CT 契约、CP 控制面、RN Runner、WB Web、DP 部署、EX 示例；验收用例以 AT 编号引用需求。
- 模块表中“阶段”是最晚交付门槛，不代表已经实现；各项已有实现子集与剩余验收见实现状态，不因目录有代码就标记整项完成。
- 设计完成：有范围、输入输出、异常处理、验收和待决项；开发完成：代码与验证证据齐备。两者分别更新。
- PR 必须列出相关需求编号、实际运行的用例、未运行原因与剩余限制；需求变更同步相关模块和验收矩阵。
- 实现前补可机器校验的 schema；影响状态权威、协议、安全和组件的选择先更新 ADR。

## 设计已收敛，实施仍需验收

核心选择与具体数值已在 D1 定义：Temporal、自有有限 Runtime、内置网关、S3 API、Linux、令牌/挂载/数据出口、保留/删除、RPO/RTO 和容量负载。原待决项不再作为未定设计散落各目录；实际组件版本/digest、endpoint、凭证、批准路径及账户额度作为发布清单输入。

37 项需求对应设计位置和验收用例见 [追踪表](design/traceability.csv)。设计完成不代表实现或验收完成；后续按 D1 的 I1～I5 增量开发，真实沙箱仍是开放动态代码入口的门槛。
