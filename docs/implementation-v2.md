# D1 v2 实现状态

D1 v2 基线已通过 PR #2 合并到 main，尚未部署生产。v2 默认关闭，数据库迁移按版本执行到 003；v1 契约快照保持不变。

## 已实现的代码链路

| 范围 | 实现 |
|---|---|
| 协议 | 严格请求/响应模型、独立 OpenAPI、RFC3339 时间投影、按创建时间与 ID 的签名分页、按身份/操作/幂等键绑定请求摘要；renew 重发不扩展原租约 |
| 状态与协调 | PG 任务/attempt/槽位/事件/outbox；Temporal workflow 与 Activity、固定 workflow ID、命令去重/CAS、安全重试、历史回放 |
| 身份 | 注册票据、凭证轮换、撤销、用户有效期；短期 AES-GCM 响应封装按注册票据原期限或轮换 60 秒窗口销毁 |
| Runner | 多槽位、关键意图 fsync、有限 WAL、崩溃不重放、有限 JSON Agent 循环、工具 capability |
| 本地私有数据 | 用户/策略/节点/本地映射四重工作区授权；fd 级防链接读取、独立快照、OpenSandbox 只读 Volume、独立宿主证明；只运行固定 program，输出留节点，中央禁止自由文本日志/模型/文件出口 |
| 边缘数据 | 固定 `numeric-jsonl-v1`，管理员限定列/数值范围/最小样本数；仅摘要副本挂载；摘要 schema/hash 与宿主证明绑定后才允许中央模型 |
| 预算 | soft 估算；hard 对具备有效审核记录的 tokenizer/模板字节上界和固定计费合同预占最坏费用；模型/价格/证明固定，实际越界如实记账并隔离路由 |
| 用量/产物 | 共享额度池、整数微货币追加账本、未知保留与人工核对；S3 version-pinned staging/copy、前后 SHA-256、受权下载与删除 |
| 保留/删除 | 事件/产物 30 天、任务 90 天转最小 tombstone、已结算用量/审计 365 天；未知费用和活跃资源不自动删除；节点本地内容必须确认删除后才结束删除队列 |
| 恢复 | 旧凭证失效、恢复任务隔离、删除账本重放时清除内容和幂等缓存；生产 Temporal TLS/mTLS 配置 |
| 出站 | 默认禁网；公共任务精确域名 HTTPS GET/HEAD 检查代理，实时 epoch/租约授权，DNS/IP/TLS/Host 绑定，宿主 nft 强制仅代理出口；真实 OpenSandbox 集成待验收 |
| Web | 版本、数据路径/工作区、软硬预算选择、任务/事件、产物、用量、节点、审计和核对 |

## 验证边界

- 本地测试与 PostgreSQL CI 验证状态、并发、幂等、过期、删除、费用与数据出口规则。
- Temporal CI 使用真实 SDK test server，覆盖重试、历史回放及 outbox → 实际 HTTP → Demo Runner → 工作流完成。
- 私有/边缘 Runner 集成测试使用模拟沙箱与受控证明。文件快照/链接防护测试在本地 Linux 文件系统执行。二者都不等同于真实 OpenSandbox 宿主机验收。
- S3 API 的固定版本与摘要校验已编码并测试；真实存储、供应商计费合同、Linux 宿主安全配置仍需部署方验证。

## 尚不能宣布完成的部署验收

1. 真实 OpenSandbox 的非 root、只读根、PID/CPU/内存、禁网、工作区只读挂载和清理证明。默认服务配置不一定符合，验证失败即拒绝。
2. 审核并配置真实模型供应商的计费上界证据；代码不会自行证明一个任意兼容接口没有隐藏计费。未经审核只可 soft。
3. 真实 S3/IAM、备份/WAL 归档、外部删除账本复制及恢复演练。离线/失联节点的删除保持 pending，不能虚报 24 小时已完成。
4. Web 浏览器交互与可访问性、三类真实节点、100 并发/1000 活跃任务/24h 稳态、RPO/RTO。尚无容量达标证据。
5. 受控 HTTPS 读取代码已实现，见 [出站部署](../deploy/egress.md)。真实 OpenSandbox 拓扑与 Chromium CA/页面仍需全链路验收；不支持写请求、登录、WebSocket 和任意 TCP。

## 验证命令

```bash
pip install -r control-plane/requirements.txt -r runner/requirements.txt
PYTHONPATH=control-plane:. python -m unittest discover -s tests/acceptance -p 'test_*.py' -v
PYTHONPATH=control-plane:. TEST_TEMPORAL=1 python -m unittest discover -s tests/acceptance -p test_temporal_integration.py -v
PYTHONPATH=control-plane:. python -m contracts.export_v2
node --check web/v2.js
```

`TEST_DATABASE_URL` 设置时使用独立 PostgreSQL schema，否则 SQLite 仅作测试替身。生产开启前必须完成上述环境验收，不能用绿色单元测试替代。
