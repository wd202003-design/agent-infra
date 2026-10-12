# 0007：受控 HTTPS 出站

- 状态：接受实现边界；生产资格仍须真实宿主与浏览器验收。
- 日期：2026-10-12。

## 决策

延续默认禁网。新增策略 `egress_rules` 精确 ASCII 小写域名列表（最多 32 个，无通配符）；只有 public 任务与 `egress_enabled=true` 的 OpenSandbox 节点可使用。private、edge_summary 继续禁网，避免扩大数据出口。

采用独立 HTTPS 检查代理。CONNECT 仅用于切换至代理签发的 TLS，绝不作为任意 TCP 隧道；解密后只接收匿名 GET/HEAD。连接目标、TLS SNI 和 HTTP Host 必须一致。上游单独建立 TLS，校验公共 DNS 全部答案，再直接连接批准 IP，保留原域名 SNI 与证书校验，防止二次 DNS 查询/重绑定及域名前置绕过。

禁止明文 HTTP、裸 IP、非 443 端口、内网/回环/链路本地/保留/组播/IPv6 转换地址、Cookie/Authorization、请求体、WebSocket、HTTP/2、压缩响应和其他方法。重定向不由代理自动跟随；客户端的新请求重新鉴权、解析和校验。允许域名不代表其页面或内容可信，模型仍应将网页视为不可信输入。

独立宿主验证器在执行动态任务前安装并读回网络命名空间内的精确 nftables 规则。OUTPUT 只允许固定代理 IP:端口、受限本地 TCP、管理请求的已建立回复；INPUT/FORWARD 默认拒绝。无 DNS/UDP/QUIC 或外部 IPv6 放行；Docker 127.0.0.11 DNS 也不在本地例外中。删除代理变量、使用自建 socket 或关闭浏览器代理不能突破内核限制。任务无 NET_ADMIN/NET_RAW/特权及宿主命名空间能力。

## 授权与资源

Runner 获取绑定 node/attempt/epoch/policy 的签名凭证，仅注入本次 guest；不暴露节点或控制面凭证。代理使用独立服务身份调用 `/internal/v2/egress/check`，不持有控制面签名密钥或 Temporal 服务身份。每次请求预留计数，重复检查不重复计数；最多 128 次/attempt，每次响应 2 MiB，连接总时限 15 秒，代理实例最多 32 个连接。检查时限受当前租约限制；传输期间每 0.5 秒重新检查，取消/撤销/失联后关闭，不声称撤回已发送字节。

不存在代理层自动重试，具有出站策略的任务也禁止自动安全重试。GET/HEAD 是协议方法限制，不证明目标业务没有副作用；管理员只批准真正可读取的站点。动态网站 POST API、账号登录、上传、通用 TCP/WebSocket 暂不支持。

## 运维边界

代理 CA 仅用于任务镜像；私钥仅代理持有，不安装到操作者系统，不复制生产浏览器会话。代理端口仅在受管沙箱专用桥上开放，服务凭证仅代理到控制面的 TLS 请求使用。宿主验证器需要 root/nsenter/nft 与 Docker 只读观察权限，部署在 Runner 之外。

任何命名空间、规则、TLS 或授权校验失败均拒绝。共享 `container:` 网络命名空间和未适配的 OpenSandbox sidecar 拓扑不会被自动放行；应在独立测试池完成兼容配置，而不是降低验证器要求。详细配置与验收见 [部署说明](../../deploy/egress.md)。
