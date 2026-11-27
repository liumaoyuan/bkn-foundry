# 多源数据入库功能对照

本文件记录原 `D:\VSCodeProjects\data` 项目迁移到 OpenBKN 后的功能对照。

## 后端

原项目的 19 个主 API、Agent 会话/审批/记忆 API 和 7 个自动扫描 API 均已保留。
原项目独立配置模型 API 的能力已改为复用 OpenBKN **模型管理** 的默认大模型，
因此对外 Agent API 为 10 个。以下核心文件
保持内容一致：

- `agent_catalog.py`
- `auto_scan.py`
- `auto_scan_api.py`
- `data_source_service.py`
- `file_inspector.py`
- `ingestion_service.py`
- `pipeline.py`
- `schemas.py`
- `secret_store.py`
- `seed.py`
- `storage.py`

`agent_api.py`、`agent_runtime.py`、`database.py`、`models.py` 增加了 OpenBKN 用户
隔离；`main.py`、`config.py`、`mcp_server.py` 增加了 Hydra、bkn-safe、内部令牌和
OpenBKN 部署适配。原文件解析、七阶段管线和幂等写入逻辑未删减。

## 前端

| 原页面/能力 | 当前实现 | 状态 |
|---|---|---|
| Dashboard | `overview` | 已合并 |
| Sources | `sources`，含连接配置、测试、存储落点 | 已合并 |
| Auto Scan | `auto-scan`，含创建、编辑、启停、立即执行、删除、间隔/每日/每周/Cron/时区、文件匹配和递归 | 已合并 |
| Catalog | `catalog`，含数据集搜索、创建、记录搜索和分页 | 已合并 |
| Ingestion | `import`，含上传、格式识别、多 Sheet 预览、模板选择、指纹去重和阶段进度 | 已合并 |
| Pipelines | `pipelines`，含三条模板及完整步骤参数 | 已合并 |
| Runs | `runs`，含搜索、状态筛选和任务列表 | 已合并 |
| Run Detail | `runs/:runId`，含阶段输入、输出、实现方式、参数、中间产物、日志和重试 | 已合并 |
| Agent Chat | `agent`，含多轮会话、审批、记忆和平台默认模型状态 | 已合并 |
| Agent Overview | Agent 顶部概览标签页 | 已合并 |
| MCP Tools | Agent 工具列表和 MCP 授权 | 已合并 |
| Agent Skill | Skill 内容展示与复制 | 已合并 |
| DSH 配置 | DSH 配置生成与复制 | 已合并 |
| 会话/记忆管理 | 新建、删除会话；添加、删除记忆 | 已合并 |

## 有意差异

- UI 从 Vue 3 + Element Plus 移植为 React + Ant Design，交互功能保留，视觉样式遵循
  OpenBKN。
- 原项目独立 MySQL/MinIO 改为 OpenBKN 已有 MariaDB `data_ingestion` schema 和 MinIO。
- API 对公网入口改为 `/api/data-ingestion`，服务内部仍保持原 `/api` 契约。
- Agent 会话按 OpenBKN 登录用户隔离。
- Agent 不再保存独立模型配置，统一使用 **模型管理** 的默认大模型，并沿用
  OpenBKN 的模型权限、配额和审计。
