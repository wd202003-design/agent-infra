# contracts：版本化契约需求

D1 完整目标细节见 [设计基线](platform-next.md)；当前代码支持范围仍按实现状态记录。以下原始需求继续作为验收依据。

状态：已实现子集由 `models.py` 与 [OpenAPI v1](openapi-v1.json) 定义，`PYTHONPATH=control-plane:. python -m contracts.export` 可重新导出；实现决策见 ADR 0004。下文保留完整设计草案；新增状态职责提议见 [ADR 0003](../docs/adr/0003-platform-contract-draft.md)。现有开发 API 继续以 [devserver](../devserver/README.md) 为准。本目录后续保存 OpenAPI/JSON Schema 或选定协议定义及兼容性测试，具体格式待决。

## 数据对象和信任边界

| 对象 | 必需信息 | 约束 |
|---|---|---|
| AgentDefinition | agent_id、version、artifact_digest、runtime、input_schema、required_capabilities、side_effect_class | 版本不可变；管理员注册；依赖、镜像与工具集固定 |
| Task | task_id、org_id、submitter、agent_ref、input_hash、pool、policy_ref、budget、deadline、state、state_version | 身份由认证上下文生成；提交人不能伪造策略或所属组织 |
| Attempt | attempt_id、task_id、execution_epoch、node_id、lease_deadline、stop_evidence、cleanup_status | epoch 单调递增；凭证与沙箱绑定 attempt |
| Node | node_id、owner、pool、labels、capabilities、slots、last_seen、draining、credential_status | 自报能力需服务端准入；普通用户不能扩大节点授权 |
| Event | event_id、task_id、attempt_id、epoch、sequence、occurred_at、received_at、type、payload | 可重复传输；脱敏、限长；到达时间不代表执行顺序 |
| Artifact | artifact_id、task_id、attempt_id、digest、size、media_type、visibility、location | API 返回授权引用；不接受任意客户端存储 URI 或宿主路径 |
| ModelUsage | request_id、task_id、attempt_id、quota_pool_id、reserved、actual、reconciliation_status | 估算与上游用量分别保存；未知用量不能当成零 |

## 接口能力草案

正式 HTTP 命名空间拟为 `/v1`；下表是完整能力分组，已实现路径和字段以 OpenAPI 为准，其余继续待决。当前 Runner 使用主动 HTTP 领取/续租，协议版本 1.0。

| 调用方 | 能力 | 成功及失败语义 |
|---|---|---|
| 用户 | 提交、列表、详情、取消任务 | 接受响应在事务提交后；同键同输入返回原任务；冲突 409；无权限不可读写 |
| 用户 | 按游标读取事件、下载产物 | 限页长；游标过期要求刷新快照；产物鉴权后签发短期访问 |
| 管理员 | 注册 Agent、注册/吊销/排空节点、配置策略、核对任务 | 管理变更有审计；核对必须提交证据与原因 |
| Runner | 注册、心跳、领取、续租、状态/事件/结果上报 | 校验节点身份、attempt、epoch、状态版本；重发幂等；旧代次不能更新当前结果 |
| Runtime | 受限模型请求、受限工具请求 | 校验任务授权、预算、工具 allowlist 与期限；不能访问管理凭证 |

提交规范化输入摘要覆盖 Agent 版本、参数、池、策略引用、预算与截止时间。幂等键作用域为组织和提交人；保存期限须覆盖允许重试窗口，在保留政策决策时给出具体数值。禁止静默遗忘键后在同一窗口再次执行。

## 需求

| 编号 | 阶段 | 要求与可验收结果 |
|---|---|---|
| CT-01 | M1 | 版本化 schema 校验所有边界；未知枚举、越界大小、过期 deadline 和非法引用被拒绝，无部分写入 |
| CT-02 | M1 | 提交、领取、事件和结果有去重规则；并发重复提交只产生一个任务；领取响应丢失可核对同一 attempt |
| CT-03 | M1 基础 / M3 故障 | 遵循 [生命周期](task-lifecycle.md)；取消与完成竞态用状态版本串行判定；旧 epoch 不覆盖状态 |
| CT-04 | M2 | 协议握手报告版本和能力；不兼容节点禁止领取；增加可选字段保持同版本旧客户端可用，破坏变更升版本 |
| CT-05 | M1 | 错误含 code、request_id、可展示 message、可选 retry_after；校验/权限错误不建议重试；日志无凭证 |
| CT-06 | M3 | 事件按 attempt 序列去重，缺口可识别和补传；迟到旧代次事件仅归档审计；分页断线续读不丢已持久化事件 |

## 交付前检查

状态/对象归本目录定义，各模块只引用。契约测试应包含重复领取、过期租约、乱序事件、取消/完成竞态、401/403/409/429 和不兼容版本。正式协议首次接受前记录鉴权方案、分页/幂等保留窗口及向开发版的迁移方式；不承诺 SQLite 测试数据自动迁移。
