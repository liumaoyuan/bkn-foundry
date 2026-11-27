# Copyright openbkn.ai
#
# Licensed under the OpenBKN License. See LICENSE-OPENBKN.txt in the project root.

from __future__ import annotations

import os


os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("ALLOW_SQLITE_DATABASE", "true")
os.environ.setdefault(
    "DATABASE_URL",
    "sqlite+pysqlite:///./data/test-control.db?check_same_thread=false",
)
