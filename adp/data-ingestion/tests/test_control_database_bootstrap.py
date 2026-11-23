# Copyright openbkn.ai
#
# Licensed under the OpenBKN License. See LICENSE-OPENBKN.txt in the project root.

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import func, select

from app.control_database_bootstrap import (
    initialize_user_control_database,
    test_control_database,
)
from app.database import (
    SessionLocal,
    configure_control_database,
    control_database_status,
    control_database_url,
    init_database,
    list_data_spaces,
    reset_control_database,
)
from app.models import DataSource, Dataset, PipelineTemplate


class ControlDatabaseBootstrapTest(unittest.TestCase):
    def setUp(self) -> None:
        original = control_database_status()
        self.original_url = control_database_url()
        self.original_source = original.source
        reset_control_database()

    def tearDown(self) -> None:
        reset_control_database()
        if self.original_url:
            configure_control_database(
                self.original_url,
                source=self.original_source,
            )
            init_database()

    def test_sqlite_probe_reports_database_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database_path = Path(temporary) / "control.db"
            database_url = f"sqlite+pysqlite:///{database_path.as_posix()}"

            result = test_control_database(database_url)

            self.assertTrue(result["connected"])
            self.assertFalse(result["database_exists"])
            self.assertEqual(result["database_name"], database_path.as_posix())

    def test_user_initialization_creates_schema_and_encrypted_config(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database_path = root / "control.db"
            config_path = root / "control-database.json"
            database_url = (
                f"sqlite+pysqlite:///{database_path.as_posix()}"
                "?check_same_thread=false"
            )

            with patch(
                "app.database.CONTROL_DATABASE_CONFIG_PATH",
                config_path,
            ):
                initialize_user_control_database(
                    database_url,
                    create_database=True,
                )

            status = control_database_status()
            self.assertTrue(status.initialized)
            self.assertEqual(status.source, "user")
            self.assertTrue(database_path.is_file())
            self.assertGreaterEqual(len(list_data_spaces()), 1)
            db = SessionLocal()
            try:
                self.assertEqual(
                    db.scalar(select(func.count(Dataset.id))) or 0,
                    0,
                )
                self.assertEqual(
                    db.scalar(select(func.count(DataSource.id))) or 0,
                    0,
                )
                self.assertEqual(
                    db.scalar(select(func.count(PipelineTemplate.id))) or 0,
                    3,
                )
            finally:
                db.close()

            raw_config = config_path.read_text(encoding="utf-8")
            payload = json.loads(raw_config)
            self.assertNotIn(database_url, raw_config)
            self.assertIn("database_url_encrypted", payload)
            self.assertEqual(payload["version"], 1)
            reset_control_database()

    def test_user_can_switch_to_a_different_control_database(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first_path = root / "first.db"
            second_path = root / "second.db"
            config_path = root / "control-database.json"
            first_url = f"sqlite+pysqlite:///{first_path.as_posix()}"
            second_url = f"sqlite+pysqlite:///{second_path.as_posix()}"

            with patch(
                "app.database.CONTROL_DATABASE_CONFIG_PATH",
                config_path,
            ):
                initialize_user_control_database(first_url)
                initialize_user_control_database(second_url)

            self.assertEqual(control_database_url(), second_url)
            self.assertTrue(first_path.is_file())
            self.assertTrue(second_path.is_file())
            self.assertEqual(control_database_status().source, "user")
            reset_control_database()


if __name__ == "__main__":
    unittest.main()
