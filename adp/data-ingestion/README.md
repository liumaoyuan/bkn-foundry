# Data Ingestion

[中文](README.zh.md) | English

Data Ingestion is an optional Foundry service that provides multi-source file import, staged
cleaning, idempotent persistence, scheduled discovery, a built-in Agent, and an MCP surface for
OpenBKN Studio.

## Supported inputs

- CSV and XLSX/XLSM
- PDF and DOCX
- Images and video assets
- Local directory, FTP/SFTP, MySQL/PostgreSQL, and HTTP API source configuration

## Architecture

- Public API: `/api` on port `8000` behind the OpenBKN gateway prefix `/api/data-ingestion`
- Database: an external MySQL/MariaDB/PostgreSQL control database plus
  user-selected external databases for each data space; it never writes Vega or
  BKN tables
- Objects: existing OpenBKN MinIO/S3
- Scheduled scans: one replica because the first release uses an in-process scheduler
- Built-in Agent: the platform default large model from OpenBKN Model Management
- MCP: stdio for the built-in Agent and optional streamable HTTP for external clients

Downstream ontology extraction and other consumers should use the
[downstream integration contract](docs/downstream-integration.md). They must not read the
`data_ingestion` database directly.

Independent MySQL/MariaDB data-space routing is documented in
[data spaces](docs/data-spaces.md).

## Local development

```bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements-dev.txt
DATA_INGESTION_AUTH_ENABLED=false uvicorn app.main:app --reload --port 8000
make test
```

See [`README.zh.md`](README.zh.md) for the Chinese guide.
