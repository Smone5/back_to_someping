# Voxitale Runtime Architecture Dictionary

This document is the lookup table for the coded nodes in [Voxitale_Runtime_Architecture.xml](/Users/amelton/ADAI_Beta_Project/google-prog/docs/Voxitale_Runtime_Architecture.xml).

How to use it:
- Each functional box in the diagram has a unique code.
- `RO-*` = `Runtime Overview`
- `RC-*` = `Reconnect and Recovery`
- `BD-*` = `ADK Bidi Flow`
- Swimlane containers, legend boxes, and edge labels are not coded. The codes are for the functional nodes a reader is most likely to inspect.

## Runtime Overview

| Code | Node | What it is | Why it matters |
|---|---|---|---|
| `RO-01` | Browser storyteller client | The main browser UI in `StorytellerLive`, including story pages, theater mode, and assembly overlays. | This is the child-facing surface where all live story state is rendered. |
| `RO-02` | Browser audio engine | Browser-side mic capture, PCM playback, page-read audio, and sound effects. | It is the frontend half of the live voice loop. |
| `RO-03` | Browser websocket transport | The browser websocket client for `/ws/story`, including reconnect handling. | It carries live audio, control messages, and streamed server events. |
| `RO-04` | UI rehydration | Frontend state restoration after reconnect, especially image, storybeat text, and assembly status. | It keeps the story visually stable when the transport drops and recovers. |
| `RO-05` | Browser-side Home Assistant apply | Optional browser path for local/private lighting commands. | It allows lights to still work in setups where backend-origin control is not the right path. |
| `RO-06` | FastAPI / ws_router | The main backend websocket/router layer. | This is where browser traffic enters the live runtime and where server events are streamed back out. |
| `RO-07` | Active websocket owner guard | Session ownership guard using the current `connection_id`, ghost-connection close, and heartbeat/watchdog logic. | It prevents split-brain delivery where different events land on different sockets. |
| `RO-08` | ADK Bidi bridge | The backend bridge that connects websocket input/output to `ResettableLiveRequestQueue` and `runner.run_live(...)`. | This is the core of the ADK bidirectional live integration. |
| `RO-09` | Session event bus | Per-session buffered events like `video_ready`, `lighting_command`, and `theater_mode`. | It decouples tool completions from the exact timing of websocket delivery. |
| `RO-10` | Storyteller tool suite | The server-side tool layer for scenes, titles, continuity, room lights, and movie assembly. | It turns model decisions into durable assets and UI side effects. |
| `RO-11` | Movie completion poll | Backend poller that waits for the final MP4 and emits `theater_mode` when ready. | It lets movie assembly finish asynchronously without blocking the live story. |
| `RO-12` | Storyteller ADK agent | The top-level ADK agent that controls conversation policy, tool use, and recovery behavior. | It is the main reasoning layer that drives the experience. |
| `RO-13` | Gemini Live native-audio Bidi session | The live Gemini session used for streaming audio conversation and tool calls. | This is the model-side endpoint of the bidirectional live loop. |
| `RO-14` | Gemini helper models | Non-live helper model calls such as image generation, title generation, and continuity/text repair. | These support the live session with heavier or more specialized work. |
| `RO-15` | Tool outputs | A conceptual handoff point for `story state`, `scene assets`, and `assembly request`. | It helps separate “tools did work” from “where that output goes next.” |
| `RO-16` | Firestore story/session mirror | Mirrored story state including pages, resume data, assembly status, and release metadata. | It is the durable state store that reconnect and movie assembly both depend on. |
| `RO-17` | GCS assets | Cloud Storage for scene stills, thumbnails, page audio, and final movies. | It is the durable media layer behind story rendering and final playback. |
| `RO-18` | Cloud Run FFmpeg assembler job | Async movie assembly worker that reads assets/pages and writes the final MP4. | It converts the interactive session into the final storybook-style movie. |
| `RO-19` | Home Assistant lights | Smart-home lighting endpoint for room color, brightness, transitions, and theater cues. | It is the lighting side effect target for both live story and movie playback. |
| `RO-20` | ElevenLabs | Third-party read-aloud and SFX/audio generation integration. | It supports page-read audio and voice fallback behavior. |

## Reconnect and Recovery

| Code | Node | What it is | Why it matters |
|---|---|---|---|
| `RC-01` | Transport/proxy closes | The initial disconnect condition, such as a proxy close or tab/network interruption. | It is the trigger that starts the recovery flow. |
| `RC-02` | Frontend enters reconnecting | The frontend reconnect state while keeping the story UI visible. | It avoids a jarring reset while transport is being re-established. |
| `RC-03` | Open a new websocket | Browser reconnect to `/ws/story` using the same `session_id`. | This preserves the live session identity during recovery. |
| `RC-04` | Apply `SESSION_REHYDRATED` | Frontend restore of current scene, storybeat text, and assembly state. | It makes the recovered UI match the server’s durable state. |
| `RC-05` | Resume mic only when safe | Frontend choice to resume interactive mic/chat or remain in passive/assembly mode. | It prevents awkward or unsafe auto-resume behavior. |
| `RC-06` | Backend accepts new websocket | The backend side of the reconnect handshake. | It starts ownership reassignment and rehydration. |
| `RC-07` | Singleton owner swap | The backend marks the new socket as the only current owner for the session. | It ensures one authoritative path for subsequent events. |
| `RC-08` | Old ghost socket closed | Forced close of the stale connection after replacement. | It prevents split delivery of late events like `video_ready`. |
| `RC-09` | `_prepare_clean_live_reconnect(...)` | Cleanup of stale live model/turn state before restarting the live stream. | It prevents poisoned state from leaking into the recovered conversation. |
| `RC-10` | Restart `_run_agent(...)` and `_forward_session_events(...)` | Re-start of the live ADK loop and session event forwarder. | It restores the backend streaming pipeline. |
| `RC-11` | Heartbeat ping + watchdog | Ongoing transport health checks after reconnect. | It helps detect and clean up stale or half-dead sockets. |
| `RC-12` | `replace_session_queue(session_id)` | Queue replacement that preserves buffered events for the same session. | It is why fast reconnects can keep late `video_ready` or `theater_mode` events. |
| `RC-13` | Cached storybook state + Firestore mirror | Durable session/story state used for restore. | It provides the truth source for reconnect UI reconstruction. |
| `RC-14` | Send `SESSION_REHYDRATED` and restore UI | Backend-origin restore payload and storybook UI replay. | It gives the frontend enough context to resume smoothly. |
| `RC-15` | Drain queued events to current socket | Delivery of buffered `video_ready`, `lighting_command`, `theater_mode`, and similar events. | It prevents completed work from being lost during reconnect timing windows. |
| `RC-16` | Late completion after reconnect | The fact that image generation or movie completion can finish after the transport changes. | It highlights why durable state plus buffered events are needed. |
| `RC-17` | Previous Gemini Live stream may clean-close | The previous model stream can end with a clean close like `1000 cancelled`. | Recovery must treat this as resumable, not necessarily fatal. |
| `RC-18` | Fresh Gemini Live session starts | New live model session for the same story conversation. | It restores the active model side of the Bidi loop. |
| `RC-19` | Pending child turn replay/recovery | Recovery path for interrupted child input or post-render continuation. | It helps preserve conversational continuity after drops. |
| `RC-20` | Conversation continues without losing story state | The desired end state after recovery. | It is the main product outcome of the reconnect design. |

## ADK Bidi Flow

| Code | Node | What it is | Why it matters |
|---|---|---|---|
| `BD-01` | Child speaks into the browser | Mic audio enters the system at the browser. | This is the start of the live voice path. |
| `BD-02` | Frontend sends audio/control over WSS | Browser sends PCM/audio and control JSON to `/ws/story`. | It is the transport handoff from frontend to backend. |
| `BD-03` | Frontend receives streamed assistant output | Browser receives streamed audio and server events like `TURN_COMPLETE` and `video_ready`. | It is the return leg of the live loop. |
| `BD-04` | UI plays voice and updates state | Browser playback/render layer for Amelia’s voice and media state. | It turns raw stream output into the child-facing experience. |
| `BD-05` | `ws_router` receives websocket frames | Backend parses child audio and JSON events. | It is the first backend processing step in the Bidi path. |
| `BD-06` | `_send_live_realtime(...)` | Backend wraps audio as a realtime blob and pushes it to the ADK queue. | It is the audio lane into the live ADK session. |
| `BD-07` | `_send_live_content(...)` | Backend sends text/control instructions into the live ADK queue. | It is the content/control lane used for repairs and recovery prompts. |
| `BD-08` | `_run_agent(...)` multiplexer | Backend loop that consumes ADK events and fans them back out to websocket and side effects. | It is the return-side multiplexer for the Bidi pipeline. |
| `BD-09` | `ResettableLiveRequestQueue` | Resettable subtype of `LiveRequestQueue` used to feed ADK live requests. | It enables queue resets and stale-frame recovery without leaving ADK’s type expectations. |
| `BD-10` | `runner.run_live(...)` | The ADK live runner bound to the current `live_request_queue`. | It is the core call that opens and drives the live ADK conversation. |
| `BD-11` | ADK Bidi data plane | Conceptual request/event stream between the queue/runner and the model. | It emphasizes that this is a streaming, bidirectional channel, not request/response RPC. |
| `BD-12` | Reset / recovery path | Queue reset, pending-turn replay, and clean reconnect support. | It is what makes the Bidi bridge resilient in production. |
| `BD-13` | Gemini Live native-audio session | The underlying Gemini Live session that handles bidirectional audio and tool calling. | It is the model session at the far end of the ADK stream. |
| `BD-14` | Streaming output events | Assistant audio chunks, transcripts, tool calls, and turn completion events coming back from the model. | These are the raw live outputs that the backend interprets. |
| `BD-15` | Tool side effects | Scene generation, lights, movie assembly, title repair, and similar non-audio work. | It shows that tools are part of the same live conversation flow. |
| `BD-16` | Session event bus publishes side effects | Backend publication of `video_ready`, `lighting_command`, and `theater_mode`. | It links tool completions back into the browser-visible experience. |
