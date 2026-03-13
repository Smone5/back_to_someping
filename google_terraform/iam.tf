# ── Service Accounts ──────────────────────────────────────────────────────────

resource "google_service_account" "backend_sa" {
  account_id   = "storyteller-backend-sa"
  display_name = "Storyteller Backend Service Account"
}

resource "google_service_account" "frontend_sa" {
  account_id   = "storyteller-frontend-sa"
  display_name = "Storyteller Frontend Service Account"
}

# ── Backend IAM Roles ─────────────────────────────────────────────────────────

# Gemini and Veo on Vertex AI
resource "google_project_iam_member" "backend_vertex_ai" {
  project = var.project_id
  role    = "roles/aiplatform.user"
  member  = "serviceAccount:${google_service_account.backend_sa.email}"
  # NOTE: Tight IAM Conditions per Iter 4 #3 — scope to specific models
  # Full IAM Conditions require Org Policy which is environment-specific.
  # For the contest, we scope by binding to only the required role.
}

# Secret Manager reads
resource "google_project_iam_member" "backend_secrets" {
  project = var.project_id
  role    = "roles/secretmanager.secretAccessor"
  member  = "serviceAccount:${google_service_account.backend_sa.email}"
}

# Cloud Storage reads/writes (session assets + final videos)
resource "google_project_iam_member" "backend_storage" {
  project = var.project_id
  role    = "roles/storage.objectAdmin"
  member  = "serviceAccount:${google_service_account.backend_sa.email}"
}

# Firestore reads/writes (for Lore persistence, Iter 8 #2)
resource "google_project_iam_member" "backend_firestore" {
  project = var.project_id
  role    = "roles/datastore.user"
  member  = "serviceAccount:${google_service_account.backend_sa.email}"
}

# Cloud Run Job invocation — scoped only to the FFmpeg job.
# The backend launches the job with container arg overrides to pass session_id,
# so it needs run.jobs.runWithOverrides, not just run.jobs.run.
resource "google_cloud_run_v2_job_iam_member" "backend_can_trigger_ffmpeg" {
  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_job.ffmpeg_worker.name
  role     = "roles/run.jobsExecutorWithOverrides"
  member   = "serviceAccount:${google_service_account.backend_sa.email}"
}

# Cloud Logging (privacy-safe observability)
resource "google_project_iam_member" "backend_logging" {
  project = var.project_id
  role    = "roles/logging.logWriter"
  member  = "serviceAccount:${google_service_account.backend_sa.email}"
}

# Allow the backend service account to consume enabled Google APIs such as
# Cloud Text-to-Speech and HomeGraph on behalf of this project.
resource "google_project_iam_member" "backend_service_usage" {
  project = var.project_id
  role    = "roles/serviceusage.serviceUsageConsumer"
  member  = "serviceAccount:${google_service_account.backend_sa.email}"
}

# Service Account Token Creator (for Signed URLs, Jobs API tokens, and the
# iam.serviceAccounts.getOpenIdToken capability referenced in Google's
# HomeGraph service-account setup docs)
resource "google_project_iam_member" "backend_token_creator" {
  project = var.project_id
  role    = "roles/iam.serviceAccountTokenCreator"
  member  = "serviceAccount:${google_service_account.backend_sa.email}"
}
