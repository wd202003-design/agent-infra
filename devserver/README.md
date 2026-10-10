# M1 开发版：任务提交到沙箱结果

提供本机 Web、Bearer 认证 API、SQLite 持久化任务、单个后台执行器、任务事件、取消和结果 JSON 下载。
支持三种后端：Demo、OpenSandbox、CubeSandbox。Demo 不执行任何提交代码。
这是 M1 的执行服务增量，不是完整 Agent 平台：尚未接入 LLM、Agent Runtime、分布式 Runner、任务引擎、计费或预算。

## 启动开发模式

Linux / Python 3.12，在仓库根目录执行：

```bash
python3.12 -m venv .venv
.venv/bin/pip install -r devserver/requirements.txt
read -rsp '设置至少 24 字符的开发访问令牌: ' AGENT_INFRA_TOKEN
export AGENT_INFRA_TOKEN
export AGENT_INFRA_BACKEND=demo
.venv/bin/python -m devserver
```

打开 http://127.0.0.1:8765，输入同一个令牌并连接。默认队列最多 100 个未完成任务，同时执行 1 个。
令牌只保留在浏览器页面内存，不写入 localStorage；关闭页面后重新输入。
提交 Python / Shell / 浏览器 Python 程序，查看状态、事件与结果，或申请取消。
默认数据位于 `.local/devserver/tasks.sqlite3`，可设置 `AGENT_INFRA_DATA`。
该目录包含提交源码及输出，应只允许操作者访问；不要放入 Git。当前没有自动保留期/删除功能。

脚本只监听 loopback。暂不通过修改监听地址将其暴露为公网或多用户服务。
后台执行器与 HTTP 服务同进程；文件锁拒绝同数据目录启动第二个服务。不使用 uvicorn 多 worker 或 reload。

## 真实 OpenSandbox 后端

先按 [沙箱 PoC](../poc/sandbox/README.md) 部署并验证单任务。
在启动进程前设置：

```bash
export AGENT_INFRA_BACKEND=opensandbox
export OPEN_SANDBOX_DOMAIN=http://127.0.0.1:8080
export OPEN_SANDBOX_IMAGE=agent-infra/poc-browser:local
read -rsp 'OpenSandbox 管理 API key: ' OPEN_SANDBOX_API_KEY
export OPEN_SANDBOX_API_KEY
.venv/bin/python -m devserver
```

适配器请求 1 CPU / 2 GiB、300 秒 TTL、默认拒绝出站网络，使用 Server proxy 访问执行端。
模板需提供 Python，浏览器任务还需 Playwright/Chromium；Shell 使用镜像内环境。
生成的容器不挂载宿主目录，不注入服务端环境或模型密钥。

## 真实 CubeSandbox 后端

先独立部署 Cube，开启认证回调并隔离管理面。设置 CUBE_API_KEY 本身不会启用服务端认证。
创建包含 Python、Playwright/Chromium、envd 执行能力的模板。模板必须存在非 root 用户 `user`；适配器显式以此用户执行命令。

```bash
export AGENT_INFRA_BACKEND=cubesandbox
export CUBE_API_URL=http://127.0.0.1:3000
export CUBE_TEMPLATE_ID=你的模板ID
export CUBE_PROXY_NODE_IP=127.0.0.1
read -rsp 'Cube API key: ' CUBE_API_KEY
export CUBE_API_KEY
.venv/bin/python -m devserver
```

本地 HTTP 仅用于受控单机测试；跨主机部署需按 Cube 官方文档配置可信网络、TLS 与代理域名。
适配器禁用 internet access 并请求 IPv4/IPv6 deny-all，不启用快照、回滚、自动重试或跨节点恢复。
资源规格由 Cube 模板/服务端配置，本版不将用户输入直接转换成资源配额。
Cube SDK 0.7.0 为同步接口，调用放入线程；取消不强行终止 SDK 线程，以免创建请求返回前丢失沙箱归属。
真实命令执行、模板兼容、策略强制、软删除状态以及 TTL 语义仍须集成测试。

## 任务与 API

所有 `/api/*` 要求 `Authorization: Bearer <AGENT_INFRA_TOKEN>`。

| 方法 | 路径 | 功能 |
|---|---|---|
| GET | /api/info | 后端、模拟标志、当前限制 |
| POST | /api/tasks | 提交；必须带 Idempotency-Key |
| GET | /api/tasks | 最近 100 个任务 |
| GET | /api/tasks/{id} | 任务详情 |
| POST | /api/tasks/{id}/cancel | 幂等申请取消 |
| GET | /api/tasks/{id}/events | 有序状态事件 |
| GET | /api/tasks/{id}/result | 下载结果 JSON；未生成返回 409 |

请求示例：

```json
{"kind":"python","source":"print(sum(range(10)))","timeout_seconds":30}
```

kind 为 python / shell / browser；browser 提交的是在预装浏览器模板中执行的 Python 程序。
源码限制 16,384 字符，执行超时 1～60 秒。相同幂等键和输入返回原任务，修改输入返回 409，队列满返回 429。
后端由服务端配置，调用者不能指定管理 URL、宿主挂载、节点凭证或环境变量。
JSON 产物包含执行退出状态和截断输出，尚无任意沙箱文件下载接口；截图导出继续使用已有独立 PoC。

## 状态与故障

- queued → running → succeeded / failed / needs_review。
- queued 取消直接进入 cancelled；running 取消先进入 cancel_requested。
- 当前取消为协作式：等待正在进行的 SDK 调用返回，再删除沙箱并核对管理 API。没有实现即时进程树中断。
- 只有回收确认后才能报告 cancelled；结果未知或回收异常优先保留 needs_review。
- 命令/创建调用超时不自动重试，可能存在外部副作用。
- 重启时 queued 保留；running/cancel_requested 改为 needs_review，并递增执行代次，拒绝旧代次结果。
- backend 配置改变后，旧后端的排队任务进入 needs_review，不能误用新后端执行。
- 创建前已持久化 task_id；若创建响应丢失，可按沙箱 metadata 的 project=agent-infra-dev、task_id 对账。
- 程序崩溃、强杀或 SDK 连接卡住时需要管理员核对残留沙箱；TTL 不是已回收证明。
- 管理 API 不存在只是一项回收证据，真实验收仍需确认宿主进程/容器/VM 已停止。

本版仅面向受信任操作者的有限测试任务。还未完成对抗性代码隔离验收；Cube SDK 会累积命令输出后返回，应用层截断不能限制其接收内存。因此不能作为开放任意代码执行入口。
OpenSandbox 路径禁用 SDK 日志累积并保留有限输出。两后端都不在控制面执行提交代码。
认证是单组织单共享令牌，没有用户级资源隔离、角色或审计身份；正式版本必须补齐。

## 验证

```bash
.venv/bin/python -m unittest discover -s tests -p 'test_devserver.py' -v
.venv/bin/python -m unittest discover -s poc/sandbox -p 'test_*.py' -v
```

2026-10-10：开发版 15 项测试通过，覆盖并发去重/领取、重启恢复、执行代次、取消、清理异常、HTTP 认证和结果下载。
已启动本机 HTTP 服务并完成认证、提交、轮询到终态及下载结果的冒烟测试；JS 语法检查通过。Demo 模式验证真实 HTTP/API 与状态机，不能作为真实沙箱执行结果。
当前环境无 Docker/KVM 沙箱服务，两个真实后端未完成集成验收；浏览器 UI 自动化也因无 Chromium 二进制未运行。
