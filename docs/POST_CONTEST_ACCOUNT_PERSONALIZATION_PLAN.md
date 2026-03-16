# Post-Contest Account, Personalization, and Monetization Plan

Status: Deferred until after the contest submission.

This document captures the product and implementation plan for adding optional parent login, saved story libraries, personalization, and paid features to `google-prog` without slowing down the current contest path.

## Why This Is Deferred

The current contest build is strongest as an anonymous, low-friction storytelling experience:

- parent gate
- instant live story
- illustrated storybook
- final movie + trading card

Adding accounts now would expand scope across frontend auth, backend auth/session management, Firestore schema, privacy copy, asset retention, and billing. That is useful product work, but it is not required to make the contest demo compelling.

## Product Direction

Keep the core experience:

- guest-first
- account-optional
- magic before friction

Recommended funnel:

1. User starts instantly with no login.
2. Child gets a great story and final movie.
3. Theater mode offers `Save This Story`.
4. Parent signs in with Google only if they want persistence.
5. Later sessions use remembered preferences to improve the experience.

Principles:

- parent account only
- child profile, not child login
- no raw voice/audio storage
- store structured preference signals only
- monetize memory, persistence, and polish, not first access

## Current System Assumptions

The current system already uses Firestore as the persistent session/story store and keeps the live experience anonymous:

- live session state is anonymous and session-based
- story/session docs are written to `storyteller_sessions`
- final story assembly reads from `storyteller_sessions`
- theater mode is already the emotional payoff and best upsell point

Implication:

- do not replace `storyteller_sessions`
- extend it and add adjacent collections

## Scope Split

### In Contest Scope

- keep guest flow fast
- keep storybook and theater mode polished
- keep demo reliable

### Post-Contest Scope

- Google sign-in for parents
- account/session ownership
- saved story library
- child profiles
- preference memory
- subscription entitlements

## Recommended Architecture

Use Google Identity Services on the frontend plus backend token verification and app session management.

Recommendation:

- do not add Firebase Auth just because Firestore exists
- verify Google ID tokens in the backend
- create a parent account record in Firestore
- issue an app session for library/profile actions

Why:

- the frontend is currently very lean
- backend already uses Google auth libraries
- Firestore access is already server-driven
- this keeps the auth surface smaller than adding full Firebase client auth now

## Firestore Data Model

Keep `storyteller_sessions/{session_id}` as the live session document and add the following collections.

### `parents/{parent_id}`

Suggested fields:

```json
{
  "google_uid": "117000000000000000000",
  "email": "parent@example.com",
  "display_name": "Parent Name",
  "plan_tier": "free",
  "marketing_opt_in": false,
  "default_child_profile_id": "child_abc123",
  "created_at": "SERVER_TIMESTAMP"
}
```

### `child_profiles/{child_profile_id}`

Suggested fields:

```json
{
  "parent_id": "parent_123",
  "display_name": "Avery",
  "age_band": "4-5",
  "tone_preference": "gentle",
  "favorite_themes": ["ocean", "trains"],
  "favorite_characters": ["penguin sidekick"],
  "story_length_preference": "medium",
  "bedtime_mode_default": true,
  "illustration_style_preference": "storybook-soft",
  "created_at": "SERVER_TIMESTAMP",
  "last_active_at": "SERVER_TIMESTAMP"
}
```

### `storyteller_sessions/{session_id}`

Keep existing story fields and add ownership and memory metadata.

Suggested additions:

```json
{
  "owner_parent_id": "parent_123",
  "child_profile_id": "child_abc123",
  "claim_status": "anonymous",
  "session_source": "guest",
  "favorite_themes_detected": ["trains", "night sky"],
  "engagement_signals": {
    "replayed_movie": true,
    "remake_requested": false,
    "saved_to_library": false
  }
}
```

Suggested `claim_status` values:

- `anonymous`
- `claimed`

Suggested `session_source` values:

- `guest`
- `account`
- `guest_claimed`

### `storybooks/{storybook_id}`

This is the durable library record for saved outputs.

```json
{
  "session_id": "sess_xyz",
  "parent_id": "parent_123",
  "child_profile_id": "child_abc123",
  "title": "Avery and the Moon Train",
  "child_name": "Avery",
  "cover_image_url": "https://...",
  "final_video_url": "https://...",
  "trading_card_url": "https://...",
  "created_at": "SERVER_TIMESTAMP",
  "is_favorite": false
}
```

### `preference_signals/{signal_id}`

Append-only signals derived from explicit settings and behavior.

```json
{
  "parent_id": "parent_123",
  "child_profile_id": "child_abc123",
  "session_id": "sess_xyz",
  "signal_type": "theme_positive",
  "signal_value": "trains",
  "weight": 0.8,
  "created_at": "SERVER_TIMESTAMP"
}
```

## Personalization Model

Personalization should be narrow, explicit, and safe.

Sources:

- explicit parent settings
- structured signals derived from prior sessions
- post-movie feedback already collected in theater mode

Examples of memory worth storing:

- child display name
- age band
- favorite themes
- favorite recurring sidekick or character type
- calm vs adventurous tone
- preferred story length
- bedtime mode preference
- replay/save/favorite signals

Do not store:

- raw voice recordings
- ad-tech style tracking
- open-ended background profiling

Working prompt summary example:

`Avery loves trains, ocean adventures, and gentle bedtime endings. Keep tone warm and calm. Prefer recurring sidekicks over brand-new casts.`

## Backend API Plan

Add the following endpoints after the contest.

### Auth

- `POST /api/auth/google/exchange`
  - input: Google ID token credential
  - action: verify token, upsert parent record, create app session
- `GET /api/auth/me`
  - returns signed-in parent + child profiles
- `POST /api/auth/logout`
  - clears app session

### Child Profiles

- `POST /api/child-profiles`
  - create first or additional child profile
- `PATCH /api/child-profiles/{id}`
  - update preferences
- `GET /api/child-profiles`
  - list parent-owned profiles

### Story Claiming and Library

- `POST /api/story-sessions/{session_id}/claim`
  - attach anonymous session to signed-in parent
  - optionally create storybook library record
- `GET /api/library`
  - list saved storybooks
- `PATCH /api/library/{storybook_id}`
  - favorite, rename, archive

### Preference Signals

- `POST /api/preferences/signals`
  - optional write endpoint for replay/save/favorite actions

## Frontend UX Plan

Best save/upsell moment:

- theater mode, after the movie has delivered the payoff

Recommended flow:

1. Guest finishes a story.
2. Theater mode shows a new primary CTA: `Save This Story`.
3. If unsigned, show a parent-only Google sign-in modal.
4. If signed in but no child profile exists, show a very short profile form.
5. Claim the session and save it to the library.
6. Confirm with `Saved to Avery's Library`.

Minimal initial child profile form:

- child display name
- age band
- tone: `gentle`, `mixed`, `adventurous`

Suggested initial UI additions:

- `Save This Story` in theater mode
- `Library` page for parents
- optional `Choose Child Profile` modal before a new story

## Code Touchpoints

Likely file touchpoints after the contest:

- `frontend/src/app/layout.tsx`
  - auth provider / Google identity script
- `frontend/src/components/story/TheaterMode.tsx`
  - add save/claim CTA
- `frontend/src/components/story/StorytellerLive.tsx`
  - pass `sessionId` and account context into theater mode
- `backend/main.py`
  - auth and library endpoints
- `backend/ws_router.py`
  - extend `client_ready` payload handling with optional profile memory
- `backend/schemas.py`
  - expand payload contracts if needed

## WebSocket Personalization Hook

The cleanest low-impact personalization entry point is the existing `client_ready` command.

Extend its payload with optional fields such as:

```json
{
  "child_profile_id": "child_abc123",
  "memory_summary": "Loves trains, ocean adventures, and gentle bedtime endings."
}
```

Backend behavior:

- validate that the profile belongs to the signed-in parent
- inject memory into session state before the first greeting
- continue using the same live flow

This avoids rebuilding the websocket architecture.

## Storage and Retention Changes

This is the biggest non-obvious infra dependency.

Current retention model:

- session assets bucket deletes after 7 days
- final videos bucket deletes after 24 hours

That works for guest privacy and cost control, but not for paid persistence.

Required post-contest change:

- keep current short-lived guest buckets for anonymous sessions
- add a durable library bucket for claimed/saved story movies
- copy or promote claimed outputs into the durable bucket during claim

Recommended rule:

- guest outputs keep short TTL
- claimed library outputs use durable retention

## Privacy and Policy Changes

Before launching accounts or saved libraries, update the privacy policy.

Current privacy messaging says the app does not collect personal information such as names, emails, or identifiers. That is compatible with the contest build, but it is not compatible with parent Google sign-in and saved story libraries.

Required updates:

- explain parent Google sign-in
- explain child profiles
- explain saved stories and retention
- confirm that raw voice/audio is not stored
- explain deletion flow for account-linked saved content

## Monetization Plan

Monetize memory and persistence, not first use.

### Free

- instant anonymous play
- optional account
- limited saved stories
- standard voices and export options

### Plus

- unlimited saved library
- recurring characters and worlds
- longer stories
- premium narration voices
- durable downloads

### Family

- multiple child profiles
- richer family library
- special themes and bundles
- stronger parental controls and presets

## Recommended Rollout Order

1. Add parent Google sign-in.
2. Add `parents` and `child_profiles`.
3. Add session claim flow from theater mode.
4. Add durable library storage and `storybooks`.
5. Add memory summaries and preference injection.
6. Add plan entitlements and paid feature gates.

## Explicitly Out of Scope for the Contest Build

Do not pull these into the current contest sprint unless priorities change:

- auth implementation
- billing
- durable library migration
- profile selection flow
- personalization prompt memory
- privacy-policy rewrite for account-based storage

The contest version should stay focused on:

- strong live story demo
- clear Google ADK + Gemini Live narrative
- polished theater mode payoff

