terraform {
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 5.0"
    }
  }
}

variable "project_id" { type = string }
variable "region"     { default = "us-central1" }
variable "gitlab_token"          { sensitive = true }
variable "gitlab_webhook_secret" { sensitive = true }


provider "google" {
  project = var.project_id
  region  = var.region
}

# Secret Manager secrets
resource "google_secret_manager_secret" "gitlab_token" {
  secret_id = "gitlab-token"
  replication {
    auto {}
  }
}

resource "google_secret_manager_secret_version" "gitlab_token" {
  secret      = google_secret_manager_secret.gitlab_token.id
  secret_data = var.gitlab_token
}

resource "google_secret_manager_secret" "webhook_secret" {
  secret_id = "webhook-secret"
  replication {
    auto {}
  }
}

resource "google_secret_manager_secret_version" "webhook_secret" {
  secret      = google_secret_manager_secret.webhook_secret.id
  secret_data = var.gitlab_webhook_secret
}

# Service account for Cloud Run
resource "google_service_account" "paramedic" {
  account_id   = "pipeline-paramedic-sa"
  display_name = "Pipeline Paramedic Service Account"
}

resource "google_project_iam_member" "vertex_user" {
  project = var.project_id
  role    = "roles/aiplatform.user"
  member  = "serviceAccount:${google_service_account.paramedic.email}"
}

resource "google_secret_manager_secret_iam_member" "paramedic_gitlab_token" {
  secret_id = google_secret_manager_secret.gitlab_token.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.paramedic.email}"
}

resource "google_secret_manager_secret_iam_member" "paramedic_webhook_secret" {
  secret_id = google_secret_manager_secret.webhook_secret.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.paramedic.email}"
}

resource "google_secret_manager_secret" "gemini_api_key" {
  secret_id = "gemini-api-key"
  replication {
    auto {}
  }
}

resource "google_secret_manager_secret_iam_member" "paramedic_gemini_api_key" {
  secret_id = google_secret_manager_secret.gemini_api_key.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.paramedic.email}"
}

# Cloud Run service
resource "google_cloud_run_v2_service" "paramedic" {
  name     = "pipeline-paramedic"
  location = var.region

  template {
    service_account = google_service_account.paramedic.email

    containers {
      image = "gcr.io/${var.project_id}/pipeline-paramedic:latest"

      resources {
        limits   = { memory = "512Mi", cpu = "1" }
        startup_cpu_boost = true
      }

      env {
        name  = "GCP_PROJECT_ID"
        value = var.project_id
      }

      env {
        name = "GITLAB_TOKEN"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.gitlab_token.secret_id
            version = "latest"
          }
        }
      }

      env {
        name = "GITLAB_WEBHOOK_SECRET"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.webhook_secret.secret_id
            version = "latest"
          }
        }
      }

      env {
        name = "GEMINI_API_KEY"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.gemini_api_key.secret_id
            version = "latest"
          }
        }
      }

    }

    scaling {
      min_instance_count = 0
      max_instance_count = 10
    }
  }
}

# Allow public webhook invocations
resource "google_cloud_run_v2_service_iam_member" "public" {
  name     = google_cloud_run_v2_service.paramedic.name
  location = var.region
  role     = "roles/run.invoker"
  member   = "allUsers"
}

output "webhook_url" {
  value = "${google_cloud_run_v2_service.paramedic.uri}/webhook/gitlab"
}
