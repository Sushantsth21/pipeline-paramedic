# 🚑 Pipeline Paramedic

> **An autonomous CI/CD repair agent** — powered by Gemini and Google Cloud Agent Builder, integrated with the GitLab MCP server.

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![Hackathon: Rapid Agent](https://img.shields.io/badge/Hackathon-Rapid_Agent-purple)](https://rapid-agent.devpost.com)
[![Track: GitLab](https://img.shields.io/badge/Track-GitLab-orange)](https://rapid-agent.devpost.com/details/gitlab-resources)

---

## The Problem

When a CI/CD pipeline breaks — a missing semicolon, a linter violation, an undefined variable — developers must **stop what they're doing**, dig through logs, find the error, apply a tiny fix, and push again. This is pure friction on a low-value chore.

## The Solution

**Pipeline Paramedic** is an autonomous agent that:

1. **Listens** for a `pipeline failed` webhook from GitLab
2. **Fetches** the failing job log and the commit diff via the **GitLab MCP server**
3. **Reasons** with **Gemini** to identify the root cause and generate a precise fix
4. **Applies** the fix by committing the patched file back to the branch
5. **Posts** an MR comment explaining what it did, then re-triggers the pipeline

The developer never leaves their current task.

---

## Architecture

```
GitLab CI (pipeline fails)
        │  webhook
        ▼
Google Cloud Run  ──────────►  Google Cloud Agent Builder
  (receiver.py)                        │
                                       │  orchestrates
                              ┌────────┴────────┐
                              ▼                 ▼
                         GitLab MCP          Gemini
                       (fetch logs,        (diagnose +
                        diff, commit,       generate fix)
                        post comment)
                              │
                              ▼
                    Auto-fix commit pushed
                    MR comment posted
                    Pipeline retried
```

---

## Hackathon Track

- **Primary track:** GitLab (required MCP integration)
- **Gemini model:** `gemini-2.0-flash-001` via Vertex AI
- **Infrastructure:** Google Cloud Run + Cloud Build + Secret Manager

---

## Project Structure

```
pipeline-paramedic/
├── main.py                          # App entrypoint
├── requirements.txt
├── Dockerfile
├── cloudbuild.yaml                  # Cloud Build CI/CD
├── terraform/
│   └── main.tf                      # All GCP resources as IaC
├── src/
│   ├── webhook/
│   │   └── receiver.py              # Flask webhook endpoint
│   ├── agent/
│   │   └── paramedic.py             # Core agent orchestrator
│   └── tools/
│       ├── gitlab_tools.py          # GitLab MCP integration
│       └── code_tools.py            # Patch applier + commit logic
├── tests/
│   └── test_paramedic.py
└── docs/
    ├── example-target-gitlab-ci.yml # How to configure target repos
    └── SETUP.md                     # Step-by-step setup guide
```

---

## Quick Start

### Prerequisites

- Google Cloud project with billing enabled
- GitLab account (cloud or self-managed)
- `gcloud` CLI authenticated
- Terraform >= 1.5

### 1. Clone and configure

```bash
git clone https://github.com/your-username/pipeline-paramedic
cd pipeline-paramedic
cp .env.example .env
# Edit .env with your values
```

### 2. Set environment variables

```bash
export GCP_PROJECT_ID="your-gcp-project-id"
export GITLAB_TOKEN="glpat-xxxxxxxxxxxxxxxxxxxx"   # needs api + write_repository scope
export GITLAB_WEBHOOK_SECRET="your-random-secret"
export GITLAB_URL="https://gitlab.com"              # or your self-managed URL
```

### 3. Deploy with Terraform

```bash
cd terraform
terraform init
terraform apply \
  -var="project_id=$GCP_PROJECT_ID" \
  -var="gitlab_token=$GITLAB_TOKEN" \
  -var="gitlab_webhook_secret=$GITLAB_WEBHOOK_SECRET"
```

Terraform outputs your webhook URL:
```
webhook_url = "https://pipeline-paramedic-xxxx-uc.a.run.app/webhook/gitlab"
```

### 4. Build and deploy the container

```bash
gcloud builds submit --config cloudbuild.yaml
```

### 5. Configure GitLab webhook

In your GitLab project → **Settings → Webhooks**:

| Field | Value |
|---|---|
| URL | `https://pipeline-paramedic-xxxx-uc.a.run.app/webhook/gitlab` |
| Secret token | `your-random-secret` |
| Trigger | ✅ Pipeline events |

### 6. Configure the target repo's `.gitlab-ci.yml`

See [`docs/example-target-gitlab-ci.yml`](docs/example-target-gitlab-ci.yml) for a drop-in template. Add two CI/CD variables to your GitLab project:

- `PARAMEDIC_WEBHOOK_URL` — your Cloud Run URL
- `PARAMEDIC_WEBHOOK_SECRET` — your webhook secret

---

## How It Works — Step by Step

### Step 1 — Webhook received

GitLab fires a `Pipeline Hook` event when a pipeline fails. Cloud Run receives it, verifies the secret token, and hands the payload to the agent.

### Step 2 — GitLab MCP: Fetch artefacts

The agent uses the GitLab MCP tools to:
- `get_job_log(project_id, job_id)` — fetches the full console output (last 200 lines used)
- `get_commit_diff(project_id, commit_sha)` — fetches the unified diff of the triggering commit

### Step 3 — Gemini reasoning

The trimmed log + diff are sent to `gemini-2.0-flash-001` with a structured system prompt. Gemini returns JSON:

```json
{
  "root_cause": "flake8 E501 line too long on line 42 of src/auth.py",
  "error_type": "lint",
  "file_path": "src/auth.py",
  "line_number": 42,
  "fix_description": "Wrap the long line to stay under 120 characters",
  "fix_patch": "@@ -41,3 +41,4 @@\n ...",
  "confidence": "high",
  "human_summary": "I noticed the linter failed due to a line exceeding 120 characters..."
}
```

### Step 4 — Apply fix

If confidence is `high` or `medium` and a patch is provided:
1. `get_file_content` fetches the current file from the branch
2. The unified diff patch is applied in-memory
3. `create_commit` pushes the change back via GitLab Commits API — **no local git clone needed**

If confidence is `low` or no patch is available, a triage comment is posted instead.

### Step 5 — Post MR comment + retry

```
🚑 Pipeline Paramedic — Auto-fix Applied

Root cause: flake8 E501 line too long on src/auth.py:42

I noticed the linter failed due to a line exceeding 120 characters in the
authentication module. I've pushed commit a3f1b2c8 to wrap the line so
you can keep working.

Automated fix by Pipeline Paramedic
```

The pipeline is then retried automatically.

---

## Error Types Handled

| Error type | Example | Confidence |
|---|---|---|
| Linter (flake8/pylint) | `E501 line too long` | High |
| Formatter (black/isort) | `would reformat src/app.py` | High |
| Syntax error | `SyntaxError: invalid syntax` | Medium |
| Missing import | `ModuleNotFoundError: No module named 'x'` | Medium |
| Undefined variable | `NameError: name 'x' is not defined` | Medium |
| Failing unit test | `AssertionError: expected 42 got 0` | Low (triage only) |

---

## Running Tests Locally

```bash
pip install -r requirements.txt pytest
pytest tests/ -v
```

---

## Environment Variables Reference

| Variable | Required | Description |
|---|---|---|
| `GCP_PROJECT_ID` | ✅ | Your Google Cloud project ID |
| `GITLAB_TOKEN` | ✅ | GitLab personal/project access token (`api` + `write_repository`) |
| `GITLAB_WEBHOOK_SECRET` | ✅ | Shared secret for webhook verification |
| `GITLAB_URL` | Optional | GitLab base URL (default: `https://gitlab.com`) |
| `GCP_LOCATION` | Optional | Vertex AI region (default: `us-central1`) |
| `PORT` | Optional | HTTP port (default: `8080`) |

---

## AWS SAA-C03 Concepts Demonstrated

This project is a practical demonstration of several exam topics:

| AWS Concept | GCP Equivalent Used | What It Teaches |
|---|---|---|
| CodePipeline / CodeBuild | Cloud Build + Cloud Run | CI/CD pipeline architecture |
| Lambda (event-driven) | Cloud Run (webhook receiver) | Serverless, event-driven compute |
| Secrets Manager | GCP Secret Manager | Secure credential storage |
| IAM Roles | GCP Service Accounts | Least-privilege access control |
| CloudWatch Logs | Cloud Logging | Observability for pipelines |
| API Gateway | Cloud Run HTTP endpoint | Webhook / API endpoint patterns |
| ECS / Fargate | Cloud Run containers | Container-based deployment |

---

## License

Apache 2.0 — see [LICENSE](LICENSE).
