# 协调、持久化与迁移

适用：CT-02/03、CP-02～05、RN-02/05。目标实现采用 Temporal 协调、PostgreSQL 状态入口；当前数据库队列继续按 ADR 0004 运行到迁移门槛通过。

## 状态所有权与命令

每个任务一个 Workflow，workflow_id=`task/<task_id>`，不复用已完成 ID。Workflow 只管理等待/超时/安全重试意图；所有决定通过 `ApplyTaskCommand(command_id, task_id, expected_version, expected_epoch, type, payload_ref)` Activity 请求控制面。控制面在一个事务中检查权限/条件、写业务状态与事件、记录 command_id，重复调用返回首次结果。

Workflow history 仅放 ID、版本、状态码和受限摘要；源码、模型内容、凭证、原始站点数据放受控存储，不放 history。Activity 只做有界幂等的数据库/HTTP 控制操作；动态代码不运行在 Temporal Worker。Workflow 重放不会重放工具执行；SDK 重试策略不能用于整项 Agent 执行。

提交事务写 task、accepted 事件和 `outbox(start_workflow)`。dispatcher 领取 outbox，按确定 workflow_id 启动；超时先查询同 ID，确认已存在则写已投递，不换 ID。投递确认独立事务；崩溃后重投是幂等的。未投递任务显示 `waiting_reason=coordinator_unavailable`，保持 queued，不授予执行权。

Runner 报告与取消在控制面落库后写 outbox notification；Workflow 收到通知再读取状态，不信任通知里的快照是最新值。通知丢失通过 10 秒定时对账补偿。所有终态仍由控制面事务串行判定，遵循 [生命周期](../../contracts/task-lifecycle.md)。

## 规范化数据模型

以下为 migration 002 起的目标，不是已有 SQL migration。

| 表 | 关键字段/唯一约束 | 索引及写入责任 |
|---|---|---|
| agents / agent_versions | agent_id；(agent_id,version) 唯一；digest、schema、policy_version、active | 管理员；禁止覆盖版本；停用只阻止新提交 |
| tasks | UUID、org_id、owner、state、state_version、active_attempt、deadline、priority、dispatch_ready、ready_at、coordinator_mode | (pool,state,ready_at,created_at,id)；按 owner/created_at 分页；已接受任务不硬删除 |
| submission_keys | (org_id,owner,key) 唯一、input_hash、task_id、expires_at | 原任务删除后保留最小 tombstone 90 天；窗口内不能复用键 |
| attempts | (task_id,epoch) 唯一；attempt_id、node_id、slot_id、lease_until、sandbox_ref、stop/cleanup 状态 | node/lease 索引；旧 attempt 只归档不覆盖 |
| nodes / node_slots | 身份、信任等级、允许池/能力、draining；(node_id,slot_id) 唯一 | 单槽位最多一项未停止执行；管理员配置槽位上限，节点自报不能扩大 |
| claims / commands | (node_id,claim_id)；command_id；请求摘要、首次响应引用 | 同键不同摘要 409；授权创建和 claim 保存同事务 |
| events / runner_events | task_seq；(attempt_id,runner_seq) 唯一、event_id、时间、类型、payload_ref | task_seq 在任务行锁下分配；旧 epoch 事件仅审计 |
| outbox | message_id、aggregate_id、type、payload_ref、available_at、locked_until、delivered_at | pending/available_at；消息标识唯一；投递与业务事务解耦 |
| model_requests / quota_pools / ledger | request_id、池、预留、实际用量/价格、核对状态 | 池事务锁；request_id 唯一；账本追加不可改历史 |
| artifacts / upload_sessions | artifact_id、attempt_id、object_key、version_id、sha256、size、status | task/attempt 索引；只允许 committed 产物下载 |
| credentials / policies / audit | token_hash、作用域、期限、吊销状态；不可变策略版本；管理审计 | 不保存明文 token；actor/action/target/time 可检索 |

外键、唯一约束、状态枚举与非负额度 CHECK 同时落到数据库，不仅依赖 Python 校验。报表读副本允许延迟；领取、授权、取消、核对只读写主库，避免旧权限视图。PG 是业务权威；Temporal 自身 persistence 使用独立数据库和账户。

## 调度和槽位

提交先校验授权池、Agent/策略版本及 1,000 活跃上限。按 pool、required_capabilities、node_owner/trust、data_location 筛选，固定 FIFO（ready_at、created_at、task_id）；本期不接受用户自选优先级。

领取事务先锁 node，再锁一个可用 slot，随后用 `FOR UPDATE SKIP LOCKED` 选择 dispatch_ready=true 的匹配 task；其他写入同样采用 node → slot → task → quota 的锁顺序，不能反向加锁。创建 attempt/epoch、claim、槽位占用和事件同事务提交。SQL 查询及索引计划必须有同负载验证；`SKIP LOCKED` 不提供全局严格公平，记录最长等待并告警。

领取响应丢失按 claim_id 查回原 attempt。空领取等待最长 20 秒，服务端每次授权前重新校验；断线指数退避 1～10 秒并抖动。心跳每 5 秒，租约 30 秒，控制面使用数据库时间。Runner 用服务器返回的剩余时长换算本地单调时钟，最多提前 2 秒截止；发现墙钟偏差 >2 秒拒绝新执行并告警。

waiting 明确 `reason`、`until`、`owns_slot`：模型限流等待保留同一 attempt/slot，最长 60 秒；任务未领取的资源等待不持 slot。无法证明停止的 needs_review/cancel_requested 不释放 slot，也不重新派给其他节点。

## 重试与停止

默认最多 2 次 attempt，且任务总 deadline/预算不重置。只有声明 pure/read_only、旧执行停止和资源回收已证明、无待核对费用/外部动作时，Workflow 才能排队下一 attempt。新 attempt 从输入重算，不恢复不受证明的浏览器会话。checkpoint 仅可恢复幂等已完成计算/产物引用，不能据此恢复外部提交。

幂等 Activity 最多 5 次、总重试窗口 60 秒；仍失败进入可见等待，由同一 Workflow 有界对账。非幂等操作不配置自动 Activity 重试。取消优先持久化 intent；已确认停止/回收但未知副作用仍 needs_review；用户取消不能改写先提交的成功终态。

## 必测故障窗口

1. DB 已提交，dispatcher 尚未启动：恢复后同 task/Workflow 唯一。
2. Temporal 已接受，outbox 未确认：同 workflow_id 查询/重投不产生第二执行。
3. Activity 已写 DB，响应丢失：同 command_id 返回原结果，不再次递增 epoch。
4. claim 已提交，Runner 未收到：同 claim_id 取得同授权；过期后只核对，不发新授权。
5. result 已写 DB，通知/响应丢失：同报告摘要幂等，Workflow 对账收敛，旧代次不能覆盖。

另外注入 DB/Temporal 双重重启、倒序通知和取消/完成竞态，检查已接受任务零丢失与未知副作用零重放。未通过这些用例不切换 coordinator_mode。

## 迁移与回退

002 先扩展规范化表和索引，不删除 body；停用新领取，等待在途或将未知任务核对，分批回填，逐项核对任务数/幂等键/当前 attempt/用量总额。导入后的未知 attempt 保持 needs_review。验证通过后新任务标记 `coordinator_mode=temporal`，旧任务仍由旧路径结束；两种协调器不能处理同一个任务。

回退先停止新 Temporal 提交，排空对应 Worker，保留 outbox、业务表和 Temporal history。只能对尚未授予执行权的任务重新选择协调器，且需事务记录变更；未知在途任务不转回数据库队列重跑。破坏性收缩迁移至少延后两个版本，在备份恢复演练通过后另行发布。
