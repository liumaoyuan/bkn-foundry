---
name: multisource-ingestion
description: 通过 MCP 预览并导入 CSV、Excel、PDF、DOCX、图片或视频，核验记录、管线阶段、存储位置，并管理用户自定义 MCP 工具、Skill 和数据管线。
---

# 多源数据入库

当用户要求预览、清洗、导入或查询数据时使用本 Skill。所有数据操作必须通过
`marine_ingestion` MCP 工具完成，并遵守用户的授权范围。

## 默认能力

以下能力属于系统默认能力，永久启用且不能关闭或删除：

- 内置入库 Agent
- 内置 MCP 工具
- 内置多源数据入库 Skill
- 内置结构化表格、文档、媒体三条管线

用户可以新增、编辑、启停和删除自己创建的 MCP 工具、Skill 与数据管线。

## 标准流程

1. 不确定平台或存储状态时，调用 `api_health` 和 `storage_info`。
2. 用户未提供准确 ID 时，调用 `list_datasets` 和 `list_pipeline_templates`。
3. 每次文件入库前调用 `inspect_local_file`，报告文件类型、Sheet、样例、警告和推荐管线。
4. 只能使用已存在的数据集，除非用户明确要求创建数据集。
5. 默认使用 `template_code="auto"`，用户指定管线时使用指定代码。
6. 文件调用 `ingest_local_file`，小型 Agent 生成内容调用 `ingest_text`。
7. 核验任务状态、插入数、重复数、错误数和每个阶段结果。
8. 周期性导入必须由用户明确提供周期、时间或 Cron 与目标数据集。

## 自定义能力

- 使用 `capability_catalog` 查看全部工具、Skill 和管线的详细信息。
- 使用 `create_mcp_tool` 将用户提供的 HTTP API 注册为 MCP 工具。
- 使用 `call_custom_mcp_tool` 调用用户工具，参数以 `arguments` 对象传入。
- 使用 `create_skill` 创建 Skill，使用 `update_skill` 编辑用户 Skill。
- 使用 `create_ingestion_pipeline` 组合内置或用户 MCP 工具形成新管线。
- 用户对象的代码必须以 `user-` 开头，不能覆盖内置工具、Skill 或管线。

## 安全与审批

- 入库、调用用户工具、创建/编辑能力都属于写入操作，必须等待用户批准。
- 不得猜测文件路径、数据集 ID、工具 ID、Skill ID 或管线代码。
- 文件路径必须在 `MCP_ALLOWED_ROOTS` 允许范围内。
- Skill 不能要求 Agent 绕过审批、直接写数据库或泄露密钥。
- 返回结果应包含任务 ID、状态、数量、目标数据集以及失败阶段。
