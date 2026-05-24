"""
Pipeline Paramedic — Core Agent
Orchestrates the fix cycle: fetch logs → reason → fix → push → comment.
"""

import json
import logging
import os
import re
import textwrap
import time
from dataclasses import dataclass, field
from typing import Optional

from google import genai
from google.genai import types

from src.tools.gitlab_tools import GitLabTools
from src.tools.code_tools import CodeTools

logger = logging.getLogger(__name__)

GEMINI_MODEL = "gemini-3-flash-preview"
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")


@dataclass
class FixResult:
    success: bool
    diagnosis: str
    fix_description: str
    files_fixed: list = field(default_factory=list)
    commit_sha: Optional[str] = None
    mr_comment_posted: bool = False
    error: Optional[str] = None


SYSTEM_PROMPT = textwrap.dedent("""
    You are the Pipeline Paramedic — an autonomous agent that diagnoses and fixes
    broken CI/CD pipelines. Your job:

    1. Analyse the failing job log provided.
    2. Identify ALL files and errors reported (there may be more than one).
    3. For each file that needs a fix, produce a unified diff patch.
    4. Respond ONLY with valid JSON in this exact shape — no markdown fences, no extra text:

    {
      "root_cause": "short one-sentence description of the overall failure",
      "error_type": "one of: lint | syntax | test_failure | import_error | type_error | other",
      "confidence": "high | medium | low",
      "human_summary": "friendly 1-2 sentence summary to post as an MR comment",
      "fixes": [
        {
          "file_path": "relative/path/to/file.py",
          "fix_description": "one sentence describing the change",
          "fix_patch": "unified diff patch string, or null if unknown"
        }
      ]
    }

    Rules:
    - Include every file that has an error in the log — do not stop at the first one.
    - File paths must be relative with no leading ./ or /.
    - If you cannot determine a patch for a file, set fix_patch to null.
    - If overall confidence is low, set fixes to an empty array [].
""").strip()


class PipelineParamedic:
    def __init__(self):
        self.client = genai.Client(api_key=GEMINI_API_KEY)
        self.gitlab = GitLabTools()
        self.code = CodeTools()

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def handle_failure(self, webhook_payload: dict) -> dict:
        """End-to-end handler called by the webhook receiver."""
        ctx = self._extract_context(webhook_payload)
        logger.info(f"Handling failure: project={ctx['project_id']} job={ctx['job_id']} branch={ctx['branch']}")

        # Step 1 — Fetch artefacts from GitLab
        job_log = self.gitlab.get_job_log(ctx["project_id"], ctx["job_id"])
        commit_diff = self.gitlab.get_commit_diff(ctx["project_id"], ctx["commit_sha"])

        # Step 2 — Reason with Gemini
        diagnosis = self._diagnose(job_log, commit_diff, ctx)
        logger.info(f"Diagnosis: {diagnosis}")

        fixes = diagnosis.get("fixes", [])
        actionable = [f for f in fixes if f.get("fix_patch")]

        if diagnosis.get("confidence") == "low" or not actionable:
            comment = self._build_triage_comment(diagnosis)
            self.gitlab.post_mr_comment(ctx["project_id"], ctx["mr_iid"], comment)
            return FixResult(
                success=False,
                diagnosis=diagnosis.get("root_cause", "Unknown"),
                fix_description="Could not determine an automatic fix — triage comment posted.",
                mr_comment_posted=True,
            ).__dict__

        # Step 3 — Apply all fixes in a single commit
        commit_sha, files_fixed = self.code.apply_all_fixes(
            project_id=ctx["project_id"],
            branch=ctx["branch"],
            fixes=actionable,
            commit_message=f"fix(paramedic): {diagnosis.get('root_cause', 'fix CI failures')}",
        )

        # Step 4 — Post MR comment
        comment = self._build_fix_comment(diagnosis, commit_sha, files_fixed)
        self.gitlab.post_mr_comment(ctx["project_id"], ctx["mr_iid"], comment)

        # Step 5 — Trigger pipeline retry
        self.gitlab.retry_pipeline(ctx["project_id"], ctx["pipeline_id"])

        return FixResult(
            success=True,
            diagnosis=diagnosis["root_cause"],
            fix_description=diagnosis.get("human_summary", ""),
            files_fixed=files_fixed,
            commit_sha=commit_sha,
            mr_comment_posted=True,
        ).__dict__

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _extract_context(self, payload: dict) -> dict:
        """Normalise Pipeline Hook and Job Hook payloads into a flat context dict."""
        attrs = payload.get("object_attributes", {})
        project = payload.get("project", {})
        commit = payload.get("commit", {}) or attrs.get("commit", {})

        return {
            "project_id": project.get("id") or payload.get("project_id"),
            "pipeline_id": attrs.get("id") or payload.get("pipeline_id"),
            "job_id": self._find_failed_job_id(payload),
            "branch": attrs.get("ref") or payload.get("ref", "main"),
            "commit_sha": attrs.get("sha") or commit.get("id", ""),
            "mr_iid": self._find_mr_iid(payload),
        }

    def _find_failed_job_id(self, payload: dict) -> Optional[int]:
        """Return the ID of the first failed job in the pipeline."""
        for build in payload.get("builds", []):
            if build.get("status") == "failed":
                return build["id"]
        return payload.get("build_id") or payload.get("id")

    def _find_mr_iid(self, payload: dict) -> Optional[int]:
        """Extract MR IID from merge_request object if present."""
        mr = payload.get("merge_request") or {}
        return mr.get("iid")

    def _diagnose(self, job_log: str, commit_diff: str, ctx: dict) -> dict:
        """Send log + diff to Gemini and parse the structured response."""
        trimmed_log = "\n".join(job_log.splitlines()[-100:])

        prompt = textwrap.dedent(f"""
            ## Failing Job Log (last 100 lines)
            ```
            {trimmed_log}
            ```

            ## Commit Diff that triggered this pipeline
            ```diff
            {commit_diff[:2000]}
            ```

            ## Context
            - Branch: {ctx['branch']}
            - Commit SHA: {ctx['commit_sha']}

            Diagnose ALL failures and respond with JSON only.
        """).strip()

        for attempt in range(3):
            try:
                response = self.client.models.generate_content(
                    model=GEMINI_MODEL,
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        system_instruction=SYSTEM_PROMPT,
                    ),
                )
                break
            except Exception as e:
                if attempt == 2:
                    raise
                wait = 30 * (attempt + 1)
                logger.warning(f"Gemini rate limit hit, retrying in {wait}s (attempt {attempt + 1})")
                time.sleep(wait)

        raw = response.text.strip()
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)

        try:
            parsed = json.loads(raw)
            # Normalize file paths — GitLab API rejects leading ./ or /
            for fix in parsed.get("fixes", []):
                if fix.get("file_path"):
                    fix["file_path"] = fix["file_path"].lstrip("./").lstrip("/")
            return parsed
        except json.JSONDecodeError:
            logger.error(f"Gemini returned non-JSON: {raw[:500]}")
            return {
                "root_cause": "Could not parse Gemini response",
                "confidence": "low",
                "fixes": [],
                "human_summary": "The Pipeline Paramedic could not determine an automatic fix. Please review the job log manually.",
            }

    @staticmethod
    def _build_fix_comment(diagnosis: dict, commit_sha: str, files_fixed: list) -> str:
        files_list = "\n".join(f"- `{f}`" for f in files_fixed)
        summary = diagnosis.get("human_summary", "")
        return textwrap.dedent(f"""
            ## 🚑 Pipeline Paramedic — Auto-fix Applied

            **Root cause:** {diagnosis.get('root_cause', 'N/A')}

            {summary}

            **Files fixed ({len(files_fixed)}):**
            {files_list}

            Pushed commit `{commit_sha[:8]}` and re-triggered the pipeline.
            Please review the changes and let me know if anything looks wrong.

            > *Automated fix by [Pipeline Paramedic](https://github.com/your-username/pipeline-paramedic)*
        """).strip()

    @staticmethod
    def _build_triage_comment(diagnosis: dict) -> str:
        summary = diagnosis.get("human_summary", "")
        return textwrap.dedent(f"""
            ## 🚑 Pipeline Paramedic — Triage Report

            **Root cause:** {diagnosis.get('root_cause', 'Could not determine')}

            {summary}

            I was unable to apply an automatic fix with high confidence.
            Please review the job log and apply a manual fix.

            > *Automated triage by [Pipeline Paramedic](https://github.com/your-username/pipeline-paramedic)*
        """).strip()
