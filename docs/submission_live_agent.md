# Live Agent Category — Submission Draft

**Use this text (edit as needed) when filling out the contest submission form for the Live Agent category.**

---

## Project name
ADAI — Audio Description AI

---

## Category
Live Agent (Gemini Live API + ADK, hosted on Google Cloud)

---

## Short description (for listing / judges)

ADAI is a live agent platform that combines **Google ADK** (Agent Development Kit) and the **Gemini Live API** on Google Cloud. Our ADK pipeline turns video into professional audio descriptions for accessibility; our Gemini Live integration lets users ask questions about the video in real time with their voice and get instant spoken answers. All agents and APIs run on Google Cloud (Cloud Run, Vertex AI, API Gateway), with infrastructure defined in Terraform.

---

## Full description (paste into submission)

ADAI uses two agent patterns on Google Cloud to deliver accessibility and in-context Q&A:

**1. ADK pipeline (batch agent)**  
Our video-to–audio-description pipeline is built with the **Google Agent Development Kit (ADK)**. A multi-agent graph (scene analysis, critic, refinement, finalization) runs as a **Cloud Run Job**, triggered when a user uploads a video. Agents use Gemini for vision and language, persist video and results as **ADK artifacts**, and call tools (e.g. TTS, storage). The pipeline produces WCAG-aligned audio description in 18+ languages.

**2. Gemini Live API (real-time agent)**  
In the same app, users watch any video and tap **“Gemini Live.”** They ask questions by voice (e.g. “What’s happening on screen?”, “Who is the person in the red shirt?”). Our **FastAPI backend on Cloud Run** proxies a WebSocket to the **Gemini Live API (Multimodal Live)** on Vertex AI. Users get low-latency, spoken answers grounded in the current video. We support optional **Vertex RAG** for support/help content.

**Hosting**  
- FastAPI backend (auth, video API, **Gemini Live proxy**): **Cloud Run** service, behind **API Gateway**.  
- ADK pipeline: **Cloud Run Job** (same codebase as agent), invoked by backend/Cloud Functions.  
- All infra is **Terraform** (GCP project, Cloud Run, IAM, API Gateway, secrets).

**Why it fits Live Agent**  
We use **both** “Gemini Live API” and “use of ADK,” and all agents are hosted on Google Cloud. The combination of a long-running ADK pipeline (accessibility) and a real-time Gemini Live experience (in-context Q&A) in one product is our differentiator.

---

## Demo video link
[ Add your 2–3 min demo URL here ]

---

## Architecture diagram link (optional)
[ Add link to image or doc ]

---

## Technologies used (for form checkboxes / tags)

- Google Agent Development Kit (ADK)
- Gemini Live API (Multimodal Live, Vertex AI)
- Vertex AI (Gemini)
- Google Cloud Run (services + jobs)
- API Gateway
- Terraform
- FastAPI (Python)
- Next.js (frontend)

---

## Subcategory to highlight (if the form allows)

- **Best Multimodal Integration & User Experience** — Video + voice input (Live) and video + text + audio output (pipeline + player) in one UX.  
- **Best Technical Execution & Agent Architecture** — ADK multi-agent pipeline + Terraform + Cloud Run Job/Service separation.
