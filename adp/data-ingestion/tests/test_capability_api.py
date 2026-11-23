# Copyright openbkn.ai
#
# Licensed under the OpenBKN License. See LICENSE-OPENBKN.txt in the project root.

from __future__ import annotations

import unittest
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import select

from app.capability_api import (
    create_mcp_tool,
    create_pipeline,
    create_skill,
    delete_mcp_tool,
    delete_pipeline,
    delete_pipeline_execution,
    delete_skill,
    update_skill,
)
from app.database import SessionLocal, init_database
from app.models import (
    McpToolDefinition,
    PipelineExecution,
    PipelineTemplate,
    SkillDefinition,
)
from app.schemas import (
    McpToolCreate,
    PipelineTemplateCreate,
    SkillCreate,
    SkillUpdate,
)


class CapabilityApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        init_database()

    def setUp(self) -> None:
        self.db = SessionLocal()

    def tearDown(self) -> None:
        self.db.rollback()
        self.db.close()

    def test_custom_mcp_tool_crud(self) -> None:
        name = f"lookup_{uuid4().hex[:8]}"
        tool = create_mcp_tool(
            McpToolCreate(
                name=name,
                display_name="Lookup",
                description="Lookup data",
                config={
                    "url": "https://example.com/lookup",
                    "method": "POST",
                    "headers": {"Authorization": "Bearer mcp-secret"},
                },
            ),
            self.db,
        )
        self.assertFalse(tool["built_in"])
        self.assertEqual(tool["name"], name)
        stored = self.db.scalar(
            select(McpToolDefinition).where(McpToolDefinition.name == name)
        )
        self.assertIsNotNone(stored)
        self.assertNotIn("headers", tool["config"])
        self.assertNotIn("mcp-secret", str(tool))
        self.assertEqual(tool["config"]["header_names"], ["Authorization"])
        self.assertNotIn("headers", stored.config)
        self.assertIn("headers_encrypted", stored.config)
        delete_mcp_tool(tool["id"], self.db)
        self.assertIsNone(self.db.get(McpToolDefinition, tool["id"]))

    def test_custom_skill_crud(self) -> None:
        name = f"skill_{uuid4().hex[:8]}"
        skill = create_skill(
            SkillCreate(
                name=name,
                display_name="Skill",
                content="# 用户 Skill",
            ),
            self.db,
        )
        updated = update_skill(
            skill["id"],
            SkillUpdate(content="# 已更新"),
            self.db,
        )
        self.assertEqual(updated["content"], "# 已更新")
        delete_skill(skill["id"], self.db)
        self.assertIsNone(self.db.get(SkillDefinition, skill["id"]))

    def test_custom_pipeline_can_combine_builtin_tools(self) -> None:
        code = f"user-{uuid4().hex[:8]}"
        pipeline = create_pipeline(
            PipelineTemplateCreate(
                code=code,
                name="User pipeline",
                supported_types=["csv"],
                steps=[
                    {"tool_name": "list_datasets"},
                    {"tool_name": "inspect_local_file"},
                ],
            ),
            self.db,
        )
        self.assertFalse(pipeline["built_in"])
        self.assertEqual(pipeline["steps"][0]["name"], "数据集目录")
        self.assertEqual(pipeline["steps"][0]["kind"], "mcp_tool")
        delete_pipeline(pipeline["id"], self.db)
        self.assertIsNone(self.db.get(PipelineTemplate, pipeline["id"]))

    def test_builtin_pipeline_cannot_be_deleted(self) -> None:
        builtin = self.db.scalar(
            select(PipelineTemplate).where(PipelineTemplate.code == "tabular_standard")
        )
        if builtin is None:
            builtin = PipelineTemplate(
                code="tabular_standard",
                name="Built-in pipeline",
                description="test fixture",
                supported_types=["csv"],
                steps=[{"tool_name": "list_datasets"}],
                enabled=True,
            )
            self.db.add(builtin)
            self.db.commit()
            self.db.refresh(builtin)
        with self.assertRaises(HTTPException) as caught:
            delete_pipeline(builtin.id, self.db)
        self.assertEqual(caught.exception.status_code, 409)

    def test_running_pipeline_execution_cannot_be_deleted(self) -> None:
        execution = PipelineExecution(
            pipeline_code="user-test",
            pipeline_name="User pipeline",
            space_id="default",
            status="running",
        )
        self.db.add(execution)
        self.db.commit()
        self.db.refresh(execution)

        with self.assertRaises(HTTPException) as caught:
            delete_pipeline_execution(execution.id, self.db)
        self.assertEqual(caught.exception.status_code, 409)

        execution.status = "success"
        self.db.commit()
        delete_pipeline_execution(execution.id, self.db)
        self.assertIsNone(self.db.get(PipelineExecution, execution.id))


if __name__ == "__main__":
    unittest.main()
