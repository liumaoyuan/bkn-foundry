# Copyright openbkn.ai
#
# Licensed under the OpenBKN License. See LICENSE-OPENBKN.txt in the project root.

import unittest

from app.pipeline import extract_rows


class PipelineTest(unittest.TestCase):
    def test_extract_csv_preserves_rows(self) -> None:
        rows, metadata, text = extract_rows(
            "csv",
            b"name,value\nalice,1\nbob,2\n",
        )

        self.assertEqual(text, "")
        self.assertEqual(metadata, {"row_count": 2})
        self.assertEqual(
            rows,
            [{"name": "alice", "value": "1"}, {"name": "bob", "value": "2"}],
        )
