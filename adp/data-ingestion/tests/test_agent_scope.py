# Copyright openbkn.ai
#
# Licensed under the OpenBKN License. See LICENSE-OPENBKN.txt in the project root.

import unittest

from app.database import SessionLocal, init_database
from app.models import AgentSession


class AgentScopeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        init_database()

    def test_sessions_persist_an_owner(self) -> None:
        db = SessionLocal()
        try:
            session = AgentSession(owner_id="alice", title="Scoped session")
            db.add(session)
            db.commit()
            db.refresh(session)
            self.assertEqual(session.owner_id, "alice")
        finally:
            db.close()
