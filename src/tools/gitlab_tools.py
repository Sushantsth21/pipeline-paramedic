"""
GitLab Tools — MCP Integration Layer
Wraps GitLab REST API v4. In production these calls go through the
GitLab MCP server; here we call the API directly for portability.
"""

import logging
import os
import time
from typing import Optional

import requests

logger = logging.getLogger(__name__)

GITLAB_URL = os.environ.get("GITLAB_URL", "https://gitlab.com")
GITLAB_TOKEN = os.environ.get("GITLAB_TOKEN", "")


class GitLabTools:
    def __init__(self):
        if not GITLAB_TOKEN:
            raise EnvironmentError("GITLAB_TOKEN environment variable is required.")
        self.session = requests.Session()
        self.session.headers.update({
            "PRIVATE-TOKEN": GITLAB_TOKEN,
            "Content-Type": "application/json",
        })
        self.base = f"{GITLAB_URL}/api/v4"

    # ------------------------------------------------------------------
    # Core MCP-mapped operations
    # ------------------------------------------------------------------

    def get_job_log(self, project_id: int, job_id: int) -> str:
        """Fetch the raw trace log for a specific CI job."""
        url = f"{self.base}/projects/{project_id}/jobs/{job_id}/trace"
        resp = self._get(url)
        return resp.text

    def get_commit_diff(self, project_id: int, commit_sha: str) -> str:
        """Return a unified diff string for the given commit."""
        url = f"{self.base}/projects/{project_id}/repository/commits/{commit_sha}/diff"
        diffs = self._get(url).json()
        parts = []
        for d in diffs:
            parts.append(f"--- a/{d['old_path']}\n+++ b/{d['new_path']}\n{d.get('diff', '')}")
        return "\n".join(parts)

    def get_file_content(self, project_id: int, file_path: str, ref: str = "main") -> str:
        """Fetch the raw content of a file at a given ref."""
        import base64
        url = f"{self.base}/projects/{project_id}/repository/files/{requests.utils.quote(file_path, safe='')}"
        resp = self._get(url, params={"ref": ref}).json()
        return base64.b64decode(resp["content"]).decode("utf-8")

    def create_commit(
        self,
        project_id: int,
        branch: str,
        message: str,
        actions: list[dict],
    ) -> dict:
        """
        Create a commit with file changes via the GitLab Commits API.
        `actions` format: [{"action": "update", "file_path": "...", "content": "..."}]
        """
        url = f"{self.base}/projects/{project_id}/repository/commits"
        payload = {
            "branch": branch,
            "commit_message": message,
            "actions": actions,
        }
        resp = self._post(url, json=payload)
        return resp.json()

    def post_mr_comment(
        self,
        project_id: int,
        mr_iid: Optional[int],
        body: str,
    ) -> Optional[dict]:
        """Post a note on a Merge Request. Skips gracefully if MR IID is unknown."""
        if not mr_iid:
            logger.warning("No MR IID — skipping comment")
            return None
        url = f"{self.base}/projects/{project_id}/merge_requests/{mr_iid}/notes"
        resp = self._post(url, json={"body": body})
        return resp.json()

    def retry_pipeline(self, project_id: int, pipeline_id: int) -> dict:
        """Trigger a retry of all failed jobs in the pipeline."""
        url = f"{self.base}/projects/{project_id}/pipelines/{pipeline_id}/retry"
        resp = self._post(url)
        return resp.json()

    def get_pipeline_jobs(self, project_id: int, pipeline_id: int) -> list[dict]:
        """List all jobs for a pipeline."""
        url = f"{self.base}/projects/{project_id}/pipelines/{pipeline_id}/jobs"
        return self._get(url).json()

    def get_open_mr_for_branch(self, project_id: int, branch: str) -> Optional[dict]:
        """Find the open MR targeting any base branch from the given source branch."""
        url = f"{self.base}/projects/{project_id}/merge_requests"
        params = {"state": "opened", "source_branch": branch}
        mrs = self._get(url, params=params).json()
        return mrs[0] if mrs else None

    # ------------------------------------------------------------------
    # HTTP helpers with retry logic
    # ------------------------------------------------------------------

    def _get(self, url: str, params: Optional[dict] = None, retries: int = 3) -> requests.Response:
        for attempt in range(retries):
            try:
                resp = self.session.get(url, params=params, timeout=30)
                resp.raise_for_status()
                return resp
            except requests.HTTPError as exc:
                if exc.response.status_code == 429:
                    wait = int(exc.response.headers.get("Retry-After", 5))
                    logger.warning(f"Rate limited — waiting {wait}s")
                    time.sleep(wait)
                elif attempt == retries - 1:
                    raise
        raise RuntimeError(f"GET {url} failed after {retries} attempts")

    def _post(self, url: str, json: Optional[dict] = None, retries: int = 3) -> requests.Response:
        for attempt in range(retries):
            try:
                resp = self.session.post(url, json=json, timeout=30)
                resp.raise_for_status()
                return resp
            except requests.HTTPError as exc:
                if exc.response.status_code == 429:
                    wait = int(exc.response.headers.get("Retry-After", 5))
                    logger.warning(f"Rate limited — waiting {wait}s")
                    time.sleep(wait)
                elif attempt == retries - 1:
                    raise
        raise RuntimeError(f"POST {url} failed after {retries} attempts")
