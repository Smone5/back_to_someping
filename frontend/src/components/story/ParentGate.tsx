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
 * - "Calm Mode" toggle (grayscale + audio off) for sensory-overloaded children (Iter 5 #3)
 * - Large, high-contrast buttons (WCAG AA/AAA, Iter 4 #5)
 * - aria-labels for screen reader support
 * - Cannot be "accidentally" passed by a child mashing the screen
 */

import { useState, useCallback, useEffect } from 'react';
import IoTSettingsModal, { IoTConfig } from './IoTSettingsModal';

interface ParentGateProps {
    onApproved: (calmMode: boolean, iotConfig: IoTConfig | null) => void;
}

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
    const [calmMode, setCalmMode] = useState(false);
    const [attempts, setAttempts] = useState(0);
    const [showIoT, setShowIoT] = useState(false);
    const [iotConfig, setIoTConfig] = useState<IoTConfig | null>(null);
    const [showHowTo, setShowHowTo] = useState(false);
    const [isMounted, setIsMounted] = useState(false);

    // Initial mount hydration fix
    useEffect(() => {
        setIsMounted(true);
    }, []);

    // Load initial IoT config if exists
    useEffect(() => {
        try {
            const saved = localStorage.getItem('storyteller_iot_config');
            if (saved) setIoTConfig(JSON.parse(saved));
        } catch (e) {
            // Ignore
        }
    }, []);

    const handleSubmit = useCallback(() => {
        const requireMath = process.env.NEXT_PUBLIC_REQUIRE_MATH !== 'false';

        if (!requireMath) {
            onApproved(calmMode, iotConfig);
            return;
        }

        const userAnswer = parseInt(input, 10);
        if (userAnswer === challenge.answer) {
            onApproved(calmMode, iotConfig);
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
    }, [input, challenge.answer, calmMode, attempts, iotConfig, onApproved]);

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
            <div className="parent-gate-card">
                <div className="parent-gate-header-row">
                    <div className="parent-gate-icon" aria-hidden="true">🔒</div>
                    <button
                        className="parent-gate-settings-btn"
                        onClick={() => setShowIoT(true)}
                        title="Connect room lights (Home Assistant)"
                        aria-label="Open Smart Home Lights settings"
                    >
                        💡 Lights
                    </button>
                </div>
                <h1 className="parent-gate-title">Hey Parent or Guardian!</h1>
                <p className="parent-gate-subtitle">
                    This app uses the microphone so your child can tell stories by talking.
                    For the best experience, please make sure you are in a quiet place without a lot of background noise.
                    Please solve this quick puzzle to give permission.
                </p>

                <button
                    type="button"
                    className="parent-gate-how-to-btn"
                    onClick={() => setShowHowTo((v) => !v)}
                    aria-expanded={showHowTo}
                >
                    {showHowTo ? '▼ Hide' : '▶ How to use'} instructions
                </button>
                {showHowTo && (
                    <div className="parent-gate-how-to" role="region" aria-label="How to use the app">
                        <p><strong>For your child:</strong></p>
                        <ul>
                            <li><strong>Just talk to Amelia</strong> when the glowing orb appears. She’ll ask for their name and what kind of story they want.</li>
                        </ul>
                        <p><strong>Optional — room lights:</strong> Tap <strong>💡 Lights</strong> above to connect Home Assistant so Amelia can change your room lights to match the story. Works with lights linked through Home Assistant (including many Google Home–compatible lights).</p>
                    </div>
                )}

                {process.env.NEXT_PUBLIC_REQUIRE_MATH !== 'false' && (
                    <div className="parent-gate-challenge" aria-label="Math challenge">
                        <span className="challenge-text">
                            What is <strong>{challenge.a}</strong> + <strong>{challenge.b}</strong>?
                        </span>
                        <input
                            type="number"
                            className="challenge-input"
                            value={input}
                            onChange={(e) => { setInput(e.target.value); setError(''); }}
                            aria-label="Enter your answer"
                            placeholder="Your answer"
                            autoFocus
                            inputMode="numeric"
                        />
                    </div>
                )}

                {error && process.env.NEXT_PUBLIC_REQUIRE_MATH !== 'false' && (
                    <p className="parent-gate-error" role="alert">{error}</p>
                )}

                {/* Calm Mode toggle — for children sensitive to sensory stimulation (Iter 5 #3) */}
                <label className="calm-mode-toggle" aria-label="Enable calm mode for sensory-sensitive children">
                    <input
                        type="checkbox"
                        checked={calmMode}
                        onChange={(e) => setCalmMode(e.target.checked)}
                    />
                    <span className="calm-toggle-label">🌙 Calm Mode (quieter sounds & softer colors)</span>
                </label>

                <button
                    className="parent-gate-btn"
                    onClick={handleSubmit}
                    aria-label="Submit answer and allow microphone"
                >
                    Allow Storytelling ✨
                </button>

                <p className="parent-gate-privacy">
                    🔒 Audio is never stored. We never collect personal information. <br />
                    Read our <a href="/privacy" target="_blank" rel="noopener noreferrer">Privacy Policy</a>.
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
        </div>
    );
}
