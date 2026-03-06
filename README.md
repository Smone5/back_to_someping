# 🧙‍♀️ Back to Somping, Back to Dody Land — AI Storyteller
> **Real-time AI storytelling for children aged 4–5.** The child speaks their choices aloud; the AI narrates with emotion, generates animated scenes, and assembles a personalized movie.

**Google Cloud AI Contest Entry · Open Source · Apache 2.0**

---

## ✨ What It Does

A child taps one glowing button. Amelia, our magical narrator, begins an adventure. The child shouts choices out loud — *"A dragon! Named Fluffy!"* — and the AI:

- **Listens & Responds**: Uses **Gemini 1.5 Flash (Bidi-streaming)** to hear the child even while music plays (Barge-in).
- **Illustrates**: Generates high-quality **scene illustrations** (Nano Banana / Gemini 2.0 Flash).
- **Animates**: Turns images into **5-second cinematic clips** (Veo 3.1).
- **Narrates**: Unique character voices and Foley effects via **ElevenLabs MCP**.
- **Remembers**: Persists characters and lore **across sessions** via **Cloud Firestore**.
- **Atmosphere**: Syncs room lights to the story's mood (Home Assistant MCP).
- **Assembles**: Compiles a **personal MP4 movie** via a **Cloud Run Job (FFmpeg)**.

### Core Tech Stack

| Layer | Technology |
|---|---|
| AI Agent | Google ADK + Gemini 1.5 Flash (native audio) |
| Scene Visuals | Gemini 2.0 Flash (Nano Banana) → Veo 3.1 |
| Character Voices | ElevenLabs MCP (`text_to_speech`, `generate_sfx`) |
| Session Memory | Cloud Firestore (cross-session lore) |
| IoT Lights | Home Assistant MCP (optional) |
| Backend | FastAPI + Python 3.11 (always-on Cloud Run) |
| Frontend | Next.js 15 + React 19 |
| Infrastructure | Terraform (GCS, Cloud Run, Secret Manager) |

---

## 🏗️ Architecture

```
[Browser — Next.js / React]
        │  16kHz PCM (downsampled AudioWorklet)
        │  JSON control events
        ▼
[Cloud Run — FastAPI + ADK Runner]
        │
        ├─ Gemini 1.25/1.5 Flash (Bidi-stream, native audio)
        ├─ ElevenLabs MCP (Character Voices + SFX)
        ├─ Home Assistant MCP (IoT room lights — optional)
        ├─ Nano Banana + Veo 3.1 (Async visual pipeline)
        └─ Cloud Firestore (Lore & session memory)
        │
        ▼
[Cloud Run Job — FFmpeg]  →  [Cloud Storage]  →  [Theater Mode]
```

---

## 🚀 Local Development Setup

### 1. Clone & Configure
```bash
git clone https://github.com/Smone5/back_to_someping.git
cd back_to_someping
cp .env.example .env
# → Fill in your API keys in .env
```

### 2. Provision Infrastructure (One-time)
```bash
cd google_terraform
terraform init
terraform apply -auto-approve
```

### 3. Start Services
- **Backend**: `uvicorn backend.main:app --reload`
- **Frontend**: `npm run dev`

---

## 🔑 Key Features for Judges

1. **Zero-Latency Bidi-Streaming**: The agent uses Gemini's native audio model to provide instantaneous, emotional feedback to the child.
2. **ADK Multi-Agent Pipeline**: Sophisticated orchestration of image generation, video clipping, and TTS refinement.
3. **Hybrid Assembly**: Support for both high-memory Cloud Run FFmpeg jobs and "Fast Storybook Assembly" for immediate local review.
4. **Parental Privacy**: Built-in parental math gates and zero-storage audio policies.

---

## 📁 Project Structure

- `agent/`: ADK Agent definitions, prompts, and toolsets.
- `backend/`: FastAPI server and WebSocket bidi-streaming bridge.
- `frontend/`: Next.js UI including the "Magic Orb" and Immersive Theater.
- `google_terraform/`: Full GCP infrastructure as code.
- `shared/`: Shared logic for meta-learning (storytelling principles).
- `deploy.sh`: One-click build and deploy script.

---

## 📄 License
Apache 2.0 — see `LICENSE` for details.
