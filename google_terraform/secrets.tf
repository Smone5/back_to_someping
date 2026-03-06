# ── Secret Manager Secrets ────────────────────────────────────────────────────
# API keys are NEVER hardcoded. They are stored in Secret Manager and
# mounted into Cloud Run at runtime (Iter 2 #5 — Secret Exposure in CI/CD).

resource "google_secret_manager_secret" "google_api_key" {
  secret_id = "storyteller-google-api-key"
  replication {
    auto {}
  }
}

resource "google_secret_manager_secret" "elevenlabs_api_key" {
  secret_id = "storyteller-elevenlabs-api-key"
  replication {
    auto {}
  }
}

# Grant backend SA access to read these secrets
resource "google_secret_manager_secret_iam_member" "backend_google_api_key" {
  secret_id = google_secret_manager_secret.google_api_key.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.backend_sa.email}"
}

resource "google_secret_manager_secret_iam_member" "backend_elevenlabs_api_key" {
  secret_id = google_secret_manager_secret.elevenlabs_api_key.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.backend_sa.email}"
}

# ── Firestore (Native mode) — for Cross-Session Lore (Iter 8 #2) ──────────────
resource "google_firestore_database" "storyteller_db" {
  name        = "storyteller-lore"
  location_id = "nam5" # multi-region for reliability
  type        = "FIRESTORE_NATIVE"
  depends_on  = [google_project_service.required["firestore.googleapis.com"]]

  deletion_policy = "DELETE"
}

# Output
output "backend_url" {
  value = google_cloud_run_v2_service.backend.uri
}

output "frontend_url" {
  value = google_cloud_run_v2_service.frontend.uri
}

output "load_balancer_ip" {
  value = google_compute_global_address.default.address
}
