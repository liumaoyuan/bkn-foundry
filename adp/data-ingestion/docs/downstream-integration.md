# 数据入库下游集成与本体抽取接入

本文档面向基于入库数据开发本体抽取、实体识别、关系抽取、知识构建或其他
下游能力的团队。

## 1. 职责边界

数据入库负责：

- 接入文件、目录、FTP/SFTP、数据库和 HTTP API。
- 执行可追踪的分阶段清洗管线。
- 将规范化结果写入 `dataset_records.payload`。
- 保存来源、运行记录、阶段日志和原始资产。
- 提供稳定的 REST API、MCP 工具和增量读取游标。

下游功能负责：

- 定义业务本体、实体类型、关系类型和抽取规则。
- 读取入库规范记录或原始资产。
- 运行本体抽取任务并保存候选、置信度和结果。
- 通过知识网络/本体公共 API 写入最终模型或实例。

数据入库不负责决定业务本体的语义。`payload` 是规范化后的结构化数据契约，
下游仍需定义什么字段代表实体、关系和属性。

## 2. 推荐调用链

```text
入库任务完成
  -> dataset_records 新增规范记录
  -> 下游按游标增量读取
  -> 本体抽取服务生成候选
  -> 用户审批或自动发布
  -> 知识网络/本体 API
```

下游服务不得直接读取 `data_ingestion` 数据库表，也不得依赖数据库内部字段。
所有集成都通过公共 API 或 MCP 完成。

## 3. 稳定标识

| 标识 | 作用 | 生命周期 |
|---|---|---|
| `dataset_id` | 目标数据集 | 数据集创建后稳定 |
| `run_id` | 一次入库执行 | 每次入库或重试生成新值 |
| `record_id` | 一条规范记录 | 创建后稳定 |
| `fingerprint` | 去重指纹 | 同一数据集内稳定 |
| `payload` | 规范化业务数据 | 当前记录版本 |
| `asset_path` | 原始资产引用 | 跟随入库任务 |

下游应持久化自己的处理状态，例如：

```text
source_dataset_id
source_run_id
source_record_id
source_fingerprint
processing_cursor
extractor_version
ontology_version
processed_at
```

不要只保存 `asset_path`。资产路径属于存储实现细节，原始文件应通过 API 获取。

## 4. REST API

### 4.1 外部网关

Studio 和其他通过网关访问的产品使用：

```text
/api/data-ingestion
```

| 方法 | 路径 | 用途 |
|---|---|---|
| `GET` | `/api/data-ingestion/spaces` | 列出可访问的数据空间 |
| `GET` | `/api/data-ingestion/datasets` | 搜索数据集 |
| `GET` | `/api/data-ingestion/datasets/{dataset_id}` | 获取数据集详情 |
| `GET` | `/api/data-ingestion/datasets/{dataset_id}/schema` | 读取抽样字段结构 |
| `GET` | `/api/data-ingestion/datasets/{dataset_id}/records` | 分页读取记录 |
| `GET` | `/api/data-ingestion/datasets/{dataset_id}/records/changes` | 按游标增量读取 |
| `GET` | `/api/data-ingestion/runs` | 查询入库任务 |
| `GET` | `/api/data-ingestion/runs/{run_id}` | 查看入库阶段和结果 |
| `GET` | `/api/data-ingestion/runs/{run_id}/asset` | 下载原始资产 |

领域数据位于独立数据库中。除数据空间列表外，请求必须选择数据空间：

```http
X-Data-Ingestion-Space: marine
```

可以使用数据空间 ID 或 `slug`。未传该 Header 时使用 `default`，仅用于兼容
旧客户端。数据空间管理方式见 [data-spaces.md](data-spaces.md)。

### 4.2 服务内部

同一集群中的服务可以直接调用：

```text
http://data-ingestion:8000/api
```

内部调用必须带服务令牌：

```http
x-data-ingestion-internal-token: <internal-service-token>
```

服务令牌只能注入后端进程，不能发送给浏览器。

## 5. 鉴权模式

### 5.1 用户触发

前端或同步请求透传当前用户 Bearer Token：

```http
Authorization: Bearer <openbkn-access-token>
```

读取接口要求 `data_ingestion:view` 权限；创建、修改、删除和任务管理分别使用
对应的数据入库权限。

### 5.2 后台任务

定时抽取和后台工作者使用内部服务令牌。任务记录必须同时保存发起用户
`owner_id`，用于审计、权限复核和结果归属。

## 6. 字段结构

```http
GET /api/data-ingestion/datasets/{dataset_id}/schema?sample_size=100
```

示例响应：

```json
{
  "schema_version": "inferred-v1",
  "sampled_records": 100,
  "total_records": 12000,
  "fields": [
    {
      "name": "station_id",
      "data_type": "string",
      "nullable": false
    },
    {
      "name": "observed_at",
      "data_type": "string",
      "nullable": false
    }
  ]
}
```

该接口基于有界样本推断字段，适合页面映射和抽取配置。它不是数据库约束，也不
替代业务本体定义。下游应保存本次使用的 `schema_version` 和样本时间。

## 7. 增量读取

推荐使用游标接口：

```http
GET /api/data-ingestion/datasets/{dataset_id}/records/changes?limit=200
```

首次调用不传 `cursor`：

```json
{
  "items": [
    {
      "id": "record-id",
      "dataset_id": "dataset-id",
      "run_id": "run-id",
      "data_type": "tabular",
      "payload": {
        "station_id": "123",
        "observed_at": "2026-11-22T10:00:00Z"
      },
      "asset_path": "s3://data-ingestion/...",
      "created_at": "2026-11-22T10:00:03Z"
    }
  ],
  "next_cursor": "eyJ2IjoxLCJkYXRhc2V0X2lkIjoiLi4uIn0",
  "has_more": true
}
```

后续请求：

```http
GET /api/data-ingestion/datasets/{dataset_id}/records/changes?cursor=...&limit=200
```

规则：

- 游标不透明，不要解析或自行生成。
- 游标绑定 `dataset_id`，不能跨数据集使用。
- 按 `created_at, id` 稳定升序处理。
- `has_more=true` 时继续使用 `next_cursor`。
- 分页结束且没有新数据时，返回空 `items` 和原游标。
- 下游必须先提交本次业务结果，再持久化新的游标。

现有分页接口继续保留，适合页面浏览：

```http
GET /api/data-ingestion/datasets/{dataset_id}/records?page=1&page_size=200&q=关键词
```

## 8. 触发方式

目前提供两种触发方式。

### 8.1 用户手动触发

用户在入库数据集详情或本体抽取页面选择：

```json
{
  "dataset_id": "dataset-id",
  "run_id": "optional-run-id",
  "owner_id": "current-user-id"
}
```

### 8.2 下游定时轮询

下游可以定时调用 `records/changes`。如果需要仅在成功入库后处理，可以查询：

```http
GET /api/data-ingestion/runs?dataset_id={dataset_id}&status=success&limit=100
```

下游保存最后处理的 `run_id`，发现新成功任务后读取该任务产生的记录。

当前版本没有入库完成事件推送。后续如需毫秒级自动触发，应增加稳定事件契约，
例如 `ingestion.run.completed`，不要在下游轮询数据库表。

## 9. TypeScript 前端示例

```ts
import { http } from "@/framework/request/http";

export async function listDatasetChanges(
  datasetId: string,
  cursor?: string,
) {
  return http
    .get<{
      items: Array<{
        id: string;
        dataset_id: string;
        run_id: string;
        data_type: string;
        payload: Record<string, unknown>;
        created_at: string;
      }>;
      next_cursor: string | null;
      has_more: boolean;
    }>(`/data-ingestion/datasets/${datasetId}/records/changes`, {
      params: { cursor: cursor || undefined, limit: 200 },
      headers: { "X-Data-Ingestion-Space": "marine" },
    })
    .then((response) => response.data);
}
```

前端不得直接请求 `data-ingestion:8000`。

## 10. Python 后台服务示例

```python
import httpx


def read_changes(
    dataset_id: str,
    *,
    cursor: str | None,
    internal_token: str,
) -> dict:
    response = httpx.get(
        f"http://data-ingestion:8000/api/datasets/{dataset_id}/records/changes",
        params={"cursor": cursor, "limit": 200},
        headers={
            "x-data-ingestion-internal-token": internal_token,
            "x-data-ingestion-space": "marine",
        },
        timeout=60,
    )
    response.raise_for_status()
    return response.json()
```

生产代码还需处理重试、超时、幂等写入和令牌轮换。

## 11. MCP 与 Agent

本体抽取 Agent 可直接使用：

| 工具调用标识 | 用途 |
|---|---|
| `mcp__marine_ingestion__list_datasets` | 查找数据集 |
| `mcp__marine_ingestion__get_dataset_schema` | 读取字段结构 |
| `mcp__marine_ingestion__get_dataset_record_changes` | 游标增量读取 |
| `mcp__marine_ingestion__query_dataset_records` | 交互式分页查询 |
| `mcp__marine_ingestion__get_ingestion_run` | 核验入库任务 |

Agent 读取工具均为只读。创建本体、发布模型或写入实例的权限由本体抽取功能
自己的权限点控制。

## 12. 新增平台功能

建议建立独立模块：

```text
src/modules/ontology-extraction/
  components/
  contracts/
  locales/
  pages/
  scenes/
  services/
  types/
  index.ts
  module.manifest.ts
  navigation.tsx
  routes.tsx
```

需要注册：

- `src/app/router/module-routes.ts`
- `src/app/shell/console-navigation.tsx`

建议页面入口：

1. 数据资源知识网络中的“本体抽取”。
2. 入库数据集详情中的“开始本体抽取”。
3. 平台任务管理中的“本体抽取任务”。

建议权限点：

```text
ontology_extraction:view
ontology_extraction:create
ontology_extraction:modify
ontology_extraction:delete
ontology_extraction:task_manage
```

后端应拥有自己的任务和结果表，通过公共 API 读取入库数据，并通过知识网络/
本体公共 API 写入最终结果，不能跨库直接写入 `data_ingestion`、`vega` 或 BKN
内部表。

## 13. 幂等与数据质量

- 使用 `record_id` 作为单条记录幂等键。
- 使用 `dataset_id + fingerprint` 识别重复入库。
- 抽取结果保存来源 `record_id`、`run_id` 和抽取器版本。
- 同一记录重复处理时使用 upsert，避免重复实体和关系。
- 对混合类型字段标记 `data_type=mixed`，要求人工配置映射。
- Schema 样本不能代表全量时，增加抽样规模或执行全量字段扫描。

## 14. 验收清单

- [ ] 能通过 API 找到目标数据集。
- [ ] 能读取数据结构并保存 `schema_version`。
- [ ] 初次全量读取后能保存游标。
- [ ] 新增入库记录后，下一次游标读取只返回新增记录。
- [ ] 跨数据集游标被拒绝。
- [ ] 用户触发与后台任务分别使用用户令牌和内部令牌。
- [ ] 下游任务保存 owner、来源记录、抽取器版本和本体版本。
- [ ] 重复消费不会产生重复本体或关系。
- [ ] 不直接连接数据入库、Vega 或 BKN 内部数据库。
