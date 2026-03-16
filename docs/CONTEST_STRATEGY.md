# Google Cloud Next 2026 Contest — ADAI Beta Strategy

**Goal:** Win a category (Live Agent, Creative Storyteller, or UI Navigator) or a subcategory (Best Multimodal Integration & UX, Best Technical Execution & Agent Architecture, Best Innovation & Thought Leadership) using the existing ADAI Beta codebase: Terraform, ADK, backend, and frontend.

**Contest reference:** `google-prog/docs/awards.txt`

---

## 1. Category Eligibility Summary

| Category | Eligibility | ADAI fit | Recommendation |
|---------|-------------|----------|----------------|
| **Live Agent** | Gemini Live API **or** ADK, agents on Google Cloud | ✅ **Strong** — You use **both** Gemini Live and ADK, all on GCP | **Primary entry** |
| **Creative Storyteller** | Gemini interleaved/mixed output, on Google Cloud | ✅ **Strong** — Narrative AD generation + TTS is interleaved creative output | **Secondary entry** |
| **UI Navigator** | Multimodal screenshots/screen recordings → executable actions, on Google Cloud | ⚠️ **Weak** — Would require new feature (e.g. “drive UI from screen”) | Skip or future |

---

## 2. Primary Entry: Live Agent Category

**Eligibility:** *"Uses Gemini Live API or the use of ADK. The agents are hosted on Google Cloud."*

### What you already have (proof points)

- **ADK (Google Agent Development Kit)**  
  - Full video-analysis pipeline in `agents/beta_agent/`:
    - `Agent`, `Runner`, `BuiltInPlanner`, `CallbackContext`, `InvocationContext`
    - Multi-agent flow: scene analysis, critic, refinement, finalization
    - ADK artifacts for video input/output, session state
    - Tools and function calling
  - Entry: `agents/beta_agent/agent.py`, `agents/beta_agent/main.py`

- **Gemini Live API**  
  - Backend: `backend/routers/gemini_live.py` — WebSocket proxy to Vertex AI Multimodal Live (live-proxy).
  - Frontend: `useGeminiLive.ts`, `EnhancedVideoPlayer.tsx`, `ImmersiveVideoPlayer.tsx` — “Ask Gemini” while watching video: voice in, real-time spoken answers about what’s on screen.
  - Account: Live Voice settings (voice/language) in `account/page.tsx` (tab `gemini-live`).

- **Hosted on Google Cloud**  
  - **Terraform:** `terraform/beta_agent_cloud_run.tf` — Cloud Run **service** + **job** for the ADK agent; `terraform/cloud_run_service.tf` — FastAPI backend (includes Gemini Live proxy); `terraform/api_gateway.tf` — API Gateway in front.
  - **ADK pipeline:** Triggered via Cloud Run Job (`beta-agent-job-dev1`), invoked from backend/Cloud Functions (e.g. `terraform/agent_trigger_src/`).
  - **Backend (Gemini Live):** Runs on same FastAPI Cloud Run service; RAG/Vertex RAG supported (`GEMINI_LIVE_VERTEX_RAG_CORPORA` in `cloud_run_service.tf`).

### Submission narrative (Live Agent)

- **One-sentence:** “ADAI is a Live Agent that uses **ADK** for automated video-to–audio-description pipelines and **Gemini Live API** for real-time voice Q&A over the same video, all running on **Google Cloud** (Cloud Run, API Gateway, Vertex AI).”
- **Demo flow:**  
  1) Upload a video → ADK pipeline (Cloud Run Job) produces professional audio description.  
  2) Play the video in the app → tap “Gemini Live” → ask in voice “What’s happening on screen?” / “Describe the person in the red shirt” → get real-time spoken answers (Gemini Live).  
- **Differentiator:** Combines **batch ADK agents** (accessibility pipeline) with **real-time Gemini Live** (in-context Q&A) on one platform, full GCP stack.

### Quick wins to strengthen Live Agent entry

1. **Short video (2–3 min)** showing: upload → pipeline run (mention ADK + Cloud Run) → play back → enable Live → ask 2–3 voice questions → spoken answers.
2. **1-page architecture diagram:** User → API Gateway → FastAPI (Cloud Run) [Gemini Live proxy] | Backend triggers Cloud Run Job → ADK Runner → multi-agent pipeline → artifacts; Vertex AI / Gemini throughout.
3. **In submission text:** Explicitly name “Google ADK” and “Gemini Live API (Multimodal Live)” and “Cloud Run Job / Cloud Run Service.”

---

## 3. Secondary Entry: Creative Storyteller Category

**Eligibility:** *"Use Gemini's interleaved/mixed output capabilities. The agents are hosted on Google Cloud."*

### What you already have

- **Interleaved/mixed output:**  
  - Pipeline produces **text** (scene scripts, critic feedback, refinements) and **audio** (TTS) in a single workflow.  
  - Gemini models generate narrative text; Cloud TTS turns it into speech; final output is mixed (description + video).  
  - Frontend presents “story” as synchronized narration over video (Standard/Extended/Hybrid modes).

- **Creative storytelling:**  
  - Professional **audio description** as a narrative form: who appears, what they do, setting, on-screen text — all in natural language and prosody.

- **Hosted on Google Cloud:** Same as above (Cloud Run, Vertex AI, Terraform).

### Submission narrative (Creative Storyteller)

- “ADAI uses Gemini’s **interleaved/mixed output** (text + audio) to generate and refine **creative narrative** — professional audio description — from video. Scripts and TTS are produced in one pipeline on Google Cloud.”
- **Demo:** Same as Live Agent; emphasize “narrative generation,” “interleaved text and speech,” “multi-scene story.”

### Quick win

- In the submission form, add 1–2 sentences: “We use Gemini for interleaved text (scene scripts, refinements) and Google Cloud TTS for synchronized audio output, producing a single narrative stream (audio description) from video.”

---

## 4. Subcategory Targeting

Use these to strengthen **either** Live Agent or Creative Storyteller submission.

| Subcategory | How ADAI fits | Where to show it |
|------------|----------------|-------------------|
| **Best Multimodal Integration & User Experience** | Video + voice input (Live), video + text + audio output (pipeline + player), one cohesive UX | Demo: watch video + Live Q&A + final AD playback in one flow |
| **Best Technical Execution & Agent Architecture** | ADK multi-agent pipeline, Terraform (Cloud Run Job/Service, API Gateway, IAM), clear separation of Live vs batch | Architecture diagram; point to `agents/beta_agent/`, `terraform/` |
| **Best Innovation & Thought Leadership** | Accessibility (WCAG, ADA), real-time + batch in one product, human-in-the-loop (refinement/critic) | Narrative in submission; link to compliance/accessibility messaging |

---

## 5. What to Create / Prepare

| Item | Purpose | Location / action |
|------|---------|-------------------|
| **Contest submission text** | One clear narrative per category (Live Agent primary, Creative Storyteller optional) | Draft in `google-prog/docs/submission_live_agent.md` (and optional `submission_creative_storyteller.md`) |
| **2–3 min demo video** | Show upload → ADK pipeline → playback + Gemini Live Q&A | Record and host; link in submission |
| **Architecture diagram** | One slide: User → API Gateway → Cloud Run (FastAPI + Live proxy); Cloud Run Job (ADK); Vertex AI / Gemini | Add to `google-prog/docs/` or repo `docs/` |
| **Proof-point checklist** | So you don’t forget to mention ADK, Gemini Live, Cloud Run, Terraform | Next section |

---

## 6. Submission Checklist (for judges)

Before submitting, confirm you explicitly mention:

- [ ] **ADK** — “Video analysis pipeline built with Google Agent Development Kit (ADK): multi-agent orchestration, artifacts, tools.”
- [ ] **Gemini Live API** — “Real-time voice Q&A over video via Gemini Live API (Multimodal Live), proxied through our FastAPI backend on Cloud Run.”
- [ ] **Google Cloud hosting** — “All agents and APIs run on Google Cloud: Cloud Run (FastAPI backend + ADK Cloud Run Job), API Gateway, Vertex AI, Terraform-managed.”
- [ ] **Differentiator** — “Combines batch ADK pipeline (accessibility) with real-time Gemini Live (in-context voice Q&A) in one product.”

---

## 7. UI Navigator (optional / future)

- **Eligibility:** Multimodal interpretation of screenshots/screen recordings → **executable actions** (e.g. UI automation).
- **Current gap:** ADAI does not “output executable actions” for a UI. To compete here you’d need something like: “User shares screen/screenshot → Gemini interprets → outputs actions (e.g. click, type) executed in browser or IDE.”
- **Recommendation:** Don’t split focus for this contest. Enter **Live Agent** (primary) and **Creative Storyteller** (secondary); consider UI Navigator for a later iteration if you add a dedicated UI-automation feature.

---

## 8. Repo Quick Reference (for submission prep)

| What | Where |
|------|--------|
| ADK agents & runner | `agents/beta_agent/agent.py`, `agents/beta_agent/main.py` |
| Gemini Live backend | `backend/routers/gemini_live.py` |
| Gemini Live frontend | `frontend/adai-beta/src/hooks/useGeminiLive.ts`, `EnhancedVideoPlayer.tsx`, `ImmersiveVideoPlayer.tsx` |
| Cloud Run (backend) | `terraform/cloud_run_service.tf` |
| Cloud Run (ADK job) | `terraform/beta_agent_cloud_run.tf` |
| API Gateway | `terraform/api_gateway.tf` |
| Agent trigger (start job) | `terraform/agent_trigger_src/main.py`, `terraform/video_processing.tf` |

---

**Next step:** Create `submission_live_agent.md` with the actual submission paragraph and demo link placeholders, then record the demo video and add the architecture diagram.
