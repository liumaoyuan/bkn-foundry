---
name: multisource-ingestion
description: Inspect and ingest CSV, Excel, PDF, DOCX, image, or video files into the multi-source ingestion platform through MCP, then verify stored records, pipeline stages, and storage locations.
---

# Multisource Ingestion

Use this skill when a user asks an Agent to preview, clean, ingest, or query files in the multi-source data platform. The Agent performs the work through the `marine_ingestion` MCP server. Follow user authorization boundaries before writing data.

## Required MCP tools

Use the server-qualified tool names:

- `mcp__marine_ingestion__api_health`
- `mcp__marine_ingestion__storage_info`
- `mcp__marine_ingestion__list_datasets`
- `mcp__marine_ingestion__list_pipeline_templates`
- `mcp__marine_ingestion__inspect_local_file`
- `mcp__marine_ingestion__ingest_local_file`
- `mcp__marine_ingestion__ingest_text`
- `mcp__marine_ingestion__get_ingestion_run`
- `mcp__marine_ingestion__query_dataset_records`
- `mcp__marine_ingestion__list_scan_jobs`
- `mcp__marine_ingestion__create_scan_job`
- `mcp__marine_ingestion__get_scan_job`
- `mcp__marine_ingestion__run_scan_job_now`

Read [references/mcp-tools.md](references/mcp-tools.md) when tool arguments, error handling, or result interpretation is unclear.

## Workflow

1. Call `api_health` and `storage_info` when the platform connection or data destination is uncertain.
2. Call `list_datasets` and `list_pipeline_templates` if the user has not supplied exact IDs.
3. Call `inspect_local_file` before every file ingestion. Report the detected type, Sheet/sample summary, warnings, and recommended pipeline.
4. Use an existing `dataset_id`. Do not create a dataset unless the user explicitly asks.
5. Start with `template_code="auto"` unless the user selects a pipeline or inspection shows a concrete override.
6. Call `ingest_local_file` for files, or `ingest_text` for small Agent-generated CSV, JSON, or Markdown content.
7. Verify the returned run status, inserted and skipped counts, and every stage result. Query records only when the user needs data confirmation or a sample.
8. When the user asks for recurring ingestion, create a scan job with an explicit interval, daily time, weekly days and time, or Cron expression and timezone.

## Ingestion rules

- A file path must be readable by the MCP process and inside its configured allowed roots.
- Treat `ingest_local_file`, `ingest_text`, and retries as writes. Run them only for a user-requested import or a task that clearly requires persistence.
- Do not retry a completed ingestion merely to make the UI look active. Re-ingesting identical content should return `inserted_count=0` and non-zero `duplicate_count`.
- CSV and Excel use `tabular_standard`; PDF and DOCX use `document_standard`; image and video use `media_standard`. Prefer the inspection result over hard-coded assumptions.
- Excel ingestion covers every worksheet. Verify the source Sheet from `__sheet_name` and `__sheet_index` when records mix multiple Sheets.
- For storage questions, distinguish database metadata from physical source files. `storage_info` reports the active SQLite/MySQL database and local-file or S3/MinIO destination.
- Scheduled scans require explicit user intent. Do not invent a recurring schedule, timezone, source, or target dataset. Report the returned `next_scan_at`.

## Result style

Return the run ID, final status, inserted count, duplicate count, target dataset, and any failed stage. Include a short sample or the relevant record query only when it supports the answer.
