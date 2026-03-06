# 🧙‍♀️ Interactive Storyteller — AI Magical Storybook for Kids

> Real-time AI storytelling for children aged 4–5. The child speaks their choices aloud; the AI narrates, generates animated scenes, and assembles a personalized movie — all in under 4 minutes.

**Google Cloud AI Contest Entry · Open Source · Apache 2.0**

---

## ✨ What It Does

A child taps one glowing button. Amelia, the narrator, begins a magical story. The child shouts choices out loud — *"A dragon! Named Fluffy!"* — and the AI:

- Generates a **scene illustration** (Nano Banana 2)
- Clips it into a **5-second animated video** (Veo 3.1)
- Plays each character's **unique voice** (ElevenLabs MCP)
- Remembers characters **across sessions** (Cloud Firestore)
- Syncs your **room lights** to the story mood (Home Assistant MCP)
- Assembles a **personal movie** the child can keep (FFmpeg Cloud Run Job)

### Core Tech Stack

| Layer | Technology |
|---|---|
| AI Agent | Google ADK + Gemini 2.5 Flash Bidi-streaming (native audio) |
| Scene Visuals | Nano Banana 2 → Veo 3.1 |
| Character Voices | ElevenLabs MCP (`text_to_speech`, `generate_sfx`) |
| Session Memory | Cloud Firestore (cross-session lore) |
| IoT Lights | Home Assistant MCP (optional) |
| Backend | FastAPI + Python 3.11 |
| Frontend | Next.js 15 + React 19 |
| Infrastructure | Terraform (GCS, Cloud Run, Secret Manager, Load Balancer) |

---

## 🏗️ Architecture

```
[Browser — Next.js / React]
        │  16kHz PCM (downsampled AudioWorklet)
        │  JSON control events
        ▼
[Cloud Run — FastAPI + ADK Runner]
        │
        ├─ Gemini 2.5 Flash (Bidi-stream, native audio)
        ├─ ElevenLabs MCP (voices + Foley SFX)
        ├─ Home Assistant MCP (IoT room lights — optional)
        ├─ Nano Banana 2 + Veo 3.1 (async scene generation)
        └─ Cloud Firestore (lore memory)
        │
        ▼
[Cloud Run Job — FFmpeg]  →  [Cloud Storage]  →  [Theater Mode]
```

A single Global HTTPS Load Balancer routes `/api/*` and `/ws/*` → backend, `/*` → frontend — **no CORS needed**.

---

## 🚀 Local Development Setup

### Prerequisites

| Tool | Version | Install |
|---|---|---|
| Python | 3.11+ | `pyenv install 3.11.12` |
| Node.js | 18+ | `brew install node` |
| Google Cloud SDK | latest | [cloud.google.com/sdk](https://cloud.google.com/sdk/docs/install) |
| Terraform | 1.5+ | `brew install terraform` |

### 1. Clone & Configure

```bash
git clone https://github.com/<your-org>/interactive-storyteller.git
cd interactive-storyteller/google-prog

# Copy the environment template
cp .env.example .env
# → Edit .env and fill in your API keys (see "Environment Variables" below)
```

### 2. Provision Cloud Infrastructure (one-time)

This creates your GCS buckets, Secret Manager secrets, and IAM — you need this even for local dev since the backend uses real GCS.

```bash
# Authenticate
gcloud auth login
gcloud auth application-default login
gcloud config set project YOUR_PROJECT_ID

# Enable required APIs
gcloud services enable storage.googleapis.com secretmanager.googleapis.com \
  run.googleapis.com firestore.googleapis.com iam.googleapis.com \
  cloudresourcemanager.googleapis.com compute.googleapis.com

# Provision infra (GCS + Secrets + IAM only — skips Cloud Run for local dev)
cd google_terraform
terraform init
terraform apply \
  -target=google_storage_bucket.session_assets \
  -target=google_storage_bucket.final_videos \
  -target=google_secret_manager_secret.google_api_key \
  -target=google_secret_manager_secret.elevenlabs_api_key \
  -target=google_service_account.backend_sa \
  -target=google_project_iam_member.backend_storage \
  -target=google_project_iam_member.backend_vertex_ai \
  -auto-approve

# Upload your API keys to Secret Manager
echo -n "YOUR_GEMINI_KEY" | gcloud secrets versions add storyteller-google-api-key --data-file=-
echo -n "YOUR_ELEVENLABS_KEY" | gcloud secrets versions add storyteller-elevenlabs-api-key --data-file=-

cd ..
```

### 3. Start the Backend

```bash
# From google-prog/ — IMPORTANT: run from this directory, NOT from backend/
cd /path/to/interactive-storyteller/google-prog

python -m venv backend/.venv
source backend/.venv/bin/activate
pip install -r backend/requirements.txt

uvicorn backend.main:app --reload --port 8000
# → http://127.0.0.1:8000/health should return {"status": "ok"}
```

### 4. Start the Frontend (separate terminal)

```bash
cd /path/to/interactive-storyteller/google-prog/frontend

npm install          # first time only
npm run dev
# → http://localhost:3000
```

### 5. (Optional) Test the Agent Directly

```bash
source backend/.venv/bin/activate
cd /path/to/interactive-storyteller/google-prog

adk web --agent-module agent --port 8080
# → http://localhost:8080  (ADK dev UI with voice)
```

---

## 🔑 Environment Variables

Copy `.env.example` to `.env` and fill in:

| Variable | Required | Description |
|---|---|---|
| `GOOGLE_API_KEY` | ✅ | Gemini API key (AI Studio or Vertex AI) |
| `GOOGLE_CLOUD_PROJECT` | ✅ | Your GCP project ID |
| `GOOGLE_CLOUD_LOCATION` | ✅ | e.g. `us-central1` |
| `ELEVENLABS_API_KEY` | ✅ | ElevenLabs API key |
| `GCS_ASSETS_BUCKET` | ✅ | Set by Terraform: `{project}-storyteller-session-assets` |
| `GCS_FINAL_VIDEOS_BUCKET` | ✅ | Set by Terraform: `{project}-storyteller-final-videos` |
| `FFMPEG_JOB_NAME` | ✅ | `storyteller-ffmpeg-assembler` |
| `MAX_STORY_TURNS` | ☑️ optional | Hard cap per story session (backend clamps to 6–14, default 10) |
| `HOME_ASSISTANT_URL` | ☑️ optional | e.g. `http://homeassistant.local:8123` |
| `HOME_ASSISTANT_TOKEN` | ☑️ optional | Long-lived access token from HA |
| `HA_LIGHT_ENTITY` | ☑️ optional | e.g. `light.living_room` |
| `FRONTEND_ORIGIN` | dev | `http://localhost:3000` |

> **In production**, all secrets are mounted from Google Secret Manager via Terraform — never hardcoded.

---

## ☁️ Full Production Deployment

You can deploy all services at once or individually:

```bash
# Option A: Deploy everything (Backend + Frontend + FFmpeg)
./deploy.sh

# Option B: Deploy individual services
./deploy-backend.sh
./deploy-frontend.sh
./deploy-ffmpeg.sh
```

> **Note:** These scripts build the Docker images, push them to GCR, and deploy to Cloud Run (or update the Cloud Run Job for FFmpeg).

---

## 💰 Estimated Cost Per Story Session (~4 min)

| Service | Est. Cost |
|---|---|
| Gemini 2.5 Flash Bidi-streaming | ~$0.01 |
| Nano Banana 2 × 3 images | ~$0.06 |
| Veo 3.1 × 3 × 5s clips | ~$0.60 |
| ElevenLabs character voices | ~$0.01 |
| Cloud Run + Storage | ~$0.006 |
| **Total** | **~$0.67/session** |

---

## 🔒 Privacy & COPPA Compliance

- **Zero audio storage** — mic audio streams to Gemini and is discarded
- **Parental gate** — math challenge required before mic access
- **PII redaction** — real names/addresses scrubbed before any logging
- **Right-to-be-forgotten** — `DELETE /api/purge-my-data` cascades across GCS + Firestore
- **24-hour TTL** — all session assets auto-deleted via GCS lifecycle rules

---

## 📁 Project Structure

```
google-prog/
├── agent/                  # ADK Agent definition, tools, prompts, state machine
│   ├── storyteller_agent.py
│   ├── tools.py            # Veo, Nano Banana, ElevenLabs, GCS, Firestore tools
│   └── prompts.py          # System prompt
├── backend/                # FastAPI server
│   ├── main.py             # App entry point, ADK Runner
│   ├── ws_router.py        # WebSocket handler (Bidi-stream bridge)
│   ├── audio.py            # AudioWorklet downsampler
│   ├── schemas.py          # Pydantic models
│   ├── requirements.txt
│   └── ffmpeg_worker/      # Cloud Run Job (Docker)
├── frontend/               # Next.js 15 / React 19 UI
│   ├── src/
│   │   ├── app/            # Next.js app router pages
│   │   ├── components/     # StorytellerLive, ParentGate, MagicMirror, Theater...
│   │   └── hooks/          # useWebSocket, useMicrophone, useSfxDucker
│   └── package.json
├── google_terraform/       # Full GCP infrastructure as code
│   ├── main.tf             # Cloud Run services, Load Balancer
│   ├── storage.tf          # GCS buckets with lifecycle rules
│   ├── secrets.tf          # Secret Manager + Firestore
│   ├── iam.tf              # Service accounts + IAM bindings
│   └── variables.tf
├── .env.example            # Environment variable template
└── README.md
```

---

## 🤝 Contributing

Pull requests welcome. Please open an issue first to discuss what you'd like to change.

## 📄 License

Apache 2.0 — see `LICENSE` for details.
