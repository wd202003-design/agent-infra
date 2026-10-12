# 正式模块首轮启动与运维

此版本将控制面、Runner、契约和 Web 分开运行，采用 PostgreSQL；范围以 [ADR 0004](../docs/adr/0004-modular-platform-increment.md) 为准。它是集成验收版本，尚未达到完整生产 MVP 的退出条件。

## Docker Compose 启动 Demo 链路

需要受控 Linux 宿主、Docker Compose v2、Python 3.12，以及可访问依赖仓库。命令在仓库根目录执行。当前环境未运行 Docker，以下配置需在测试宿主验证。

```bash
python3.12 -m venv .venv
.venv/bin/pip install -r control-plane/requirements.txt -r runner/requirements.txt
.venv/bin/python deploy/configure.py
docker compose --env-file .local/platform.env -f deploy/compose.yaml up --build -d db migrate control
.venv/bin/python examples/bootstrap.py
docker compose --env-file .local/platform.env -f deploy/compose.yaml --profile runner up --build -d runner
```

打开 http://127.0.0.1:8766，在页面输入 `.local/user.token` 的内容；管理员使用 `.local/admin.token`。令牌文件为本机私有配置，不提交、不发送给他人。初始化脚本拒绝覆盖已有凭证；Compose 日志不应包含令牌。不要把 `docker compose config` 输出粘贴到公共位置，其中包含展开的环境秘密。

选择 `python-analysis @ 1`、`demo` 池提交。独立 Runner 领取后应生成显式 SIMULATED 结果；Demo 不运行源码、不调用模型，也不证明沙箱可用。管理员可以排空和吊销节点；吊销不可恢复原凭证，需重新注册。

停止应用：

```bash
docker compose --env-file .local/platform.env -f deploy/compose.yaml --profile runner stop runner control
```

保留数据库和 Runner 数据卷供核对。不要用删除卷作为重启/恢复手段。

## 不使用 Compose 的控制面

先由管理员创建专用 PostgreSQL 数据库和受限账户。迁移账户需建表权限；应用账户至少访问本项目表。不要复用生产业务数据库。设置 `AGENT_DATABASE_URL` 为 PostgreSQL DSN，`AGENT_USERS_JSON` 为用户配置数组；可从生成的本机环境文件读取配置，注意 Compose 的 `db` 主机名不能直接用于宿主进程。

```bash
export PYTHONPATH=control-plane:.
.venv/bin/python -m infra_control migrate
.venv/bin/python -m infra_control serve --host 127.0.0.1 --port 8766
```

`migrate` 为幂等建表，schema version 1；服务启动要求 schema 已就绪。当前尚无自动降级迁移。应用/数据库不兼容时先恢复匹配版本，不能盲目删除字段或数据库。

用户配置每项包含 `id`、`role`（admin/user）、`token_sha256`、允许的 `pools`。服务端只保存令牌哈希配置；用户身份由认证确定，普通使用者不能读其他人的任务/结果。配置变更需重启控制进程，暂未提供登录系统或自动轮换界面。

## 独立真实 Runner

先完成 OpenSandbox [PoC 准备](../poc/sandbox/README.md)，验证网络/文件隔离及宿主回收。正式 Runner 不接受任意管理 URL、镜像、挂载或环境变量作为任务参数；这些由节点管理员配置。

注册新节点并保存独立凭证：

```bash
.venv/bin/python examples/bootstrap.py --backend opensandbox --pool server --name server-01 --output .local/server-01.env
set -a
. .local/server-01.env
set +a
export OPEN_SANDBOX_IMAGE=agent-infra/poc-browser:local
export OPEN_SANDBOX_DOMAIN=http://127.0.0.1:8080
read -rsp 'OpenSandbox 管理凭证: ' OPEN_SANDBOX_API_KEY
export OPEN_SANDBOX_API_KEY
.venv/bin/python -m runner --backend opensandbox --data .local/runner-server-01
```

凭证文件绑定此节点；每个 Runner 使用独立数据目录。单节点单槽位。跨主机控制面连接使用 HTTPS；`http://control` 仅用于 Compose 私有网络。本机/边缘可用相同协议注册位置标签，但真实工作区挂载、摘要上传限制和三类业务样例尚未交付。

真实后端执行返回后，当前自动化证据只到管理 API 删除确认。Runner 会保留结果并上报 `needs_review`，管理员完成宿主残留检查后通过控制台提交证据才能转终态。这是明确的验收缺口，不宣称已实现自动宿主证明。Cube 仍用于开发 PoC，正式 Runner 禁用，直到输出接收硬限制及回收验证完成。

## 真实模型循环

管理员设置以下控制面变量，Runner 不需要上游密钥：

| 配置 | 含义 |
|---|---|
| AGENT_MODEL_URL | 可信网关基址，支持 POST `<base>/chat/completions`；不接受用户指定 URL |
| AGENT_MODEL_KEY | 网关服务凭证，仅在控制面保存 |
| AGENT_MODEL_NAME | 网关已配置模型名 |

响应必须提供文本 `choices[0].message.content` 和整数 `usage.total_tokens`。模型文本遵循 `Action` 的 action/content JSON 契约，不能自动执行额外工具或解析 Markdown 代码块。流式响应、第三方 Runtime 插件和工具并行尚不支持。

在真实节点池提交 `bounded-analyst @ 1`，输入合成数据。每次模型请求先预留 token；固定默认全局额度为 2 个未结算请求、20 RPM、200000 TPM，多个控制实例共享同一数据库计数。控制实例必须采用相同配置。用量未知时保留预留，不自动重发；当前 token 估算不等于供应商实际账单或硬货币预算。

## 核对与恢复

| 故障 | 操作 |
|---|---|
| 控制面重启 | 保留数据库，启动后读取原任务；不重放已领取 attempt；下一次查询/领取触发租约过期处理 |
| Runner 崩溃 | 保留 journal；重启只重发已保存结果或报告 needs_review，不重新执行旧工具 |
| 取消后失联 | 保持 cancel_requested；检查宿主进程树、浏览器、容器/VM 和外部请求，之后提交核对证据 |
| 创建响应丢失 | 按沙箱 metadata 的 project=agent-infra、task_id（含 attempt）查找；不能重新创建掩盖未知资源 |
| 模型响应/用量未知 | 管理员先查询任务 `/usage`，核对上游记录，调用对应 `/usage/{request_id}/reconcile`；随后核对任务，不自动继续旧执行 |
| 日志/数据库增长 | 当前未自动清理，监控容量；删除/保留政策未冻结前，不直接删除幂等键、claim 或未知账目 |

用量核对请求为 `{"actual_tokens":123,"evidence":"可追溯的上游核对证据"}`。仍在执行的请求不能立即核对，pending 至少等待 60 秒。未知真实费用不能填零来解锁额度。所有核对操作记录管理员身份与审计。

## 交付前仍需完成

工作流引擎选型、对象存储文件产物、任务安全自动重试、日志补传队列、短期节点引导/轮换、本机工作区和边缘上传策略、保留/删除政策、备份恢复 RPO/RTO、100 并行和 24h 容量验收。Compose 镜像目前使用 major tag，生产交付前必须固定 digest 并扫描；本文件不构成生产发布许可。
