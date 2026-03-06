'use client';

/**
 * TheaterMode — Storybook Theater for reviewing and sharing the final story movie.
 *
 * Features (Iteration 8, Contest Winner Audit #3 — Storybook Theater):
 * - Full-screen video playback of the assembled .mp4
 * - "Hero Trading Card" display alongside the video
 * - "Watch Again" and "Save to Tablet" buttons
 * - No public sharing links (COPPA — Iter 3 #6)
 * - Confetti burst on entry (playful micro-interaction, Iter 4 #5)
 * - Accessible: keyboard navigable, aria-labels, role="dialog"
 * - Corrupted MP4 header detection (Iter 5 #6 — graceful error)
 */

import { useEffect, useRef, useState } from 'react';

interface TheaterModeProps {
    mp4Url: string;
    tradingCardUrl?: string;
    childName?: string;
    narrationLines?: string[];
    audioAvailable?: boolean;
    onClose: () => void;
}

export default function TheaterMode({
    mp4Url,
    tradingCardUrl,
    childName,
    narrationLines,
    audioAvailable,
    onClose,
}: TheaterModeProps) {
    const videoRef = useRef<HTMLVideoElement>(null);
    const [videoError, setVideoError] = useState(false);
    const [isPlaying, setIsPlaying] = useState(false);
    const [replayCount, setReplayCount] = useState(0);
    const [cooldownRemaining, setCooldownRemaining] = useState(0);
    const [needsUserGesture, setNeedsUserGesture] = useState(false);
    const [isSaving, setIsSaving] = useState(false);
    const [ttsSupported, setTtsSupported] = useState(false);
    const [ttsActive, setTtsActive] = useState(false);

    // Auto play on mount
    useEffect(() => {
        const video = videoRef.current;
        if (!video) return;
        setNeedsUserGesture(false);
        video.play().then(() => {
            setIsPlaying(true);
            setNeedsUserGesture(false);
        }).catch((e) => {
            console.warn('Autoplay blocked:', e);
            setIsPlaying(false);
            setNeedsUserGesture(true);
        });
    }, [mp4Url]);

    // Keyboard close (Esc)
    useEffect(() => {
        const handler = (e: KeyboardEvent) => {
            if (e.key === 'Escape') onClose();
        };
        window.addEventListener('keydown', handler);
        return () => window.removeEventListener('keydown', handler);
    }, [onClose]);

    useEffect(() => {
        if (cooldownRemaining <= 0) return;
        const timer = window.setInterval(() => {
            setCooldownRemaining((seconds) => Math.max(seconds - 1, 0));
        }, 1000);
        return () => window.clearInterval(timer);
    }, [cooldownRemaining]);

    useEffect(() => {
        if (typeof window === 'undefined') return;
        setTtsSupported('speechSynthesis' in window);
        return () => {
            if ('speechSynthesis' in window) {
                window.speechSynthesis.cancel();
            }
        };
    }, []);

    const handleVideoError = () => {
        // Corrupted MP4 Header detection (Iter 5 #6)
        setVideoError(true);
    };

    const handleManualPlay = () => {
        const video = videoRef.current;
        if (!video) return;
        video.play().then(() => {
            setIsPlaying(true);
            setNeedsUserGesture(false);
        }).catch((e) => {
            console.warn('Manual play failed:', e);
            setNeedsUserGesture(true);
        });
    };

    const canNarrate = audioAvailable === false && narrationLines && narrationLines.length > 0 && ttsSupported;

    const stopNarration = () => {
        if (typeof window !== 'undefined' && 'speechSynthesis' in window) {
            window.speechSynthesis.cancel();
        }
        setTtsActive(false);
    };

    const startNarration = () => {
        if (!canNarrate || !narrationLines) return;
        stopNarration();
        setTtsActive(true);
        let idx = 0;
        const speakNext = () => {
            if (!narrationLines || idx >= narrationLines.length) {
                setTtsActive(false);
                return;
            }
            const utter = new SpeechSynthesisUtterance(narrationLines[idx]);
            utter.rate = 0.9;
            utter.pitch = 1.0;
            utter.onend = () => {
                idx += 1;
                speakNext();
            };
            utter.onerror = () => {
                setTtsActive(false);
            };
            window.speechSynthesis.speak(utter);
        };
        speakNext();
    };

    const handleSaveToDevice = async () => {
        if (isSaving) return;
        setIsSaving(true);
        try {
            // Fetch & blob ensures browser shows Save dialog even for
            // cross-origin GCS URLs that default Content-Disposition:inline.
            const resp = await fetch(mp4Url);
            const blob = await resp.blob();
            const blobUrl = URL.createObjectURL(blob);
            const a = document.createElement('a');
            a.href = blobUrl;
            a.download = `${childName ?? 'my'}-story.mp4`;
            document.body.appendChild(a);
            a.click();
            document.body.removeChild(a);
            setTimeout(() => URL.revokeObjectURL(blobUrl), 10000);
        } catch (e) {
            console.warn('Download failed, falling back to direct link:', e);
            const a = document.createElement('a');
            a.href = mp4Url;
            a.download = `${childName ?? 'my'}-story.mp4`;
            a.click();
        } finally {
            setIsSaving(false);
        }
    };

    return (
        <div
            className="theater-overlay"
            role="dialog"
            aria-modal="true"
            aria-label="Your Story Movie Theater"
        >
            {/* Confetti burst — CSS keyframe animation (Iter 4 #5 micro-interactions) */}
            <div className="confetti-burst" aria-hidden="true">
                {Array.from({ length: 20 }).map((_, i) => (
                    <span key={i} className={`confetti-piece confetti-${i % 5}`} />
                ))}
            </div>

            <div className="theater-card">
                <div className="theater-header">
                    <h2 className="theater-title">
                        🎬 {childName ? `${childName}'s` : 'Your'} Magical Story!
                    </h2>
                    <button
                        className="theater-close-btn"
                        onClick={onClose}
                        aria-label="Close theater and return to story"
                    >
                        ✕
                    </button>
                </div>

                <div className="theater-content">
                    {/* Main Video Player */}
                    <div className="theater-video-wrapper">
                        {videoError ? (
                            <div className="theater-video-error" role="alert">
                                <p>🌟 The movie is still being made! Come back in a moment.</p>
                                <button onClick={() => setVideoError(false)} className="retry-btn">
                                    Try Again
                                </button>
                            </div>
                        ) : (
                            <>
                                <video
                                    ref={videoRef}
                                    src={mp4Url}
                                    className="theater-video"
                                    controls
                                    playsInline
                                    onError={handleVideoError}
                                    onPlay={() => setIsPlaying(true)}
                                    onPause={() => setIsPlaying(false)}
                                    aria-label="Your completed story movie"
                                />
                                {needsUserGesture && (
                                    <button
                                        className="theater-play-overlay"
                                        onClick={handleManualPlay}
                                        aria-label="Play your story movie"
                                    >
                                        ▶ Tap to Play with Sound
                                    </button>
                                )}
                            </>
                        )}
                    </div>

                    {/* Hero Trading Card (Iter 8 #3) */}
                    {tradingCardUrl && (
                        <div className="trading-card-section">
                            <h3 className="trading-card-title">🃏 Your Hero Card!</h3>
                            <img
                                src={tradingCardUrl}
                                alt="Your story's hero trading card"
                                className="trading-card-image"
                            />
                        </div>
                    )}
                </div>

                {/* Action buttons */}
                <div className="theater-actions">
                    <button
                        className="theater-action-btn watch-again"
                        onClick={() => {
                            if (cooldownRemaining > 0) return;
                            if (videoRef.current) {
                                const nextReplayCount = replayCount + 1;
                                setReplayCount(nextReplayCount);
                                if (nextReplayCount >= 3) {
                                    setCooldownRemaining(60);
                                }
                                videoRef.current.currentTime = 0;
                                videoRef.current.play();
                            }
                        }}
                        disabled={cooldownRemaining > 0}
                        aria-label="Watch the movie again from the beginning"
                    >
                        {cooldownRemaining > 0 ? `⏳ Stretch Break (${cooldownRemaining}s)` : '🔄 Watch Again!'}
                    </button>

                    <button
                        className="theater-action-btn save-btn"
                        onClick={handleSaveToDevice}
                        disabled={isSaving}
                        aria-label="Save your movie to this device"
                    >
                        {isSaving ? '⏳ Saving…' : '💾 Save My Movie'}
                    </button>

                    {canNarrate && (
                        <button
                            className="theater-action-btn narration-btn"
                            onClick={() => (ttsActive ? stopNarration() : startNarration())}
                            aria-label={ttsActive ? 'Stop narration' : 'Read the story aloud'}
                        >
                            {ttsActive ? '🔇 Stop Narration' : '🔊 Read It Aloud'}
                        </button>
                    )}

                    {/* NO public sharing link per COPPA (Iter 3 #6) */}
                    <button
                        className="theater-action-btn new-story-btn"
                        onClick={onClose}
                        aria-label="Go back and make a new story"
                    >
                        ✨ Make Another Story!
                    </button>
                </div>

                <p className="theater-privacy-note" aria-label="Privacy note">
                    🔒 Your movie is saved privately and will be automatically deleted after 24 hours.
                </p>
                {canNarrate && (
                    <p className="theater-audio-note" role="status" aria-live="polite">
                        Narration isn’t baked into this video yet — tap “Read It Aloud” to hear the story.
                    </p>
                )}
                {cooldownRemaining > 0 && (
                    <p className="theater-privacy-note" role="status" aria-live="polite">
                        Time for a new adventure or a stretch break!
                    </p>
                )}
            </div>
        </div>
    );
}
