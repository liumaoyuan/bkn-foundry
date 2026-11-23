# Copyright openbkn.ai
#
# Licensed under the OpenBKN License. See LICENSE-OPENBKN.txt in the project root.

import unittest
from pathlib import Path

from app.file_inspector import inspect_file


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class FileInspectorTest(unittest.TestCase):
    def test_inspects_csv_fixture(self) -> None:
        path = PROJECT_ROOT / "taiwan_marine_observations.csv"
        inspection = inspect_file(path.name, path.read_bytes())

        self.assertEqual(inspection["file_type"], "csv")
        self.assertEqual(inspection["recommended_pipeline"], "tabular_standard")
        self.assertEqual(inspection["preview_type"], "table")
        self.assertTrue(inspection["columns"])
        self.assertTrue(inspection["rows"])

    def test_inspects_all_excel_sheets(self) -> None:
        path = PROJECT_ROOT / "taiwan_marine_multi_sheet.xlsx"
        inspection = inspect_file(path.name, path.read_bytes())

        self.assertEqual(inspection["file_type"], "excel")
        self.assertEqual(inspection["recommended_pipeline"], "tabular_standard")
        self.assertEqual(
            inspection["metadata"]["sheet_count"],
            len(inspection["sheets"]),
        )
        self.assertGreater(len(inspection["sheets"]), 1)
        self.assertTrue(all(sheet["columns"] for sheet in inspection["sheets"]))

    def test_legacy_office_formats_are_rejected_with_conversion_guidance(self) -> None:
        with self.assertRaisesRegex(ValueError, r"\.xlsx"):
            inspect_file("legacy.xls", b"legacy")
        with self.assertRaisesRegex(ValueError, r"\.docx"):
            inspect_file("legacy.doc", b"legacy")
