# ── Terraform Variables for Interactive Storyteller ──────────────────────────

variable "project_id" {
  description = "Your Google Cloud Project ID."
  type        = string
}

variable "region" {
  description = "Primary deployment region."
  type        = string
  default     = "us-central1"
}

variable "domain_name" {
  description = "The custom domain for the Global Load Balancer (e.g. storyteller.example.com)."
  type        = string
}

variable "backend_image" {
  description = "Docker image URI for the FastAPI backend (pushed to Artifact Registry)."
  type        = string
}

variable "frontend_image" {
  description = "Docker image URI for the Next.js frontend."
  type        = string
}

variable "ffmpeg_image" {
  description = "Docker image URI for the FFmpeg Cloud Run Job."
  type        = string
}

variable "max_backend_instances" {
  description = "Hard cap on backend Cloud Run instances (billing ceiling)."
  type        = number
  default     = 50
}

variable "max_frontend_instances" {
  description = "Hard cap on frontend Cloud Run instances."
  type        = number
  default     = 50
}

variable "max_live_sessions" {
  description = "Maximum concurrent live storyteller sessions admitted by backend."
  type        = number
  default     = 50
}

variable "enable_elevenlabs_mcp" {
  description = "Enable ElevenLabs MCP toolset in backend runtime."
  type        = bool
  default     = false
}

variable "max_story_turns" {
  description = "Hard cap on story turns per session (backend enforces 6-14)."
  type        = number
  default     = 10
}

variable "disable_barge_in" {
  description = "Disable barge-in so background noise cannot interrupt Amelia."
  type        = bool
  default     = true
}

variable "barge_in_suppress_seconds" {
  description = "How long (seconds) to suppress mic input after Amelia speaks."
  type        = number
  default     = 1.2
}

variable "local_storybook_mode" {
  description = "If true, assemble story videos locally in the backend (skip Cloud Run Job)."
  type        = bool
  default     = false
}

variable "elevenlabs_voice_id" {
  description = "ElevenLabs voice ID for TTS narration."
  type        = string
  default     = ""
}

variable "elevenlabs_tts_model" {
  description = "ElevenLabs TTS model id (e.g. eleven_multilingual_v2)."
  type        = string
  default     = "eleven_multilingual_v2"
}

variable "elevenlabs_tts_endpoint" {
  description = "Optional override for ElevenLabs TTS endpoint; leave empty to use voice id."
  type        = string
  default     = ""
}
