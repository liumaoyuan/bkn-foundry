# Copyright openbkn.ai
#
# Licensed under the OpenBKN License. See LICENSE-OPENBKN.txt in the project root.

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from uuid import uuid4

from fastapi import Request

from app.database import (
    SessionLocal,
    get_db,
    init_database,
    invalidate_space_cache,
    initialize_space_database,
    session_for_space,
)
from app.models import DataSpace, Dataset
from app.schemas import DataSpaceCreate
from app.space_api import create_data_space


class DataSpaceRoutingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        init_database()

    def test_space_routes_data_to_an_independent_database(self) -> None:
        suffix = uuid4().hex[:8]
        with tempfile.TemporaryDirectory() as temporary:
            database_path = Path(temporary) / "space.db"
            space = create_data_space(
                DataSpaceCreate(
                    name=f"海洋数据 {suffix}",
                    slug=f"marine-{suffix}",
                    database_url=f"sqlite+pysqlite:///{database_path.as_posix()}",
                )
            )
            target = session_for_space(space.slug)
            dataset = Dataset(
                name=f"Space dataset {suffix}",
                slug=f"space-dataset-{suffix}",
                description="Independent database routing test",
                data_type="tabular",
                target_table="space_dataset",
            )
            try:
                target.add(dataset)
                target.commit()
                target.refresh(dataset)

                control = SessionLocal()
                try:
                    self.assertIsNone(control.get(Dataset, dataset.id))
                finally:
                    control.close()

                request = Request(
                    {
                        "type": "http",
                        "method": "GET",
                        "path": "/api/datasets",
                        "headers": [
                            (
                                b"x-data-ingestion-space",
                                space.slug.encode("ascii"),
                            )
                        ],
                        "query_string": b"",
                        "scheme": "http",
                        "server": ("testserver", 80),
                        "client": ("testclient", 50000),
                    }
                )
                dependency = get_db(request)
                routed = next(dependency)
                try:
                    self.assertIsNotNone(routed.get(Dataset, dataset.id))
                    self.assertEqual(request.state.data_space_id, space.id)
                finally:
                    dependency.close()
            finally:
                target.close()
                configured_engine = initialize_space_database(space)
                configured_engine.dispose()
                invalidate_space_cache(space.id)
                control = SessionLocal()
                try:
                    stored = control.get(DataSpace, space.id)
                    if stored is not None:
                        control.delete(stored)
                        control.commit()
                finally:
                    control.close()


if __name__ == "__main__":
    unittest.main()
