# D1 编码：v2 实现与开放范围

本轮在 `feat/d1-v2-platform` 编码，v1 接口与快照保持兼容。新增迁移 `002_v2.sql`、独立 `/v2` 与 `/internal/v2`，显式开关 `AGENT_ENABLE_V2=1`。**这不是 D1 全量完成声明**。未完成的数据路径和硬预算明确拒绝，真实后端认证与容量验收不能用 Demo 结果替代。

## 已编码

| 模块 | 代码与行为 | 验证范围 |
|---|---|---|
| 契约 | `contracts/v2.py`；独立公开/内部 OpenAPI；严格输入、请求 ID、分页签名 | 类型/鉴权/游标测试；快照漂移 CI |
| 数据与状态 | 规范化任务、attempt、槽位、事件、凭证、outbox、usage、ledger、artifact；v2 不使用 v1 全局锁 | 同键并发提交、并发领取、取消竞态、旧 epoch 拒绝 |
| 协调 | Temporal Workflow/Activity、outbox dispatcher、固定 workflow ID、命令去重、CAS、安全重试两次上限、历史 continue-as-new | 出站启动响应未知重试测试；真实 SDK test-server 与 replay CI |
| 身份 | 一次注册票据、24h 节点凭证、60s 轮换重叠、不可逆撤销、AES-GCM 响应胶囊、用户 30 天上限 | 不存明文节点 token、重复注册、过期、轮换及撤销测试 |
| 执行授权 | tool/参数摘要/策略/attempt/epoch 绑定 capability，实时吊销；真实节点必须通过隔离证明 | capability 篡改、权限和停止证据测试 |
| Runner | 独立 HTTP 客户端、多槽位、64 MiB 日志、1 MiB 关键记录保留区、崩溃不重放、步骤日志、JSON Agent 循环 | 两槽位 Demo 端到端、日志恢复、单进程锁与磁盘配额逻辑 |
| 主机证明 | 独立 Docker 观察服务；校验只读 root、非 root、禁网、无挂载、CPU/内存/PID、capabilities；管理 API 404 与主机容器/卷皆消失后签名 | 签名绑定与失效；尚未通过 OpenSandbox 真实宿主机认证 |
| 模型与费用 | 共享 quota_pool、预占、版本价格、微货币整数、追加账本、未知保留、管理员对账；仅可信未执行 429 可重试 | 模拟 HTTP 成功/超时、缓存、不重复计费、未知不重试 |
| 产物 | S3 staging 上传、version-pinned copy、复制前后 SHA-256/大小校验、授权下载、限量、删除队列 | S3 客户端固定版本/错误摘要测试；尚无真实 S3 验收 |
| Web | `/v2.html`：任务、版本发布、票据、节点排空/撤销、费用、产物、审计、核对、内容删除 | JS 语法检查；浏览器交互验收尚未完成 |
| 运维 | 日志/产物保留处理、删除 worker、恢复后撤销节点与旧用户凭证、拆分模拟/真实 attempt 指标 | 恢复流程须在关闭入口和 worker 时执行；备份演练未完成 |

## 必须继续完成的 D1 差距

1. 私有本地工作区的不可变只读快照与远端挂载交付、边缘可信摘要程序及出口路径尚未实现。API 返回 `data_path_not_certified`，禁止原始数据误送中央模型。
2. 硬货币预算所需的模型 tokenizer、输入/输出上界与价格证明尚未实现。当前仅软预算；hard 请求拒绝。
3. 主机验证器采用严格 Docker 离线策略。普通 OpenSandbox 默认容器不一定满足这些约束，会被拒绝；没有可用性豁免。需要在实际部署补齐服务侧限制与标签映射，并验证适配。
4. S3 IAM/版本/生命周期、Temporal 持久化服务/TLS、备份及外部删除账本归档属于部署依赖；真实集成需单独验收。
5. 90 天任务元数据和 365 天用量清理尚未自动化；当前优先保留金融/核对记录，避免删除未决账本。保留运维不能声称完整实现。
6. v2 响应仍有内部 epoch 秒字段（如 deadline），尚未完全统一 D1 RFC3339；列表排序、全量响应 schema、每种写接口的统一幂等处理还需契约收敛。
7. 100 并发/10–20 节点、1000 活跃任务、24h 稳态和恢复 RPO/RTO 没有测量；不得据此宣布可生产或容量达标。

## 验证命令

```bash
pip install -r control-plane/requirements.txt -r runner/requirements.txt
PYTHONPATH=control-plane:. python -m unittest discover -s tests/acceptance -p 'test_*.py' -v
PYTHONPATH=control-plane:. TEST_TEMPORAL=1 python -m unittest discover -s tests/acceptance -p test_temporal_integration.py -v
PYTHONPATH=control-plane:. python -m contracts.export_v2
node --check web/v2.js
```

`TEST_DATABASE_URL` 指向 PostgreSQL 时，测试使用独立随机 schema；未设置时 SQLite 仅作显式测试替身。Temporal 测试下载临时 test server，不是生产 Temporal 集群验收。真实 S3 和真实沙箱测试不可用 Mock 的绿色结果替代。
