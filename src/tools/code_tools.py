"""
Code Tools
Clones the branch (via GitLab API — no local git needed in Cloud Run),
applies the fix patch, and commits back.
"""

import logging
import re
from typing import Optional

from src.tools.gitlab_tools import GitLabTools

logger = logging.getLogger(__name__)


class CodeTools:
    def __init__(self):
        self.gitlab = GitLabTools()

    def apply_fix(
        self,
        project_id: int,
        branch: str,
        file_path: str,
        patch: str,
        commit_message: str,
    ) -> str:
        """
        Fetch the current file content, apply the unified diff patch,
        commit the change back via the GitLab Commits API.
        Returns the new commit SHA.
        """
        # Get current content
        original = self.gitlab.get_file_content(project_id, file_path, ref=branch)

        # Apply the patch
        fixed = apply_unified_diff(original, patch)

        if fixed == original:
            logger.warning("Patch produced no changes — skipping commit")
            raise ValueError("Patch applied but file content unchanged; fix may already be present.")

        # Commit via GitLab API (no local git required)
        result = self.gitlab.create_commit(
            project_id=project_id,
            branch=branch,
            message=commit_message,
            actions=[{
                "action": "update",
                "file_path": file_path,
                "content": fixed,
            }],
        )

        sha = result.get("id", "unknown")
        logger.info(f"Fix committed: {sha[:8]} to {branch}:{file_path}")
        return sha


# ---------------------------------------------------------------------------
# Minimal unified-diff applier
# Handles the simple single-hunk patches Gemini typically generates.
# For complex multi-hunk patches, falls back to line-by-line replacement.
# ---------------------------------------------------------------------------

def apply_unified_diff(original: str, patch: str) -> str:
    """
    Apply a unified diff patch to `original` text.
    Supports single-hunk patches with standard +/- lines.
    """
    lines = original.splitlines(keepends=True)
    patch_lines = patch.splitlines()

    # Extract hunk header: @@ -start,count +start,count @@
    hunk_re = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")

    result_lines = list(lines)

    i = 0
    while i < len(patch_lines):
        m = hunk_re.match(patch_lines[i])
        if m:
            src_start = int(m.group(1)) - 1  # 0-indexed
            hunk_lines = patch_lines[i + 1:]
            result_lines, consumed = _apply_hunk(result_lines, src_start, hunk_lines)
            i += consumed + 1
        else:
            i += 1

    return "".join(result_lines)


def _apply_hunk(lines: list[str], src_start: int, hunk_lines: list[str]):
    """Apply a single diff hunk starting at `src_start` (0-indexed)."""
    result = list(lines[:src_start])
    src_pos = src_start
    consumed = 0

    for hl in hunk_lines:
        consumed += 1
        if hl.startswith("@@"):
            break  # Next hunk starts
        if hl.startswith("-"):
            src_pos += 1  # Skip this line (removed)
        elif hl.startswith("+"):
            result.append(hl[1:] if not hl[1:].endswith("\n") else hl[1:])
        else:
            # Context line — keep from source
            if src_pos < len(lines):
                result.append(lines[src_pos])
            src_pos += 1

    # Append remaining source lines
    result.extend(lines[src_pos:])
    return result, consumed
