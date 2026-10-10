# Agent Infra

面向服务器、本机和在线边缘节点的统一 Agent 执行平台。

> 当前状态：沙箱 PoC 开发中。仓库包含设计、目录骨架和独立验证脚本，尚无完整平台服务；容量指标均为待验证目标，不代表已具备的能力。

## MVP 目标

通过统一 Web 与任务 API 提交 Agent 任务，调度至指定执行池，通过统一模型网关访问模型，并提供状态、日志、结果、取消和费用估算。

- 10～20 个真实节点，覆盖服务器、本机和受管 Linux 边缘节点。
- 100 个代表性任务真实并行，1,000 个活跃任务管理。
- 1,000 个模拟 Runner 连接压力测试，独立于真实执行能力验收。
- 统一身份、节点槽位、模型额度、任务预算与受控故障恢复。
- 同一个 Runner 协议覆盖三类节点；MVP 边缘节点在线接入中央调度。

## 文档入口

- [MVP 范围与验收](docs/mvp.md)
- [架构与可靠性约束](docs/architecture.md)
- [实施路线](docs/roadmap.md)
- [沙箱 PoC 操作说明](poc/sandbox/README.md)
- [沙箱验收矩阵](docs/sandbox-poc.md)
- [架构决策记录](docs/adr/README.md)
- [贡献规范](CONTRIBUTING.md)
- [Agent 开发约束](AGENTS.md)

## 目录

| 路径 | 规划职责 |
|---|---|
| `control-plane/` | 任务 API、节点接入、授权、调度集成 |
| `runner/` | 统一节点代理、租约、执行与事件回传 |
| `web/` | 提交任务、查看进度、结果与节点管理 |
| `contracts/` | 版本化 API、任务及 Runner 协议 |
| `deploy/` | 开发环境与部署配置 |
| `examples/` | 只读或产物生成型示例 Agent |
| `tests/acceptance/` | 故障、容量及端到端验收 |
| `docs/` | 需求、架构、路线与决策 |

平台组件目录仍为占位；poc/sandbox 提供独立的代码、命令和浏览器执行验证脚本。

## 技术决策状态

控制面按模块化服务设计，使用 PostgreSQL 与对象存储作为目标存储方案。
工作流引擎在 Hatchet / Temporal 中择一；Agent Runtime 和模型网关各选一种。
OpenSandbox 已选为动态执行 PoC 基线，详见 ADR 0001。Python 仅为 PoC 脚本语言；平台语言、本机首个目标操作系统及生产部署方案仍待决。

## 获取项目

```bash
git clone https://github.com/wd202003-design/agent-infra.git
cd agent-infra
```

先按 [沙箱 PoC 操作说明](poc/sandbox/README.md) 在 Linux 测试 VM 上验证执行链路。当前尚无 Web、统一 Runner 或任务 API 服务。

## 许可与凭证

尚未选择开源许可证。添加第三方代码或准备公开发布前，先确认许可证及依赖许可。
不得提交密钥、Token、客户数据或真实生产配置。
