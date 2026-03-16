# Real “Wow” Ideas

Things that are **genuinely surprising** and that you can build on what you already have. Pick one or two for the contest.

---

## 1. “Gemini sees your video in real time” (already true — surface it)

**What you have:** In `useGeminiLive.ts` you already send **video frames at 1 FPS** to Gemini Live (`realtime_input.media_chunks` with `image/jpeg`). The backend prompt says: *“You have access to a continuous video stream of what the user is watching (provided as image frames).”*

**Wow:** Most demos only send *audio*. You send **audio + live video**. So Gemini literally sees the same frame the user sees.

**What to do:**
- **Demo line:** In the contest video, say: “When I press Live, we stream the video frames to Gemini in real time — so it sees exactly what I see, not just metadata.”
- **Optional UI:** Small label when Live is on: “Live: video + voice” or “Gemini is watching this frame” (no new backend — just copy).
- **Submission:** Explicitly say “We send real-time video frames (1 FPS) plus voice to Gemini Live so the agent has full visual context.”

**Effort:** Zero (story) or ~30 min (label). **Impact:** High — judges may not assume you send video.

---

## 2. Live “director’s commentary” mode

**What you have:** Same as above: 1 FPS frames + `get_current_context` (time, captions, nearby scenes). Gemini can describe what’s on screen and what’s in the index.

**Wow:** User turns on “Commentary” and Gemini acts like a director: *“The camera pans left to reveal the city…”, “Notice the expression on his face — that’s the turning point.”* No question needed — proactive, continuous narration in real time.

**What to do:**
- New **mode** in the player: “Watch” (default) vs “Commentary.”
- When Commentary is on, use a **different system instruction**: “You are a film director giving live commentary. Describe what’s on screen in real time: composition, movement, emotion, and what might happen next. Keep each line short (one sentence). Speak every 10–15 seconds. Do not wait for the user to ask.”
- Rely on **proactive audio** if the Live API supports it; otherwise you may need a client-side “nudge” (e.g. send a silent or minimal prompt every 15 s) to trigger a turn.

**Effort:** Medium (new mode + prompt + optional nudge). **Impact:** Very high — unique “creative storytelling” + “live agent” moment.

---

## 3. “What would the audio description say here?”

**What you have:** ADK pipeline produces full audio description scripts; Gemini Live has `get_current_context` and sees the current frame. You can ask Gemini to generate one AD-style sentence for the current moment.

**Wow:** User asks: “What would the audio description say right here?” Gemini (with current frame + context) generates a **single sentence** in professional AD style, then you **speak it with TTS** (or Gemini speaks it). Direct link between “batch ADK pipeline” and “live agent.”

**What to do:**
- **Option A (no new tool):** In the default system instruction, add: “If the user asks ‘what would the audio description say’ or ‘describe this like audio description’, generate one short, present-tense, objective sentence describing only what is visible (actions, setting, expressions, on-screen text). Use get_current_context first. Speak that sentence as your reply.”
- **Option B (tool):** New tool `get_ad_style_description` that returns a pre-written AD sentence for the current scene if the pipeline has already generated AD for this video (so it’s the *actual* AD, not generated on the fly). Then Live can say: “The audio description for this moment is: [sentence].”

**Effort:** Low (A) or Medium (B). **Impact:** High — ties Live and ADK in one demo beat.

---

## 4. Support agent that sees your screen and takes action (UI Navigator–adjacent)

**What you have:** Support mode already has:
- **Video feed** of the user’s screen (same 1 FPS stream).
- **`navigate_to_page`** — “Take me to billing” → agent navigates the app.
- **`point_to_location`** — “The button is here” → agent sends x,y and label to highlight/point on screen.

So you already have: **multimodal (screenshots) → executable actions (navigate, point).** That’s the UI Navigator category.

**Wow:** “Our support agent doesn’t just chat — it **sees your screen** and can **click for you** (navigate) or **point** to where to click.” Demo: user on a confusing page → “Where do I add credits?” → agent says “I’ll take you there” and navigates to billing; or “The button is here” and a visual pointer appears.

**What to do:**
- **Contest:** Consider a **second entry** for **UI Navigator**: “ADAI Support uses Gemini multimodal to interpret the user’s screen (video stream) and outputs executable actions: navigate_to_page (browser navigation) and point_to_location (visual pointer). Hosted on Google Cloud.”
- **Demo:** Short clip on the support page: user asks “Where’s the upload button?” → agent points; “Take me to my videos” → agent navigates.
- Ensure **`point_to_location`** actually shows a pointer/highlight on the frontend (if not already). That’s the “executable” part.

**Effort:** Low if pointing UI exists; medium if you need to draw a pointer. **Impact:** Unlocks a whole extra category (UI Navigator).

---

## 5. “Describe this moment” button + interrupt

**What you have:** `get_current_context`; barge-in handling (`serverContent.interrupted`); play/pause/seek by voice.

**Wow:** One big button: **“Describe this moment.”** User clicks (or says it). Video can pause. Gemini gets current frame + context, speaks 1–2 sentences, then video resumes (or stays paused). Clean, repeatable demo moment. Plus: user can **interrupt** the description (“OK, that’s enough”) and Gemini stops (you already support interrupt).

**What to do:**
- Add a prominent button (e.g. next to the Live button): “Describe this moment” or a mic icon with a frame icon.
- On click: (1) optionally pause video; (2) if Live isn’t on, start Live and send a single user message: “Describe exactly what you see on screen right now in one or two sentences.” (3) If Live is already on, send the same as a user turn. (4) Play Gemini’s reply. (5) Optionally resume video.
- In the demo: “I’ll hit Describe this moment… [click] … ‘A woman in a red coat is walking through a snowy park; the camera is following her from behind.’ That’s Gemini Live, seeing this frame.”

**Effort:** Low. **Impact:** Very high — one clear “multimodal + live” moment for judges.

---

## 6. Pipeline status: “Your video is in the ADK”

**What you have:** Backend triggers a Cloud Run Job (ADK); job has stages (scene analysis, critic, refinement, finalize). Frontend already shows processing status.

**Wow:** Instead of a generic “Processing…”, show **agent-stage names**: “Scene analysis” → “Critic review” → “Refinement” → “Finalizing.” Subtext: “Our ADK agents are working on your video on Google Cloud Run.”

**What to do:**
- Map your existing pipeline status (or job logs) to 3–4 human-readable stages. Expose them in the API if not already.
- In the Videos page (or upload success), show: “Your video is in the pipeline: **Scene analysis** ✓ → **Critic** … → Refinement → Finalize.”
- Link or tooltip: “Powered by Google ADK on Cloud Run.”

**Effort:** Low–medium. **Impact:** Makes “ADK” and “agent architecture” visible and tangible.

---

## Recommendation

- **Fastest wow:** **#1** (tell the story that you send video frames) + **#5** (“Describe this moment” button). Both are low effort; together they give a clear, memorable demo.
- **If you want one big bet:** **#2** (director’s commentary mode) — unique and very “creative storyteller” + “live agent.”
- **If you want a second category:** **#4** (Support as UI Navigator) — you already have the behavior; frame it and add a short demo.
