# MCP Tool Reference

## Connection assumptions

Use MCP server name `marine_ingestion`. The Agent sees tools as
`mcp__marine_ingestion__<tool>`.

The MCP adapter calls the ingestion REST API configured by `MCP_API_URL`. File
paths are resolved on the MCP process host and must remain below
`MCP_ALLOWED_ROOTS`.

## Tool arguments

### `inspect_local_file`

Arguments:

- `file_path`: absolute file path on the MCP host.

Returns the detected file type, media type, size, warnings, up to 20 preview
rows, multi-Sheet previews, and `recommended_pipeline`. This tool does not
write data.

### `ingest_local_file`

Arguments:

- `file_path`: absolute file path on the MCP host.
- `dataset_id`: target dataset identifier from `list_datasets`.
- `template_code`: `auto` by default; otherwise a template code.
- `source_id`: optional registered source identifier.
- `wait`: wait for completion by default.

Returns the completed ingestion run with `status`, counts, fingerprint, and
all stage results. Failures raised by the API are protocol errors.

### `ingest_text`

Arguments:

- `content`: UTF-8 text content, maximum 10 MB.
- `filename`: name with an extension that identifies content type.
- `dataset_id`: target dataset identifier.
- `template_code`: `auto` by default.
- `wait`: wait for completion by default.

Use this for small CSV, JSON, or Markdown content produced by the Agent. Prefer
`ingest_local_file` for existing or large files.

### `query_dataset_records`

Arguments:

- `dataset_id`
- `page`: starts at 1.
- `page_size`: 1 to 200.
- `query`: optional text search against the stored payload.

Returns `total`, pagination fields, and records. Structured files produce one
record per non-empty row. Documents and media produce one asset record.

## Error handling

- A `400` response means the file type and selected pipeline do not match.
- A `404` response means the dataset, pipeline, or run is absent.
- A `413` response means the upload exceeds `MAX_UPLOAD_BYTES`.
- A permission error before an API call means the file is outside
  `MCP_ALLOWED_ROOTS`.
- If the API is unavailable, report that the ingestion platform is unreachable
  rather than attempting to write database files directly.

## Storage interpretation

`storage_info` returns:

- `database_backend`: SQLite or MySQL for catalogs, runs, stages, records.
- `backend=local`: raw files under the reported `root`.
- `backend=s3`: raw files and artifacts in the reported bucket and prefix.

The database stores metadata and normalized record payloads. Large source files
remain in the file backend.

### `create_scan_job`

Creates a recurring scan for local directories, FTP/SFTP, or an incremental
database source. Supported schedule types:

- `interval`: repeat after `interval_seconds`.
- `daily`: execute at `schedule_time` every day.
- `weekly`: execute at `schedule_time` on selected `weekdays`, where Monday is 0.
- `cron`: execute using a five-field `cron_expression`.

`timezone` defaults to `Asia/Shanghai`. The response includes `next_scan_at`.
Creating a scan is a write and requires explicit user intent.

### `list_scan_jobs` and `get_scan_job`

Read scan plans, activation status, last and next execution, processed counts,
and the latest error. Use these before reporting whether automatic ingestion
is active.

### `run_scan_job_now`

Executes one existing scan immediately while keeping its next scheduled time.
This is a write and requires explicit user intent.
