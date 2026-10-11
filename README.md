# Agent Infra

面向服务器、本机和在线边缘节点的统一 Agent 执行平台。

> 当前状态：正式模块首轮实现。已提供 PostgreSQL 控制面、独立 Runner、v1 契约、Web 和有限模型工具循环；真实沙箱/模型及三类节点、容量仍待验收。完整差距见 docs/implementation-status.md。

## MVP 目标

通过统一 Web 与任务 API 提交 Agent 任务，调度至指定执行池，通过统一模型网关访问模型，并提供状态、日志、结果、取消和费用估算。

- 10～20 个真实节点，覆盖服务器、本机和受管 Linux 边缘节点。
- 100 个代表性任务真实并行，1,000 个活跃任务管理。
- 1,000 个模拟 Runner 连接压力测试，独立于真实执行能力验收。
- 统一身份、节点槽位、模型额度、任务预算与受控故障恢复。
- 同一个 Runner 协议覆盖三类节点；MVP 边缘节点在线接入中央调度。

## 文档入口

- [正式模块启动](deploy/platform.md)
- [实现状态与验证证据](docs/implementation-status.md)
- [按目录推进的设计需求](docs/requirements.md)
- [MVP 范围与验收](docs/mvp.md)
- [架构与可靠性约束](docs/architecture.md)
- [实施路线](docs/roadmap.md)
- [启动开发控制台](devserver/README.md)
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

正式模块位于 control-plane、runner、contracts、web；deploy 和 examples 提供启动与样例。devserver 保留早期开发实现，poc/sandbox 保留独立沙箱验证脚本。

## 技术决策状态

控制面按模块化服务设计，使用 PostgreSQL 与对象存储作为目标存储方案。
工作流引擎在 Hatchet / Temporal 中择一；Agent Runtime 和模型网关各选一种。
OpenSandbox 为 PoC 基线，生产沙箱仍待实测。ADR 0004 选择 Python 作为首轮模块语言、Linux 为首个节点 OS，数据库队列与有限 JSON Runtime 作为当前实现；外部工作流引擎、Runtime/网关产品选型仍待决。

## 获取项目

```bash
git clone https://github.com/wd202003-design/agent-infra.git
cd agent-infra
```

按 [正式模块启动说明](deploy/platform.md) 启动 http://127.0.0.1:8766。默认 Demo 不执行源码。旧版开发控制台仍可按 devserver/README.md 启动在 8765；两个服务使用不同数据和契约。正式 Runner 暂未开放 Cube 后端。

开发版使用 Python/FastAPI/SQLite，见 ADR 0002；不代表已确定正式控制面语言、工作流引擎或生产数据库替代方案。

## 许可与凭证

尚未选择开源许可证。添加第三方代码或准备公开发布前，先确认许可证及依赖许可。
不得提交密钥、Token、客户数据或真实生产配置。
