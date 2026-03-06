# ── Session Assets Bucket (Nano Banana images, Veo clips, ElevenLabs audio) ───

resource "google_storage_bucket" "session_assets" {
  name                        = "${var.project_id}-storyteller-session-assets"
  location                    = "US"
  force_destroy               = false
  uniform_bucket_level_access = true

  # Auto-delete session assets after 7 days (storage cost control)
  lifecycle_rule {
    condition {
      age = 7
    }
    action {
      type = "Delete"
    }
  }

  cors {
    origin          = ["https://${var.domain_name}"]
    method          = ["GET", "PUT", "POST"]
    response_header = ["Content-Type", "Authorization"]
    max_age_seconds = 3600
  }
}

# ── Final Videos Bucket (assembled .mp4 files) ────────────────────────────────

resource "google_storage_bucket" "final_videos" {
  name                        = "${var.project_id}-storyteller-final-videos"
  location                    = "US"
  force_destroy               = false
  uniform_bucket_level_access = true

  # 24-hour TTL for child privacy (COPPA compliance, Iter 3 #6)
  lifecycle_rule {
    condition {
      age = 1
    }
    action {
      type = "Delete"
    }
  }

  cors {
    origin          = ["https://${var.domain_name}"]
    method          = ["GET"]
    response_header = ["Content-Type"]
    max_age_seconds = 3600
  }
}

# Final movie playback URLs are sent directly to the browser from Theater Mode.
# With uniform bucket-level access enabled, use bucket-level IAM (not object ACLs).
resource "google_storage_bucket_iam_member" "final_videos_public_read" {
  bucket = google_storage_bucket.final_videos.name
  role   = "roles/storage.objectViewer"
  member = "allUsers"
}
