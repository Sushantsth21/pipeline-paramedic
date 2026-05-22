"""Tests for Pipeline Paramedic core logic."""
import json
import pytest
from unittest.mock import MagicMock, patch

from src.tools.code_tools import apply_unified_diff


# ---------------------------------------------------------------------------
# Patch applier tests
# ---------------------------------------------------------------------------

class TestApplyUnifiedDiff:
    def test_adds_semicolon(self):
        original = "const x = 1\nconst y = 2\n"
        patch = (
            "@@ -1,2 +1,2 @@\n"
            "-const x = 1\n"
            "+const x = 1;\n"
            " const y = 2\n"
        )
        result = apply_unified_diff(original, patch)
        assert "const x = 1;" in result

    def test_removes_debug_line(self):
        original = "def foo():\n    print('debug')\n    return 42\n"
        patch = (
            "@@ -1,3 +1,2 @@\n"
            " def foo():\n"
            "-    print('debug')\n"
            "     return 42\n"
        )
        result = apply_unified_diff(original, patch)
        assert "print('debug')" not in result
        assert "return 42" in result

    def test_noop_patch_returns_original(self):
        original = "hello world\n"
        result = apply_unified_diff(original, "")
        assert result == original

    def test_adds_import(self):
        original = "import os\n\ndef main(): pass\n"
        patch = (
            "@@ -1,3 +1,4 @@\n"
            " import os\n"
            "+import sys\n"
            " \n"
            " def main(): pass\n"
        )
        result = apply_unified_diff(original, patch)
        assert "import sys" in result
        assert "import os" in result


# ---------------------------------------------------------------------------
# Webhook payload extraction tests
# ---------------------------------------------------------------------------

class TestPipelineParamedic:
    def _make_payload(self, status="failed", builds=None):
        return {
            "object_kind": "pipeline",
            "object_attributes": {
                "id": 999,
                "ref": "feature/fix-auth",
                "sha": "abc123def456",
                "status": status,
            },
            "project": {"id": 42, "name": "my-app"},
            "builds": builds or [
                {"id": 101, "name": "lint", "status": "failed"},
                {"id": 102, "name": "test", "status": "success"},
            ],
            "merge_request": {"iid": 7},
            "commit": {"id": "abc123def456", "message": "Add feature"},
        }

    @patch("src.agent.paramedic.GitLabTools")
    @patch("src.agent.paramedic.CodeTools")
    @patch("src.agent.paramedic.vertexai")
    @patch("src.agent.paramedic.GenerativeModel")
    def test_extracts_correct_context(self, mock_model_cls, mock_vai, mock_code, mock_gl):
        from src.agent.paramedic import PipelineParamedic
        agent = PipelineParamedic.__new__(PipelineParamedic)
        agent.gitlab = MagicMock()
        agent.code = MagicMock()
        agent.model = MagicMock()

        payload = self._make_payload()
        ctx = agent._extract_context(payload)

        assert ctx["project_id"] == 42
        assert ctx["pipeline_id"] == 999
        assert ctx["branch"] == "feature/fix-auth"
        assert ctx["commit_sha"] == "abc123def456"
        assert ctx["mr_iid"] == 7
        assert ctx["job_id"] == 101  # first failed build

    @patch("src.agent.paramedic.GitLabTools")
    @patch("src.agent.paramedic.CodeTools")
    @patch("src.agent.paramedic.vertexai")
    @patch("src.agent.paramedic.GenerativeModel")
    def test_low_confidence_posts_triage(self, mock_model_cls, mock_vai, mock_code, mock_gl):
        from src.agent.paramedic import PipelineParamedic
        agent = PipelineParamedic.__new__(PipelineParamedic)
        agent.gitlab = MagicMock()
        agent.code = MagicMock()
        agent.model = MagicMock()
        agent.gitlab.get_job_log.return_value = "some log"
        agent.gitlab.get_commit_diff.return_value = "some diff"
        agent.gitlab.get_open_mr_for_branch.return_value = {"iid": 7}

        low_conf = {
            "root_cause": "Unknown error",
            "confidence": "low",
            "fix_patch": None,
            "human_summary": "Could not determine fix.",
            "fix_description": "N/A",
        }
        agent._diagnose = MagicMock(return_value=low_conf)

        result = agent.handle_failure(self._make_payload())
        assert result["success"] is False
        agent.gitlab.post_mr_comment.assert_called_once()
        agent.code.apply_fix.assert_not_called()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
