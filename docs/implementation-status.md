# 正式模块实现与证据

实现证据基线：2026-10-11；设计更新：2026-10-12。对应 [ADR 0004](adr/0004-modular-platform-increment.md)。当前为正式目录首轮可运行实现，不等于 37 项需求全部验收通过。

| 目录 | 已有代码 | 明确剩余范围 |
|---|---|---|
| contracts | Pydantic 严格输入/任务输出、v1 OpenAPI、版本握手、统一错误码 | 完整事件/产物契约、兼容版本矩阵与废弃策略 |
| control-plane | PostgreSQL 事务状态、角色/池权限、Agent 注册、持久队列、单槽位调度、租约/epoch、取消、核对、模型预留与共享计数 | 外部工作流引擎、行级调度优化、安全任务重试、对象存储、货币预算、身份系统和自动轮换 |
| runner | 独立进程、节点凭证、主动领取/续租、持久 journal、有限模型/工具循环、Demo/OpenSandbox | Cube 输出有界方案、自动宿主停止证明、日志补传、多槽位、挂载与边缘上传策略 |
| web | 版本化 API、Agent 提交、池选择、状态/事件/结果、取消、token 用量、管理员节点和任务核对 | 真实浏览器自动化、用量核对表单、用户登录、任意文件产物、完整事件续读 |
| deploy | Dockerfile、PostgreSQL Compose、幂等迁移、私有凭证初始化、启动与恢复说明 | 本环境 Docker 实测、TLS 部署模板、备份恢复/回退演练、保留策略 |
| examples | 固定 Python/Shell/浏览器 Agent 定义、有限模型 Agent、注册脚本 | 真实 EX-01/02/03 证据、只读工作区和边缘数据策略 |
| tests/acceptance | 状态/权限/并发/额度/Runner 故障与模拟模型循环；PostgreSQL CI | 真实沙箱/模型/三类节点/容量/24h 与运维演练 |

D1 完整目标设计现已补齐，见 [设计入口](design/README.md)；以下未实现/未实测项仍保留，不随设计合并自动完成。

## 本地证据

- 正式模块 28 项测试通过：SQLite 显式测试适配器；模型和沙箱均使用模拟基础设施。并发提交/领取、权限拒绝、吊销不可撤销、取消竞态、epoch、journal 崩溃恢复、用量预留/核对和工具循环有覆盖。
- Python 编译检查、Web JavaScript 语法检查通过。
- 实际启动控制面与独立 Runner 两个进程，通过本机 HTTP 完成注册、提交、领取、持久化终态和结果下载；该冒烟使用 Demo 与显式 SQLite 测试适配器。
- 原有开发服务 15 项与沙箱 PoC 9 项回归检查通过。
- PostgreSQL 用例由 `platform.yml` 在真实 PostgreSQL 服务上执行；结果以本次 commit 的 Actions 为准，不把本地 SQLite 测试当作 PostgreSQL 证明。
- 本地没有 Docker/KVM 或真实模型配置；实际沙箱、真实 LLM 和 UI 浏览器验收未运行。

运行命令：

```bash
PYTHONPATH=control-plane:. .venv/bin/python -m unittest discover -s tests/acceptance -p 'test_*.py' -v
node --check web/app.js
```

设置 `TEST_DATABASE_URL` 可对专用 PostgreSQL 测试数据库执行同一套测试；每个测试创建随机 test_* schema 并在结束时删除。账户必须有建 schema 权限，不使用生产数据库。

## 与需求的关系

CT-01/02/03/05、CP-01/02/03/05、RN-01/02/05、WB-01/02/04、DP-01/04 已有子集实现与自动化证据；CP-06/07、RN-03/04 的真实后端/模型验证仍未完成。M2–M4 指标未达成，不以节点位置标签、Demo 或模拟连接代替三类节点与真实容量。

下一门槛是测试宿主上的真实 OpenSandbox 执行/隔离/宿主回收和模型循环，按 ADR 0005 接入已选目标协调器，并验证首发沙箱生产资格，逐步补齐三类节点的数据策略。

## v2 编码分支

D1 后续编码见 [v2 实现与剩余差距](implementation-v2.md)。本页原有 v1 验收结论不扩展为 v2 完成声明。

## v3 图片应用增量

见 [Pi 图片 Agent](image-agent-v3.md) 与 [ADR 0008](adr/0008-pi-image-agent.md)。本地新增 16 项 Python 状态/故障测试及 6 项真实 Pi SDK/Worker 模拟供应商测试通过；全部 acceptance 共 110 项，其中 108 通过、2 项真实 Temporal 按环境跳过。PostgreSQL 与 Temporal 以当前提交 CI 为准。未做真实付费供应商、真实 S3 或生产网络验收；异步槽位释放、完整会话恢复、专用图片 UI 与自动归档未实现。
