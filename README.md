# Pipeline Paramedic

> An autonomous CI/CD repair agent — powered by Gemini 3 Flash and the GitLab REST API.

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)

---

## Roadmap

- [x] Core agent — fetch logs, diagnose with Gemini, patch files, commit, post MR comment, retry pipeline
- [x] Multi-file fix in a single commit
- [x] Upgrade to Gemini 3 (`gemini-3-flash-preview`)
- [ ] Wire up the real GitLab MCP server (currently calls GitLab REST API directly)
- [ ] Async webhook handling via Cloud Tasks (currently blocks until agent finishes — can time out)
- [ ] Human-in-the-loop approval for medium-confidence fixes (post MR comment asking for sign-off before committing)
- [ ] Auto-re-enable disabled webhooks via GitLab API (GitLab disables webhooks after repeated failures)
- [ ] Record 3-minute demo video showing end-to-end fix on a real repo
- [ ] Devpost submission write-up

---

## What It Does

When a GitLab CI/CD pipeline fails, Pipeline Paramedic:

1. Receives the failure webhook from GitLab
2. Fetches the failing job log and the commit diff
3. Sends both to Gemini 2.5 Flash for diagnosis
4. Identifies **all** files with errors — not just the first one
5. Patches every affected file, commits all fixes in a single commit, posts an MR comment, and re-triggers the pipeline
6. If confidence is low or no patch can be determined — posts a triage comment instead

The developer never has to leave their current task for a simple linter or syntax error.

---

## Architecture

```
GitLab CI pipeline fails
         │
         │  Pipeline Hook webhook (POST /webhook/gitlab)
         ▼
┌─────────────────────┐
│   Flask Web Server  │  (Cloud Run in production)
│   receiver.py       │
│                     │
│  1. Verify token    │
│  2. Filter "failed" │
│  3. Hand off        │
└────────┬────────────┘
         │
         ▼
┌─────────────────────────────────────────────────────┐
│   PipelineParamedic Agent  (paramedic.py)           │
│                                                     │
│  Step 1 ── extract context from payload             │
│            (project_id, job_id, branch, sha, mr)    │
│                                                     │
│  Step 2 ── GitLabTools.get_job_log()                │
│            GitLabTools.get_commit_diff()            │
│                 │                                   │
│                 ▼                                   │
│  Step 3 ── Gemini 2.5 Flash                        │
│            (trimmed log + diff → JSON with          │
│             "fixes" array covering ALL files)       │
│                 │                                   │
│         ┌───── ▼ ─────┐                            │
│   high/medium       low / no patches                │
│   confidence              │                         │
│         │                 ▼                         │
│  Step 4 │        post triage MR comment             │
│  CodeTools.apply_all_fixes()                        │
│  (fetch each file → patch → single commit)          │
│         │                                           │
│  Step 5 ── post fix MR comment (lists all files)   │
│         ── GitLabTools.retry_pipeline()             │
└─────────────────────────────────────────────────────┘
```

### Component breakdown

| File | Role |
|---|---|
| `main.py` | Entrypoint — imports Flask app and sets port |
| `src/webhook/receiver.py` | Flask server; verifies webhook token; routes to agent |
| `src/agent/paramedic.py` | Orchestrates the full fix cycle; calls Gemini |
| `src/tools/gitlab_tools.py` | Wraps GitLab REST API v4 (logs, diffs, commits, MR notes, pipeline retry) |
| `src/tools/code_tools.py` | Applies unified diff patches in memory; commits via GitLab API |
| `terraform/main.tf` | All GCP infrastructure as code |
| `cloudbuild.yaml` | CI/CD: test → build → push → deploy to Cloud Run |
| `tests/test_paramedic.py` | Unit tests for patch applier and agent orchestration |

---

## How It Works — Step by Step

### 1. Webhook received

GitLab fires a `Pipeline Hook` event on failure. The Flask receiver at `/webhook/gitlab`:
- Checks `X-Gitlab-Token` header against `GITLAB_WEBHOOK_SECRET`
- Ignores any event type other than `Pipeline Hook` or `Job Hook`
- Ignores any pipeline with status other than `failed`
- Returns a clean response for non-failure events so GitLab does not disable the webhook

### 2. Context extraction

The payload is normalised into a flat context dict:

```python
{
    "project_id": 82455138,
    "pipeline_id": 2547342256,
    "job_id": 14511776899,   # first failed build in the builds array
    "branch": "main",
    "commit_sha": "7d82a30...",
    "mr_iid": 7              # None if no open MR — comment is silently skipped
}
```

Both `Pipeline Hook` and `Job Hook` payload shapes are handled.

### 3. GitLab artefacts fetched

```
GET /api/v4/projects/{id}/jobs/{job_id}/trace        → job log (raw text)
GET /api/v4/projects/{id}/repository/commits/{sha}/diff → commit diff
```

The job log is trimmed to the last 100 lines before being sent to Gemini to stay within free-tier token limits.

### 4. Gemini diagnosis — all files at once

The trimmed log and diff (capped at 2 000 chars) are sent to `gemini-2.5-flash` with a system prompt that instructs it to identify **every** file with an error, not just the first. Gemini responds with a `fixes` array:

```json
{
  "root_cause": "Multiple flake8 E225 violations across 3 files and W292 in a fourth",
  "error_type": "lint",
  "confidence": "high",
  "human_summary": "Four files had linting issues...",
  "fixes": [
    {
      "file_path": "src/bad_code.py",
      "fix_description": "Add spaces around = operator",
      "fix_patch": "@@ -1 +1 @@\n-x=1\n+x = 1\n"
    },
    {
      "file_path": "src/bad_code2.py",
      "fix_description": "Add spaces around = operator",
      "fix_patch": "@@ -1 +1 @@\n-my_var=1\n+my_var = 1\n"
    }
  ]
}
```

File paths are normalised after parsing — leading `./` and `/` are stripped because the GitLab API rejects them.

The agent retries up to 3 times with 30 s / 60 s backoff on `429 RESOURCE_EXHAUSTED`.

### 5. All fixes applied in one commit

```
For each fix in diagnosis["fixes"]:
    GET /api/v4/projects/{id}/repository/files/{path}?ref={branch}  → file content
    apply_unified_diff(original, patch)                              → patched content

POST /api/v4/projects/{id}/repository/commits
    actions: [
        {"action": "update", "file_path": "src/bad_code.py",  "content": "..."},
        {"action": "update", "file_path": "src/bad_code2.py", "content": "..."},
        ...
    ]
→ single commit SHA
```

No local `git` is required. If a patch produces no change (fix already present), that file is skipped. If all patches are no-ops, a `ValueError` is raised and no commit is made.

### 6. Triage only (low confidence or no patches)

When Gemini cannot produce fix patches with confidence, a triage comment is posted:

```
🚑 Pipeline Paramedic — Triage Report

Root cause: Unknown error in build step

I was unable to apply an automatic fix with high confidence.
Please review the job log and apply a manual fix.
```

### 7. Post comment + retry pipeline

On a successful fix, the MR comment lists every file that was changed:

```
🚑 Pipeline Paramedic — Auto-fix Applied

Root cause: Multiple flake8 violations across 4 files

Files fixed (4):
- `src/bad_code.py`
- `src/bad_code2.py`
- `src/bad_code3.py`
- `src/bad_code4.py`

Pushed commit 8e2d637b and re-triggered the pipeline.
```

Then `POST /api/v4/projects/{id}/pipelines/{pipeline_id}/retry` re-queues all failed jobs.

---

## Error Types Handled

| Error type | Examples | Typical outcome |
|---|---|---|
| Linter violation | `E225 missing whitespace`, `E501 line too long` | Auto-fix committed |
| Formatter | `black would reformat src/app.py` | Auto-fix committed |
| Syntax error | `SyntaxError: invalid syntax` | Auto-fix if line is clear |
| Missing import | `ModuleNotFoundError` | Triage comment |
| Undefined variable | `NameError` | Triage comment |
| Failing unit test | `AssertionError` | Triage comment |

---

## Running Locally

### Prerequisites

- Python 3.12+
- A GitLab personal access token with `api` + `write_repository` scopes
- A Gemini API key from [aistudio.google.com](https://aistudio.google.com) — must use `gemini-2.5-flash` (2.0-flash free tier quota is very limited)

### 1. Clone and set up environment

```bash
git clone https://github.com/your-username/pipeline-paramedic
cd pipeline-paramedic
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Configure `.env`

```bash
cp .env.example .env
```

Edit `.env`:

```env
GCP_PROJECT_ID=your-gcp-project-id
GCP_LOCATION=us-central1

GITLAB_URL=https://gitlab.com
GITLAB_TOKEN=glpat-xxxxxxxxxxxxxxxxxxxx
GITLAB_WEBHOOK_SECRET=your-random-secret

GEMINI_API_KEY=AIzaSy...

PORT=8080
```

### 3. Run the server

```bash
export $(cat .env | grep -v '^#' | xargs)
python main.py
```

Verify it is running:

```bash
curl http://localhost:8080/health
# {"service": "pipeline-paramedic", "status": "ok"}
```

### 4. Send a test webhook

Use real `project_id`, `job_id`, and `commit_sha` values from a GitLab project that has a failing pipeline:

```bash
curl -X POST http://localhost:8080/webhook/gitlab \
  -H "Content-Type: application/json" \
  -H "X-Gitlab-Event: Pipeline Hook" \
  -H "X-Gitlab-Token: your-random-secret" \
  -d '{
    "object_attributes": {
      "id": 123456,
      "ref": "main",
      "sha": "abc123def456",
      "status": "failed"
    },
    "project": {"id": 456, "name": "my-project"},
    "commit": {"id": "abc123def456"},
    "builds": [
      {"id": 789, "name": "lint", "status": "failed"}
    ]
  }'
```

---

## Running Tests

```bash
source .venv/bin/activate
python -m pytest tests/ -v
```

Expected output:

```
tests/test_paramedic.py::TestApplyUnifiedDiff::test_adds_semicolon       PASSED
tests/test_paramedic.py::TestApplyUnifiedDiff::test_removes_debug_line   PASSED
tests/test_paramedic.py::TestApplyUnifiedDiff::test_noop_patch_returns_original PASSED
tests/test_paramedic.py::TestApplyUnifiedDiff::test_adds_import          PASSED
tests/test_paramedic.py::TestPipelineParamedic::test_extracts_correct_context   PASSED
tests/test_paramedic.py::TestPipelineParamedic::test_low_confidence_posts_triage PASSED

6 passed in 0.47s
```

---

## Deploying to Google Cloud Run

### Prerequisites

- Google Cloud project with billing enabled
- `gcloud` CLI authenticated (`gcloud auth login`)
- Terraform >= 1.5

### 1. Provision infrastructure

```bash
cd terraform
terraform init
terraform apply \
  -var="project_id=your-gcp-project-id" \
  -var="gitlab_token=glpat-xxxxxxxxxxxxxxxxxxxx" \
  -var="gitlab_webhook_secret=your-random-secret"
```

Terraform creates:
- **Secret Manager secrets** — `gitlab-token`, `webhook-secret`, `gemini-api-key`
- **Service account** `pipeline-paramedic-sa` with `roles/aiplatform.user` and `roles/secretmanager.secretAccessor`
- **Cloud Run v2 service** — 512 Mi memory, 0–10 instances, secrets injected as env vars

After apply, note the output:

```
webhook_url = "https://pipeline-paramedic-xxxx-uc.a.run.app/webhook/gitlab"
```

### 2. Store the Gemini API key

```bash
echo -n "AIzaSy..." | gcloud secrets versions add gemini-api-key \
  --data-file=- \
  --project=your-gcp-project-id
```

### 3. Build and deploy

```bash
gcloud builds submit --config cloudbuild.yaml --project=your-gcp-project-id
```

Cloud Build runs in order:
1. `test` — runs `pytest tests/ -v` inside `python:3.12-slim`
2. `build` — builds the Docker image tagged with `$COMMIT_SHA` and `latest`
3. `push` — pushes both tags to Container Registry
4. `deploy` — deploys to Cloud Run with secrets injected

### 4. Configure GitLab webhook

In your GitLab project → **Settings → Webhooks → Add new webhook**:

| Field | Value |
|---|---|
| URL | `https://pipeline-paramedic-xxxx-uc.a.run.app/webhook/gitlab` |
| Secret token | value of `GITLAB_WEBHOOK_SECRET` |
| Trigger | Pipeline events |
| SSL verification | Enabled |

> **Note:** GitLab automatically disables a webhook after several consecutive failures (e.g. if the paramedic server is down). Re-enable it under **Settings → Webhooks** after fixing the issue.

---

## Configuring a Target Repository

Add two CI/CD variables to your GitLab project (**Settings → CI/CD → Variables**):

| Variable | Value |
|---|---|
| `PARAMEDIC_WEBHOOK_URL` | Your Cloud Run URL (without trailing slash) |
| `PARAMEDIC_WEBHOOK_SECRET` | Your webhook secret |

Then add `ci/notify.py` to your repo and wire it into the `after_script`:

**`ci/notify.py`** — uses Python's built-in `urllib` (no `curl` required, works in any `python:*` image):

```python
import json
import os
import sys
import urllib.request

url = os.environ.get("PARAMEDIC_WEBHOOK_URL", "") + "/webhook/gitlab"
token = os.environ.get("PARAMEDIC_WEBHOOK_SECRET", "")
mr = os.environ.get("CI_MERGE_REQUEST_IID", "")

body = json.dumps({
    "object_kind": "pipeline",
    "object_attributes": {
        "id": int(os.environ["CI_PIPELINE_ID"]),
        "ref": os.environ["CI_COMMIT_REF_NAME"],
        "sha": os.environ["CI_COMMIT_SHA"],
        "status": "failed",
    },
    "project": {
        "id": int(os.environ["CI_PROJECT_ID"]),
        "name": os.environ["CI_PROJECT_NAME"],
    },
    "builds": [{
        "id": int(os.environ["CI_JOB_ID"]),
        "name": os.environ["CI_JOB_NAME"],
        "status": "failed",
    }],
    "commit": {"id": os.environ["CI_COMMIT_SHA"]},
    "merge_request": {"iid": int(mr)} if mr else None,
}).encode()

req = urllib.request.Request(url, data=body, headers={
    "Content-Type": "application/json",
    "X-Gitlab-Token": token,
    "X-Gitlab-Event": "Pipeline Hook",
})
try:
    urllib.request.urlopen(req, timeout=10)
    print("[paramedic] webhook sent")
except Exception as e:
    print(f"[paramedic] webhook failed: {e}", file=sys.stderr)
```

**`.gitlab-ci.yml`** — call `ci/notify.py` from the `after_script`:

```yaml
.notify_paramedic: &notify_paramedic
  after_script:
    - if [ "$CI_JOB_STATUS" = "failed" ]; then python3 ci/notify.py; fi

lint:
  stage: lint
  image: python:3.12-slim
  <<: *notify_paramedic
  script:
    - pip install flake8 black isort --quiet
    - flake8 . --max-line-length=120 --exclude=.git,__pycache__
    - black --check .
    - isort --check-only .
```

> **Why `ci/notify.py` instead of inline `curl`?**
> `python:slim` images do not include `curl`. Embedding multi-line Python in a YAML block scalar also causes parse errors because unindented Python lines terminate the YAML block. A separate script file avoids both problems.

---

## Environment Variables Reference

| Variable | Required | Description |
|---|---|---|
| `GEMINI_API_KEY` | Yes | Gemini API key from [aistudio.google.com](https://aistudio.google.com). Use `gemini-2.5-flash` — the 2.0-flash free tier quota resets daily and is easily exhausted. |
| `GITLAB_TOKEN` | Yes | GitLab personal or project access token. Needs `api` and `write_repository` scopes. |
| `GITLAB_WEBHOOK_SECRET` | Yes | Arbitrary secret string. Set the same value in the GitLab webhook config. |
| `GITLAB_URL` | No | GitLab base URL. Default: `https://gitlab.com`. Override for self-managed instances. |
| `GCP_PROJECT_ID` | No | GCP project ID. Required only if using Vertex AI mode. |
| `GCP_LOCATION` | No | GCP region for Vertex AI. Default: `us-central1`. |
| `PORT` | No | HTTP port the Flask server listens on. Default: `8080`. |

---

## Gemini API — Quota Notes

The project uses `gemini-2.5-flash` via the Google AI API (`generativelanguage.googleapis.com`). Free tier quota resets daily at midnight Pacific time.

If you hit `429 RESOURCE_EXHAUSTED`:

- **Enable billing** on the Google Cloud project tied to your API key.
- **Use a different model** — `gemini-2.5-flash` has separate quota from `gemini-2.0-flash`.
- **Use Vertex AI mode** — authenticates via Application Default Credentials instead of an API key:

  ```python
  from google import genai
  from google.genai.types import HttpOptions

  client = genai.Client(
      vertexai=True,
      project="your-gcp-project-id",
      location="us-central1",
      http_options=HttpOptions(api_version="v1"),
  )
  ```

  Run `gcloud auth application-default login` first. Note: Vertex AI requires the `aiplatform.googleapis.com` API enabled and billing active on the project.

---

## Notable Implementation Decisions

**Multi-file fix in a single commit.** The system prompt instructs Gemini to return a `fixes` array covering every file mentioned in the log. `apply_all_fixes` iterates over all fixes, applies each patch in memory, and calls `create_commit` once with all `actions`. This avoids chaining multiple pipeline triggers for the same failure run.

**No local git clone.** The entire fetch → patch → commit cycle goes through the GitLab REST API. `get_file_content` fetches files, patches are applied in memory by `apply_unified_diff`, and `create_commit` pushes the result. The Cloud Run container stays stateless.

**Unified diff marker handling.** Git diffs include `\ No newline at end of file` as a special marker line starting with `\`. The diff applier skips any line starting with `\` rather than treating it as content, which previously caused patches for `W292` (missing newline) to produce no change.

**File path normalisation.** Gemini sometimes returns file paths with a leading `./` (e.g. `./src/foo.py`). The GitLab API rejects such paths. All `file_path` values in the parsed diagnosis are stripped of leading `./` and `/` before being used.

**Synchronous agent in the webhook handler.** In production this should be offloaded to a task queue (Cloud Tasks, Pub/Sub) so the webhook returns immediately. The current implementation blocks until the agent finishes, which works for fast lint/syntax fixes but may time out on slow Gemini responses.

**Token trimming.** Only the last 100 lines of the job log and the first 2 000 characters of the commit diff are sent to Gemini to stay within free-tier token limits.

**Retry with backoff.** Gemini `429` errors are retried up to 3 times with 30 s and 60 s waits before the exception propagates.

**Graceful MR comment skip.** If `mr_iid` is `None` (no open MR for the branch), the comment step is silently skipped. The fix commit still happens.

**GitLab webhook auto-disable.** GitLab disables a webhook after several consecutive delivery failures. If the paramedic server was down during a failure run, re-enable the webhook under **Settings → Webhooks** and trigger the pipeline manually.

---

## Project Structure

```
pipeline-paramedic/
├── main.py                   # Entrypoint — runs Flask app
├── requirements.txt          # flask, requests, google-genai, gunicorn
├── Dockerfile                # python:3.12-slim, exposes 8080
├── cloudbuild.yaml           # test → build → push → Cloud Run deploy
├── .env.example              # Template for local env config
├── src/
│   ├── webhook/
│   │   └── receiver.py       # Flask routes, token verification
│   ├── agent/
│   │   └── paramedic.py      # Core agent — Gemini calls, orchestration
│   └── tools/
│       ├── gitlab_tools.py   # GitLab API v4 wrapper
│       └── code_tools.py     # Unified diff applier, multi-file commit
├── tests/
│   └── test_paramedic.py     # Unit tests (6 tests, all passing)
└── terraform/
    └── main.tf               # Secret Manager, Service Account, Cloud Run v2
```

---

## License

Apache 2.0 — see [LICENSE](LICENSE).
