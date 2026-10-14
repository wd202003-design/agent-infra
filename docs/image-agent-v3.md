# Pi 图片 Agent 首版

该增量实现生成、编辑、依赖图片筛选、调度与结果输出。默认不开启；与 v1/v2 独立。
运行模型与图片模型可分别配置，原始 SK 只在管理员写入绑定和可信 Worker 领用时传输。

## 模块目录

| 目录 | 职责 |
|---|---|
| `contracts/v3.py`、`openapi-v3.json` | 严格请求模型、OpenAPI；响应 data 首版仍为通用对象 |
| `control-plane/infra_control/v3/` | 调度、预算、凭据、图片校验、API |
| `control-plane/migrations/004_image_tasks.sql` | 根任务、子任务、操作、账本、审计、幂等记录 |
| `runtime/pi-agent/` | Pi 会话、供应商请求、可信 Worker、持久化 intent |
| `tests/acceptance/test_v3.py` | 状态、并发、授权、对象版本与故障测试 |

## 执行及并发规则

1. 用户提交根任务与 agent/image 绑定版本；再提交一次静态 DAG 计划。
2. 控制面检查 DAG、上下文版本、子预算总和、pool 与图片参数；只有依赖成功的子任务可以领取。
3. claim 在一个数据库事务内检查根并行数、用户运行数、Worker 槽位，并递增 epoch。
4. 每个 Pi 会话先预占模型预算与供应商 quota；图片工具再独立预占图片预算与 quota。
   同一供应商账号的所有绑定必须使用相同 quota_pool_id 和 max_inflight。
   配额包括 reserved/dispatching/accepted/unknown。若 agent 与 image 共享配额，配置必须
   给嵌套调用留出余量；配额不足会停止本次 attempt，而非无限等待或自动重试。
5. 输出写入 S3 暂存版本，控制面验证 hash、大小、格式、解码、像素后复制至私有 final 版本。
   完成回执只能引用已提交图片；依赖节点只读取已完成输出。
6. 根任务按 failure_policy、allow_partial 聚合；needs_review 优先于成功和失败。

`max_children <= 8`、`max_parallel_children <= 8`，默认并行 2。每用户最多 32 个活动根任务，
全局最多 1000 个活动根任务；这些是保护性配置值，**不是实测容量**。首版历史记录未自动归档，
长时间运行前应补归档、对象保留策略及索引优化。

## 上下文与恢复

根 brief/constraints/reference_artifact_ids 通过 CAS 新建不可变版本；已排定子任务固定引用原版本。
依赖必须使用相同 context_version。每个子任务独立 Pi 会话，兄弟不共享消息历史、密钥闭包或
工作文件。上下文只包含简报、约束、工作分配和依赖的产物引用，图片按版本下载并校验 hash。

checkpoint 用 expected_revision CAS，保存完成操作 ID、产物 ID 和阶段，不保存完整消息历史。
Worker 重启发现 journal 中已有 job 时只 abandon；unknown 保留费用和配额，管理员查供应商账单
后结算，不能直接重新调用图片接口。30 秒租约、每 5 秒续租；控制面每 5 秒扫描期限。
停止/断网后原始供应商 SK 的有效性由供应商决定，因此不可把租约当成供应商凭据 TTL。

## API 约定

所有请求需 Bearer 身份；所有 POST 需 Idempotency-Key（1–128 个字母数字或 `_.:-`）。
成功返回 `{data, request_id, next_cursor}`。错误返回 `{error:{code,message,retryable,details},request_id}`。
金额使用 USD/CNY 的百万分之一整数，时间用 UTC RFC3339；数据库及 claim attempt 中的内部
lease_until 是 Unix 秒。事件使用 attempt 局部 seq 去重，读取使用根任务单调 sequence 游标。
列表任务按 ID 排序，after 是上一页末尾 ID，不表示创建时间。

| 路径（均加 `/v3`） | 用途与权限 |
|---|---|
| `POST /model-bindings/{id}` | 管理员发布不可变模型版本；返回值不含 SK |
| `POST /model-bindings/{id:version}/revoke` | 撤销绑定，停止新操作及续租授权 |
| `GET /model-bindings`、`GET /image-apps` | 当前用户可用模型与应用能力 |
| `POST /image-tasks`、`GET /image-tasks` | 提交与分页列表 |
| `POST /image-tasks/{id}/plans` | CAS 一次性计划，最多 8 个子任务 |
| `POST /image-tasks/{id}/contexts` | CAS 新建上下文版本 |
| `GET /contexts/{id}/versions/{version}` | 读取固定上下文 |
| `GET /image-tasks/{id}`、`/children`、`/result`、`/events?after=N` | 状态、结果、事件 |
| `POST /image-tasks/{id}/cancel` | CAS 取消，已派发操作仍需结算 |
| `POST /reference-uploads`、`/reference-artifacts/{id}/commit` | 上传并验证参考图 |
| `POST /artifacts/{id}/download-url` | 所有者/管理员获取 60 秒版本化下载 URL |
| `POST /runner/claims` | 专用 Worker 领取；header key 必须等于 claim_id |
| `POST /runner/attempts/{id}/renew`、`/checkpoints`、`/events`、`/complete`、`/abandon` | attempt 生命周期 |
| `POST /runner/attempts/{id}/execution-config`、`/credentials:issue` | 当前绑定与仅限 reserved 操作的 SK |
| `POST /runner/attempts/{id}/image-operations` | 原子预占 agent/generate/edit 操作 |
| `POST /runner/image-operations/{id}/dispatch`、`/report` | 一次性执行许可与费用结果 |
| `POST /runner/attempts/{id}/uploads`、`/artifacts/{aid}/commit`、`/artifacts/{aid}/download-url` | 受 attempt 约束的产物读写 |
| `POST /image-operations/{id}/review` | 管理员凭证据结算 unknown |
| `POST /image-children/{id}/close-review` | 已无未结算操作的异常子任务关闭为失败 |

所有 Worker 变更包含 `{task_id,epoch}`；API 校验 Worker 身份、pool、attempt、租约。
`dispatch` 刻意不返回可再次执行的幂等许可，重复请求返回冲突。其余幂等冲突（同 key 不同参数）
返回 409。用户取消、更新上下文、提交计划的 expected_version 来自最新根任务 state_version。

示例根任务请求（示例价格不是任何供应商报价）：

```json
{
  "instruction": "为咖啡品牌设计两种海报，暖色、无文字",
  "pool": "image-demo",
  "agent_binding": {"id": "designer", "version": "1"},
  "image_binding": {"id": "renderer", "version": "1"},
  "cost_limit_micro": 3000000,
  "timeout_seconds": 600,
  "policy": {"max_parallel_children": 2, "max_children": 8,
             "failure_policy": "continue_independent", "allow_partial": true}
}
```

随后向 `/{root_id}/plans` 提交：

```json
{
  "expected_version": 1,
  "children": [
    {"child_key":"warm","instruction":"暖橙色，简洁构图","context_version":1,"budget_limit_micro":1000000},
    {"child_key":"earth","instruction":"大地色，手绘风格","context_version":1,"budget_limit_micro":1000000},
    {"child_key":"select","kind":"selection","depends_on":["warm","earth"],
     "instruction":"根据品牌一致性挑选较佳方案","context_version":1,"budget_limit_micro":1000000}
  ]
}
```

selection 接收实际图片，只允许返回其依赖输出 ID。编辑任务先上传并 commit 参考图，再新建
上下文版本并在计划中引用该版本；更新上下文后计划 expected_version 也须更新。

## 部署

在仓库根目录安装控制面依赖，沿用 PostgreSQL 与版本化私有 S3 配置：

```sh
pip install -r control-plane/requirements.txt
PYTHONPATH=control-plane:. python -m infra_control migrate
# 设置下表环境变量后启动
PYTHONPATH=control-plane:. python -m infra_control serve --host 0.0.0.0 --port 8080
```

| 环境变量 | 配置 |
|---|---|
| `AGENT_DATABASE_URL` | PostgreSQL DSN；SQLite 仅测试 |
| `AGENT_ENABLE_V3` | `1` 开启图片 API 与扫描器 |
| `AGENT_V3_KEY` | 独立随机 32 字节的 URL-safe base64 主密钥；重启必须保持一致 |
| `AGENT_USERS_JSON` | 原有用户配置，v3 额外要求 issued_at/expires_at 为 Unix 秒 |
| `AGENT_IMAGE_WORKERS_JSON` | 数组，每项 id、pools、slots、token_sha256、issued_at、expires_at、revoked |
| `AGENT_S3_BUCKET`、`AGENT_S3_ENDPOINT` | 私有版本化桶及 endpoint；AWS 身份通过部署凭据提供 |

用户与 Worker token 至少 24 字符，服务端只配置 SHA256。TLS 在入口终止，禁止记录请求体、
Authorization 或 presigned URL。S3 的 Worker 可达 URL 必须 HTTPS，PUT 返回 x-amz-version-id。
S3 暂存对象及旧版本应配置生命周期；final 图片的保留/删除需求尚需部署方明确并实现。

Worker 使用独立部署，不与 v2 Worker 复用同一槽位计数或状态目录：

```sh
cd runtime/pi-agent
npm ci --ignore-scripts
# 通过部署环境设置：AGENT_CONTROL_URL=https://control.example
# AGENT_IMAGE_WORKER_TOKEN、AGENT_IMAGE_SLOTS=2、AGENT_IMAGE_STATE=/persistent/pi-image
node worker.mjs
```

也可从仓库根构建 `docker build -f runtime/pi-agent/Dockerfile -t agent-pi-image .`。
为 `/state` 挂载可写持久卷、进程使用非 root，其余根文件系统建议只读。worker.lock 防止两个
进程使用同一目录；SIGKILL 后确认旧进程已停止，才可人工删除遗留锁。WAL 不保存 SK，但会保存
任务简报和引用，应按用户内容保护。不要同时使用同一 Worker 身份运行两个独立部署。

管理员通过 `/model-bindings/{id}` 发布 agent/image 绑定；所需字段见 OpenAPI 的 Binding。
`base_url` 必须为运营方审核的 HTTPS 地址，不允许重定向；model/secret 不接受用户覆盖。
agent 的 input_micro_per_million/output_micro_per_million 须按目标接口计费填写，prompt_tokens
包括缓存量并按输入费率结算；不同缓存价格的供应商须另做适配。图片 reserve_micro 既是预占
也是该适配器的固定每张结算价，只能绑定确实按张固定计价且参数价格一致的供应商配置。
不要使用零费率默认值作为真实模型配置。相同价格版本不得后改。

Worker 含原始供应商 SK，必须放在可信节点。部署网络应只允许控制面、S3 与已审核供应商端点；
本增量未把 Pi Worker 接入 v2 代码沙箱的宿主网络验证，不宣称已有相同的内核隔离保障。

## 验证与开放门槛

```sh
PYTHONPATH=control-plane:. python -m unittest discover -s tests/acceptance -p 'test_*.py' -v
PYTHONPATH=control-plane:. python -m contracts.export_v3
cd runtime/pi-agent && npm ci --ignore-scripts && npm test
```

测试使用真实 Pi SDK 和模拟供应商响应，覆盖并发会话隔离、唯一工具、禁重试、故障恢复、
预算/配额、租约、CAS、授权及版本化图片校验。CI 用 PostgreSQL 17 跑同一状态测试。
尚未调用真实付费模型、真实图片供应商或真实 S3；生产开放前必须完成目标接口参数、usage、
固定价格、TLS、S3 PUT 版本头、真实取消/超时/对账及部署网络的联调。异步 yield/resume、完整
会话恢复、自动费用对账与专用图片 UI 均不属于首版可用功能。
