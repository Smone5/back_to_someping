# Storybook Movie Quality Spec

This defines the minimum product bar for the final storybook movie so fixes are
driven by an explicit target instead of ad hoc reactions.

## Release Gate

A movie may enter theater mode only if all of these are true:

- A final MP4 exists.
- The final MP4 has a playable video stream.
- If narration is expected, the final MP4 must also have a real audio stream.
- Final duration must be sane for playback and not effectively zero-length.
- `theater_release_ready` must be true in the persisted assembly state.

The release gate is enforced in:

- `/Users/amelton/ADAI_Beta_Project/google-prog/backend/storybook_flow.py`
- `/Users/amelton/ADAI_Beta_Project/google-prog/backend/ffmpeg_worker/assemble.py`
- `/Users/amelton/ADAI_Beta_Project/google-prog/agent/tools.py`

## Narration

- Narration is required for read-aloud storybook movies unless the product is explicitly configured for a silent special case.
- Shared default: `NARRATION_REQUIRED_DEFAULT = True`
- Every narration-bearing movie must ship with a real muxed audio stream.
- Silent movies should fail assembly rather than opening theater mode as if they were complete.
- Narration lines should be short, simple, and understandable for young children.

## Page Pacing

- Baseline page duration target: `4.0s`
- Baseline minimum page duration: `3.0s`
- Baseline maximum page duration without narration pressure: `6.0s`
- If narration for a page runs longer, the page may stretch just enough to fit `narration length + 0.4s`
- Pages should never advance before narration finishes.

The shared pacing policy lives in:

- `/Users/amelton/ADAI_Beta_Project/google-prog/shared/storybook_movie_quality.py`

## Pan And Zoom

- Motion style: slow Ken Burns only
- Zoom range: about `1.03x` to `1.13x`
- Shared bounds: `KEN_BURNS_ZOOM_MIN = 1.03`, `KEN_BURNS_ZOOM_MAX = 1.13`
- Settle-in time: `0.10s` to `0.24s`
- Travel window: `1.1s` to `3.8s`
- Motion must taper and then hold instead of drifting forever across long narration
- No fast camera pushes, whip pans, or motion that feels like subtitles are the primary content

## Audio Mix

- Narration should remain clearly dominant over all other sound
- Music bed volume range: `0.05` to `0.18`
- Default music bed volume: `0.10`
- SFX volume range: `0.08` to `0.32`
- Default SFX volume: `0.22`
- Narration should duck music and SFX when enabled
- Music and SFX should support the scene, not compete with the storyteller

## SFX Density

- Default maximum SFX cues per short storybook movie: `2`
- Hard maximum SFX cues: `3`
- Minimum cooldown: at least `1` page between SFX-heavy beats by default
- SFX should be accent moments, not a constant layer

## Text Policy

- No subtitle-looking text burned over the movie by default
- Shared default: `BURNED_CAPTIONS_DEFAULT = False`
- No visible watermark, signature, label, or prompt leakage
- Prompts for generated art must explicitly discourage text in the image
- If text is ever used intentionally in the future, it must look like part of the book page, not a streaming caption overlay

## Visible Video Stage

- Desktop minimum visible stage height: `420px`
- Tablet minimum visible stage height: `300px`
- Mobile minimum visible stage height: `260px`
- Theater video stage remains `16:9`
- The video area should always dominate the theater layout, with supporting controls around it rather than above-the-fold competing panels

The stage sizing is enforced in:

- `/Users/amelton/ADAI_Beta_Project/google-prog/frontend/src/app/globals.css`

## Source Of Truth

These values should stay aligned:

- Shared policy: `/Users/amelton/ADAI_Beta_Project/google-prog/shared/storybook_movie_quality.py`
- Worker assembly: `/Users/amelton/ADAI_Beta_Project/google-prog/backend/ffmpeg_worker/assemble.py`
- Fast and local assembly: `/Users/amelton/ADAI_Beta_Project/google-prog/agent/tools.py`
- Backend env defaults: `/Users/amelton/ADAI_Beta_Project/google-prog/backend/main.py`
- Example env values: `/Users/amelton/ADAI_Beta_Project/google-prog/.env.example`
- Theater layout: `/Users/amelton/ADAI_Beta_Project/google-prog/frontend/src/app/globals.css`

## Regression Coverage

The policy has unit coverage in:

- `/Users/amelton/ADAI_Beta_Project/google-prog/backend/tests/test_storybook_movie_quality.py`

Any change to pacing, motion, mix, SFX density, or theater minimum size should update both the shared policy and this document in the same change.
