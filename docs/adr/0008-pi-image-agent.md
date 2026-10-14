# ADR 0008：Pi 图片 Agent 与两层任务调度

状态：接受用于首版实现；生产开放仍需本文验收门槛。

## 决策

新增独立 `/v3`、协议 `image-agent/1.0`，v1/v2 接口保持不变。图片应用使用
`@mariozechner/pi-coding-agent@0.68.0`，依赖锁定在 `runtime/pi-agent/package-lock.json`。
该决定只覆盖图片应用，不把通用代码、命令、浏览器 Runner 替换为 Pi。

控制面管理根任务、最多 8 个子任务、静态无环依赖、不可变上下文、租约和费用账本。
默认每根任务并行 2 个子任务，用户最多 8 个运行 attempt，Worker 及供应商另设上限。
PostgreSQL 是状态权威；首版用一个事务级 advisory lock 串行保护 v3 调度状态。
这便于验证原子性，不代表已完成大规模调度性能测试。

每个子任务使用独立内存 Pi Session、AuthStorage 和请求函数。禁止通过 Pi 全局 API
registry 安装携带凭据的请求闭包。ResourceLoader 不读取项目扩展、技能、提示模板或
AGENTS.md；工具仅有 generate_image、edit_image 或 select_images 中分配的一种。
不向模型提供 Bash、文件、网络请求或密钥读取工具。此 Worker 是可信服务进程，
不是用于运行任意用户代码的安全沙箱。

控制面版本化下发 agent/image 两个模型绑定。SK 加密保存在控制面，只有持有有效
attempt 租约的可信 Worker 在操作预占后可以获取；SK 不进入提示、事件、检查点、
Worker journal 或前端。原始供应商 SK 不因平台租约过期自动失效；撤销绑定阻止新调用，
续租通知 Worker 停止，供应商侧撤销与网络隔离仍须部署方执行。

## 故障语义

Worker 持久化 claim intent 和任务引用，然后获取凭据；控制面先持久化 dispatch，
再调用供应商。dispatch 是一次性许可，丢失应答不重放。SDK、HTTP 适配器均禁用
透明重试。lease/进程失效后，未 dispatch 的预占退回，已 dispatch 的操作进入 unknown，
保留预算及供应商配额。重启只执行 abandon，不重跑图片或模型调用。

管理员根据供应商证据确认已经停止后结算 unknown；未产生外部操作但失效的任务可用
close-review 关闭。恢复为失败，重新生成必须创建新的显式任务。不可宣称 exactly-once。

## 首版边界

- 支持同步 OpenAI-compatible Chat Completions 与 images generations/edits；返回必须是
  b64_json，拒绝跟随供应商图片 URL。不同厂商或型号的参数差异需单独适配并验收。
- 一个子任务最多一次图片调用、最多 4 张图片、Pi 最多 8 次模型请求；筛选节点实际接收
  依赖图片后调用 select_images。上下文图片总计不超过 24 MiB、16 张。
- 固定版本上下文及结构化检查点已实现；不持久化完整 Pi 会话，不支持会话跨 Worker 续跑。
- 同步调用等待期间占用 Worker 槽位。accepted 是人工对账协议状态，尚无异步 provider
  polling、yield/reclaim 或自动恢复适配器，不得将其当作可用异步执行流程。
- 预算是 soft admission：发起前原子预占，非供应商账单硬上限。模型用返回 usage 与固定费率
  结算；图片用配置的每张固定价格。超出预占如实入账、撤销绑定、停止后续执行并人工处理。
- 只提供 API 与 Worker；专用图片工作台、多阶段动态扩图、队列公平性、自动存储清理、
  供应商取消/对账适配和跨进程恢复尚未实现。

协议与部署步骤见 [图片执行说明](../image-agent-v3.md)。
