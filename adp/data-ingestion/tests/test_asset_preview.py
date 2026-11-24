# Copyright openbkn.ai
#
# Licensed under the OpenBKN License. See LICENSE-OPENBKN.txt in the project root.

from __future__ import annotations

import io
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image

from app.main import preview_source_asset


class FakeDb:
    def __init__(self, run: SimpleNamespace) -> None:
        self.run = run

    def get(self, _model: object, _run_id: str) -> SimpleNamespace:
        return self.run


class AssetPreviewTest(unittest.TestCase):
    def test_tiff_image_is_converted_to_browser_preview(self) -> None:
        source = Image.new("RGB", (2200, 1200), "navy")
        encoded = io.BytesIO()
        source.save(encoded, format="TIFF")
        run = SimpleNamespace(
            file_type="image",
            media_type="image/tiff",
            original_name="sample.tif",
            source_path="raw/run-1/sample.tif",
        )

        with (
            patch("app.main.storage.exists", return_value=True),
            patch("app.main.storage.read_bytes", return_value=encoded.getvalue()),
        ):
            response = preview_source_asset("run-1", FakeDb(run))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.media_type, "image/jpeg")
        preview = Image.open(io.BytesIO(response.body))
        self.assertEqual(preview.format, "JPEG")
        self.assertLessEqual(max(preview.size), 1600)

    def test_non_image_asset_does_not_return_an_image_preview(self) -> None:
        run = SimpleNamespace(
            file_type="document",
            media_type="application/pdf",
            original_name="sample.pdf",
            source_path="raw/run-2/sample.pdf",
        )

        with self.assertRaises(Exception) as raised:
            preview_source_asset("run-2", FakeDb(run))

        self.assertEqual(raised.exception.status_code, 415)


if __name__ == "__main__":
    unittest.main()
