"""
Code Tools
Applies fix patches and commits them back via the GitLab API — no local git needed.
"""

import logging
import re
from typing import Optional

from src.tools.gitlab_tools import GitLabTools

logger = logging.getLogger(__name__)


class CodeTools:
    def __init__(self):
        self.gitlab = GitLabTools()

    def apply_all_fixes(
        self,
        project_id: int,
        branch: str,
        fixes: list[dict],
        commit_message: str,
    ) -> tuple[str, list[str]]:
        """
        Apply patches for multiple files and commit them all in a single GitLab commit.
        Returns (commit_sha, list_of_fixed_file_paths).
        """
        actions = []
        files_fixed = []

        for fix in fixes:
            file_path = fix["file_path"]
            patch = fix["fix_patch"]

            try:
                original = self.gitlab.get_file_content(project_id, file_path, ref=branch)
                fixed = apply_unified_diff(original, patch)

                if fixed == original:
                    logger.warning(f"Patch for {file_path} produced no changes — skipping")
                    continue

                actions.append({
                    "action": "update",
                    "file_path": file_path,
                    "content": fixed,
                })
                files_fixed.append(file_path)
                logger.info(f"Patch applied for {file_path}")
            except Exception as exc:
                logger.warning(f"Could not apply patch for {file_path}: {exc}")

        if not actions:
            raise ValueError("All patches produced no changes; fixes may already be present.")

        result = self.gitlab.create_commit(
            project_id=project_id,
            branch=branch,
            message=commit_message,
            actions=actions,
        )

        sha = result.get("id", "unknown")
        logger.info(f"Committed {len(files_fixed)} file(s) as {sha[:8]} to {branch}")
        return sha, files_fixed

    def apply_fix(
        self,
        project_id: int,
        branch: str,
        file_path: str,
        patch: str,
        commit_message: str,
    ) -> str:
        """Single-file convenience wrapper around apply_all_fixes."""
        sha, _ = self.apply_all_fixes(
            project_id=project_id,
            branch=branch,
            fixes=[{"file_path": file_path, "fix_patch": patch}],
            commit_message=commit_message,
        )
        return sha


# ---------------------------------------------------------------------------
# Unified diff applier
# ---------------------------------------------------------------------------

def apply_unified_diff(original: str, patch: str) -> str:
    """Apply a unified diff patch to `original` text."""
    lines = original.splitlines(keepends=True)
    patch_lines = patch.splitlines()

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
        # Skip "\ No newline at end of file" diff markers — not actual file content
        if hl.startswith("\\"):
            continue
        if hl.startswith("-"):
            src_pos += 1  # Remove this source line
        elif hl.startswith("+"):
            content = hl[1:]
            if not content.endswith("\n"):
                content += "\n"
            result.append(content)
        else:
            # Context line — keep from source
            if src_pos < len(lines):
                result.append(lines[src_pos])
            src_pos += 1

    result.extend(lines[src_pos:])
    return result, consumed
