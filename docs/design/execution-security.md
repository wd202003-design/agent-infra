# 执行、节点身份与数据授权

适用：CP-01/06、RN-01～07、DP-02、EX-01～03。默认配置见 [defaults.json](defaults.json)。本页是目标安全设计，当前实现缺口不因文档存在而解除。

## 身份与授权链

用户令牌是至少 256 bit 随机值，服务器只存哈希、owner、角色、池/工作区权限与期限。单组织使用者只读写自己的资源；管理员管理 Agent/策略/节点和核对。管理审计与业务变更同事务提交。用户令牌最长 30 天，管理员可提前吊销；轮换后旧值最多重叠 60 秒。网页只在页面内存保存 token，不写 localStorage/URL。

节点注册由管理员发一次性 10 分钟 ticket，绑定 node_id、owner、location、pool、能力/槽位上限、信任等级与批准的模板。Runner 经 TLS 消耗 ticket 换取独立 24 小时 node token；每 12 小时轮换，旧值重叠 60 秒。票据和轮换请求均有幂等键；响应丢失使用原请求查回安全交付记录，不能重复生成不同节点。吊销永久生效，不能对同一凭证“取消吊销”。过期/吊销节点可由独立管理员核对资源，但无权续租/领取。

凭证表只存 token_hash；为支持交付响应丢失，另存短期 AES-256-GCM 加密响应封装，密钥位于数据库之外的秘密管理服务。注册封装最多保留到 ticket 的原到期时间，且只有相同 ticket_secret、registration_id 和请求摘要可查回；轮换封装只在旧凭证60秒重叠窗口内可查回。到期删除封装并由管理员重新注册，不能永久明文保存 token 来获得幂等性。

每个工具请求由受管 Runtime 带上 attempt/epoch/step_id、operation_id、策略摘要和最多 30 秒 capability。控制面授权有效期不超过租约/任务 deadline。工具入口验证身份、作用域、当前 epoch、允许动作、期限、参数限额与数据规则后执行；沙箱只收到动作内容和允许数据，不能拿到 node token。授权校验不可用时停止新步骤。

节点是管理员接纳的受管节点；客户端自报 location/capabilities 不等于可信。非受管个人机器只可处理节点所有者自己的任务，不能接收共享敏感任务；本期不能证明此隔离条件时，节点不得加入共享池。

## Runtime 和工具语义

固定 AgentDefinition 绑定输入 schema、代码/提示模板摘要、tools、policy_version、max_steps、deadline、输出上限。Runtime 是受管有限状态循环，不导入模型建议的 Python 模块到宿主，也不加载任意上传插件。每步记录 intent → authorized → running → completed/unknown；模型输出只能符合 action/content schema。

工具类型仅 python、shell、browser；模型不能增加挂载、出站域名、镜像、管理地址或凭证。非零退出码作为受控工具结果返回；已知失败可由模型在剩余步数内产生新步骤，但 operation_id 不复用来掩盖旧未知结果。未知外部操作使任务停止并进入核对。

工具时间最多 60 秒，任务最多 600 秒/16 步；每流接收最多 64 KiB、总输出 128 KiB，超限截断并继续有界排空或终止该工具，不在 SDK 内无限积累。模型上下文同样限字节；日志字段按白名单编码，不能把完整 SDK 异常或环境输出上传。任务日志总量 10 MiB。

## 沙箱适配器与停止证明

适配器必须提供 create、execute、stop、inspect、destroy、list_by_owner；创建前落库意图，归属为 task_id/attempt_id/epoch/backend/node_id。控制面选择池与节点，CubeMaster 等后端只负责其域内资源安置。

默认 1 CPU、2 GiB、pids.max=256、只读根文件系统（镜像确需写入目录用限额 tmpfs）、no-new-privileges、无宿主 socket/特权能力，独立任务工作区。模板管理面拒绝未批准镜像/digest。浏览器使用独立 profile；需要特殊 sandbox 参数的浏览器镜像必须依靠外层隔离并经过专门测试，不能当作浏览器自身仍提供完整隔离。

TTL 设置为任务 deadline 剩余时间 + 120 秒，上限 900 秒；以控制面任务 deadline 为执行期限，TTL 仅补偿残留。stop 先终止任务进程树/浏览器会话，再 destroy，随后由受管宿主 verifier 检查实例、cgroup/VM 进程、挂载和网络资源。verifier 使用独立只读检查凭证，输出绑定 sandbox_id/epoch 的证据、观察时间及资源清单摘要；报告到达不能超过 30 秒。

管理 API 404 与宿主 verifier 缺一不可，或使用经过等价验收的原生终止证明。verifier 不能执行 Agent 提交的命令。证据缺失/权限拒绝/宿主失联时记 unknown，不释放槽位、不报告已取消成功。回收扫描每 60 秒列举归属资源，缺失归属的资源隔离并交管理员，不按猜测批量删除。

OpenSandbox 首先执行这套验收；Cube 必须额外验证输出接收边界、KVM/模板、资源限额和宿主回收。若 Cube SDK 不能流式有界读取，则 SDK 放独立受限工作进程，IPC 帧限额、内存硬限制和退出后资源核对；线程内返回后截断不合格。Cube 未通过前保持禁用，不以“更强隔离”替代实测。

## 网络和工作区

默认 deny-all；允许外部只读任务时由批准策略列举目标域名、端口与协议，经受控代理执行。DNS 解析与连接均校验目的 IP，禁止 metadata、loopback、link-local、管理网段及未授权私网，覆盖 IPv4/IPv6、重定向和 DNS 重绑定。仅在浏览器应用层拦 URL 或只检查域名字符串不合格。

本机首发 Linux。管理员将 workspace_id 映射到确定挂载根、所有者和数据级别；用户不能提交任意路径。输入快照/只读绑定挂载，输出在新的 per-attempt 目录；使用 OS 级挂载隔离和路径解析保护符号链接/硬链接/竞态，拒绝越根访问。不得复用日常浏览器会话、SSH agent、Docker socket 或宿主环境。输入/输出目录重叠直接拒绝。

## 边缘与模型数据出口

受管 Linux 边缘保持在线中央调度；失联不领取，授权到期停止新工具操作，尽力终止已有执行。journal 保存未知意图，重连只补传/核对，不自行重跑。

数据分为 public_fixture、workspace_private、edge_raw、approved_summary。日志、模型输入、工具输出、产物都受同一出口策略，不只检查文件上传。edge_raw 默认不能进入中央模型提示；先由可信固定摘要程序生成 schema 限定的聚合（例如 count/min/max/mean），再由受管出口校验器限制字段、长度和总字节。不能靠 LLM 自称“摘要”来证明无原始数据。

MVP 边缘 Agent 的模型只接收批准摘要；动态代码只能访问摘要副本，不挂载原始目录。需要在原始数据上运行任意动态代码且同时允许自由文本出站的场景，本期拒绝；否则无法仅用字段检查阻止编码泄漏。本机 private 数据默认不上传、不进入中央模型，需显式批准最小摘录或在允许的本地模型路径运行；本期不批准时只执行固定本地分析。

## 事件 WAL 和恢复

Runner 对关键 intent、sandbox 归属、停止证据和最终报告先 fsync 再发送，使用 (attempt_id, runner_seq) 去重。每个 Runner WAL 最多 64 MiB，保留 1 MiB 给终止/恢复记录。普通日志可截断并写 truncation marker；关键状态不得丢弃。达到阈值暂停新领取/工具操作，停止已有安全执行并上报（或待重连补报）。

普通日志缓冲最长 24 小时；已确认关键事件可删，未确认执行归属/结果即使超过 24 小时也保留到核对，不因 TTL 清掉恢复依据。重启遇到 running intent 一律核对，不重放；补传网络失败有退避和总等待边界，不能导致无限内存或磁盘增长。
