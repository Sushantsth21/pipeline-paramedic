# Setup Guide — Pipeline Paramedic

## Prerequisites Checklist

- [ ] Google Cloud project created, billing enabled
- [ ] `gcloud` CLI installed and authenticated (`gcloud auth login`)
- [ ] Terraform >= 1.5 installed
- [ ] GitLab account (free tier is fine)
- [ ] Python 3.12+ for local testing

---

## Step 1 — Enable GCP APIs

```bash
gcloud services enable \
  run.googleapis.com \
  cloudbuild.googleapis.com \
  secretmanager.googleapis.com \
  aiplatform.googleapis.com \
  containerregistry.googleapis.com \
  --project=$GCP_PROJECT_ID
```

---

## Step 2 — Create a GitLab Access Token

1. GitLab → **User Settings → Access Tokens**
2. Name: `pipeline-paramedic`
3. Scopes: `api`, `read_repository`, `write_repository`
4. Copy the token — you'll only see it once

---

## Step 3 — Deploy Infrastructure

```bash
cd terraform
terraform init
terraform apply \
  -var="project_id=YOUR_PROJECT_ID" \
  -var="gitlab_token=glpat-xxxx" \
  -var="gitlab_webhook_secret=$(openssl rand -hex 32)"
```

Save the `webhook_url` output value.

---

## Step 4 — Build and Push the Container

```bash
# From the repo root
gcloud builds submit \
  --config=cloudbuild.yaml \
  --project=$GCP_PROJECT_ID
```

This runs tests, builds the Docker image, pushes to GCR, and deploys to Cloud Run.

---

## Step 5 — Add GitLab Webhook

In the GitLab repo you want to monitor:

1. **Settings → Webhooks → Add new webhook**
2. URL: paste the `webhook_url` from Terraform output
3. Secret token: the same `gitlab_webhook_secret` you used in Terraform
4. Triggers: ✅ **Pipeline events**
5. Click **Add webhook**, then **Test → Pipeline events** to verify

Expected response: `{"status": "ignored", "reason": "pipeline status is 'null', not 'failed'"}`
(That's correct — the test event has no status.)

---

## Step 6 — Configure the Target Repo CI

Copy `docs/example-target-gitlab-ci.yml` into your project as `.gitlab-ci.yml`, then add two CI/CD variables under **Settings → CI/CD → Variables**:

| Key | Value | Protected | Masked |
|---|---|---|---|
| `PARAMEDIC_WEBHOOK_URL` | `https://pipeline-paramedic-xxxx-uc.a.run.app` | ✅ | ❌ |
| `PARAMEDIC_WEBHOOK_SECRET` | your secret | ✅ | ✅ |

---

## Step 7 — Test It

Introduce a deliberate linter error:

```bash
# In your target repo
echo "x=1+2+3+4+5+6+7+8+9+10+11+12+13+14+15+16+17+18+19+20+21+22+23+24+25" >> src/any_file.py
git add . && git commit -m "test: trigger pipeline paramedic"
git push origin feature/test-paramedic
```

Watch the pipeline fail, then check the MR for a comment from Pipeline Paramedic and a new fix commit.

---

## Monitoring

View agent logs in Cloud Logging:

```bash
gcloud logging read \
  'resource.type="cloud_run_revision" AND resource.labels.service_name="pipeline-paramedic"' \
  --limit=50 \
  --format="table(timestamp, textPayload)"
```

---

## Local Development

```bash
# Install deps
pip install -r requirements.txt

# Set env vars
export GITLAB_TOKEN="glpat-xxxx"
export GITLAB_WEBHOOK_SECRET="dev-secret"
export GCP_PROJECT_ID="your-project"

# Run locally
python main.py

# In another terminal, simulate a webhook
curl -X POST http://localhost:8080/webhook/gitlab \
  -H "X-Gitlab-Token: dev-secret" \
  -H "X-Gitlab-Event: Pipeline Hook" \
  -H "Content-Type: application/json" \
  -d @docs/sample-webhook-payload.json
```

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| 401 on webhook | Token mismatch | Verify `GITLAB_WEBHOOK_SECRET` matches in GitLab and Secret Manager |
| `EnvironmentError: GITLAB_TOKEN required` | Secret not mounted | Check Cloud Run secret bindings in Terraform |
| Gemini returns non-JSON | Model hallucination | Check logs; patch regex strips fences; retry is automatic |
| `Patch applied but file content unchanged` | Fix already present | Normal — no commit needed, pipeline retried anyway |
| Cloud Run cold start timeout | First request after scale-to-zero | GitLab retries webhooks — second attempt will succeed |
