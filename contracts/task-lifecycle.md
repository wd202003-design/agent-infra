# 任务生命周期草案

状态：随 ADR 0003 提议，正式实现前须评审。本表描述目标行为，不更改开发版的重启和协作取消语义。

## 状态转换

所有状态命令通过控制面权威入口，以 task state_version 和当前 attempt/epoch 条件更新。一次事务保存状态及关键事件；工作流适配器不能绕过入口。

| 起始状态 | 触发 | 目标状态 | 必要条件 |
|---|---|---|---|
| 无 | 提交通过准入 | queued | 输入、权限、幂等检查及持久化事务成功 |
| queued | 授予执行权 | running | 匹配节点/槽位/策略；生成 attempt、epoch、租约；响应丢失可重新查询原授权 |
| queued / running | 有界等待 | waiting | 记录原因、截止时间、是否仍持有执行权和槽位；不能因此隐式再派发 |
| waiting | 条件满足 | queued / running | 未持有 attempt 才回 queued；恢复同一有效 attempt 才回 running |
| queued / waiting | 截止时间或等待上限耗尽 | failed | 没有未停止的执行；如仍有执行，先请求停止并核对，不能直接报告失败终态 |
| running / waiting | 安全 attempt 失败后重试 | queued | 唯一协调者确认可重试、未超次数/预算/截止时间；旧执行已停止或充分隔离；下一次授予生成新 attempt/epoch |
| queued / waiting | 取消 | cancelled | 未授权任何执行，或已有可验证停止与回收证据 |
| running / waiting | 取消 | cancel_requested | 仍存在执行或停止未知；先落库再通知节点 |
| running | 正常完成 | succeeded / failed | 当前 attempt 结果已持久化，停止与回收确认，无未核对副作用；成功需校验必需产物 |
| cancel_requested | 停止与回收确认 | cancelled | 不存在未知外部副作用；已有副作用不因取消回滚 |
| running / waiting | 租约失效、调用结果未知 | needs_review 或受控等待 | 安全策略判断；不可仅因租约过期自动派发新执行 |
| cancel_requested | 失联 | cancel_requested | 展示停止未知；若发现副作用/清理异常，转 needs_review 并保留取消意图 |
| 非终态 | 副作用或回收异常 | needs_review | 保存未知原因、已知资源归属与证据；暂停自动重放 |
| needs_review | 管理员完成核对 | cancelled / succeeded / failed | 有结果、停止及清理证据，权限与审计齐全；不提供无证据“强制成功” |

`succeeded / failed / cancelled` 是不可改写终态。`needs_review` 暂停调度，属于待人工或对账处理状态；人工重试须新建关联任务、重新准入，不直接复活旧 attempt。安全自动重试仅在终态前由工作流协调：确认旧执行停止，或证明副作用隔离/fencing 足够后，再排队并创建新 attempt。

取消与完成并发时，按权威事务顺序决定：完成先提交则取消返回已有终态；取消先提交则迟到成功不能覆盖取消意图，仍需核对停止、清理与副作用。Task 保留 cancel_requested_at，避免进入 needs_review 后丢失意图。

## 执行状态与资源状态

分别保存执行退出结果、stop_evidence、cleanup_status（pending/confirmed/unknown）及 sandbox_id。管理 API 404 只是证据之一，真实验收须检查宿主进程/容器/VM。TTL 和孤儿扫描用于补偿，不能即时标记回收成功。

租约决定是否可发起新步骤；epoch 用于拒绝旧结果。二者均不能撤回已经发出的外部请求。需要外部写的工具必须执行 fencing/幂等检查；首批样例不开放此类写入。无法证明旧执行失效时，不在其他节点重放可能有副作用的步骤。

## 重试责任

| 层 | 可重试内容 | 禁止与上限 |
|---|---|---|
| 客户端 | 相同幂等键的提交、幂等读取 | 不生成新键掩盖未知提交结果 |
| Runner | 同一 event/result 的传输 | 不自行重跑任务；持久化补传队列有字节和时间上限 |
| 模型网关 | 声明可重试的暂态模型失败 | 有总次数、总时间、累计费用上限；未知用量保留核对 |
| 工作流协调 | 明确安全的 attempt 重试 | 保存原因与预算；未知写入不自动重试；不与 SDK 独立重复重试 |
| 对账服务 | 查询状态、验证残留、幂等清理 | 不把查询未知改写成执行成功 |

上述上限在正式配置 schema 中必填并提供有限默认值，禁止用无限重试或无限等待隐藏故障。
