# 受控互联网出站（HTTPS 读取）

此功能需要独立代理和宿主验证器；默认策略空列表仍禁网。先在隔离测试 VM/池验收，不能只设置 `HTTPS_PROXY` 就宣布隔离生效。

## 1. 网络与信任拓扑

为沙箱使用专用 Docker bridge，管理服务可信且与任务分离。guest 需要独立 netns、非 root、只读根、drop ALL、no-new-privileges 和既有资源上限。当前验证器拒绝 host/default/bridge/none 和 container: 共享模式；自定义 bridge 名称例如 `agent-egress`。若 SDK/Server 版本强制使用共享 sidecar netns，先完成适配验收，当前代码会拒绝。

示例地址：代理 `172.30.0.1:3128`，管理来源 `172.30.0.1/32`，guest execd TCP 44772。按实际拓扑调整管理来源和端口；代理不可暴露到公网或普通业务网。验证器不能与 guest 共享 netns，任务不可挂 Docker socket 或代理私钥。宿主桥防火墙限制其他来源访问代理，仅由该受管池使用。

独立验证器主机安装 `nftables`、`iproute2`、`util-linux`；使用 root 服务运行已有 `runner.host_verifier:create_app`，配置：

```bash
export AGENT_EGRESS_PROXY_IP=172.30.0.1
export AGENT_EGRESS_PROXY_PORT=3128
export AGENT_EGRESS_MANAGEMENT_CIDR=172.30.0.1/32
export AGENT_EGRESS_MANAGEMENT_PORT=44772
```

保留既有验证器 TLS、Docker 标签、管理 API、签名密钥与认证配置。`/network` 根据 Docker 观察出的 PID 安装规则；调用者不能传 PID、命令或 nft 内容。安装不覆盖未知旧规则；读取和精确比对失败则拒绝。策略规则只在 guest netns 内，不修改宿主 ruleset。停止/删除容器后该 netns 规则随之消失。

OpenSandbox 管理配置必须预先使用专用网络。SDK 仅为代理 IPv4 /32 提交 allow，精确端口与绕过限制由验证器 nft 执行。镜像 entrypoint 必须可信；在首次宿主证明前不会提交任务源码。

## 2. 代理 CA 与进程

仅在代理的私有配置目录生成专用测试 CA（生产按组织 CA 生命周期管理）：

```bash
umask 077
mkdir -p .local/egress
openssl req -x509 -newkey rsa:3072 -sha256 -nodes -days 30 \
  -subj '/CN=Agent Sandbox Egress CA' \
  -addext 'basicConstraints=critical,CA:TRUE' \
  -keyout .local/egress/ca.key -out .local/egress/ca.pem
```

任务镜像仅安装 `ca.pem` 为 `/etc/agent-egress/ca.pem`，并安装到 Chromium 的 CA 信任库；不复制 `ca.key`。不要全局关闭 TLS 验证，代理上游始终执行系统 CA 验证。CA 接近失效或轮换后更新镜像并重建代理。

安装 `control-plane/requirements.txt`，从项目根运行：

```bash
export PYTHONPATH=control-plane:.
export AGENT_EGRESS_BIND=172.30.0.1
export AGENT_EGRESS_PROXY_PORT=3128
export AGENT_EGRESS_CA_CERT=/secure/egress/ca.pem
export AGENT_EGRESS_CA_KEY=/secure/egress/ca.key
export AGENT_EGRESS_CONTROL_URL=https://control.internal/internal/v2
python -m egress.proxy
```

服务管理器通过私有凭证文件注入随机 `AGENT_EGRESS_SERVICE_TOKEN`（至少 24 字符）。控制面配置其 SHA-256 为 `AGENT_EGRESS_SERVICE_TOKEN_SHA256`，与 Temporal 的内部 Token 不同。不得写入 Git、任务镜像或网页。代理为非 root 用户，只有 CA 私钥读取权限；使用只读文件系统和有限私有临时目录，部署健康监控与重启策略。服务不会记录 Token、URL 路径/查询、请求头或正文；日志仅含请求 ID、域名、状态/拒绝类型和字节数，日志系统负责保留与访问授权。

## 3. 策略与 Runner

发布新的不可变 policy version，示例：

```json
{"version":"2","allowed_pools":["server"],"tool_kinds":["python","shell","browser"],"egress_rules":["docs.python.org","www.python.org"],"data_classes":["public"]}
```

精确域名独立批准：`python.org` 不隐含 `www.python.org`，重定向及 CDN 子资源的域名也要独立批准；不支持通配符、尾点、端口或 URL。站点可能因 POST、压缩、登录或第三方子资源限制不能完整显示，这是明确的读取范围，不自动扩大权限。

用新的节点注册票据设置 `backend=opensandbox, egress_enabled=true`。旧节点默认 false。Runner 设置与验证器一致的 `AGENT_EGRESS_PROXY_IP/PORT`；沿用 TLS 验证器配置。策略空列表的任务不注入代理，私有/边缘数据提交到出站策略会返回拒绝。

Runner 只给 guest 注入短期 attempt 凭证、HTTPS_PROXY、CA 路径及 Playwright 所需变量，不把凭证存入 Runner WAL。节点管理 API/Docker inspect 属于管理员权限，其中 guest 环境包含短期凭证，限制访问且不要转储日志。

Python/命令可使用支持标准 HTTPS 代理的客户端；Playwright 使用：

```python
import os
from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    browser = p.chromium.launch(proxy={
        "server": os.environ["AGENT_EGRESS_PROXY"],
        "username": "agent",
        "password": os.environ["AGENT_EGRESS_TOKEN"],
    })
    page = browser.new_page()
    page.goto("https://docs.python.org/3/", wait_until="domcontentloaded")
    print(page.title())
    browser.close()
```

单次 CONNECT 仅处理一次 HTTP 请求，然后关闭；支持浏览器重建连接，未提供连接复用。请求仅 GET/HEAD、无 body/Cookie/Authorization，响应最大 2 MiB，128 次/attempt，15 秒/连接；控制面不可达或租约失效即关闭，不退款、不自动重试。

## 4. 验证与启用门槛

```bash
PYTHONPATH=control-plane:. python -m unittest discover -s tests/acceptance -p test_egress.py -v
sudo env PYTHONPATH=. python3 tests/acceptance/egress_kernel.py
```

第二条仅在专用测试 VM/CI 运行：创建临时 netns/veth 和测试地址，验证真实 TCP/UDP/IPv6/元数据阻断及规则篡改拒绝，结束销毁，不接触宿主 nft ruleset。GitHub `Controlled egress kernel checks` 自动执行。

实际 OpenSandbox 启用前还必须验证：授权域名页面成功；未批准重定向/子资源被拒；CA 错误失败；移除代理变量仍无法直连；127.0.0.11 DNS、QUIC、IPv6、元数据和其他 guest 不可达；取消/撤销/代理故障停止；重启不重放；清理后 Docker/netns 无残留。用真实镜像/Server/网络拓扑执行并保存证据，不能以单元测试或独立 netns 测试代替全链路验收。

官方接口依据：[Python Stream TLS](https://docs.python.org/3.12/library/asyncio-stream.html#asyncio.StreamWriter.start_tls)、[Playwright 代理](https://playwright.dev/python/docs/network#http-proxy)、[nftables](https://netfilter.org/projects/nftables/manpage.html)。
