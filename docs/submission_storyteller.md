# 🧙‍♀️ Interactive Storyteller — Submission Draft

**Use this text when filling out the contest submission form. This is tailored specifically for the Storyteller project.**

---

## Project Name
Interactive Storyteller — AI Magical Storybook for Kids

---

## Category
**Live Agent** (Google ADK + Gemini Live API, hosted on Google Cloud Run)

---

## Short Description
Interactive Storyteller is a real-time AI magical storybook for children. Built with the **Google Agent Development Kit (ADK)** and the **Gemini Live API**, it allows a child to "speak" to the story narrator, make choices that change the plot, and see their words instantly turn into animated scenes (Nano Banana 2 + Veo 3.1) and personalized movies.

---

## Full Description

The Interactive Storyteller pushes the boundaries of real-time multimodal agents on Google Cloud to create a world where a child's imagination comes to life instantly.

**1. Real-Time Interaction (Gemini Live API & ADK)**
We use the **Gemini Live API (Multimodal Live)** on Vertex AI to power "Amelia," our AI narrator. Amelia doesn't just read a story; she listens. Using **Bidi-streaming** and a child-tuned VAD pipeline, the agent understands the child's shouted choices — *"The dragon flies to the moon!"* — and reacts with emotional, prosodic speech via native audio output. The orchestration is handled by the **Google ADK (Agent Development Kit)**, which manages the session state, user preferences, and tools.

**2. Interleaved Multimodal Output**
Every story beat triggers an automated visual pipeline. Our **ADK Tools** call **Nano Banana 2** for scene illustration and **Veo 3.1** for 5-second cinematic animations. These are interleaved with character-specific voices from the **ElevenLabs MCP** toolset.

**3. Production-Grade Architecture**
- **Hosting:** The FastAPI backend and ADK Runner are hosted on **Google Cloud Run** using always-on CPU for high-frequency, low-latency streaming.
- **Infrastructure as Code:** The entire stack — including GCS buckets for session assets, Firestore for cross-session "lore memory," and Secret Manager for API keys — is defined and deployed via **Terraform**.
- **Edge cases:** We implement a **Parental Gate** and zero-audio-storage policies to ensure kid-friendly safety and privacy.

**Why it fits the Live Agent Category:**
It represents a complete "Zero-to-Hero" use of the **Google ADK** and **Gemini Live API**. It demonstrates how a live agent can move beyond simple chat into a complex, tool-rich environment that generates real-time video and audio content synchronized with a human conversation.

---

## Demo Video Link
[ Add your 2–3 min demo URL here ]

---

## Key Technologies
- **Google Agent Development Kit (ADK)**
- **Gemini Live API** (Multimodal Live on Vertex AI)
- **Google Cloud Run** (Services & Jobs)
- **Terraform**
- **Nano Banana 2 & Veo 3.1** (Visual generation)
- **ElevenLabs MCP** (Voices & Foley SFX)
- **FastAPI & Next.js**

---

## Subcategory Highlighting
- **Best Multimodal Integration & User Experience:** Voice-in, Voice-out, Real-time Image/Video generation, and IoT light syncing (Home Assistant MCP) combined into a premium child-facing UI.
- **Best Technical Execution & Agent Architecture:** Advanced use of ADK Runners, custom toolsets, and a complex FFmpeg video assembly pipeline running as a Cloud Run Job.
