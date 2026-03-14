'use client';

import { useEffect, useRef, useState } from 'react';

import { useUiSounds } from '@/hooks/useUiSounds';
import type { TheaterLightingCue } from './homeAssistant';

type MovieFeedbackRating = 'loved_it' | 'pretty_good' | 'needs_fixing';

interface MovieFeedbackPayload {
    rating: MovieFeedbackRating;
    reasons: string[];
    note: string;
}

const MOVIE_FEEDBACK_REASONS = [
    { id: 'didnt_match_story', label: "Didn't match the story" },
    { id: 'characters_changed', label: 'Characters changed' },
    { id: 'wrong_place_or_props', label: 'Wrong place or props' },
    { id: 'too_much_text', label: 'Too much text' },
    { id: 'too_busy', label: 'Too busy' },
    { id: 'too_scary', label: 'Too scary' },
    { id: 'pacing_off', label: 'Pacing felt off' },
    { id: 'camera_motion', label: 'Motion felt choppy' },
] as const;

interface TheaterModeProps {
    mp4Url: string;
    tradingCardUrl?: string;
    childName?: string;
    storyTitle?: string;
    narrationLines?: string[];
    audioAvailable?: boolean;
    calmMode?: boolean;
    uiSoundsEnabled?: boolean;
    onSubmitFeedback?: (payload: MovieFeedbackPayload) => Promise<void> | void;
    onRequestRemake?: (payload: MovieFeedbackPayload) => Promise<void> | void;
    onTheaterOpened?: () => void;
    onPlaybackStart?: () => void;
    onPlaybackPause?: () => void;
    onPlaybackEnded?: () => void;
    lightingCues?: TheaterLightingCue[];
    onLightingCueChange?: (cue: TheaterLightingCue) => void;
    onMakeAnotherStory?: () => void;
    onClose: () => void;
}

function buildMovieFileName(childName?: string): string {
    const safeChildName = (childName ?? 'my')
        .replace(/[^a-z0-9-_ ]/gi, ' ')
        .trim()
        .replace(/\s+/g, '-')
        .slice(0, 40) || 'my';
    return `${safeChildName}-story.mp4`;
}

function clampLightingBrightness(value: number | undefined, fallback: number = 180): number {
    const resolved = Number.isFinite(value) ? Math.round(Number(value)) : fallback;
    return Math.max(18, Math.min(255, resolved));
}

function clampLightingIntervalMs(value: number | undefined, fallback: number): number {
    const resolved = Number.isFinite(value) ? Math.round(Number(value)) : fallback;
    return Math.max(180, Math.min(3600, resolved));
}

function parseRgbFromHex(hexColor?: string): [number, number, number] | null {
    const normalized = String(hexColor ?? '').trim().replace(/^#/, '');
    if (!/^[0-9a-fA-F]{6}$/.test(normalized)) {
        return null;
    }
    return [
        Number.parseInt(normalized.slice(0, 2), 16),
        Number.parseInt(normalized.slice(2, 4), 16),
        Number.parseInt(normalized.slice(4, 6), 16),
    ];
}

function cueRgbColor(cue: TheaterLightingCue): [number, number, number] | null {
    if (Array.isArray(cue.rgb_color) && cue.rgb_color.length === 3) {
        return [cue.rgb_color[0], cue.rgb_color[1], cue.rgb_color[2]];
    }
    return parseRgbFromHex(cue.hex_color);
}

function blendRgb(
    base: [number, number, number],
    target: [number, number, number],
    amount: number,
): [number, number, number] {
    const weight = Math.max(0, Math.min(1, amount));
    return [
        Math.round(base[0] + ((target[0] - base[0]) * weight)),
        Math.round(base[1] + ((target[1] - base[1]) * weight)),
        Math.round(base[2] + ((target[2] - base[2]) * weight)),
    ];
}

function buildCueVariant(
    cue: TheaterLightingCue,
    overrides: Partial<TheaterLightingCue>,
): TheaterLightingCue {
    const nextRgb = overrides.rgb_color ?? cue.rgb_color;
    return {
        ...cue,
        ...overrides,
        rgb_color: Array.isArray(nextRgb) && nextRgb.length === 3
            ? [nextRgb[0], nextRgb[1], nextRgb[2]]
            : undefined,
        hex_color: overrides.rgb_color ? undefined : overrides.hex_color ?? cue.hex_color,
    };
}

export default function TheaterMode({
    mp4Url,
    tradingCardUrl,
    childName,
    storyTitle,
    narrationLines,
    audioAvailable,
    calmMode = false,
    uiSoundsEnabled = true,
    onSubmitFeedback,
    onRequestRemake,
    onTheaterOpened,
    onPlaybackStart,
    onPlaybackPause,
    onPlaybackEnded,
    lightingCues,
    onLightingCueChange,
    onMakeAnotherStory,
    onClose,
}: TheaterModeProps) {
    const videoRef = useRef<HTMLVideoElement>(null);
    const audioProbeTimerRef = useRef<number | null>(null);
    const lastLightingCueIndexRef = useRef<number | null>(null);
    const lightingEffectIntervalRef = useRef<number | null>(null);
    const lightingEffectTimeoutsRef = useRef<number[]>([]);
    const onTheaterOpenedRef = useRef(onTheaterOpened);
    const [videoError, setVideoError] = useState(false);
    const [isPlaying, setIsPlaying] = useState(false);
    const [needsUserGesture, setNeedsUserGesture] = useState(false);
    const [isSaving, setIsSaving] = useState(false);
    const [isSharing, setIsSharing] = useState(false);
    const [shareNotice, setShareNotice] = useState<string | null>(null);
    const [hasVideoEnded, setHasVideoEnded] = useState(false);
    const [isRevealed, setIsRevealed] = useState(false);
    const [feedbackRating, setFeedbackRating] = useState<MovieFeedbackRating | null>(null);
    const [feedbackReasons, setFeedbackReasons] = useState<string[]>([]);
    const [feedbackNote, setFeedbackNote] = useState('');
    const [feedbackSubmitting, setFeedbackSubmitting] = useState(false);
    const [feedbackSubmitted, setFeedbackSubmitted] = useState(false);
    const [feedbackNotice, setFeedbackNotice] = useState<string | null>(null);
    const [remakeSubmitting, setRemakeSubmitting] = useState(false);
    const [feedbackModalOpen, setFeedbackModalOpen] = useState(false);
    const [heroCardModalOpen, setHeroCardModalOpen] = useState(false);
    const { playUiSound } = useUiSounds({ enabled: uiSoundsEnabled, volume: 0.92 });

    const movieFileName = buildMovieFileName(childName);
    const resolvedStoryTitle = (storyTitle ?? '').trim() || (childName ? `${childName}'s Story` : 'My Storybook Movie');
    const shareTitle = resolvedStoryTitle;
    const shareText = childName
        ? `A grown-up is sharing ${childName}'s story movie, "${resolvedStoryTitle}."`
        : 'A grown-up is sharing this story movie.';
    const hasNarrationPlan = Boolean(narrationLines?.length);
    const detailedFeedbackSelected = feedbackRating === 'pretty_good' || feedbackRating === 'needs_fixing';
    const shouldShowFeedbackPanel = hasVideoEnded || feedbackSubmitted;
    const canSendFeedback = Boolean(feedbackRating) && !feedbackSubmitting;
    const canRequestRemake = Boolean(
        onRequestRemake && detailedFeedbackSelected && !remakeSubmitting
    );

    const clearAudioProbe = () => {
        if (audioProbeTimerRef.current !== null) {
            window.clearTimeout(audioProbeTimerRef.current);
            audioProbeTimerRef.current = null;
        }
    };

    const clearLightingEffectTimers = () => {
        if (lightingEffectIntervalRef.current !== null) {
            window.clearInterval(lightingEffectIntervalRef.current);
            lightingEffectIntervalRef.current = null;
        }
        if (lightingEffectTimeoutsRef.current.length) {
            lightingEffectTimeoutsRef.current.forEach((timerId) => window.clearTimeout(timerId));
            lightingEffectTimeoutsRef.current = [];
        }
    };

    const queueLightingEffectTimeout = (callback: () => void, delayMs: number) => {
        const timerId = window.setTimeout(() => {
            lightingEffectTimeoutsRef.current = lightingEffectTimeoutsRef.current.filter((value) => value !== timerId);
            callback();
        }, delayMs);
        lightingEffectTimeoutsRef.current.push(timerId);
        return timerId;
    };

    const startLightingEffectLoop = (cue: TheaterLightingCue) => {
        if (!onLightingCueChange) {
            return;
        }
        clearLightingEffectTimers();
        const effect = cue.effect ?? 'steady';
        if (effect === 'steady') {
            return;
        }

        const baseBrightness = clampLightingBrightness(cue.brightness);
        const baseRgb = cueRgbColor(cue);

        if (effect === 'pulse') {
            const intervalMs = clampLightingIntervalMs(cue.effect_interval_ms, 960);
            let rising = false;
            lightingEffectIntervalRef.current = window.setInterval(() => {
                rising = !rising;
                onLightingCueChange(
                    buildCueVariant(cue, {
                        brightness: rising
                            ? clampLightingBrightness(Math.round((baseBrightness * 1.06) + 6), baseBrightness)
                            : clampLightingBrightness(Math.round(baseBrightness * 0.68), baseBrightness),
                        rgb_color: baseRgb
                            ? blendRgb(baseRgb, [255, 244, 224], rising ? 0.12 : 0.03)
                            : undefined,
                        transition: rising ? 0.42 : 0.68,
                    })
                );
            }, intervalMs);
            return;
        }

        if (effect === 'flicker') {
            const intervalMs = clampLightingIntervalMs(cue.effect_interval_ms, 380);
            const brightnessPattern = [0.88, 0.58, 0.96, 0.72, 1.0, 0.64];
            let step = 0;
            lightingEffectIntervalRef.current = window.setInterval(() => {
                const multiplier = brightnessPattern[step % brightnessPattern.length];
                step += 1;
                onLightingCueChange(
                    buildCueVariant(cue, {
                        brightness: clampLightingBrightness(Math.round(baseBrightness * multiplier), baseBrightness),
                        rgb_color: baseRgb
                            ? blendRgb(baseRgb, [255, 236, 196], step % 3 === 0 ? 0.1 : 0.04)
                            : undefined,
                        transition: 0.12,
                    })
                );
            }, intervalMs);
            return;
        }

        if (effect === 'flash') {
            const intervalMs = clampLightingIntervalMs(cue.effect_interval_ms, 2400);
            const flashRgb: [number, number, number] = baseRgb
                ? blendRgb(baseRgb, [255, 250, 255], 0.82)
                : [244, 248, 255];
            const runFlashSequence = () => {
                onLightingCueChange(
                    buildCueVariant(cue, {
                        brightness: clampLightingBrightness(baseBrightness + 78, baseBrightness),
                        rgb_color: flashRgb,
                        transition: 0.08,
                    })
                );
                queueLightingEffectTimeout(() => {
                    onLightingCueChange(
                        buildCueVariant(cue, {
                            brightness: baseBrightness,
                            rgb_color: baseRgb ?? cue.rgb_color,
                            transition: 0.18,
                        })
                    );
                }, 120);
                queueLightingEffectTimeout(() => {
                    onLightingCueChange(
                        buildCueVariant(cue, {
                            brightness: clampLightingBrightness(baseBrightness + 36, baseBrightness),
                            rgb_color: baseRgb ? blendRgb(baseRgb, [255, 250, 255], 0.48) : flashRgb,
                            transition: 0.06,
                        })
                    );
                }, 260);
                queueLightingEffectTimeout(() => {
                    onLightingCueChange(
                        buildCueVariant(cue, {
                            brightness: baseBrightness,
                            rgb_color: baseRgb ?? cue.rgb_color,
                            transition: Math.min(0.5, Math.max(0.18, Number(cue.transition ?? 0.34))),
                        })
                    );
                }, 390);
            };

            queueLightingEffectTimeout(runFlashSequence, Math.min(620, Math.round(intervalMs * 0.35)));
            lightingEffectIntervalRef.current = window.setInterval(runFlashSequence, intervalMs);
        }
    };

    const browserReportsAudibleTrack = (video: HTMLVideoElement | null): boolean | null => {
        if (!video) {
            return null;
        }
        const browserVideo = video as HTMLVideoElement & {
            mozHasAudio?: boolean;
            webkitAudioDecodedByteCount?: number;
            audioTracks?: { length: number } | undefined;
        };
        if (typeof browserVideo.mozHasAudio === 'boolean') {
            return browserVideo.mozHasAudio;
        }
        if (browserVideo.audioTracks && typeof browserVideo.audioTracks.length === 'number') {
            return browserVideo.audioTracks.length > 0;
        }
        if (typeof browserVideo.webkitAudioDecodedByteCount === 'number') {
            return browserVideo.webkitAudioDecodedByteCount > 0;
        }
        return null;
    };

    useEffect(() => {
        onTheaterOpenedRef.current = onTheaterOpened;
    }, [onTheaterOpened]);

    useEffect(() => {
        const video = videoRef.current;
        if (!video) {
            return;
        }
        onTheaterOpenedRef.current?.();
        setVideoError(false);
        setIsRevealed(false);
        setNeedsUserGesture(false);
        setHasVideoEnded(false);
        setFeedbackRating(null);
        setFeedbackReasons([]);
        setFeedbackNote('');
        setFeedbackSubmitting(false);
        setFeedbackSubmitted(false);
        setFeedbackNotice(null);
        setRemakeSubmitting(false);
        setFeedbackModalOpen(false);
        setHeroCardModalOpen(false);
        lastLightingCueIndexRef.current = null;
        clearAudioProbe();
        clearLightingEffectTimers();
        video.pause();
        video.load();
        video.currentTime = 0;
        video.muted = calmMode;
        video.volume = calmMode ? 0 : 1;
        video.play().then(() => {
            setIsPlaying(true);
            setNeedsUserGesture(false);
        }).catch((error) => {
            console.warn('Autoplay blocked:', error);
            setIsPlaying(false);
            setNeedsUserGesture(true);
        });
    }, [mp4Url]);

    useEffect(() => {
        const timer = window.setTimeout(() => {
            setIsRevealed(true);
        }, 60);
        return () => window.clearTimeout(timer);
    }, [mp4Url]);

    useEffect(() => {
        const onKeyDown = (event: KeyboardEvent) => {
            if (event.key === 'Escape') {
                if (heroCardModalOpen) {
                    setHeroCardModalOpen(false);
                    return;
                }
                if (feedbackModalOpen) {
                    setFeedbackModalOpen(false);
                    return;
                }
                onClose();
            }
        };
        window.addEventListener('keydown', onKeyDown);
        return () => window.removeEventListener('keydown', onKeyDown);
    }, [feedbackModalOpen, heroCardModalOpen, onClose]);

    useEffect(() => {
        return () => {
            clearAudioProbe();
            clearLightingEffectTimers();
        };
    }, []);

    const emitLightingCueForCurrentTime = (force: boolean = false) => {
        if (!onLightingCueChange || !lightingCues?.length) {
            clearLightingEffectTimers();
            return;
        }
        const video = videoRef.current;
        if (!video) {
            clearLightingEffectTimers();
            return;
        }
        const currentTime = Math.max(0, Number(video.currentTime || 0));
        let cueIndex = -1;
        for (let idx = 0; idx < lightingCues.length; idx += 1) {
            const cue = lightingCues[idx];
            const endSeconds = typeof cue.end_seconds === 'number'
                ? cue.end_seconds
                : Number.POSITIVE_INFINITY;
            if (currentTime >= cue.start_seconds && currentTime < endSeconds) {
                cueIndex = idx;
                break;
            }
        }
        if (cueIndex < 0) {
            if (currentTime >= lightingCues[lightingCues.length - 1].start_seconds) {
                cueIndex = lightingCues.length - 1;
            } else {
                return;
            }
        }
        if (!force && lastLightingCueIndexRef.current === cueIndex) {
            return;
        }
        lastLightingCueIndexRef.current = cueIndex;
        const activeCue = lightingCues[cueIndex];
        onLightingCueChange(activeCue);
        if (!calmMode && !video.paused && !video.ended) {
            startLightingEffectLoop(activeCue);
        } else {
            clearLightingEffectTimers();
        }
    };

    useEffect(() => {
        if (videoRef.current) {
            videoRef.current.muted = calmMode;
            videoRef.current.volume = calmMode ? 0 : 1;
        }
        if (videoRef.current && lightingCues?.length && onLightingCueChange) {
            emitLightingCueForCurrentTime(true);
        } else if (calmMode) {
            clearLightingEffectTimers();
        }
        if (calmMode) {
            clearAudioProbe();
        }
    }, [calmMode, lightingCues, onLightingCueChange]);

    useEffect(() => {
        if (!shareNotice) {
            return;
        }
        const timer = window.setTimeout(() => setShareNotice(null), 4500);
        return () => window.clearTimeout(timer);
    }, [shareNotice]);

    useEffect(() => {
        if (!feedbackNotice) {
            return;
        }
        const timer = window.setTimeout(() => setFeedbackNotice(null), 5000);
        return () => window.clearTimeout(timer);
    }, [feedbackNotice]);

    const fetchMovieFile = async (): Promise<File> => {
        const response = await fetch(mp4Url);
        if (!response.ok) {
            throw new Error(`Movie download failed with status ${response.status}`);
        }
        const blob = await response.blob();
        return new File([blob], movieFileName, { type: blob.type || 'video/mp4' });
    };

    const handleManualPlay = () => {
        const video = videoRef.current;
        if (!video) {
            return;
        }
        setHasVideoEnded(false);
        video.play().then(() => {
            setIsPlaying(true);
            setNeedsUserGesture(false);
        }).catch((error) => {
            console.warn('Manual play failed:', error);
            setNeedsUserGesture(true);
        });
    };

    const handleReplay = () => {
        const video = videoRef.current;
        if (!video) {
            return;
        }
        setHasVideoEnded(false);
        setNeedsUserGesture(false);
        setVideoError(false);
        video.currentTime = 0;
        video.load();
        video.play().then(() => {
            setIsPlaying(true);
        }).catch((error) => {
            console.warn('Replay failed:', error);
            setNeedsUserGesture(true);
        });
    };

    const handleSaveToDevice = async () => {
        if (isSaving) {
            return;
        }
        setIsSaving(true);
        try {
            const movieFile = await fetchMovieFile();
            const blob = new Blob([movieFile], { type: movieFile.type || 'video/mp4' });
            const blobUrl = URL.createObjectURL(blob);
            const anchor = document.createElement('a');
            anchor.href = blobUrl;
            anchor.download = movieFileName;
            document.body.appendChild(anchor);
            anchor.click();
            document.body.removeChild(anchor);
            setTimeout(() => URL.revokeObjectURL(blobUrl), 10000);
            setShareNotice('Movie saved to this device.');
        } catch (error) {
            console.warn('Download failed, falling back to direct link:', error);
            const anchor = document.createElement('a');
            anchor.href = mp4Url;
            anchor.download = movieFileName;
            anchor.click();
            setShareNotice('Opening the movie file directly.');
        } finally {
            setIsSaving(false);
        }
    };

    const handleCopyMovieLink = async () => {
        try {
            if (navigator.clipboard?.writeText) {
                await navigator.clipboard.writeText(mp4Url);
                setShareNotice('Movie link copied for a grown-up.');
                return;
            }
        } catch (error) {
            console.warn('Clipboard copy failed:', error);
        }
        window.prompt('Copy this movie link:', mp4Url);
        setShareNotice('Movie link ready to copy.');
    };

    const handleShareWithFamily = async () => {
        if (isSharing) {
            return;
        }
        setIsSharing(true);
        try {
            if (typeof navigator !== 'undefined' && typeof navigator.share === 'function') {
                try {
                    const movieFile = await fetchMovieFile();
                    if (typeof navigator.canShare === 'function' && navigator.canShare({ files: [movieFile] })) {
                        await navigator.share({
                            title: shareTitle,
                            text: shareText,
                            files: [movieFile],
                        });
                        setShareNotice('Movie shared.');
                        return;
                    }
                } catch (fileShareError) {
                    console.warn('File share fallback to link:', fileShareError);
                }

                await navigator.share({
                    title: shareTitle,
                    text: shareText,
                    url: mp4Url,
                });
                setShareNotice('Movie shared.');
                return;
            }

            await handleCopyMovieLink();
        } catch (error) {
            if ((error as Error)?.name !== 'AbortError') {
                console.warn('Share failed:', error);
                setShareNotice('Sharing did not work there. Try Copy Movie Link.');
            }
        } finally {
            setIsSharing(false);
        }
    };

    const toggleFeedbackReason = (reasonId: string) => {
        setFeedbackReasons((current) =>
            current.includes(reasonId)
                ? current.filter((value) => value !== reasonId)
                : [...current, reasonId]
        );
    };

    const handleRatingSelect = (rating: MovieFeedbackRating) => {
        setFeedbackRating(rating);
        setFeedbackSubmitted(false);
        setFeedbackNotice(null);
        if (rating === 'loved_it') {
            setFeedbackReasons([]);
            setFeedbackModalOpen(false);
            return;
        }
        setFeedbackModalOpen(true);
    };

    const handleFeedbackSubmit = async () => {
        if (!feedbackRating || feedbackSubmitting) {
            return;
        }
        setFeedbackSubmitting(true);
        try {
            await Promise.resolve(
                onSubmitFeedback?.({
                    rating: feedbackRating,
                    reasons: feedbackRating === 'loved_it' ? [] : feedbackReasons,
                    note: feedbackNote.trim(),
                })
            );
            setFeedbackSubmitted(true);
            setFeedbackModalOpen(false);
            setFeedbackNotice('Thanks. Amelia saved that grown-up feedback.');
        } catch (error) {
            console.warn('Movie feedback failed:', error);
            setFeedbackNotice('Feedback did not send. Please try again.');
        } finally {
            setFeedbackSubmitting(false);
        }
    };

    const handleRequestRemake = async () => {
        if (!feedbackRating || feedbackRating === 'loved_it' || remakeSubmitting) {
            return;
        }
        setRemakeSubmitting(true);
        try {
            await Promise.resolve(
                onRequestRemake?.({
                    rating: feedbackRating,
                    reasons: feedbackReasons,
                    note: feedbackNote.trim(),
                })
            );
            setFeedbackSubmitted(true);
            setFeedbackModalOpen(false);
            setFeedbackNotice('Making a better version now...');
        } catch (error) {
            console.warn('Movie remake request failed:', error);
            setFeedbackNotice('Could not start a better version. Please try again.');
        } finally {
            setRemakeSubmitting(false);
        }
    };

    return (
        <div
            className={`theater-overlay ${isRevealed ? 'open' : ''}`}
            role="dialog"
            aria-modal="true"
            aria-label="Your Story Movie Theater"
        >
            <div className="theater-curtain" aria-hidden="true">
                <div className="curtain-panel curtain-left" />
                <div className="curtain-panel curtain-right" />
            </div>

            <div className="confetti-burst" aria-hidden="true">
                {Array.from({ length: 20 }).map((_, index) => (
                    <span key={index} className={`confetti-piece confetti-${index % 5}`} />
                ))}
            </div>

            <div className="theater-card">
                <div className="theater-header">
                    <div className="theater-title-block">
                        <div className="theater-kicker">Storybook Premiere</div>
                        <h2 className="theater-title">{resolvedStoryTitle}</h2>
                        {childName && (
                            <p className="theater-byline">Made for {childName}</p>
                        )}
                    </div>
                    <button
                        className="theater-close-btn"
                        onClick={() => {
                            playUiSound('close');
                            onClose();
                        }}
                        aria-label="Close theater"
                    >
                        ✕
                    </button>
                </div>

                <div className="theater-content">
                    <div className="theater-video-stage">
                        <div className="theater-video-wrapper">
                            {videoError ? (
                                <div className="theater-video-error" role="alert">
                                    <p>The movie is still getting ready. Try again in a moment.</p>
                                    <button
                                        className="magic-btn"
                                        onClick={() => {
                                            playUiSound('tap');
                                            setVideoError(false);
                                            const video = videoRef.current;
                                            if (!video) {
                                                return;
                                            }
                                            video.load();
                                            void video.play().then(() => {
                                                setIsPlaying(true);
                                                setNeedsUserGesture(false);
                                            }).catch((error) => {
                                                console.warn('Retry play failed:', error);
                                                setNeedsUserGesture(true);
                                            });
                                        }}
                                    >
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
                                        preload="metadata"
                                        muted={calmMode}
                                        playsInline
                                        onError={() => setVideoError(true)}
                                        onLoadedMetadata={() => {
                                            const hasAudioTrack = browserReportsAudibleTrack(videoRef.current);
                                            if (audioAvailable === true && hasNarrationPlan && hasAudioTrack === false) {
                                                setFeedbackNotice('This movie render is missing its narrator audio and needs a rebuild.');
                                            }
                                        }}
                                        onPlay={() => {
                                            setIsPlaying(true);
                                            setHasVideoEnded(false);
                                            clearAudioProbe();
                                            onPlaybackStart?.();
                                            emitLightingCueForCurrentTime(true);
                                        }}
                                        onPause={() => {
                                            setIsPlaying(false);
                                            clearAudioProbe();
                                            clearLightingEffectTimers();
                                            onPlaybackPause?.();
                                        }}
                                        onEnded={() => {
                                            setIsPlaying(false);
                                            setHasVideoEnded(true);
                                            clearAudioProbe();
                                            clearLightingEffectTimers();
                                            onPlaybackEnded?.();
                                        }}
                                        onSeeked={() => {
                                            emitLightingCueForCurrentTime(true);
                                        }}
                                        onTimeUpdate={() => {
                                            emitLightingCueForCurrentTime(false);
                                        }}
                                        aria-label="Your completed story movie"
                                    />
                                    {needsUserGesture && (
                                        <button
                                            className="theater-play-overlay"
                                            onClick={() => {
                                                playUiSound('tap');
                                                handleManualPlay();
                                            }}
                                            aria-label="Play your story movie"
                                        >
                                            ▶ Tap to Play
                                        </button>
                                    )}
                                    {hasVideoEnded && !videoError && (
                                        <div className="theater-end-card" role="status" aria-live="polite">
                                            <div className="theater-end-kicker">The End</div>
                                            <h3 className="theater-end-title">Your storybook movie is ready to replay.</h3>
                                            <p className="theater-end-copy">
                                                Watch it again or jump into a brand-new adventure.
                                            </p>
                                            <div className="theater-end-actions">
                                                <button
                                                    className="magic-btn"
                                                    onClick={() => {
                                                        playUiSound('celebrate');
                                                        handleReplay();
                                                    }}
                                                >
                                                    🔄 Watch Again!
                                                </button>
                                                <button
                                                    className="magic-btn magic-btn-gold"
                                                    onClick={() => {
                                                        playUiSound('magic');
                                                        if (onMakeAnotherStory) {
                                                            onMakeAnotherStory();
                                                        } else {
                                                            onClose();
                                                        }
                                                    }}
                                                >
                                                    ✨ Make Another Story!
                                                </button>
                                            </div>
                                        </div>
                                    )}
                                </>
                            )}
                        </div>
                    </div>

                    <div className="theater-status-row" role="status" aria-live="polite">
                        {feedbackNotice || shareNotice || (
                            isPlaying
                                ? 'Your storybook movie is playing.'
                                : hasVideoEnded
                                    ? 'The movie finished. Replay it or share grown-up feedback.'
                                    : 'Your storybook movie is ready.'
                        )}
                    </div>
                </div>

                <div className="theater-footer">
                    <div className="theater-actions-kid">
                        <button
                            className="magic-btn"
                            onClick={() => {
                                playUiSound('celebrate');
                                handleReplay();
                            }}
                        >
                            🔄 Watch Again!
                        </button>

                        <button
                            className="magic-btn magic-btn-gold"
                            onClick={() => {
                                playUiSound('magic');
                                if (onMakeAnotherStory) {
                                    onMakeAnotherStory();
                                } else {
                                    onClose();
                                }
                            }}
                        >
                            ✨ Make Another Story!
                        </button>
                    </div>

                    {shouldShowFeedbackPanel && (
                        <section className="theater-feedback-panel">
                            <div className="theater-feedback-copy">
                                <div className="theater-feedback-kicker">Grown-up Review</div>
                                <p className="theater-feedback-helper">
                                    Keep the video big on screen. Use the quick rating here, then open detailed feedback only if needed.
                                </p>
                            </div>
                            <div className="theater-feedback-ratings">
                                <button
                                    type="button"
                                    className={`theater-feedback-btn ${feedbackRating === 'loved_it' ? 'active' : ''}`}
                                    onClick={() => {
                                        playUiSound('tap');
                                        handleRatingSelect('loved_it');
                                    }}
                                >
                                    Loved It!
                                </button>
                                <button
                                    type="button"
                                    className={`theater-feedback-btn ${feedbackRating === 'pretty_good' ? 'active' : ''}`}
                                    onClick={() => {
                                        playUiSound('tap');
                                        handleRatingSelect('pretty_good');
                                    }}
                                >
                                    Pretty Good
                                </button>
                                <button
                                    type="button"
                                    className={`theater-feedback-btn ${feedbackRating === 'needs_fixing' ? 'active' : ''}`}
                                    onClick={() => {
                                        playUiSound('tap');
                                        handleRatingSelect('needs_fixing');
                                    }}
                                >
                                    Needs Fixing
                                </button>
                            </div>
                            <div className="theater-feedback-actions">
                                {feedbackRating === 'loved_it' && (
                                    <button
                                        className="theater-feedback-submit"
                                        onClick={() => {
                                            playUiSound('tap');
                                            void handleFeedbackSubmit();
                                        }}
                                        disabled={!canSendFeedback}
                                    >
                                        {feedbackSubmitting ? 'Sending...' : feedbackSubmitted ? 'Feedback Sent' : 'Send Quick Feedback'}
                                    </button>
                                )}
                                {detailedFeedbackSelected && (
                                    <>
                                        <button
                                            className="theater-feedback-submit"
                                            onClick={() => {
                                                playUiSound('tap');
                                                setFeedbackModalOpen(true);
                                            }}
                                        >
                                            {feedbackSubmitted ? 'Edit Grown-up Feedback' : 'Give Grown-up Feedback'}
                                        </button>
                                        {canRequestRemake && (
                                            <button
                                                className="theater-feedback-remake"
                                                onClick={() => {
                                                    playUiSound('magic');
                                                    void handleRequestRemake();
                                                }}
                                                disabled={remakeSubmitting}
                                            >
                                                {remakeSubmitting ? 'Starting Better Version...' : 'Make Better Version'}
                                            </button>
                                        )}
                                    </>
                                )}
                            </div>
                        </section>
                    )}

                    <div className="theater-adult-tools">
                        <button
                            onClick={() => {
                                playUiSound('tap');
                                void handleSaveToDevice();
                            }}
                            disabled={isSaving}
                        >
                            {isSaving ? '⏳ Saving…' : '💾 Save Movie'}
                        </button>
                        <button
                            onClick={() => {
                                playUiSound('tap');
                                void handleShareWithFamily();
                            }}
                            disabled={isSharing}
                        >
                            {isSharing ? '⏳ Sharing…' : '💌 Share with Family'}
                        </button>
                        {tradingCardUrl && (
                            <button
                                onClick={() => {
                                    playUiSound('tap');
                                    setHeroCardModalOpen(true);
                                }}
                            >
                                🃏 Hero Card
                            </button>
                        )}
                    </div>
                </div>
            </div>

            {feedbackModalOpen && (
                <div className="theater-modal-backdrop" role="presentation">
                    <div className="theater-modal-card" role="dialog" aria-modal="true" aria-label="Grown-up feedback">
                        <div className="theater-modal-header">
                            <div>
                                <div className="theater-feedback-kicker">Grown-up Feedback</div>
                                <h3 className="theater-modal-title">Help Amelia improve the next version</h3>
                            </div>
                            <button
                                className="theater-modal-close"
                                onClick={() => {
                                    playUiSound('close');
                                    setFeedbackModalOpen(false);
                                }}
                                aria-label="Close feedback"
                            >
                                ✕
                            </button>
                        </div>

                        <p className="theater-modal-copy">
                            Pick anything that felt off, then add a short note if you want. The video stays on screen behind this.
                        </p>

                        <div className="theater-reason-grid">
                            {MOVIE_FEEDBACK_REASONS.map((reason) => (
                                <button
                                    key={reason.id}
                                    type="button"
                                    className={`theater-reason-chip ${feedbackReasons.includes(reason.id) ? 'active' : ''}`}
                                    onClick={() => {
                                        playUiSound('tap');
                                        toggleFeedbackReason(reason.id);
                                    }}
                                >
                                    {reason.label}
                                </button>
                            ))}
                        </div>

                        <label className="theater-feedback-label" htmlFor="theater-feedback-note">
                            Anything else to fix?
                        </label>
                        <textarea
                            id="theater-feedback-note"
                            className="theater-feedback-textarea"
                            value={feedbackNote}
                            onChange={(event) => setFeedbackNote(event.target.value)}
                            placeholder="Example: Make the river sparkle more and keep the main character looking the same on every page."
                        />

                        <div className="theater-modal-actions">
                            <button
                                className="theater-modal-secondary"
                                onClick={() => {
                                    playUiSound('close');
                                    setFeedbackModalOpen(false);
                                }}
                            >
                                Keep Watching
                            </button>
                            <button
                                className="theater-modal-primary"
                                onClick={() => {
                                    playUiSound('tap');
                                    void handleFeedbackSubmit();
                                }}
                                disabled={!canSendFeedback}
                            >
                                {feedbackSubmitting ? 'Sending...' : 'Send Grown-up Feedback'}
                            </button>
                            {canRequestRemake && (
                                <button
                                    className="theater-modal-remake"
                                    onClick={() => {
                                        playUiSound('magic');
                                        void handleRequestRemake();
                                    }}
                                    disabled={remakeSubmitting}
                                >
                                    {remakeSubmitting ? 'Starting...' : 'Make Better Version'}
                                </button>
                            )}
                        </div>
                    </div>
                </div>
            )}

            {heroCardModalOpen && tradingCardUrl && (
                <div className="theater-modal-backdrop" role="presentation">
                    <div className="theater-modal-card theater-hero-card-modal" role="dialog" aria-modal="true" aria-label="Hero card">
                        <div className="theater-modal-header">
                            <div>
                                <div className="theater-feedback-kicker">Hero Card</div>
                                <h3 className="theater-modal-title">{resolvedStoryTitle}</h3>
                            </div>
                            <button
                                className="theater-modal-close"
                                onClick={() => {
                                    playUiSound('close');
                                    setHeroCardModalOpen(false);
                                }}
                                aria-label="Close hero card"
                            >
                                ✕
                            </button>
                        </div>
                        <div className="theater-hero-card-stage">
                            <img src={tradingCardUrl} alt={`${resolvedStoryTitle} hero card`} className="theater-hero-card-image" />
                        </div>
                    </div>
                </div>
            )}
        </div>
    );
}
