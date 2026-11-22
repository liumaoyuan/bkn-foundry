# Agent 入库与 MCP 集成

## 三个层次的职责

MCP 服务负责执行，Agent Skill 负责决策和使用顺序，DSH 是加载 MCP 工具与
Skill 的 Agent 宿主。

```text
Agent / DSH
  -> Skill: 何时检查、如何选择数据集与管线、如何核验
  -> MCP: 标准工具调用与结构化结果
  -> Ingestion API: 任务、阶段、数据库写入
  -> MariaDB `data_ingestion` / OpenBKN MinIO
```

当前项目提供：

- MCP server：`adp/data-ingestion/app/mcp_server.py`
- 内置 Agent Runtime：`adp/data-ingestion/app/agent_runtime.py`
- Agent API：`adp/data-ingestion/app/agent_api.py`
- Agent Skill：`.agents/skills/multisource-ingestion/SKILL.md`
- DSH 配置示例：`docs/dsh-mcp.example.yml`

## 数据最终存到哪里

| 数据 | 默认本地开发 | Docker 部署 |
|---|---|---|
| 数据集、任务、阶段、规范记录、资产索引 | `data/app.db` SQLite | MariaDB `data_ingestion` schema |
| 原始 Excel、PDF、图片、视频等 | `data/storage/raw/<run_id>/` | MinIO `data-ingestion` Bucket |
| PDF/DOCX 提取文本等中间产物 | `data/storage/artifacts/<run_id>/` | MinIO `data-ingestion` Bucket |
| 原始文件访问 URI | `source_path` | `s3://data-ingestion/...` |

设置 `STORAGE_BACKEND=local` 使用本地文件；设置 `STORAGE_BACKEND=s3` 使用
S3 兼容对象存储。OpenBKN Docker Compose 复用已有 MinIO 并使用 S3。

`dataset_records.payload` 保存清洗后的结构化 JSON。原始大文件不写入数据库，
数据库只保存对象 URI、大小、MIME、SHA-256 和业务元数据。

生产环境可以按业务领域配置独立 MySQL/MariaDB 数据空间。控制数据库保存数据空间
注册信息，领域数据、任务、扫描和 Agent 会话保存在各自的领域数据库。接入方式见
[`data-spaces.md`](data-spaces.md)。

## 内置 Agent Runtime

Agent 工作台复用 OpenBKN 的 **模型管理**。用户在
`/model-resources/models?tab=llm` 注册大模型并设置为系统默认模型，
入库 Agent 不再单独保存 Base URL、模型名称或 API Key。

`GET /api/agent/status` 读取默认模型状态；执行时请求
`mf-model-api` 并透传当前用户的 OpenBKN Bearer Token。模型调用继续执行平台
的大模型 `execute` 权限与配额校验。没有默认模型时，前端会禁用发送并引导用户
进入模型管理。

数据源连接凭据使用 `DATA_SOURCE_SECRET_KEY` 派生 Fernet 密钥加密保存。

运行状态保存在以下表中：

| 表 | 内容 |
|---|---|
| `agent_sessions` | 按 OpenBKN 用户隔离的会话标题、运行状态和时间 |
| `agent_messages` | 用户、助手和工具消息 |
| `agent_approvals` | 写入工具的待审批调用和结果 |
| `agent_memories` | 会话长期记忆 |

一次 Agent 请求的状态流：

```text
user message
  -> model tool call
  -> read-only tool executes
  -> write tool creates approval
  -> awaiting_approval
  -> user approves/rejects
  -> tool result rejoins history
  -> model continues
  -> final response
```

这套流程提供多轮对话、人机回环和会话记忆。Agent 的 MCP 工具使用 stdio
子进程调用同一个 `marine_ingestion` MCP server，因此网页中的内置 Agent 与
DSH 中的外部 Agent 使用相同工具协议和执行逻辑。

### Agent API

```text
GET    /api/agent/status
GET    /api/agent/sessions
POST   /api/agent/sessions
GET    /api/agent/sessions/{session_id}
DELETE /api/agent/sessions/{session_id}
POST   /api/agent/sessions/{session_id}/messages
POST   /api/agent/sessions/{session_id}/approvals/{approval_id}/approve
POST   /api/agent/sessions/{session_id}/approvals/{approval_id}/reject
POST   /api/agent/sessions/{session_id}/memories
DELETE /api/agent/sessions/{session_id}/memories/{memory_id}
```

## MCP 工具

连接名为 `marine_ingestion` 后，DSH 和其他兼容 MCP 的宿主会看到：

```text
mcp__marine_ingestion__api_health
mcp__marine_ingestion__storage_info
mcp__marine_ingestion__list_datasets
mcp__marine_ingestion__list_pipeline_templates
mcp__marine_ingestion__inspect_local_file
mcp__marine_ingestion__ingest_local_file
mcp__marine_ingestion__ingest_text
mcp__marine_ingestion__get_ingestion_run
mcp__marine_ingestion__query_dataset_records
mcp__marine_ingestion__list_scan_jobs
mcp__marine_ingestion__create_scan_job
mcp__marine_ingestion__get_scan_job
mcp__marine_ingestion__run_scan_job_now
```

`inspect_local_file` 只预览。`ingest_local_file` 和 `ingest_text` 会创建任务并
写入数据库，属于需要用户明确意图的写操作。

Agent 的标准执行顺序：

1. `api_health`
2. `storage_info`
3. `list_datasets`
4. `list_pipeline_templates`
5. `inspect_local_file`
6. `ingest_local_file` 或 `ingest_text`
7. `get_ingestion_run`
8. 按需调用 `query_dataset_records`
9. 用户要求持续同步时调用 `create_scan_job`

文件必须位于 `MCP_ALLOWED_ROOTS`。默认只允许项目目录，避免 Agent 读取服务
主机上的任意文件。

## 定时扫描

定时扫描支持：

- 固定间隔
- 每天指定 HH:MM
- 每周指定星期和时间
- 五段 Cron 表达式
- 独立时区

扫描器支持本地目录、FTP、SFTP 和 MySQL/PostgreSQL 增量查询。调度器每 5
秒检查到期任务，使用文件 SHA-256 或数据库水位避免重复处理。`create_scan_job`
和 `run_scan_job_now` 属于写操作，在内置 Agent Runtime 中需要人工批准。

## DSH stdio 接入

先启动 REST API：

```powershell
cd D:\VSCodeProjects\openbkn\bkn-foundry-main\adp\data-ingestion
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

把 `docs/dsh-mcp.example.yml` 中的 stdio 条目合并到 DSH 对应 profile 的
Cordis composition。DSH 会启动 MCP 子进程、发现工具，并注册为：

```text
mcp__marine_ingestion__<tool_name>
```

项目 Skill 位于 `.agents/skills/multisource-ingestion/SKILL.md`，DSH 的项目
Skill provider 可以从工作区 `.agents/skills` 发现它。

## DSH Streamable HTTP 接入

Docker Compose 会同时启动 `data-ingestion-mcp` 服务：

```text
http://data-ingestion-mcp:8010/mcp
```

HTTP MCP 容器与入库 API 共享 `/data` 数据卷。Agent 传入的路径必须位于
`MCP_ALLOWED_ROOTS` 下：

```text
/data/<path-to-file>
```

小型 CSV、JSON、Markdown 可由 Agent 直接调用 `ingest_text`。

在 DSH profile 中使用：

```yaml
- id: mcp-marine-ingestion-http
  name: '@deepseek-ai/dsh-mcp-client'
  config:
    serverName: marine_ingestion
    transport: streamable-http
    url: 'http://127.0.0.1:8010/mcp'
    toolCallTimeoutMs: 300000
```

本地不启动 Docker 时，也可以手工启动 HTTP MCP：

```powershell
cd D:\VSCodeProjects\openbkn\bkn-foundry-main\adp\data-ingestion
$env:MCP_TRANSPORT='streamable-http'
$env:MCP_HOST='127.0.0.1'
$env:MCP_PORT='8010'
$env:MCP_API_URL='http://127.0.0.1:8000'
python -m app.mcp_server
```

## Agent 直接入库示例

```text
用户：把 D:\VSCodeProjects\openbkn\bkn-foundry-main\adp\data-ingestion\taiwan_marine_multi_sheet.xlsx
导入“台湾近海观测数据”，使用系统推荐的管线。
```

Agent 应：

1. 通过 `list_datasets` 找到数据集 ID。
2. 通过 `inspect_local_file` 确认三个 Sheet 和推荐的 `tabular_standard`。
3. 调用 `ingest_local_file`，传 `template_code="auto"`。
4. 检查返回的 `status`、`inserted_count`、`duplicate_count` 和七个阶段。
5. 需要确认时调用 `query_dataset_records`。

## 安全边界

- REST API 在生产环境通过 Hydra introspection 和 bkn-safe 校验用户与
  `data_ingestion` 操作权限；Agent 会话按 OpenBKN 用户隔离。
- 大模型调用透传当前用户令牌，由 `mf-model-api` 执行模型访问权限与配额校验。
- MCP 文件访问受 `MCP_ALLOWED_ROOTS` 限制。
- 内部 MCP 使用服务令牌；对外暴露时应增加 TLS，并限制 `ingest_*` 工具权限。
- S3/MinIO 密钥只通过环境变量注入，不写入 Skill 或 MCP 配置示例。
