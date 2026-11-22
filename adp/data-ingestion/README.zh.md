# 多源数据入库

[中文](README.zh.md) | [English](README.md)

多源数据入库是 Foundry 的可选服务，为 OpenBKN Studio 提供文件接入、分阶段清洗、幂等写入、
自动扫描、内置 Agent 和 MCP 工具。

## 支持范围

- CSV、XLSX/XLSM，Excel 全部工作表
- PDF、DOCX
- 图片与视频资产
- 本地上传/目录、FTP/SFTP、MySQL/PostgreSQL、HTTP API 数据源配置

## 集成约束

- OpenBKN 网关路径为 `/api/data-ingestion`，服务内部继续使用原项目的 `/api` 路径。
- 数据库使用独立的 `data_ingestion` schema，不写 Vega 或 BKN 表。
- 原始文件和中间产物进入现有 MinIO/S3。
- 自动扫描首版依赖进程内调度器，因此生产副本数固定为 1。
- 所有公开请求先经过 Hydra introspection，再通过 bkn-safe 校验
  `data_ingestion:op` 权限。
- 入库 Agent 直接复用 **模型管理** 的默认大模型，并沿用现有模型权限与配额。
- MCP 服务只供平台内部使用；stdio MCP 供内置 Agent 调用。

下游本体抽取、实体识别和知识构建功能的接入方式见
[`docs/downstream-integration.md`](docs/downstream-integration.md)。

独立数据库数据空间的配置和路由方式见
[`docs/data-spaces.md`](docs/data-spaces.md)。

## 本地运行

```bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements-dev.txt
DATA_INGESTION_AUTH_ENABLED=false uvicorn app.main:app --reload --port 8000
make test
```
