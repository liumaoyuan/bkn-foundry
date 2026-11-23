from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parents[1]
PROJECT_DIR = BASE_DIR
load_dotenv(PROJECT_DIR / ".env")

DATA_DIR = Path(os.getenv("DATA_DIR", str(PROJECT_DIR / "data"))).resolve()
STORAGE_DIR = Path(os.getenv("STORAGE_DIR", str(DATA_DIR / "storage"))).resolve()
STORAGE_BACKEND = os.getenv("STORAGE_BACKEND", "local").strip().lower()
S3_ENDPOINT_URL = os.getenv("S3_ENDPOINT_URL", "").strip()
S3_BUCKET = os.getenv("S3_BUCKET", "ingestion-data")
S3_PREFIX = os.getenv("S3_PREFIX", "").strip().strip("/")
S3_ACCESS_KEY = os.getenv("S3_ACCESS_KEY", "").strip()
S3_SECRET_KEY = os.getenv("S3_SECRET_KEY", "").strip()
S3_REGION = os.getenv("S3_REGION", "us-east-1").strip()
S3_FORCE_PATH_STYLE = os.getenv("S3_FORCE_PATH_STYLE", "true").lower() in {
    "1",
    "true",
    "yes",
}
DATABASE_PATH = DATA_DIR / "app.db"
APP_ENV = os.getenv("APP_ENV", "development").strip().lower()
ENV_DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
DEFAULT_DATABASE_URL = f"sqlite+pysqlite:///{DATABASE_PATH.as_posix()}"
DATABASE_URL = ENV_DATABASE_URL or (
    DEFAULT_DATABASE_URL if APP_ENV != "production" else ""
)
CONTROL_DATABASE_CONFIG_PATH = Path(
    os.getenv(
        "CONTROL_DATABASE_CONFIG_PATH",
        str(DATA_DIR / "control-database.json"),
    )
).expanduser()
CONTROL_DATABASE_BOOTSTRAP_ENABLED = os.getenv(
    "CONTROL_DATABASE_BOOTSTRAP_ENABLED",
    "true",
).strip().lower() in {
    "1",
    "true",
    "yes",
}

if APP_ENV == "production" and ENV_DATABASE_URL.startswith("sqlite"):
    raise RuntimeError(
        "生产环境不允许使用 SQLite。请配置 MySQL/MariaDB 控制库，"
        "或启用前端控制库初始化。"
    )

DATA_DIR.mkdir(parents=True, exist_ok=True)
STORAGE_DIR.mkdir(parents=True, exist_ok=True)

APP_NAME = os.getenv("APP_NAME", "九格多源数据入库系统")
MAX_UPLOAD_BYTES = int(os.getenv("MAX_UPLOAD_BYTES", str(512 * 1024 * 1024)))
TRUSTED_PROXY_HEADERS = os.getenv("TRUSTED_PROXY_HEADERS", "false").lower() in {
    "1",
    "true",
    "yes",
}
AUTH_ENABLED = os.getenv("DATA_INGESTION_AUTH_ENABLED", "true").lower() in {
    "1",
    "true",
    "yes",
}
HYDRA_ADMIN_URL = os.getenv(
    "HYDRA_ADMIN_URL",
    "http://bkn-safe-hydra-admin:4445",
).rstrip("/")
BKN_SAFE_URL = os.getenv("BKN_SAFE_URL", "http://bkn-safe:3000").rstrip("/")
AUTHZ_RESOURCE_TYPE = os.getenv("DATA_INGESTION_AUTHZ_RESOURCE_TYPE", "data_ingestion")
AUTHZ_RESOURCE_ID = os.getenv("DATA_INGESTION_AUTHZ_RESOURCE_ID", "platform")
INTERNAL_SERVICE_TOKEN = os.getenv("DATA_INGESTION_INTERNAL_TOKEN", "").strip()
if APP_ENV == "production" and len(INTERNAL_SERVICE_TOKEN) < 24:
    raise RuntimeError(
        "生产环境必须设置至少 24 个字符的 DATA_INGESTION_INTERNAL_TOKEN。"
    )
CORS_ALLOW_ORIGINS = [
    item.strip()
    for item in os.getenv("CORS_ALLOW_ORIGINS", "").split(",")
    if item.strip()
]
MCP_API_URL = os.getenv("MCP_API_URL", "http://127.0.0.1:8000").rstrip("/")
MODEL_API_URL = os.getenv(
    "MODEL_API_URL",
    "http://mf-model-api:9898/api/mf-model-api/v1/chat/completions",
).rstrip("/")
MODEL_MANAGER_URL = os.getenv(
    "MODEL_MANAGER_URL",
    "http://mf-model-manager:9898",
).rstrip("/")
AGENT_MCP_URL = os.getenv(
    "AGENT_MCP_URL",
    "http://data-ingestion-mcp:8010/mcp",
).rstrip("/")
AGENT_SELF_API_URL = os.getenv(
    "AGENT_SELF_API_URL",
    "http://127.0.0.1:8000",
).rstrip("/")
DATA_SOURCE_SECRET_KEY = (
    os.getenv("DATA_SOURCE_SECRET_KEY", "").strip()
    or os.getenv("AGENT_SETTINGS_SECRET", "").strip()
    or "change-me-data-source-secret"
)
if APP_ENV == "production" and DATA_SOURCE_SECRET_KEY == "change-me-data-source-secret":
    raise RuntimeError(
        "生产环境必须设置独立的 DATA_SOURCE_SECRET_KEY，不能使用默认值。"
    )
AGENT_MAX_TOOL_ROUNDS = int(os.getenv("AGENT_MAX_TOOL_ROUNDS", "8"))
AGENT_HISTORY_LIMIT = int(os.getenv("AGENT_HISTORY_LIMIT", "60"))
AUTO_SCAN_ENABLED = os.getenv("AUTO_SCAN_ENABLED", "true").lower() in {
    "1",
    "true",
    "yes",
}
AUTO_SCAN_TICK_SECONDS = int(os.getenv("AUTO_SCAN_TICK_SECONDS", "5"))
AUTO_SCAN_MAX_FILES_PER_RUN = int(
    os.getenv("AUTO_SCAN_MAX_FILES_PER_RUN", "20"),
)
MCP_ALLOWED_ROOTS = os.getenv(
    "MCP_ALLOWED_ROOTS",
    str(PROJECT_DIR),
).split(os.pathsep)
MCP_AUTH_ENABLED = os.getenv("MCP_AUTH_ENABLED", "true").lower() in {
    "1",
    "true",
    "yes",
}
MCP_RESOURCE_URL = os.getenv(
    "MCP_RESOURCE_URL",
    "http://data-ingestion-mcp:8010",
).rstrip("/")
OAUTH_ISSUER_URL = os.getenv(
    "OAUTH_ISSUER_URL",
    "http://bkn-safe-hydra-public:4444",
).rstrip("/")
