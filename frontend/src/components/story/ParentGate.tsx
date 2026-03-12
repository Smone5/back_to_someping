'use client';

/**
 * ParentGate — COPPA-compliant adult verification before microphone access.
 *
 * Per Iteration 3 (Expert Audit #6 — Legal Parental Gate):
 * COPPA requires verifiable parental consent before collecting audio from a
 * child under 13. We use a simple math challenge that is trivial for an adult
 * but hard for a 4-year-old to pass.
 *
 * Additional accessibility features:
 * - Large, high-contrast buttons (WCAG AA/AAA, Iter 4 #5)
 * - aria-labels for screen reader support
 * - Cannot be "accidentally" passed by a child mashing the screen
 */

import { useState, useCallback, useEffect } from 'react';
import { useStoryVoicePreview } from '@/hooks/useStoryVoicePreview';
import { useUiSounds } from '@/hooks/useUiSounds';
import { normalizeHomeAssistantConfig } from './homeAssistant';
import IoTSettingsModal, { IoTConfig } from './IoTSettingsModal';
import HowItWorksModal from './HowItWorksModal';
import {
    DEFAULT_STORY_READER_VOICE_ID,
    STORY_READER_VOICE_OPTIONS,
    getStoryReaderVoiceOption,
    normalizeStoryReaderVoiceId,
} from './storyVoiceOptions';

export type StoryTone = 'cozy' | 'gentle_spooky' | 'adventure_spooky';
export type StorybookMoviePacing = 'read_to_me' | 'read_with_me' | 'fast_movie';

interface ParentGateProps {
    onApproved: (
        calmMode: boolean,
        iotConfig: IoTConfig | null,
        storyTone: StoryTone,
        childAge: number,
        storybookMoviePacing: StorybookMoviePacing,
        storyReaderVoiceId: string,
    ) => void;
}

const REQUIRE_MATH_GATE = ['1', 'true', 'yes', 'on'].includes(
    (process.env.NEXT_PUBLIC_REQUIRE_MATH ?? '').trim().toLowerCase()
);

function generateMathChallenge(): { a: number; b: number; answer: number } {
    // Use numbers 10–49 so a child cannot guess easily but an adult solves instantly
    const a = Math.floor(Math.random() * 40) + 10;
    const b = Math.floor(Math.random() * 40) + 10;
    return { a, b, answer: a + b };
}

export default function ParentGate({ onApproved }: ParentGateProps) {
    const [challenge, setChallenge] = useState(generateMathChallenge);
    const [input, setInput] = useState('');
    const [error, setError] = useState('');
    const calmMode = false;
    const [storyTone, setStoryTone] = useState<StoryTone>('cozy');
    const [childAge, setChildAge] = useState(4);
    const [storybookMoviePacing, setStorybookMoviePacing] = useState<StorybookMoviePacing>('read_with_me');
    const [storyReaderVoiceId, setStoryReaderVoiceId] = useState(DEFAULT_STORY_READER_VOICE_ID);
    const [attempts, setAttempts] = useState(0);
    const [showIoT, setShowIoT] = useState(false);
    const [iotConfig, setIoTConfig] = useState<IoTConfig | null>(null);
    const [showHowTo, setShowHowTo] = useState(false);
    const [isMounted, setIsMounted] = useState(false);
    const { playUiSound } = useUiSounds({ enabled: !calmMode, volume: 0.9 });
    const {
        previewError,
        previewLoading,
        previewPlaying,
        previewVoiceId,
        previewVoice,
        stopPreview,
    } = useStoryVoicePreview({
        childAge,
        storybookMoviePacing,
    });

    // Initial mount hydration fix
    useEffect(() => {
        setIsMounted(true);
    }, []);

    // Load initial IoT config if exists
    useEffect(() => {
        try {
            const saved = localStorage.getItem('storyteller_iot_config');
            if (saved) {
                setIoTConfig(normalizeHomeAssistantConfig(JSON.parse(saved)));
            }
        } catch (e) {
            // Ignore
        }
    }, []);

    useEffect(() => {
        try {
            const saved = localStorage.getItem('storyteller_story_tone');
            if (saved === 'cozy' || saved === 'gentle_spooky' || saved === 'adventure_spooky') {
                setStoryTone(saved);
            }
        } catch {
            // Ignore
        }
    }, []);

    useEffect(() => {
        try {
            const saved = Number(localStorage.getItem('storyteller_child_age') ?? '');
            if (Number.isFinite(saved) && saved >= 4 && saved <= 10) {
                setChildAge(saved);
            }
        } catch {
            // Ignore
        }
    }, []);

    useEffect(() => {
        try {
            const saved = localStorage.getItem('storyteller_storybook_movie_pacing');
            if (saved === 'read_to_me' || saved === 'read_with_me' || saved === 'fast_movie') {
                setStorybookMoviePacing(saved);
            }
        } catch {
            // Ignore
        }
    }, []);

    useEffect(() => {
        try {
            const saved = localStorage.getItem('storyteller_story_reader_voice_id');
            if (saved) {
                setStoryReaderVoiceId(normalizeStoryReaderVoiceId(saved));
            }
        } catch {
            // Ignore
        }
    }, []);

    useEffect(() => {
        if (previewVoiceId && previewVoiceId !== storyReaderVoiceId) {
            stopPreview();
        }
    }, [previewVoiceId, stopPreview, storyReaderVoiceId]);

    const handleSubmit = useCallback(() => {
        const persistParentPrefs = () => {
            try {
                localStorage.setItem('storyteller_story_tone', storyTone);
                localStorage.setItem('storyteller_child_age', String(childAge));
                localStorage.setItem('storyteller_storybook_movie_pacing', storybookMoviePacing);
                localStorage.setItem('storyteller_story_reader_voice_id', storyReaderVoiceId);
            } catch {
                // Ignore
            }
        };

        if (!REQUIRE_MATH_GATE) {
            persistParentPrefs();
            onApproved(calmMode, iotConfig, storyTone, childAge, storybookMoviePacing, storyReaderVoiceId);
            return;
        }

        const userAnswer = parseInt(input, 10);
        if (userAnswer === challenge.answer) {
            persistParentPrefs();
            onApproved(calmMode, iotConfig, storyTone, childAge, storybookMoviePacing, storyReaderVoiceId);
        } else {
            setAttempts((a) => a + 1);
            setError('Hmm, that\'s not quite right! Try again.');
            setInput('');
            // Regenerate challenge after 3 failed attempts (Iter 3 #6 — anti-bypass)
            if (attempts >= 2) {
                setChallenge(generateMathChallenge());
                setAttempts(0);
                setError('New challenge generated. Please try again.');
            }
        }
    }, [input, challenge.answer, calmMode, attempts, childAge, iotConfig, onApproved, storyTone, storyReaderVoiceId, storybookMoviePacing]);

    useEffect(() => {
        const handleKey = (e: KeyboardEvent) => {
            if (e.key === 'Enter') handleSubmit();
        };
        window.addEventListener('keydown', handleKey);
        return () => window.removeEventListener('keydown', handleKey);
    }, [handleSubmit]);

    if (!isMounted) return null;

    return (
        <div className="parent-gate-overlay" role="dialog" aria-modal="true" aria-label="Parental consent required">
            <div className="parent-gate-stars" aria-hidden="true" />

            <div className="parent-gate-card">
                <div className="parent-gate-header-row">
                    <div className="parent-gate-icon" aria-hidden="true">✨</div>
                    <button
                        className="parent-gate-settings-btn"
                        onClick={() => {
                            playUiSound('tap');
                            setShowIoT(true);
                        }}
                        title="Connect room lights (Home Assistant)"
                        aria-label="Open Smart Home Lights settings"
                    >
                        💡 Smart Lights
                    </button>
                </div>

                <h1 className="parent-gate-title">Ready for an Adventure?</h1>
                <p className="parent-gate-subtitle">
                    Amelia turns your child&apos;s spoken ideas into a picture story.
                    To start the journey, we just need a little help from a grown-up.
                </p>

                <button
                    type="button"
                    className="parent-gate-how-to-btn"
                    onClick={() => {
                        playUiSound('tap');
                        setShowHowTo(true);
                    }}
                    aria-haspopup="dialog"
                >
                    ▶ How it works
                </button>

                {/* How It Works Content moved to responsive modal */}

                {!showHowTo && (
                    <div className="parent-gate-tone-picker" role="radiogroup" aria-label="Story mood">
                        <div className="parent-gate-tone-heading">
                            <strong>Story Mood</strong>
                            <span>Pick how spooky Amelia may get.</span>
                        </div>
                        <div className="parent-gate-scroll-row">
                            <button
                                type="button"
                                className={`parent-gate-tone-option ${storyTone === 'cozy' ? 'is-selected' : ''}`}
                                onClick={() => setStoryTone('cozy')}
                                aria-pressed={storyTone === 'cozy'}
                            >
                                <span className="parent-gate-tone-label">Cozy</span>
                                <span className="parent-gate-tone-copy">Warm, bright, never spooky.</span>
                            </button>
                            <button
                                type="button"
                                className={`parent-gate-tone-option ${storyTone === 'gentle_spooky' ? 'is-selected' : ''}`}
                                onClick={() => setStoryTone('gentle_spooky')}
                                aria-pressed={storyTone === 'gentle_spooky'}
                            >
                                <span className="parent-gate-tone-label">Gentle Spooky</span>
                                <span className="parent-gate-tone-copy">Moonlit towers, creaky doors, silly goblins.</span>
                            </button>
                            <button
                                type="button"
                                className={`parent-gate-tone-option ${storyTone === 'adventure_spooky' ? 'is-selected' : ''}`}
                                onClick={() => setStoryTone('adventure_spooky')}
                                aria-pressed={storyTone === 'adventure_spooky'}
                            >
                                <span className="parent-gate-tone-label">Adventure Spooky</span>
                                <span className="parent-gate-tone-copy">Brave mystery and dark castles, still preschool-safe.</span>
                            </button>
                        </div>
                    </div>
                )}

                {!showHowTo && (
                    <div className="parent-gate-tone-picker" role="radiogroup" aria-label="Final storybook movie pace">
                        <div className="parent-gate-tone-heading">
                            <strong>Final Movie Pace</strong>
                            <span>Choose how long pages stay on screen.</span>
                        </div>
                        <div className="parent-gate-scroll-row">
                            <button
                                type="button"
                                className={`parent-gate-tone-option ${storybookMoviePacing === 'read_to_me' ? 'is-selected' : ''}`}
                                onClick={() => setStorybookMoviePacing('read_to_me')}
                                aria-pressed={storybookMoviePacing === 'read_to_me'}
                            >
                                <span className="parent-gate-tone-label">Read to Me</span>
                                <span className="parent-gate-tone-copy">Voice leads. Shorter page text. Best for pre-readers.</span>
                            </button>
                            <button
                                type="button"
                                className={`parent-gate-tone-option ${storybookMoviePacing === 'read_with_me' ? 'is-selected' : ''}`}
                                onClick={() => setStorybookMoviePacing('read_with_me')}
                                aria-pressed={storybookMoviePacing === 'read_with_me'}
                            >
                                <span className="parent-gate-tone-label">Read with Me</span>
                                <span className="parent-gate-tone-copy">Balanced read-along timing with more time to look and read.</span>
                            </button>
                            <button
                                type="button"
                                className={`parent-gate-tone-option ${storybookMoviePacing === 'fast_movie' ? 'is-selected' : ''}`}
                                onClick={() => setStorybookMoviePacing('fast_movie')}
                                aria-pressed={storybookMoviePacing === 'fast_movie'}
                            >
                                <span className="parent-gate-tone-label">Fast Movie</span>
                                <span className="parent-gate-tone-copy">Brisker page turns for replays or quick sharing.</span>
                            </button>
                        </div>
                    </div>
                )}

                {!showHowTo && (
                    <div className="parent-gate-dropdowns-row">
                        <div className="parent-gate-age-picker">
                            <div className="parent-gate-tone-heading">
                                <strong>Child Age</strong>
                            </div>
                            <label className="parent-gate-age-field">
                                <select
                                    value={childAge}
                                    onChange={(e) => setChildAge(Math.max(4, Math.min(10, Number(e.target.value) || 4)))}
                                    aria-label="Child age"
                                >
                                    {[4, 5, 6, 7, 8, 9, 10].map((age) => (
                                        <option key={age} value={age}>
                                            {age} years old
                                        </option>
                                    ))}
                                </select>
                            </label>
                        </div>

                        <div className="parent-gate-age-picker">
                            <div className="parent-gate-tone-heading">
                                <strong>Narrator Voice</strong>
                            </div>
                            <label className="parent-gate-age-field">
                                <div className="story-reader-voice-row">
                                    <select
                                        value={storyReaderVoiceId}
                                        onChange={(e) => setStoryReaderVoiceId(normalizeStoryReaderVoiceId(e.target.value))}
                                        aria-label="Story reader voice"
                                    >
                                        {STORY_READER_VOICE_OPTIONS.map((voice) => (
                                            <option key={voice.id} value={voice.id}>
                                                {voice.name}
                                            </option>
                                        ))}
                                    </select>
                                    <button
                                        type="button"
                                        className={`story-reader-voice-preview-btn ${(previewLoading || previewPlaying) && previewVoiceId === storyReaderVoiceId ? 'is-active' : ''}`}
                                        onClick={() => {
                                            playUiSound((previewLoading || previewPlaying) && previewVoiceId === storyReaderVoiceId ? 'close' : 'tap');
                                            void previewVoice(storyReaderVoiceId);
                                        }}
                                        aria-label={previewLoading || previewPlaying ? 'Stop voice preview' : `Preview ${getStoryReaderVoiceOption(storyReaderVoiceId).name} voice`}
                                        aria-pressed={(previewLoading || previewPlaying) && previewVoiceId === storyReaderVoiceId}
                                        title={getStoryReaderVoiceOption(storyReaderVoiceId).blurb}
                                    >
                                        {previewLoading && previewVoiceId === storyReaderVoiceId
                                            ? '...'
                                            : previewPlaying && previewVoiceId === storyReaderVoiceId
                                                ? 'Stop'
                                                : '▶'}
                                    </button>
                                </div>
                            </label>
                            {previewError ? (
                                <p className="story-reader-voice-preview-error" role="status">
                                    {previewError}
                                </p>
                            ) : null}
                        </div>
                    </div>
                )}

                {REQUIRE_MATH_GATE && (
                    <div className="parent-gate-challenge" aria-label="Math challenge">
                        <span className="challenge-text">
                            Quick check: What is <strong>{challenge.a}</strong> + <strong>{challenge.b}</strong>?
                        </span>
                        <input
                            type="number"
                            className="challenge-input"
                            value={input}
                            onChange={(e) => { setInput(e.target.value); setError(''); }}
                            aria-label="Enter your answer"
                            placeholder="??"
                            autoFocus
                            inputMode="numeric"
                        />
                    </div>
                )}

                {error && REQUIRE_MATH_GATE && (
                    <p className="parent-gate-error" role="alert">{error}</p>
                )}

                <div className="parent-gate-footer-controls">
                    <button
                        className="parent-gate-btn"
                        onClick={() => {
                            playUiSound('magic');
                            handleSubmit();
                        }}
                        aria-label="Unlock the Magic"
                    >
                        Unlock the Magic ✨
                    </button>
                </div>

                <p className="parent-gate-privacy">
                    🔒 Secure & Private. No personal data collected. <br />
                    <a href="/privacy" target="_blank" rel="noopener noreferrer">Privacy Policy</a>
                </p>
            </div>

            {showIoT && (
                <IoTSettingsModal
                    onClose={() => setShowIoT(false)}
                    onSave={(config) => {
                        setIoTConfig(config);
                    }}
                />
            )}
            {showHowTo && (
                <HowItWorksModal onClose={() => setShowHowTo(false)} />
            )}
        </div>
    );
}
