# ── Required Google APIs ──────────────────────────────────────────────────────
# Manage service enablement in Terraform so full infra applies are repeatable.

locals {
  required_google_apis = toset([
    "aiplatform.googleapis.com",
    "artifactregistry.googleapis.com",
    "cloudbuild.googleapis.com",
    "compute.googleapis.com",
    "containerregistry.googleapis.com",
    "firestore.googleapis.com",
    "homegraph.googleapis.com",
    "iam.googleapis.com",
    "run.googleapis.com",
    "secretmanager.googleapis.com",
    "storage.googleapis.com",
    "texttospeech.googleapis.com",
  ])
}

resource "google_project_service" "required" {
  for_each = local.required_google_apis

  project            = var.project_id
  service            = each.value
  disable_on_destroy = false
}
