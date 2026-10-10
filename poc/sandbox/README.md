# 动态执行沙箱 PoC

第一阶段验证工具执行面：创建 → Shell → Python → Chromium 本地页面交互 → 导出产物 → 命令超时/中断 → 删除。
脚本使用固定测试代码，不调用 LLM，也不是完整 Agent 或 Runner；下一阶段才接入模型生成的工具调用。
当前只支持 Linux 测试宿主。SDK 和 Server 基线均为 1.1.1，尚未完成真实沙箱兼容性验收。

## 准备与启动

在隔离的 Linux 测试 VM 上运行，需 Docker Engine、Python 3.12、镜像仓库及 PyPI 访问能力。
Server 管理宿主 Docker，不能放进执行不可信代码的任务沙箱。

```bash
python3.12 -m venv .venv
.venv/bin/pip install -r poc/sandbox/requirements.txt opensandbox-server==1.1.1
docker build -t agent-infra/poc-browser:local poc/sandbox
mkdir -p .local
.venv/bin/opensandbox-server init-config .local/sandbox.toml --example docker
chmod 600 .local/sandbox.toml
```

编辑生成的配置（不要覆盖已有生产配置）：

- `[server]`：保留 `host = "127.0.0.1"`，设置独立随机 `api_key`，将 `max_sandbox_timeout_seconds` 设为 300。
- `[docker]`：使用 `network_mode = "bridge"`，保留 `no_new_privileges`、默认能力限制；本 PoC 不挂载宿主目录或 Docker socket 到任务容器。
- `[egress]`：保留生成的版本化 sidecar 镜像，设置 `mode = "dns+nft"`、`disable_ipv6 = true`。
- `[runtime]`：保留生成的 execd 镜像版本；记录实际 digest。
- 不将服务端 API key 配置在 `sandbox_env` 中。检查宿主防火墙，沙箱不能访问管理面和其他容器的执行端口。

DNS 模式不阻止直接 IP 访问；即使使用 dns+nft，也必须通过受控网络测试证明强制生效。
本脚本发送默认拒绝的网络策略，但脚本通过不代表网络隔离验收通过。

终端一：

```bash
.venv/bin/opensandbox-server --config .local/sandbox.toml
```

终端二，在 Bash 中安全输入与配置一致的 key：

```bash
read -rsp 'Sandbox API key: ' OPEN_SANDBOX_API_KEY
export OPEN_SANDBOX_API_KEY
.venv/bin/python poc/sandbox/probe.py --image agent-infra/poc-browser:local
unset OPEN_SANDBOX_API_KEY
```

每次运行在 `.local/sandbox-poc/<run_id>/` 生成 report.json、result.json 与 browser.png。
镜像须在 Server 所使用的 Docker daemon 中可用；跨主机时推送到受控镜像仓库并使用 digest。
本地 tag 仅用于开发，基准测试须记录 `docker image inspect` 的镜像 ID/digest、CPU/内存和所有组件版本。

浏览器使用本地 HTML，验证点击和截图，避免依赖外网或真实账号。
不会验证站点导航、登录、下载、上传或真实业务提交；这些在第二阶段使用受控测试站点验证。

## 结果与恢复

- 退出码 0：固定功能检查完成，且管理 API 已确认沙箱不存在。
- 退出码非 0：检查 report.json 中的状态、已完成检查和错误类型；报告不保存原始 SDK 异常正文。
- `needs_review` / `cleanup=unknown`：使用记录的 sandbox_id 从管理面核对；禁止将其视为已回收。
- `creation_outcome_unknown`：按 metadata 中的 project/run_id 查询本次创建结果，再人工清理。不得自动重发创建。
- SIGTERM / 首次 Ctrl+C 尽力回收；SIGKILL、断网、重复中断无法保证 finally 执行，依赖 TTL 和管理员对账。
- TTL 为 300 秒；总执行等待 180 秒，另预留清理时间。TTL 不是已删除的证据。
- 命令停止通过执行状态确认；沙箱删除通过管理 API 404 确认。这仍不等于宿主进程、网络及计费资源完全清理，真实验收必须检查 Docker 容器与残留进程。

脚本仅适用于固定小型 fixture，尚未实现任意代码输出量限制、流式产物传输或文件大小预检；不可直接暴露为用户执行 API。

## 本地与 CI 测试

```bash
.venv/bin/python -m unittest discover -s poc/sandbox -p 'test_*.py' -v
.venv/bin/python poc/sandbox/probe.py --help
```

单元测试使用模拟 SDK，覆盖成功、执行失败、取消、超时、创建结果未知、清理失败、403 误判以及未知退出状态。
GitHub Actions 只跑这些检查，不创建真实沙箱、不使用生产密钥。

## 本次验证记录（2026-10-10）

- Python 3.12.14，安装并导入 opensandbox==1.1.1 成功；SDK 参数检查和 CLI 帮助通过。
- 9 项检查通过（包含生命周期故障单元测试和浏览器 payload 语法检查）。
- 当前开发环境无 Docker，也未提供可连接的 OpenSandbox 服务；镜像构建、真实代码/Shell/Chromium、网络隔离及宿主残留检查未执行。
- 未验证 LLM、控制面、Runner、任务引擎和容量；不得据此宣布 MVP 完成。

## 官方接口依据

- [Playwright 示例](https://github.com/opensandbox-group/OpenSandbox/tree/release-1.1.1/examples/playwright)
- [Python SDK](https://github.com/opensandbox-group/OpenSandbox/tree/release-1.1.1/sdks/sandbox/python)
- [Server 配置](https://github.com/opensandbox-group/OpenSandbox/blob/release-1.1.1/server/configuration.md)

本仓库脚本为独立编写，使用 SDK 公共接口；没有复制上游示例代码。
