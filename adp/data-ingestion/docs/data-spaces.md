# 独立数据库数据空间

## 1. 目标

多源数据入库支持按业务领域建立不同数据空间。每个数据空间连接一个独立的
MySQL/MariaDB 数据库，避免不同领域的数据表、任务和权限混在一个库里。

```text
控制数据库：data_ingestion
├── 数据空间注册表
└── 默认数据空间（兼容原有数据）

海洋数据空间：data_ingestion_marine
├── 数据源
├── 数据集
├── 入库任务
├── 规范记录
└── 自动扫描

气象数据空间：data_ingestion_weather
├── 数据源
├── 数据集
├── 入库任务
├── 规范记录
└── 自动扫描
```

原有 `data_ingestion` 数据库作为默认空间继续使用，已有数据不迁移、不删除。

## 2. 两类数据库

### 控制数据库

`DATABASE_URL` 指向控制数据库，保存：

- 数据空间名称和标识。
- 加密后的目标数据库连接地址。
- 目标数据库的脱敏显示名称。
- 数据空间健康状态。

控制数据库默认也是第一个数据空间。

### 领域数据库

每个数据空间有独立数据库：

- `data_sources`
- `datasets`
- `pipeline_templates`
- `auto_scan_jobs`
- `ingestion_runs`
- `stage_runs`
- `dataset_records`
- `data_assets`
- `agent_sessions`
- `agent_messages`
- `agent_approvals`
- `agent_memories`

系统首次连接时会自动创建这些表，并初始化三条标准入库管线。

## 3. 创建数据空间

Studio 页面右上角可选择“当前数据空间”，具备
`data_ingestion:task_manage` 权限的用户可以点击“新建数据空间”。

需要填写：

```text
空间名称：海洋观测
空间标识：marine
数据库连接：mysql+pymysql://user:password@mysql:3306/data_ingestion_marine?charset=utf8mb4
```

目标数据库必须提前创建，数据库账号至少需要：

```sql
CREATE TABLE
ALTER TABLE
CREATE INDEX
SELECT
INSERT
UPDATE
DELETE
```

数据库连接地址使用 `DATA_SOURCE_SECRET_KEY` 加密保存，查询接口只返回脱敏后的
主机、端口和数据库名，不会再次返回密码。

## 4. API

### 4.1 列出数据空间

```http
GET /api/data-ingestion/spaces
```

示例响应：

```json
[
  {
    "id": "default",
    "name": "默认数据空间",
    "slug": "default",
    "database_label": "mysql://mariadb:3306/data_ingestion",
    "is_default": true,
    "status": "ready",
    "last_error": ""
  },
  {
    "id": "abc123",
    "name": "海洋观测",
    "slug": "marine",
    "database_label": "mysql://mysql:3306/data_ingestion_marine",
    "is_default": false,
    "status": "ready",
    "last_error": ""
  }
]
```

### 4.2 创建数据空间

```http
POST /api/data-ingestion/spaces
Content-Type: application/json

{
  "name": "海洋观测",
  "slug": "marine",
  "database_url": "mysql+pymysql://user:password@mysql:3306/data_ingestion_marine?charset=utf8mb4"
}
```

### 4.3 测试数据库

```http
POST /api/data-ingestion/spaces/{space_id}/test
```

## 5. 后续请求如何选择数据库

除数据空间接口外，所有数据入库请求必须带：

```http
X-Data-Ingestion-Space: marine
```

也支持数据空间 ID。示例：

```http
GET /api/data-ingestion/datasets
X-Data-Ingestion-Space: marine
Authorization: Bearer <openbkn-access-token>
```

未提供该 Header 时使用 `default`，与旧客户端保持兼容。

以下接口都会路由到所选领域数据库：

- 数据源
- 数据集和数据目录
- 上传与入库任务
- 自动扫描
- 数据结构和增量读取
- Agent 会话、审批和记忆

## 6. 对象存储隔离

原始文件和中间产物仍使用本地文件或 MinIO/S3，但新数据空间增加目录隔离：

```text
本地：
data/storage/spaces/marine/raw/<run_id>/<filename>
data/storage/spaces/marine/artifacts/<run_id>/extracted.txt

MinIO/S3：
[S3_PREFIX]/spaces/marine/raw/<run_id>/<filename>
[S3_PREFIX]/spaces/marine/artifacts/<run_id>/extracted.txt
```

默认空间继续使用原路径，确保历史数据可读。

## 7. 自动扫描

调度器遍历所有可用数据空间，并从每个领域数据库读取自己的扫描计划。某个目标
数据库暂时不可用时，只跳过该空间，不阻塞其他空间的任务。

## 8. Agent 与 MCP

Agent 会话按当前数据空间保存在对应数据库中。内置 Agent 启动 MCP 子进程时会
传递：

```env
DATA_INGESTION_SPACE_ID=marine
```

外部 MCP/DSH 配置可设置同名环境变量，或者调用 REST API 时显式传递
`X-Data-Ingestion-Space`。

## 9. 下游本体抽取

下游服务先从 `/api/data-ingestion/spaces` 选择领域，然后使用对应 Header 读取：

```http
GET /api/data-ingestion/datasets/{dataset_id}/schema
X-Data-Ingestion-Space: marine
```

详细契约见 [downstream-integration.md](downstream-integration.md)。

## 10. 运维

- 控制数据库和目标数据库分别备份。
- `DATA_SOURCE_SECRET_KEY` 必须稳定保存；更换后旧连接地址无法解密。
- 删除数据空间前必须先停用扫描任务、Agent 会话和下游任务；当前版本不提供自动
  删除目标数据库。
- 目标数据库不可用时，接口返回 503，控制数据库和已成功的其他空间不受影响。
- 数据空间之间不提供跨库事务。涉及多个领域的事务必须由上层业务编排。

## 11. 当前限制

- 默认空间使用原 `DATABASE_URL`，已有数据不会自动迁入新领域数据库。
- 新建空间只初始化空结构和标准管线，不复制默认空间的示例数据集。
- 数据迁移需要单独制定停机窗口、校验和回滚方案。
- 当前应用权限仍沿用平台级 `data_ingestion` 权限。物理数据库已经隔离，但按单个数据
  空间授予用户或部门权限，需要后续在 bkn-safe 中注册空间资源并增加对象级授权。
