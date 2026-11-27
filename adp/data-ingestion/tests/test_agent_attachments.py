#
# Licensed under the OpenBKN License. See LICENSE-OPENBKN.txt in the project root.

from __future__ import annotations

import io
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from fastapi import UploadFile
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.agent_attachments import (
    cleanup_agent_attachments,
    store_agent_attachment,
)
from app.agent_runtime import (
    _history_messages,
    _requires_dataset_clarification,
    _user_names_dataset,
)
from app.models import AgentMessage, AgentSession, Base, Dataset


class AgentAttachmentTest(unittest.IsolatedAsyncioTestCase):
    async def test_uploaded_attachment_is_stored_inside_an_allowed_root(self) -> None:
        with TemporaryDirectory() as temporary:
            upload = UploadFile(
                file=io.BytesIO(b"name,amount\nbooks,20\n"),
                filename=r"C:\fakepath\sample.csv",
            )
            with patch("app.agent_attachments.MCP_ALLOWED_ROOTS", [temporary]):
                attachment = await store_agent_attachment(
                    owner_id="alice",
                    session_id="session-1",
                    upload=upload,
                )
                stored_path = Path(attachment["file_path"])

                self.assertEqual(attachment["filename"], "sample.csv")
                self.assertEqual(attachment["file_type"], "csv")
                self.assertTrue(stored_path.is_file())
                self.assertTrue(stored_path.is_relative_to(Path(temporary).resolve()))

                cleanup_agent_attachments([attachment])
                self.assertFalse(stored_path.exists())


class AgentHistoryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine)
        self.session = AgentSession(owner_id="alice", title="Attachment session")
        self.db.add(self.session)
        self.db.commit()

    def tearDown(self) -> None:
        self.db.close()
        self.engine.dispose()

    def test_history_exposes_attachment_path_to_the_model(self) -> None:
        self.db.add(
            AgentMessage(
                session_id=self.session.id,
                role="user",
                content="请入库",
                attachments=[
                    {
                        "id": "attachment-1",
                        "filename": "orders.csv",
                        "file_type": "csv",
                        "size": 24,
                        "file_path": "/data/agent-uploads/orders.csv",
                    }
                ],
            )
        )
        self.db.commit()

        history = _history_messages(self.db, self.session.id)

        self.assertIn("[用户上传附件]", history[0]["content"])
        self.assertIn("file_path=/data/agent-uploads/orders.csv", history[0]["content"])

    def test_dataset_mention_is_detected_before_requesting_clarification(self) -> None:
        self.db.add(
            Dataset(
                id="dataset-1",
                name="销售订单",
                slug="sales-orders",
                data_type="tabular",
            )
        )
        self.db.commit()

        self.assertTrue(_user_names_dataset(self.db, "请导入到销售订单"))
        self.assertTrue(_user_names_dataset(self.db, "请导入到 dataset-1"))
        self.assertFalse(_user_names_dataset(self.db, "请帮我导入这个文件"))
        self.assertTrue(
            _requires_dataset_clarification(
                self.db,
                "请帮我导入这个文件",
                [{"id": "attachment-1"}],
            )
        )
        self.assertFalse(
            _requires_dataset_clarification(
                self.db,
                "请导入到销售订单",
                [{"id": "attachment-1"}],
            )
        )


if __name__ == "__main__":
    unittest.main()
