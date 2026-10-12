# 后续完整协议设计（尚未实现）

本文件属于 D1，**不是当前可调用 API**。已实现接口以 [OpenAPI v1](openapi-v1.json) 为准；以下设计实现时使用 `/v2`，Runner 协议 `2.0`，避免给严格校验的 v1 增加破坏性字段。两版本并行一个迁移窗口，默认 30 天；v1 正常结束已领取任务，不领取仅 v2 才能安全执行的策略。

## 通用规则

所有 ID 为服务端 UUID（外部 Agent 名/版本仍为受限字符串），时间为 UTC RFC3339，时长整数秒、大小整数 bytes、费用整数微货币单位。JSON 严格拒绝未知字段，枚举增加视为协议变更；每个请求体最大 256 KiB，文件不经此 JSON 路径传输。

请求携带 Bearer，身份不来自请求中的 owner；管理与 Runner token 不可互用。POST 变更要求 Idempotency-Key，128 字符以内，组织/调用者/操作作用域唯一；同键异摘要 409。需要并发控制的管理/状态变更携带 expected_version，冲突返回当前版本，不静默覆盖。

响应外壳 `{data,request_id}`，列表另含 `next_cursor`。错误 `{code,message,request_id,retry_after_seconds?,details?}`；details 只含字段名/安全状态，不返回原始输入。401 失效身份、403 无操作权、404 不存在或不可见、409 幂等/版本/代次冲突、410 游标/记录已过期、413 体积超限、422 参数/策略不合格、429 容量/额度不足、503 依赖不可用。

## 核心对象

| 对象 | 必需字段 | 行为约束 |
|---|---|---|
| AgentVersion | agent_id,version,digest,runtime_version,input_schema,tools,policy_ref,max_steps,limits,side_effect_class | 版本不可变；runtime 固定 JSON-loop；side_effect 为 pure/read_only/artifact_only，MVP 不接受 external_write |
| PolicyVersion | policy_id,version,allowed_pools,workspace_refs,tool_kinds,egress_rules,data_classes,upload_schema,limits | 管理员发布；提交时绑定版本/摘要；收紧或吊销立即禁止新步骤，不原地扩大旧权限 |
| Task | id,owner,agent_ref,input_ref,pool,workspace_ref?,policy_ref,budget,deadline,state,state_version,waiting?,active_attempt?,created_at | waiting={reason,until,owns_slot}；未实现操作不伪造状态 |
| Attempt | id,task_id,epoch,node_id,slot_id,lease_until,sandbox_ref,stop_status,cleanup_status | epoch 单调递增；stop/cleanup 为 pending/confirmed/unknown，有 evidence_ref |
| Event | event_id,task_id,attempt_id?,epoch?,runner_seq?,task_seq,occurred_at,received_at,type,payload_ref | 关键状态事件只由控制面产生；Runner 不自行提供可信 task_seq |
| Artifact | id,task_id,attempt_id,name,media_type,size,sha256,status,data_class,retention_until | status=staging/verifying/committed/deleted；存储 key/version 不由终端用户任意指定 |
| Usage | request_id,task_id,attempt_id,quota_pool_id,price_version,currency,reserved,actual,estimated_cost,reconciled_cost,status | 未知不当零；费用和用量核对记录追加保存 |

## 用户/管理员 API

下表路径前缀 `/v2`。所有成功变更都在持久化之后响应。

| 方法/路径 | 主要输入 → 输出 | 权限/幂等/异常 |
|---|---|---|
| POST /agents/{agent_id}/versions | AgentVersion → 固定版本 | admin；版本同内容幂等，异内容 409 |
| POST /policies/{policy_id}/versions | PolicyVersion → 版本/摘要 | admin；禁止任意源码作为策略校验器 |
| POST /tasks | agent_ref,input,pool,workspace_ref?,policy_ref,budget,timeout_seconds → Task | user 的池/数据授权；超过活跃上限 429；同键同结果 |
| GET /tasks、/tasks/{id} | filters,cursor → 授权列表/快照 | 默认50/最大100；按 owner、创建时间与 ID 游标 |
| POST /tasks/{id}/cancel | expected_version,reason → Task | owner/admin；重复取消返回已有状态 |
| GET /tasks/{id}/events | cursor → Event 页 | 游标过期410；旧 attempt 通过显式参数查询 |
| GET /tasks/{id}/artifacts | cursor → committed 元数据 | owner/admin；删除内容不返回下载引用 |
| POST /artifacts/{id}/download | disposition → URL/expires_at 或代理引用 | 二次鉴权；最大60秒；拒绝未提交对象 |
| GET /tasks/{id}/usage | cursor → Usage 页 | owner/admin；不返回上游 key |
| POST /tasks/{id}/usage/{request_id}/reconcile | actual_tokens,cost,currency,evidence_ref,expected_version → 核对记录 | admin；仍可能在途先等待/查证；未知不填零解锁 |
| POST /tasks/{id}/review | outcome,evidence_refs,stop_confirmed,cleanup_confirmed,expected_version → Task | admin；必须无未知副作用/费用，取消意图不能被成功覆盖 |
| POST /tasks/{id}/rerun | 新预算/输入引用 → 新 Task | owner/admin；新 idempotency_key，记录 parent_task_id，不复活旧 epoch |
| DELETE /tasks/{id}/content | expected_version → 删除请求/期限 | owner/admin；未停止或待核对409；元数据最小 tombstone 保留 |
| POST /nodes/tickets | NodePolicy → ticket_id、一次性秘密、expires_at | admin；10分钟；绑定池/所有者/上限 |
| PATCH /nodes/{id} | expected_version,draining?,slots_limit? → Node | admin；不得降低到当前占用以下后强杀；可排空后缩容 |
| POST /nodes/{id}/revoke | reason → revoked | admin；幂等、不可撤销；新凭证用新注册流程 |
| GET /audit | filters,cursor → 审计页 | admin；不输出秘密/原始内容 |

## Runner/Runtime API

| 方法/路径 | 主要输入 → 输出 | 规则 |
|---|---|---|
| POST /runner/register | ticket_id,ticket_secret,registration_id,protocol,instance_public_info → node_id,node_token,expires_at,policy | ticket原子消费；同 registration_id 安全查回结果；不可自报扩大能力 |
| POST /runner/rotate | rotation_id,protocol → 新 token/期限 | 旧token有效才可轮换；失效后重新由管理员注册 |
| POST /runner/claim | claim_id,protocol,advertised_slots → Task/Attempt/Policy 或空 | 服务端核实槽位；同claim回同attempt；不兼容版本拒绝 |
| POST /runner/attempts/{id}/renew | task_id,epoch,health,last_ack_seq → authorized,remaining_seconds,stop_reason? | 过期不能续活；返回剩余期限用于本地单调时钟 |
| POST /runner/attempts/{id}/authorize-step | epoch,step_id,operation_id,tool,args_hash,policy_digest → capability/期限 | 服务端检查当前状态/数据/工具权限；敏感动作拒绝 |
| POST /runner/attempts/{id}/events | epoch,events[{runner_seq,event_id,type,payload}] → contiguous_ack,gaps | 批次最大256KiB；不可确认未持久化事件；同序号异内容409 |
| POST /runner/attempts/{id}/artifacts | epoch,manifest → upload_session,受限上传URL | size/type/sha256/出口检查；管理密钥不下发 |
| POST /runner/uploads/{id}/complete | epoch,sha256,size → verifying/committed | 校验不通过422；重复提交幂等；不信任上传者自报成功 |
| POST /runner/attempts/{id}/model | epoch,step_id,request_id,messages,budget_ref → 响应/用量引用 | 固定上游；最大输入/输出；未知不能重发 |
| POST /runner/attempts/{id}/report | epoch,result_ref,outcome,stop_evidence_ref,cleanup_evidence_ref,unknown_effects → Task | 状态入口条件更新；旧epoch拒绝；缺证据needs_review |

capability 不是上游 key，也不允许沙箱直接访问 `/runner/*`。受管工具代理只接受该 capability 指定的 action/args_hash/attempt，并查有效授权；需要任务控制权的节点服务与不可信代码进程分离。

## 内部协调命令

`POST /internal/v2/task-commands` 仅接受 Temporal Worker 的服务身份（TLS、独立服务 token、task-command scope），不接受用户或 Runner token。请求为 command_id、task_id、expected_version、expected_epoch（可空）、type、payload_ref；type 限 make_ready、schedule_safe_retry、mark_waiting、request_stop、reconcile。权限/状态校验后在同一事务保存首次响应及审计；重发摘要冲突409，状态版本冲突409需重新读取而非覆盖。

make_ready 设置 dispatch_ready 和 ready_at；Temporal 模式任务只有这一步完成才能被 Runner 领取。schedule_safe_retry 必须验证旧 attempt 停止/回收、无未知副作用或用量、次数/总预算和 deadline。内部服务不能绕过同一状态入口自行设置 succeeded；完成由经过校验的执行/产物/核对证据驱动。

## 兼容与冻结

实现时生成独立 `openapi-v2.json`、请求/响应模型及 schema 漂移检查；D1 不修改现有 v1 快照。契约测试必须涵盖所有错误码、边界值、重复/冲突/乱序、版本握手、权限与过期场景。字段或枚举破坏变更新开版本；部署顺序是控制面兼容读 → 新 Runner → 新任务开关 → 旧任务排空 → 停旧协议，不通过静默忽略字段获得兼容。
