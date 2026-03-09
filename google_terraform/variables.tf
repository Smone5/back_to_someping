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
  description = "Hard cap on story turns per session (backend enforces 6-20)."
  type        = number
  default     = 20
}

variable "disable_barge_in" {
  description = "Disable barge-in so background noise cannot interrupt Amelia."
  type        = bool
  default     = false
}

variable "barge_in_suppress_seconds" {
  description = "How long (seconds) to suppress mic input after Amelia speaks."
  type        = number
  default     = 0.25
}

variable "local_storybook_mode" {
  description = "If true, assemble story videos locally in the backend (skip Cloud Run Job)."
  type        = bool
  default     = false
}

variable "enable_fast_storybook_assembly" {
  description = "If true, run storybook assembly inside the backend service instead of the FFmpeg Cloud Run Job."
  type        = bool
  default     = false
}

variable "enable_client_direct_live" {
  description = "Enable experimental client-direct Gemini Live ephemeral tokens from the backend."
  type        = bool
  default     = false
}

variable "google_genai_use_vertexai" {
  description = "Use Vertex AI for backend Gemini/ADK runtime instead of AI Studio."
  type        = bool
  default     = true
}

variable "storyteller_live_model" {
  description = "Gemini Live model id for the backend storyteller runtime."
  type        = string
  default     = "gemini-live-2.5-flash-native-audio"
}

variable "interactive_scene_image_model" {
  description = "Gemini image model used by the live interactive scene generator."
  type        = string
  default     = "gemini-2.5-flash-image"
}

variable "client_direct_live_model" {
  description = "Gemini Live model for experimental client-direct audio."
  type        = string
  default     = "gemini-2.5-flash-native-audio-preview-12-2025"
}

variable "client_direct_live_voice" {
  description = "Gemini Live prebuilt voice name for experimental client-direct audio."
  type        = string
  default     = "Aoede"
}

variable "client_direct_live_temperature" {
  description = "Temperature for experimental client-direct Gemini Live sessions."
  type        = number
  default     = 0.82
}

variable "client_direct_live_top_p" {
  description = "Top-p for experimental client-direct Gemini Live sessions."
  type        = number
  default     = 0.9
}

variable "client_direct_live_expire_minutes" {
  description = "Ephemeral token lifetime in minutes for client-direct Gemini Live."
  type        = number
  default     = 30
}

variable "client_direct_live_new_session_minutes" {
  description = "How long a freshly minted token may be used to start a new session."
  type        = number
  default     = 1
}

variable "enable_storybook_director_workflow" {
  description = "Enable the heavier storyboard director review/repair workflow in the FFmpeg job."
  type        = bool
  default     = false
}

variable "enable_storybook_studio_workflow" {
  description = "Enable the storybook studio planning workflow in the FFmpeg job."
  type        = bool
  default     = true
}

variable "storybook_scene_review_max_passes" {
  description = "Maximum storyboard review passes when director workflow is enabled."
  type        = number
  default     = 1
}

variable "storybook_studio_max_revisions" {
  description = "Maximum studio revision rounds for FFmpeg job planning."
  type        = number
  default     = 0
}

variable "elevenlabs_voice_id" {
  description = "ElevenLabs voice ID for TTS narration."
  type        = string
  default     = "JBFqnCBsd6RMkjVDRZzb"
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
