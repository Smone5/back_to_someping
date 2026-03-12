terraform {
  required_version = ">= 1.5.0"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 5.0"
    }
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
}

# ── Cloud Run: Backend (FastAPI / ADK) ─────────────────────────────────────────
resource "google_cloud_run_v2_service" "backend" {
  name       = "storyteller-backend"
  location   = var.region
  depends_on = [google_project_service.required]

  template {
    scaling {
      min_instance_count = 1                         # NO cold starts (Iter 1 Expert Audit #3)
      max_instance_count = var.max_backend_instances # Hard billing ceiling (Iter 4 #10)
    }

    timeout = "3600s" # WebSockets on Cloud Run are still request-bound; avoid 5-minute disconnects.

    service_account = google_service_account.backend_sa.email

    containers {
      image = var.backend_image

      resources {
        limits = {
          cpu    = "2"
          memory = "1Gi"
        }
        cpu_idle = false # CPU always-on for low-latency Bidi-streaming
      }

      env {
        name  = "GOOGLE_CLOUD_PROJECT"
        value = var.project_id
      }
      env {
        name  = "GOOGLE_GENAI_USE_VERTEXAI"
        value = var.google_genai_use_vertexai ? "TRUE" : "FALSE"
      }
      env {
        name  = "GOOGLE_CLOUD_LOCATION"
        value = var.region
      }
      env {
        name  = "STORYTELLER_LIVE_MODEL"
        value = var.storyteller_live_model
      }
      env {
        name  = "IMAGE_MODEL"
        value = var.interactive_scene_image_model
      }
      env {
        name  = "ENABLE_PROMPT_META_LEARNING"
        value = "1"
      }
      env {
        name  = "PROMPT_META_LEARNING_CACHE_SECONDS"
        value = "120"
      }
      env {
        name  = "PROMPT_META_LEARNING_MAX_PRINCIPLES"
        value = "5"
      }
      env {
        name  = "PROMPT_META_LEARNING_PRINCIPLES_COLLECTION"
        value = "prompt_learning_principles"
      }
      env {
        name  = "PROMPT_META_LEARNING_FEEDBACK_COLLECTION"
        value = "prompt_learning_feedback"
      }
      env {
        name  = "PROMPT_META_LEARNING_ROLLUPS_COLLECTION"
        value = "prompt_learning_issue_rollups"
      }
      env {
        name  = "GCS_ASSETS_BUCKET"
        value = google_storage_bucket.session_assets.name
      }
      env {
        name  = "GCS_FINAL_VIDEOS_BUCKET"
        value = google_storage_bucket.final_videos.name
      }
      env {
        name  = "FFMPEG_JOB_NAME"
        value = google_cloud_run_v2_job.ffmpeg_worker.name
      }
      env {
        name  = "FIRESTORE_DATABASE"
        value = google_firestore_database.storyteller_db.name
      }
      env {
        name  = "PROD_FRONTEND_ORIGIN"
        value = "https://${var.domain_name}"
      }
      env {
        name  = "BACKEND_PUBLIC_URL"
        value = "https://${var.domain_name}"
      }
      env {
        name  = "MAX_LIVE_SESSIONS"
        value = tostring(var.max_live_sessions)
      }
      env {
        name  = "MAX_STORY_TURNS"
        value = tostring(var.max_story_turns)
      }
      env {
        name  = "DISABLE_BARGE_IN"
        value = tostring(var.disable_barge_in)
      }
      env {
        name  = "BARGE_IN_SUPPRESS_SECONDS"
        value = tostring(var.barge_in_suppress_seconds)
      }
      env {
        name  = "LOCAL_STORYBOOK_MODE"
        value = tostring(var.local_storybook_mode)
      }
      env {
        name  = "ENABLE_FAST_STORYBOOK_ASSEMBLY"
        value = tostring(var.enable_fast_storybook_assembly)
      }
      env {
        name  = "ENABLE_CLIENT_DIRECT_LIVE"
        value = tostring(var.enable_client_direct_live)
      }
      env {
        name  = "CLIENT_DIRECT_LIVE_MODEL"
        value = var.client_direct_live_model
      }
      env {
        name  = "CLIENT_DIRECT_LIVE_VOICE"
        value = var.client_direct_live_voice
      }
      env {
        name  = "CLIENT_DIRECT_LIVE_TEMPERATURE"
        value = tostring(var.client_direct_live_temperature)
      }
      env {
        name  = "CLIENT_DIRECT_LIVE_TOP_P"
        value = tostring(var.client_direct_live_top_p)
      }
      env {
        name  = "CLIENT_DIRECT_LIVE_EXPIRE_MINUTES"
        value = tostring(var.client_direct_live_expire_minutes)
      }
      env {
        name  = "CLIENT_DIRECT_LIVE_NEW_SESSION_MINUTES"
        value = tostring(var.client_direct_live_new_session_minutes)
      }
      env {
        name  = "PAGE_READ_ALOUD_MODEL"
        value = var.page_read_aloud_model
      }
      env {
        name  = "PAGE_READ_ALOUD_VOICE"
        value = var.page_read_aloud_voice
      }
      env {
        name  = "PAGE_READ_ALOUD_LANGUAGE_CODE"
        value = var.page_read_aloud_language_code
      }
      env {
        name  = "ENABLE_STORYBOOK_MUSIC"
        value = "1"
      }
      env {
        name  = "STORYBOOK_MUSIC_PROVIDER"
        value = "auto"
      }
      env {
        name  = "STORYBOOK_MUSIC_VOLUME"
        value = "0.10"
      }
      env {
        name  = "ENABLE_STORYBOOK_SFX"
        value = "1"
      }
      env {
        name  = "STORYBOOK_SFX_PROVIDER"
        value = "elevenlabs"
      }
      env {
        name  = "STORYBOOK_SFX_VOLUME"
        value = "0.18"
      }
      env {
        name  = "STORYBOOK_SFX_MAX"
        value = "2"
      }
      env {
        name  = "STORYBOOK_SFX_MIN_SCORE"
        value = "2"
      }
      env {
        name  = "STORYBOOK_SFX_COOLDOWN"
        value = "1"
      }
      env {
        name  = "STORYBOOK_NARRATION_VOLUME"
        value = "2.0"
      }
      env {
        name  = "ENABLE_STORYBOOK_END_CARD"
        value = "1"
      }
      env {
        name  = "STORYBOOK_END_CARD_SECONDS"
        value = "2.8"
      }
      env {
        name  = "STORYBOOK_END_CARD_TITLE"
        value = "The End"
      }
      env {
        name  = "STORYBOOK_END_CARD_NARRATION"
        value = "The end."
      }
      env {
        name  = "ENABLE_STORYBOOK_DUCKING"
        value = "1"
      }
      env {
        name  = "ENABLE_STORYBOOK_AUDIO_MASTERING"
        value = "1"
      }
      env {
        name  = "IMAGE_SIZE"
        value = "512px"
      }
      env {
        name  = "ALLOW_512PX"
        value = "1"
      }
      env {
        name  = "ENABLE_ELEVENLABS_MCP"
        value = tostring(var.enable_elevenlabs_mcp)
      }

      # Secrets mounted from Secret Manager (Iter 2 #5 — no raw env vars)
      env {
        name = "GOOGLE_API_KEY"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.google_api_key.secret_id
            version = "latest"
          }
        }
      }
      env {
        name = "ELEVENLABS_API_KEY"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.elevenlabs_api_key.secret_id
            version = "latest"
          }
        }
      }
      env {
        name  = "ELEVENLABS_VOICE_ID"
        value = var.elevenlabs_voice_id
      }
      env {
        name  = "ELEVENLABS_TTS_MODEL"
        value = var.elevenlabs_tts_model
      }
      env {
        name  = "ELEVENLABS_TTS_ENDPOINT"
        value = var.elevenlabs_tts_endpoint
      }
    }
  }

  traffic {
    type    = "TRAFFIC_TARGET_ALLOCATION_TYPE_LATEST"
    percent = 100
  }
}

# ── Cloud Run: Frontend (Next.js) ──────────────────────────────────────────────
resource "google_cloud_run_v2_service" "frontend" {
  name       = "storyteller-frontend"
  location   = var.region
  depends_on = [google_project_service.required]

  template {
    scaling {
      min_instance_count = 1
      max_instance_count = var.max_frontend_instances
    }

    service_account = google_service_account.frontend_sa.email

    containers {
      image = var.frontend_image

      resources {
        limits = {
          cpu    = "1"
          memory = "512Mi"
        }
      }

      env {
        name  = "NEXT_PUBLIC_WS_URL"
        value = "${google_cloud_run_v2_service.backend.uri}/ws/story"
      }
      env {
        name  = "NEXT_PUBLIC_UPLOAD_URL"
        value = "${google_cloud_run_v2_service.backend.uri}/api/upload"
      }
      env {
        name  = "BACKEND_URL"
        value = google_cloud_run_v2_service.backend.uri
      }
    }
  }

  traffic {
    type    = "TRAFFIC_TARGET_ALLOCATION_TYPE_LATEST"
    percent = 100
  }
}

# ── Allow public access to both Cloud Run services ────────────────────────────
resource "google_cloud_run_v2_service_iam_member" "backend_public" {
  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_service.backend.name
  role     = "roles/run.invoker"
  member   = "allUsers"
}

resource "google_cloud_run_v2_service_iam_member" "frontend_public" {
  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_service.frontend.name
  role     = "roles/run.invoker"
  member   = "allUsers"
}

# ── Cloud Run Job: FFmpeg Video Assembler ──────────────────────────────────────
resource "google_cloud_run_v2_job" "ffmpeg_worker" {
  name       = "storyteller-ffmpeg-assembler"
  location   = var.region
  depends_on = [google_project_service.required]

  template {
    template {
      service_account = google_service_account.backend_sa.email

      containers {
        image = var.ffmpeg_image

        resources {
          limits = {
            cpu    = "2"
            memory = "2Gi" # High memory for 8 Veo clips (Iter 7 #6 OOM fix)
          }
        }

        env {
          name  = "GOOGLE_CLOUD_PROJECT"
          value = var.project_id
        }
        env {
          name  = "FIRESTORE_DATABASE"
          value = google_firestore_database.storyteller_db.name
        }
        env {
          name  = "GCS_FINAL_VIDEOS_BUCKET"
          value = google_storage_bucket.final_videos.name
        }
        env {
          name  = "GCS_ASSETS_BUCKET"
          value = google_storage_bucket.session_assets.name
        }
        env {
          name  = "GOOGLE_CLOUD_LOCATION"
          value = var.region
        }
        env {
          name  = "GOOGLE_GENAI_USE_VERTEXAI"
          value = var.google_genai_use_vertexai ? "TRUE" : "FALSE"
        }
        env {
          name  = "ENABLE_PROMPT_META_LEARNING"
          value = "1"
        }
        env {
          name  = "PROMPT_META_LEARNING_CACHE_SECONDS"
          value = "120"
        }
        env {
          name  = "PROMPT_META_LEARNING_MAX_PRINCIPLES"
          value = "5"
        }
        env {
          name  = "PROMPT_META_LEARNING_PRINCIPLES_COLLECTION"
          value = "prompt_learning_principles"
        }
        env {
          name  = "PROMPT_META_LEARNING_FEEDBACK_COLLECTION"
          value = "prompt_learning_feedback"
        }
        env {
          name  = "PROMPT_META_LEARNING_ROLLUPS_COLLECTION"
          value = "prompt_learning_issue_rollups"
        }
        env {
          name  = "ENABLE_VEO_FINAL"
          value = "0"
        }
        env {
          name  = "VEO_FINAL_SCENE_COUNT"
          value = "1"
        }
        env {
          name  = "VEO_MAGIC_MIN_SCORE"
          value = "2"
        }
        env {
          name  = "FINAL_SCENE_SECONDS"
          value = "4"
        }
        env {
          name  = "ENABLE_FAST_STORYBOOK_ASSEMBLY"
          value = tostring(var.enable_fast_storybook_assembly)
        }
        env {
          name  = "ENABLE_STORYBOOK_TTS"
          value = "1"
        }
        env {
          name  = "ENABLE_STORYBOOK_SCENE_REVIEW"
          value = "1"
        }
        env {
          name  = "ENABLE_STORYBOOK_DIRECTOR_WORKFLOW"
          value = var.enable_storybook_director_workflow ? "1" : "0"
        }
        env {
          name  = "STORYBOOK_SCENE_REVIEW_MODEL"
          value = "gemini-2.5-flash"
        }
        env {
          name  = "STORYBOOK_SCENE_REVIEW_MAX_PASSES"
          value = tostring(var.storybook_scene_review_max_passes)
        }
        env {
          name  = "STORYBOOK_SCENE_REVIEW_MAX_FIXES"
          value = "3"
        }
        env {
          name  = "ENABLE_STORYBOOK_STUDIO_WORKFLOW"
          value = var.enable_storybook_studio_workflow ? "1" : "0"
        }
        env {
          name  = "STORYBOOK_STUDIO_MAX_REVISIONS"
          value = tostring(var.storybook_studio_max_revisions)
        }
        env {
          name  = "STORYBOOK_SCENE_IMAGE_MODEL"
          value = "gemini-2.0-flash-preview-image-generation"
        }
        env {
          name  = "STORYBOOK_SCENE_IMAGE_SIZE"
          value = "512px"
        }
        env {
          name  = "ENABLE_STORYBOOK_CAPTIONS"
          value = "1"
        }
        env {
          name  = "ENABLE_STORYBOOK_COVER"
          value = "1"
        }
        env {
          name  = "STORYBOOK_COVER_SECONDS"
          value = "3.5"
        }
        env {
          name  = "ENABLE_STORYBOOK_COVER_IMAGE"
          value = "1"
        }
        env {
          name  = "STORYBOOK_COVER_IMAGE_MODEL"
          value = "gemini-2.0-flash-preview-image-generation"
        }
        env {
          name  = "ENABLE_STORYBOOK_TITLE_LLM"
          value = "1"
        }
        env {
          name  = "STORYBOOK_TITLE_MODEL"
          value = "gemini-2.5-flash"
        }
        env {
          name  = "STORYBOOK_TITLE"
          value = ""
        }
        env {
          name  = "STORYBOOK_TTS_LANG"
          value = "en-US"
        }
        env {
          name  = "STORYBOOK_TTS_VOICE"
          value = "en-US-Neural2-F"
        }
        env {
          name  = "STORYBOOK_TTS_RATE"
          value = "0.9"
        }
        env {
          name  = "STORYBOOK_TTS_PITCH"
          value = "0.0"
        }
        env {
          name  = "FORCE_STORYBOOK_TTS"
          value = "1"
        }
        env {
          name  = "ENABLE_STORYBOOK_MUSIC"
          value = "1"
        }
        env {
          name  = "STORYBOOK_MUSIC_PROVIDER"
          value = "auto"
        }
        env {
          name  = "STORYBOOK_MUSIC_VOLUME"
          value = "0.08"
        }
        env {
          name  = "STORYBOOK_MUSIC_CUE_SECONDS"
          value = "10"
        }
        env {
          name  = "STORYBOOK_MUSIC_MAX"
          value = "2"
        }
        env {
          name  = "ENABLE_STORYBOOK_SFX"
          value = "1"
        }
        env {
          name  = "STORYBOOK_SFX_PROVIDER"
          value = "elevenlabs"
        }
        env {
          name  = "STORYBOOK_SFX_VOLUME"
          value = "0.12"
        }
        env {
          name  = "STORYBOOK_SFX_CUE_SECONDS"
          value = "2"
        }
        env {
          name  = "STORYBOOK_SFX_MAX"
          value = "2"
        }
        env {
          name  = "STORYBOOK_SFX_MIN_SCORE"
          value = "2"
        }
        env {
          name  = "STORYBOOK_SFX_COOLDOWN"
          value = "1"
        }
        env {
          name  = "STORYBOOK_NARRATION_VOLUME"
          value = "2.2"
        }
        env {
          name  = "ENABLE_STORYBOOK_END_CARD"
          value = "1"
        }
        env {
          name  = "STORYBOOK_END_CARD_SECONDS"
          value = "2.8"
        }
        env {
          name  = "STORYBOOK_END_CARD_TITLE"
          value = "The End"
        }
        env {
          name  = "STORYBOOK_END_CARD_NARRATION"
          value = "And that is the happy end of our story."
        }
        env {
          name  = "ENABLE_STORYBOOK_DUCKING"
          value = "1"
        }
        env {
          name  = "ENABLE_STORYBOOK_AUDIO_MASTERING"
          value = "1"
        }
        env {
          name  = "ENABLE_STORYBOOK_AUDIO_LLM"
          value = "0"
        }
        env {
          name  = "ENABLE_STORYBOOK_AUDIO_LLM_IMAGES"
          value = "0"
        }
        env {
          name  = "STORYBOOK_AUDIO_MODEL"
          value = "gemini-2.5-flash"
        }
        env {
          name  = "ELEVENLABS_MUSIC_ENDPOINT"
          value = "https://api.elevenlabs.io/v1/music"
        }
        env {
          name  = "ELEVENLABS_SOUND_ENDPOINT"
          value = "https://api.elevenlabs.io/v1/sound-generation"
        }
        env {
          name  = "ELEVENLABS_VOICE_ID"
          value = var.elevenlabs_voice_id
        }
        env {
          name  = "ELEVENLABS_TTS_MODEL"
          value = var.elevenlabs_tts_model
        }
        env {
          name  = "ELEVENLABS_TTS_ENDPOINT"
          value = var.elevenlabs_tts_endpoint
        }
        env {
          name = "GOOGLE_API_KEY"
          value_source {
            secret_key_ref {
              secret  = google_secret_manager_secret.google_api_key.secret_id
              version = "latest"
            }
          }
        }
        env {
          name = "ELEVENLABS_API_KEY"
          value_source {
            secret_key_ref {
              secret  = google_secret_manager_secret.elevenlabs_api_key.secret_id
              version = "latest"
            }
          }
        }
      }
    }
  }
}

# ── Global HTTP(S) Load Balancer — single domain, no CORS (Iter 9 #1) ─────────
resource "google_compute_global_address" "default" {
  name = "storyteller-ip"
}

resource "google_compute_managed_ssl_certificate" "default" {
  name = "storyteller-ssl"
  managed {
    domains = [var.domain_name]
  }
}

resource "google_compute_backend_service" "backend_cr" {
  name                  = "storyteller-backend-cr"
  load_balancing_scheme = "EXTERNAL_MANAGED"
  protocol              = "HTTP"
  timeout_sec           = 30

  backend {
    group = google_compute_region_network_endpoint_group.backend_neg.id
  }
}

resource "google_compute_backend_service" "frontend_cr" {
  name                  = "storyteller-frontend-cr"
  load_balancing_scheme = "EXTERNAL_MANAGED"
  protocol              = "HTTP"
  timeout_sec           = 30

  backend {
    group = google_compute_region_network_endpoint_group.frontend_neg.id
  }
}

resource "google_compute_region_network_endpoint_group" "backend_neg" {
  name                  = "storyteller-backend-neg"
  network_endpoint_type = "SERVERLESS"
  region                = var.region

  cloud_run {
    service = google_cloud_run_v2_service.backend.name
  }
}

resource "google_compute_region_network_endpoint_group" "frontend_neg" {
  name                  = "storyteller-frontend-neg"
  network_endpoint_type = "SERVERLESS"
  region                = var.region

  cloud_run {
    service = google_cloud_run_v2_service.frontend.name
  }
}

resource "google_compute_url_map" "default" {
  name            = "storyteller-urlmap"
  default_service = google_compute_backend_service.frontend_cr.id

  host_rule {
    hosts        = [var.domain_name]
    path_matcher = "allpaths"
  }

  path_matcher {
    name            = "allpaths"
    default_service = google_compute_backend_service.frontend_cr.id

    path_rule {
      paths   = ["/api/*", "/ws/*", "/health"]
      service = google_compute_backend_service.backend_cr.id
    }
  }
}

resource "google_compute_target_https_proxy" "default" {
  name             = "storyteller-https-proxy"
  url_map          = google_compute_url_map.default.id
  ssl_certificates = [google_compute_managed_ssl_certificate.default.id]
}

resource "google_compute_global_forwarding_rule" "default" {
  name                  = "storyteller-forwarding-rule"
  load_balancing_scheme = "EXTERNAL_MANAGED"
  ip_address            = google_compute_global_address.default.address
  port_range            = "443"
  target                = google_compute_target_https_proxy.default.id
}
