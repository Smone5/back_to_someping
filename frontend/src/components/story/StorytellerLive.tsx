'use client';

/**
 * StorytellerLive — The main Bidi-streaming storytelling experience.
 *
 * This is the root component that orchestrates:
 * 1. ParentGate (COPPA approval)
 * 2. useWebSocket (multiplexed WS connection)
 * 3. useAudioWorklet (16kHz mic -> WS, 24kHz playback, wake lock)
 * 4. useSfxDucker (sequential Foley SFX queue)
 * 5. MagicMirror (WebGL visualizer)
 * 6. Scene Video display (Veo 3.1 clips)
 * 7. TheaterMode (final movie playback)
 * 8. Rewind wand button (with disable-state debounce)
 * 9. Optional camera capture (physical object integration)
 *
 * Interaction is fully voice-controlled: mic streams to Gemini Live and ADK
 * handles turn-taking via automatic voice activity detection (no tap required).
 */

import dynamic from 'next/dynamic';
import { useCallback, useEffect, useRef, useState } from 'react';

import { useAudioWorklet } from '@/hooks/useAudioWorklet';
import { useBackgroundMusic } from '@/hooks/useBackgroundMusic';
import { useWebSocket, ConnectionState } from '@/hooks/useWebSocket';
import { useSfxDucker } from '@/hooks/useSfxDucker';
import ParentGate from './ParentGate';
import { IoTConfig } from './IoTSettingsModal';
import TheaterMode from './TheaterMode';

// Lazy-load MagicMirror (heavy WebGL) — don't block initial render.
// On ChunkLoadError (e.g. stale cache after rebuild), render nothing so the rest of the app still works.
const MagicMirror = dynamic(
    () =>
        import('./MagicMirror').catch(() => ({
            default: function MagicMirrorFallback() {
                return null;
            },
        })),
    { ssr: false, loading: () => null }
);

type AppPhase = 'gate' | 'mic-check' | 'story' | 'theater';
const MIC_OK_KEY = 'storyteller_mic_ok';
const QUICK_ACK_AUDIO_URL = '/audio/got-it-lets-go.mp3';

function deriveBackendRunAppHost(host: string): string | null {
    const match = host.match(/^storyteller-frontend-(.+\.(?:a\.)?run\.app)$/);
    if (!match) return null;
    return `storyteller-backend-${match[1]}`;
}

function resolveFrontendWebSocketUrl(): string {
    const configured = process.env.NEXT_PUBLIC_WS_URL ?? '';
    if (typeof window === 'undefined') {
        return configured || '/ws/story';
    }

    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const host = window.location.host;
    const backendRunAppHost = deriveBackendRunAppHost(host);
    const normalized = configured.trim();
    const isBadDefault =
        !normalized ||
        normalized.includes('localhost:8000') ||
        normalized.includes('storyteller.example.com');

    if (isBadDefault) {
        if (backendRunAppHost) {
            return `${protocol}//${backendRunAppHost}/ws/story`;
        }
        return `${protocol}//${host}/ws/story`;
    }

    if (normalized.startsWith('/')) {
        if (backendRunAppHost && normalized.startsWith('/ws/')) {
            return `${protocol}//${backendRunAppHost}${normalized}`;
        }
        return `${protocol}//${host}${normalized}`;
    }

    if (normalized.startsWith('http://') || normalized.startsWith('https://')) {
        return normalized.replace(/^http/i, 'ws');
    }

    return normalized;
}

function resolveUploadUrl(): string {
    const configured = process.env.NEXT_PUBLIC_UPLOAD_URL ?? '';
    if (typeof window === 'undefined') {
        return configured || '/api/upload';
    }
    const protocol = window.location.protocol;
    const host = window.location.host;
    const backendRunAppHost = deriveBackendRunAppHost(host);
    const normalized = configured.trim();
    if (!normalized) {
        if (backendRunAppHost) return `${protocol}//${backendRunAppHost}/api/upload`;
        return '/api/upload';
    }
    if (normalized.includes('localhost:8000') || normalized.includes('storyteller.example.com')) {
        if (backendRunAppHost) return `${protocol}//${backendRunAppHost}/api/upload`;
        return '/api/upload';
    }
    if (normalized.startsWith('/')) {
        if (backendRunAppHost && normalized.startsWith('/api/')) {
            return `${protocol}//${backendRunAppHost}${normalized}`;
        }
        return `${protocol}//${host}${normalized}`;
    }
    return normalized;
}

function mergeStreamingTranscript(previous: string | null, incomingRaw: string): string {
    const incoming = incomingRaw.trim();
    if (!incoming) return previous?.trim() ?? '';

    const prev = previous?.trim() ?? '';
    if (!prev) return incoming;

    // Cumulative update.
    if (incoming.startsWith(prev)) return incoming;
    // Older/shorter update arriving late.
    if (prev.startsWith(incoming) || prev.endsWith(incoming)) return prev;

    // Long duplicate block already present.
    if (incoming.length >= 12 && prev.includes(incoming)) return prev;

    // Try suffix/prefix overlap for delta-style chunks.
    const maxOverlap = Math.min(prev.length, incoming.length);
    let overlap = 0;
    for (let i = maxOverlap; i > 0; i -= 1) {
        if (prev.slice(-i).toLowerCase() === incoming.slice(0, i).toLowerCase()) {
            overlap = i;
            break;
        }
    }

    const merged = overlap > 0
        ? `${prev}${incoming.slice(overlap)}`
        : /[([{'"-]$/.test(prev) || /^[,.;:!?)}\]'"]/.test(incoming)
            ? `${prev}${incoming}`
            : `${prev} ${incoming}`;

    // Collapse accidental exact doubled block: "Sentence.Sentence."
    if (merged.length % 2 === 0) {
        const half = merged.slice(0, merged.length / 2);
        if (half && `${half}${half}` === merged) {
            return half.trim();
        }
    }
    return merged.trim();
}

export default function StorytellerLive() {
    const [phase, setPhase] = useState<AppPhase>('gate');
    const [splashIndex] = useState(() => Math.floor(Math.random() * 30) + 1);
    const [calmMode, setCalmMode] = useState(false);
    const calmModeRef = useRef(false);
    const [voiceRms, setVoiceRms] = useState(0);
    const [currentSceneImageUrl, setCurrentSceneImageUrl] = useState<string | null>(null);
    const [currentSceneThumbnailB64, setCurrentSceneThumbnailB64] = useState<string | null>(null);
    const [currentSceneVideoUrl, setCurrentSceneVideoUrl] = useState<string | null>(null);
    const [finalMovieUrl, setFinalMovieUrl] = useState<string | null>(null);
    const [tradingCardUrl, setTradingCardUrl] = useState<string | null>(null);
    const [isMicMuted, setIsMicMuted] = useState(false);
    const [showRestartConfirm, setShowRestartConfirm] = useState(false);
    const isMicMutedRef = useRef(isMicMuted);
    useEffect(() => {
        isMicMutedRef.current = isMicMuted;
    }, [isMicMuted]);
    const [agentThinking, setAgentThinking] = useState(false);
    const [hasHeardAgent, setHasHeardAgent] = useState(false);
    const [isNarrow, setIsNarrow] = useState(false);
    const [isCompact, setIsCompact] = useState(false);
    const [userSpeaking, setUserSpeaking] = useState(false);
    const [spyglassStream, setSpyglassStream] = useState<MediaStream | null>(null);
    const [userTranscript, setUserTranscript] = useState<{ text: string, isFinished: boolean } | null>(null);
    const [displayedAgentText, setDisplayedAgentText] = useState("");
    const agentWordsRef = useRef<string[]>([]);
    const revealedCountRef = useRef(0);
    const agentFinishedRef = useRef(false);
    const typewriterTimerRef = useRef<ReturnType<typeof setInterval> | null>(null);
    const agentClearTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
    const userTranscriptTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);
    const [spyglassCapturing, setSpyglassCapturing] = useState(false);
    const spyglassVideoRef = useRef<HTMLVideoElement | null>(null);
    const pendingSceneImageRef = useRef<string | null>(null);
    const currentSceneImageUrlRef = useRef<string | null>(null);
    const placeholderTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);
    const [sceneLoading, setSceneLoading] = useState(false);
    const [sceneError, setSceneError] = useState<string | null>(null);
    const [zoomedImageUrl, setZoomedImageUrl] = useState<string | null>(null);
    const [isEndingStory, setIsEndingStory] = useState(false);
    const [storybookStatus, setStorybookStatus] = useState<{ message: string; etaSeconds?: number } | null>(null);
    const [storybookNarration, setStorybookNarration] = useState<string[] | null>(null);
    const [storybookAudioAvailable, setStorybookAudioAvailable] = useState<boolean | null>(null);
    const [serverVadEnabled, setServerVadEnabled] = useState(false);
    const [micCheckError, setMicCheckError] = useState<string | null>(null);
    const micCheckTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);
    const micCheckCompletedRef = useRef(false);
    const iotConfigRef = useRef<IoTConfig | null>(null);
    const phaseRef = useRef<AppPhase>('gate');
    const completeMicCheckRef = useRef<(reason: 'heard' | 'timeout' | 'skip') => void>(() => { });
    const sendClientReadyRef = useRef<() => void>(() => { });
    const lastConnectionStateRef = useRef<ConnectionState>('disconnected');
    const lastUserTranscriptRef = useRef<string>('');
    const lastAgentTranscriptRef = useRef<string>('');
    const resumeMicOnReconnectRef = useRef(false);

    useEffect(() => {
        calmModeRef.current = calmMode;
    }, [calmMode]);

    useEffect(() => {
        phaseRef.current = phase;
        streamAudioRef.current = phase === 'story';
    }, [phase]);


    const hasStoredMicOk = useCallback(() => {
        try {
            return typeof window !== 'undefined' && localStorage.getItem(MIC_OK_KEY) === '1';
        } catch {
            return false;
        }
    }, []);

    const markMicOk = useCallback(() => {
        try {
            if (typeof window !== 'undefined') {
                localStorage.setItem(MIC_OK_KEY, '1');
            }
        } catch {
            // ignore
        }
    }, []);

    useEffect(() => {
        currentSceneImageUrlRef.current = currentSceneImageUrl;
    }, [currentSceneImageUrl]);

    // Keep the last scene image visible until a newer image URL is confirmed loadable.
    const commitSceneImage = useCallback((url: string, thumbnailB64?: string | null) => {
        if (!url) return;

        if (thumbnailB64) {
            setCurrentSceneThumbnailB64(thumbnailB64);
        }

        if (url.startsWith('data:image')) {
            const isPlaceholder = url.startsWith('data:image/svg+xml');

            // Guard: If we already have a real image on screen, do NOT commit the placeholder URL.
            // This keeps the old scene visible while the 'sceneLoading' state (set elsewhere) 
            // shows the "Amelia is drawing" overlay on top.
            if (isPlaceholder && currentSceneImageUrlRef.current && !currentSceneImageUrlRef.current.startsWith('data:image/svg+xml')) {
                console.log('Preserving existing scene image while new one generates.');
                return;
            }

            pendingSceneImageRef.current = url;
            setCurrentSceneImageUrl(url);
            setSceneLoading(isPlaceholder);
            if (placeholderTimeoutRef.current) {
                clearTimeout(placeholderTimeoutRef.current);
                placeholderTimeoutRef.current = null;
            }
            if (isPlaceholder) {
                placeholderTimeoutRef.current = setTimeout(() => {
                    if (pendingSceneImageRef.current === url) {
                        setSceneLoading(false);
                        setSceneError('Picture is taking a bit longer. Keep talking and we’ll catch up!');
                    }
                }, 20000);
            }
            return;
        }

        const probe = new window.Image();
        pendingSceneImageRef.current = url;
        probe.onload = () => {
            if (pendingSceneImageRef.current === url) {
                if (placeholderTimeoutRef.current) {
                    clearTimeout(placeholderTimeoutRef.current);
                    placeholderTimeoutRef.current = null;
                }
                setCurrentSceneImageUrl(url);
                setSceneLoading(false);
                setSceneError(null);
            }
        };
        probe.onerror = () => {
            if (pendingSceneImageRef.current === url) {
                if (placeholderTimeoutRef.current) {
                    clearTimeout(placeholderTimeoutRef.current);
                    placeholderTimeoutRef.current = null;
                }
                console.warn('Scene image failed to load; preserving previous scene image.');
                setSceneLoading(false);
                setSceneError('Picture unavailable right now.');
            }
        };
        probe.src = url;
    }, []);

    // Safety net: never let the UI stay in "Thinking..." forever if a turn terminator is missed 
    // or if the microphone VAD picks up room noise but the LLM ignores it (silence).
    // The Gemini Live API typically responds within 1-2 seconds, so 4 seconds is a safe upper bound.
    useEffect(() => {
        if (!agentThinking) return;
        const timer = setTimeout(() => setAgentThinking(false), 4000);
        return () => clearTimeout(timer);
    }, [agentThinking]);

    // Shorten camera label on mobile to prevent bleed
    useEffect(() => {
        const mq = window.matchMedia('(max-width: 500px)');
        const update = () => setIsNarrow(mq.matches);
        update();
        mq.addEventListener('change', update);
        return () => mq.removeEventListener('change', update);
    }, []);

    useEffect(() => {
        const mq = window.matchMedia('(max-width: 900px)');
        const update = () => setIsCompact(mq.matches);
        update();
        mq.addEventListener('change', update);
        return () => mq.removeEventListener('change', update);
    }, []);

    // Optional camera: attach stream to preview video and stop tracks on cleanup
    useEffect(() => {
        if (!spyglassStream || !spyglassVideoRef.current) return;
        const video = spyglassVideoRef.current;
        video.srcObject = spyglassStream;
        video.play().catch(() => { });
        return () => {
            spyglassStream.getTracks().forEach((t) => t.stop());
        };
    }, [spyglassStream]);

    // Refs to break circular dependency between hooks:
    // useWebSocket needs playPcmChunk (from useAudioWorklet) and enqueueSfx (from useSfxDucker),
    // but useAudioWorklet needs send (from useWebSocket).
    // Solution: use refs that are updated synchronously after each hook call.
    const playPcmChunkRef = useRef<(data: ArrayBuffer) => void>(() => { });
    const flushPlaybackBufferRef = useRef<() => void>(() => { });
    const enqueueSfxRef = useRef<(url: string, label?: string) => void>(() => { });
    const sendJsonRef = useRef<(msg: Record<string, unknown>) => void>(() => { });
    const sessionIdRef = useRef<string>('');
    const wsUrlRef = useRef<string>(resolveFrontendWebSocketUrl());
    const uploadUrlRef = useRef<string>(resolveUploadUrl());

    // ── Audio (defined FIRST — provides playPcmChunk) ────────────────────────────
    const sendRef = useRef<(data: string | ArrayBuffer) => void>(() => { });
    const streamAudioRef = useRef(false);
    const startListeningRef = useRef<() => Promise<void>>(async () => { });
    const { audioState, narrationGainNode, primeAudio, startListening, stopListening, playPcmChunk, flushPlaybackBuffer } =
        useAudioWorklet({
            onPcmChunk: useCallback((pcm: ArrayBuffer) => {
                if (streamAudioRef.current) {
                    sendRef.current(pcm);
                }
            }, []),
            onVoiceVolume: setVoiceRms,
            onFlushComplete: useCallback(() => {
                if (!isMicMutedRef.current) {
                    startListeningRef.current();
                }
            }, []),
            onVoiceActivityStart: useCallback(() => {
                setAgentThinking(false);
                setUserSpeaking(true);
                if (phaseRef.current === 'mic-check') {
                    streamAudioRef.current = true;
                    completeMicCheckRef.current('heard');
                    return;
                }
                if (phaseRef.current !== 'story') {
                    return;
                }
                if (!serverVadEnabled && sessionIdRef.current) {
                    sendJsonRef.current({
                        type: 'activity_start',
                        session_id: sessionIdRef.current,
                        payload: {},
                    });
                }
            }, [serverVadEnabled]),
            onVoiceActivityEnd: useCallback(() => {
                setAgentThinking(true);
                setUserSpeaking(false);
                if (phaseRef.current !== 'story') {
                    return;
                }
                if (!serverVadEnabled && sessionIdRef.current) {
                    sendJsonRef.current({
                        type: 'activity_end',
                        session_id: sessionIdRef.current,
                        payload: {},
                    });
                }
            }, [serverVadEnabled]),
        });
    startListeningRef.current = startListening;
    playPcmChunkRef.current = playPcmChunk;
    flushPlaybackBufferRef.current = flushPlaybackBuffer;

    // ── SFX Ducker (defined SECOND — provides enqueueSfx) ────────────────────────
    const { enqueueSfx } = useSfxDucker(narrationGainNode);
    enqueueSfxRef.current = enqueueSfx;
    const { setMusicMood } = useBackgroundMusic();

    // Try to lock to landscape storybook orientation when possible.
    useEffect(() => {
        const orientation = screen.orientation as ScreenOrientation & {
            lock?: (orientation: string) => Promise<void>;
            unlock?: () => void;
        };
        if (orientation && orientation.lock) {
            orientation.lock('landscape').catch(() => { });
        }
        return () => {
            orientation?.unlock?.();
        };
    }, []);

    // ── WebSocket (defined THIRD — uses playPcmChunkRef + enqueueSfxRef) ─────────
    const { connectionState, sendJson, send, sessionId } = useWebSocket({
        url: wsUrlRef.current,
        onBinaryMessage: useCallback((data: ArrayBuffer) => {
            // PCM audio from Gemini native audio — forward to playback worklet
            playPcmChunkRef.current(data);
            setAgentThinking(false);
            setHasHeardAgent(true);
        }, []),
        onJsonMessage: useCallback((msg: { type: string; payload?: Record<string, unknown> }) => {
            switch (msg.type) {
                case 'TURN_COMPLETE':
                    // Critical: clear "Thinking..." even when the model produced no audible chunk.
                    // Otherwise the UI can get stuck in thinking state after tool-heavy/silent turns.
                    setAgentThinking(false);
                    lastAgentTranscriptRef.current = '';
                    lastUserTranscriptRef.current = '';
                    flushPlaybackBufferRef.current();
                    break;
                case 'video_ready':
                    {
                        const url = msg.payload?.url as string;
                        if (!url) break;
                        const isPlaceholder = Boolean(msg.payload?.is_placeholder);
                        const isFallback = Boolean(msg.payload?.is_fallback);
                        const mediaType = (msg.payload?.media_type as string | undefined)?.toLowerCase();
                        const inferredType: 'image' | 'video' =
                            mediaType === 'image' ||
                                url?.startsWith('data:image') ||
                                /\.(png|jpe?g|webp|gif|svg)(\?|$)/i.test(url)
                                ? 'image'
                                : 'video';
                        if (inferredType === 'image') {
                            const thumbB64 = msg.payload?.thumbnail_b64 as string | undefined;
                            commitSceneImage(url, thumbB64);
                            setCurrentSceneVideoUrl(null);

                            if (isFallback) {
                                if (placeholderTimeoutRef.current) {
                                    clearTimeout(placeholderTimeoutRef.current);
                                    placeholderTimeoutRef.current = null;
                                }
                                setSceneLoading(false);
                                setSceneError('Picture unavailable right now.');
                            } else if (isPlaceholder || url.startsWith('data:image/svg+xml')) {
                                // Important: even if commitSceneImage ignored the URL (because an image exists),
                                // we still want to show the loading state.
                                setSceneLoading(true);
                                setSceneError(null);
                            } else {
                                // Full image arrived: commitSceneImage will handle clearing loading state
                                // once it probes the URL and confirms it loaded.
                                // We ALSO force it here just in case commitSceneImage's data-url path
                                // needs an extra nudge to clear the state.
                                console.log('Full image arrived via video_ready, clearing loading state.');
                                setSceneLoading(false);
                            }
                        } else if (!currentSceneImageUrlRef.current) {
                            // Show video only when no image has been established yet.
                            setCurrentSceneVideoUrl(url);
                            setSceneLoading(false);
                            setSceneError(null);
                        }
                    }
                    break;
                case 'theater_mode':
                    setFinalMovieUrl(msg.payload?.mp4_url as string);
                    setTradingCardUrl(msg.payload?.trading_card_url as string ?? null);
                    {
                        const narrationRaw = msg.payload?.narration_lines;
                        const narration = Array.isArray(narrationRaw)
                            ? narrationRaw.filter((line) => typeof line === 'string' && line.trim().length > 0)
                            : null;
                        setStorybookNarration(narration && narration.length ? narration : null);
                        const audioAvailable = typeof msg.payload?.audio_available === 'boolean'
                            ? (msg.payload?.audio_available as boolean)
                            : null;
                        setStorybookAudioAvailable(audioAvailable);
                    }
                    setPhase('theater');
                    setIsEndingStory(false);
                    setStorybookStatus(null);
                    break;
                case 'trading_card_ready': {
                    const cardUrl = msg.payload?.trading_card_url as string | undefined;
                    if (cardUrl) setTradingCardUrl(cardUrl);
                    break;
                }
                case 'video_generation_started': {
                    const message = (msg.payload?.message as string) || 'Making your storybook movie…';
                    const eta = Number(msg.payload?.eta_seconds ?? 0);
                    setStorybookStatus({ message, etaSeconds: Number.isFinite(eta) && eta > 0 ? eta : undefined });
                    setIsEndingStory(true);
                    break;
                }
                case 'music_command': {
                    const mood = msg.payload?.mood as string;
                    const intensity = Number(msg.payload?.intensity ?? 5);
                    void setMusicMood(mood, intensity);
                    break;
                }
                case 'sfx_command':
                    enqueueSfxRef.current(msg.payload?.url as string, msg.payload?.label as string);
                    break;
                case 'heartbeat':
                    // Respond to server-side ping to keep proxy connection alive
                    sendJson({
                        type: 'heartbeat' as any,
                        session_id: sessionId,
                        payload: { pong: true }
                    });
                    break;
                case 'quick_ack':
                    if (!calmModeRef.current) {
                        enqueueSfxRef.current(QUICK_ACK_AUDIO_URL, 'quick_ack');
                    }
                    break;
                case 'rewind_complete':
                    break;
                case 'session_rehydrated':
                    setAgentThinking(false);
                    setServerVadEnabled(Boolean(msg.payload?.server_vad_enabled));
                    resumeMicOnReconnectRef.current = Boolean(msg.payload?.story_started)
                        && !Boolean(msg.payload?.assistant_speaking)
                        && !Boolean(msg.payload?.pending_response)
                        && !Boolean(msg.payload?.ending_story);

                    // Recover UI state after reconnect
                    if (msg.payload?.current_scene_image_url) {
                        commitSceneImage(msg.payload.current_scene_image_url as string);
                    }
                    if (msg.payload?.story_started) {
                        setPhase('story');
                        if (resumeMicOnReconnectRef.current && !isMicMutedRef.current) {
                            void startListening();
                        }
                    }
                    break;
                case 'error':
                    // Clear thinking spinner on backend recoverable errors.
                    setAgentThinking(false);
                    setIsEndingStory(false);
                    setStorybookStatus(null);
                    // If the backend is resetting and asks for an auto-resume,
                    // re-trigger listening so the child doesn't get stuck.
                    if (msg.payload?.auto_resume) {
                        console.log('Auto-resume hint received from backend error.');
                        startListening();
                    }
                    break;
                case 'user_transcription':
                    {
                        const text = msg.payload?.text as string;
                        const finished = !!msg.payload?.finished;
                        if (text) {
                            const merged = mergeStreamingTranscript(lastUserTranscriptRef.current, text);
                            lastUserTranscriptRef.current = merged;
                            // Show only the current utterance — no accumulation
                            setUserTranscript({ text: merged, isFinished: finished });
                            if (userTranscriptTimeoutRef.current) clearTimeout(userTranscriptTimeoutRef.current);
                            if (finished) {
                                setAgentThinking(true); // fallback: show "thinking" even if VAD misses
                                setUserSpeaking(false);
                                userTranscriptTimeoutRef.current = setTimeout(() => {
                                    setUserTranscript(null);
                                    lastUserTranscriptRef.current = '';
                                }, 2500);
                            }
                        }
                    }
                    break;
                case 'agent_transcription':
                    {
                        const text = msg.payload?.text as string;
                        const finished = !!msg.payload?.finished;
                        if (text) {
                            setAgentThinking(false);
                            setHasHeardAgent(true);
                            const merged = mergeStreamingTranscript(lastAgentTranscriptRef.current, text);
                            lastAgentTranscriptRef.current = merged;
                            // Feed cumulative words into the queue
                            const words = merged.trim().split(/\s+/);
                            agentWordsRef.current = words;
                            agentFinishedRef.current = finished;

                            // Cancel any pending clear
                            if (agentClearTimerRef.current) {
                                clearTimeout(agentClearTimerRef.current);
                                agentClearTimerRef.current = null;
                            }

                            // Start the typewriter timer if not already running
                            if (!typewriterTimerRef.current) {
                                typewriterTimerRef.current = setInterval(() => {
                                    const pool = agentWordsRef.current;
                                    const count = revealedCountRef.current;
                                    if (count < pool.length) {
                                        revealedCountRef.current = count + 1;
                                        // Show only the last ~12 words (sliding window for small screens)
                                        const revealed = pool.slice(0, count + 1);
                                        const tail = revealed.length > 12 ? revealed.slice(-12) : revealed;
                                        setDisplayedAgentText(tail.join(' '));
                                    } else if (agentFinishedRef.current) {
                                        // All words revealed & narrator finished
                                        clearInterval(typewriterTimerRef.current!);
                                        typewriterTimerRef.current = null;
                                        agentClearTimerRef.current = setTimeout(() => {
                                            setDisplayedAgentText("");
                                            agentWordsRef.current = [];
                                            revealedCountRef.current = 0;
                                            agentFinishedRef.current = false;
                                        }, 2500);
                                    }
                                    // else: waiting for more words from backend
                                }, 150); // ~150ms per word ≈ child reading pace
                            }
                        }
                    }
                    break;
                default:
                    break;
            }
        }, []),
    });
    // Update sendRef now that useWebSocket has returned
    sendRef.current = send;
    sendJsonRef.current = sendJson;
    sessionIdRef.current = sessionId;

    const sendClientReady = useCallback(() => {
        const viewport = {
            width: window.innerWidth,
            height: window.innerHeight,
            devicePixelRatio: window.devicePixelRatio || 1,
            isCompact,
        };
        const panelWidth = isCompact
            ? Math.min(window.innerWidth * 0.96, 560)
            : Math.min(window.innerWidth * 0.94, 860);
        const panelHeight = isCompact
            ? Math.min(window.innerHeight * 0.56, 520)
            : (panelWidth * 9) / 16;
        const panel = {
            width: panelWidth,
            height: panelHeight,
        };
        sendJsonRef.current({ type: 'client_ready', session_id: sessionIdRef.current, payload: { viewport, panel } });

        const cfg = iotConfigRef.current;
        if (cfg && cfg.ha_url && cfg.ha_token) {
            sendJsonRef.current({
                type: 'iot_config',
                session_id: sessionIdRef.current,
                payload: { config: cfg },
            });
        }
    }, [isCompact]);
    sendClientReadyRef.current = sendClientReady;

    useEffect(() => {
        const prev = lastConnectionStateRef.current;
        if (connectionState === 'connected' && prev === 'reconnecting') {
            if (phaseRef.current === 'story') {
                sendClientReadyRef.current();
            }
        }
        if (connectionState === 'reconnecting') {
            resumeMicOnReconnectRef.current = false;
        }
        lastConnectionStateRef.current = connectionState;
    }, [connectionState]);

    const completeMicCheck = useCallback((reason: 'heard' | 'timeout' | 'skip') => {
        if (micCheckCompletedRef.current) return;
        micCheckCompletedRef.current = true;
        if (micCheckTimeoutRef.current) {
            clearTimeout(micCheckTimeoutRef.current);
            micCheckTimeoutRef.current = null;
        }
        setMicCheckError(null);
        markMicOk();
        setPhase('story');
        setHasHeardAgent(false);
        setAgentThinking(true);
        sendClientReady();
    }, [markMicOk, sendClientReady]);

    useEffect(() => {
        completeMicCheckRef.current = completeMicCheck;
        return () => {
            if (micCheckTimeoutRef.current) {
                clearTimeout(micCheckTimeoutRef.current);
                micCheckTimeoutRef.current = null;
            }
        };
    }, [completeMicCheck]);

    // ── Parent Gate approval ─────────────────────────────────────────────────────
    const handleGateApproved = useCallback(async (calm: boolean, iotConfig: IoTConfig | null) => {
        setCalmMode(calm);
        setHasHeardAgent(false);
        setMicCheckError(null);
        micCheckCompletedRef.current = false;
        iotConfigRef.current = iotConfig;
        const skipMicCheck = hasStoredMicOk();
        try {
            // Prime playback so Amelia can speak immediately, then start mic capture asynchronously.
            await primeAudio();
            setAgentThinking(false);
        } catch (e) {
            console.error('Microphone initialization failed:', e);
            setAgentThinking(false);
            setMicCheckError('Microphone is blocked. Please allow mic access.');
            return;
        }
        try {
            await startListening();
        } catch (e) {
            console.error('Microphone initialization failed:', e);
            setMicCheckError('Microphone is blocked. Please allow mic access.');
            setPhase('mic-check');
            return;
        }

        if (skipMicCheck) {
            markMicOk();
            setPhase('story');
            setAgentThinking(true);
            sendClientReady();
            return;
        }

        setPhase('mic-check');
        if (micCheckTimeoutRef.current) {
            clearTimeout(micCheckTimeoutRef.current);
        }
        micCheckTimeoutRef.current = setTimeout(() => {
            completeMicCheckRef.current('timeout');
        }, 7000);
    }, [hasStoredMicOk, markMicOk, primeAudio, sendClientReady, startListening]);

    // Orb is display-only: turn boundaries are detected automatically from voice activity.

    const handleEndStory = useCallback(() => {
        if (isEndingStory) return;
        setIsEndingStory(true);
        setStorybookStatus({ message: 'Making your storybook movie…', etaSeconds: 90 });
        setAgentThinking(true);
        flushPlaybackBufferRef.current();
        sendJson({ type: 'end_story', session_id: sessionId, payload: {} });
        // Safety: re-enable after 2 minutes in case backend never responds
        setTimeout(() => setIsEndingStory(false), 120000);
    }, [isEndingStory, sendJson, sessionId]);

    // ── Optional camera: open preview (user sees camera, then taps Take photo) ───
    const handleSpyglass = useCallback(async () => {
        if (spyglassStream) return; // already open
        try {
            const stream = await navigator.mediaDevices.getUserMedia({ video: { facingMode: 'user' } });
            setSpyglassStream(stream);
        } catch (e) {
            console.warn('Optional camera failed:', e);
        }
    }, [spyglassStream]);

    const handleRestartStory = useCallback(() => {
        setShowRestartConfirm(true);
    }, []);

    const confirmRestart = useCallback(() => {
        sessionStorage.removeItem('storyteller_session_id');
        window.location.reload();
    }, []);

    const cancelRestart = useCallback(() => {
        setShowRestartConfirm(false);
    }, []);

    const toggleMic = useCallback(() => {
        setIsMicMuted((prev) => !prev);
    }, []);

    useEffect(() => {
        if (phaseRef.current === 'story' && !isEndingStory) {
            if (isMicMuted) {
                stopListening();
            } else if (!hasHeardAgent && !agentThinking) { // Don't interrupt if we shouldn't be listening anyway.
                void startListening();
            }
        }
    }, [isMicMuted, isEndingStory, stopListening, startListening, hasHeardAgent, agentThinking]);

    const handleSpyglassCancel = useCallback(() => {
        if (spyglassStream) {
            spyglassStream.getTracks().forEach((t) => t.stop());
            setSpyglassStream(null);
        }
    }, [spyglassStream]);

    const handleSpyglassCapture = useCallback(async () => {
        const video = spyglassVideoRef.current;
        if (!video || !spyglassStream || spyglassCapturing) return;
        setSpyglassCapturing(true);
        try {
            const canvas = document.createElement('canvas');
            canvas.width = 640;
            canvas.height = 480;
            const ctx = canvas.getContext('2d')!;
            ctx.drawImage(video, 0, 0);
            spyglassStream.getTracks().forEach((t) => t.stop());
            setSpyglassStream(null);

            const blob = await new Promise<Blob>((res) => canvas.toBlob((b) => res(b!), 'image/jpeg', 0.8));
            const formData = new FormData();
            formData.append('file', blob, 'spyglass.jpg');
            formData.append('session_id', sessionId);
            const resp = await fetch(uploadUrlRef.current, { method: 'POST', body: formData });
            const { gcs_url } = await resp.json();
            sendJson({ type: 'spyglass_image', session_id: sessionId, payload: { gcs_url } });
        } catch (e) {
            console.warn('Optional camera capture failed:', e);
            if (spyglassStream) {
                spyglassStream.getTracks().forEach((t) => t.stop());
                setSpyglassStream(null);
            }
        } finally {
            setSpyglassCapturing(false);
        }
    }, [sessionId, sendJson, spyglassStream, spyglassCapturing]);

    // ── Theater close ────────────────────────────────────────────────────────────
    const handleTheaterClose = useCallback(() => {
        sendJson({ type: 'theater_close', session_id: sessionId, payload: {} });
        setPhase('story');
        setFinalMovieUrl(null);
        setStorybookNarration(null);
        setStorybookAudioAvailable(null);
        void startListening();
    }, [sendJson, sessionId, startListening]);

    useEffect(() => {
        if (phase === 'theater') {
            stopListening();
            sendJson({ type: 'theater_close', session_id: sessionId, payload: {} });
        }
    }, [phase, sendJson, sessionId, stopListening]);

    // ── Render ───────────────────────────────────────────────────────────────────
    if (phase === 'gate') {
        return <ParentGate onApproved={handleGateApproved} />;
    }

    if (phase === 'mic-check') {
        const micLevel = Math.min(1, Math.max(0, (voiceRms - 0.002) / 0.02));
        return (
            <main className="storyteller-main mic-check-stage" aria-label="Microphone check">
                <div className="mic-check-card" role="status" aria-live="polite">
                    <div className="mic-check-icon" aria-hidden="true">🎤</div>
                    <div className="mic-check-title">Let’s test the mic</div>
                    <div className="mic-check-subtitle">Say “Hi Amelia!”</div>
                    <div className="mic-check-meter" aria-hidden="true">
                        <div className="mic-check-fill" style={{ width: `${micLevel * 100}%` }} />
                    </div>
                    {micCheckError && <div className="mic-check-error">{micCheckError}</div>}
                    <button
                        className="mic-check-skip"
                        onClick={() => completeMicCheckRef.current('skip')}
                        aria-label="Skip microphone test"
                        disabled={!!micCheckError}
                    >
                        Skip
                    </button>
                </div>
            </main>
        );
    }

    if (phase === 'theater' && finalMovieUrl) {
        return (
            <TheaterMode
                mp4Url={finalMovieUrl}
                tradingCardUrl={tradingCardUrl ?? undefined}
                narrationLines={storybookNarration ?? undefined}
                audioAvailable={storybookAudioAvailable ?? undefined}
                onClose={handleTheaterClose}
            />
        );
    }

    const isListening = audioState === 'listening' || userSpeaking;
    const isSpeaking = audioState === 'speaking' && !userSpeaking;
    const showBackground = !isCompact;
    const isStarting = phase === 'story' && !hasHeardAgent;
    const isBuffering = audioState === 'buffering';
    const etaSeconds = storybookStatus?.etaSeconds ?? 0;
    const etaLabel = etaSeconds ? `About ${Math.ceil(etaSeconds / 30) * 30} seconds` : null;

    return (
        <main
            className={`storyteller-main ${calmMode ? 'calm-mode' : ''} ${(currentSceneImageUrl || currentSceneVideoUrl) ? 'has-scene' : ''}`}
            aria-label="Interactive Storytelling Experience"
        >
            {/* Scene background */}
            {showBackground && !currentSceneImageUrl && currentSceneVideoUrl && (
                <video
                    key={currentSceneVideoUrl}
                    src={currentSceneVideoUrl}
                    autoPlay
                    loop
                    muted={calmMode}
                    playsInline
                    className="scene-video-bg"
                    aria-hidden="true"
                />
            )}
            {showBackground && currentSceneThumbnailB64 && (
                <img
                    key={`thumb-${currentSceneThumbnailB64}`}
                    src={`data:image/jpeg;base64,${currentSceneThumbnailB64}`}
                    className="scene-image-bg"
                    style={{ filter: 'blur(30px)', transform: 'scale(1.1)' }}
                    alt=""
                    aria-hidden="true"
                />
            )}
            {showBackground && currentSceneImageUrl && (
                <img
                    key={currentSceneImageUrl}
                    src={currentSceneImageUrl}
                    className="scene-image-bg"
                    alt=""
                    aria-hidden="true"
                />
            )}

            {/* Magic Mirror WebGL visualizer */}
            <div className="magic-mirror-container" aria-hidden="true" style={{ zIndex: 10 }}>
                <MagicMirror voiceRms={voiceRms} isActive={isSpeaking || agentThinking} />
                {agentThinking && (
                    <div className="magic-sparkles">
                        {Array.from({ length: 12 }).map((_, i) => (
                            <div
                                key={i}
                                className="sparkle"
                                style={{
                                    left: `${Math.random() * 100}%`,
                                    top: `${Math.random() * 100}%`,
                                    '--tx': `${(Math.random() - 0.5) * 100}px`,
                                    '--ty': `${(Math.random() - 0.5) * 100}px`,
                                    animationDelay: `${Math.random() * 2}s`
                                } as any}
                            />
                        ))}
                    </div>
                )}
            </div>

            {/* Storybook panel: scene + narration aligned like a page */}
            {(currentSceneImageUrl || currentSceneVideoUrl) && (
                <section className="storybook-panel" aria-live="polite">
                    {currentSceneThumbnailB64 && (
                        <img
                            src={`data:image/jpeg;base64,${currentSceneThumbnailB64}`}
                            alt=""
                            className="storybook-media"
                            style={{ filter: 'blur(30px)', position: 'absolute', top: 0, left: 0, zIndex: 0, width: '100%', height: '100%', objectFit: 'cover' }}
                            aria-hidden="true"
                        />
                    )}
                    {currentSceneImageUrl && (
                        <img
                            src={currentSceneImageUrl}
                            alt="Story scene illustration"
                            className="storybook-media"
                            style={{ position: 'relative', zIndex: 1 }}
                            loading="eager"
                            decoding="async"
                            onClick={() => {
                                setZoomedImageUrl(currentSceneImageUrl);
                            }}
                        />
                    )}
                    {!currentSceneImageUrl && currentSceneVideoUrl && (
                        <video
                            src={currentSceneVideoUrl}
                            autoPlay
                            loop
                            muted={calmMode}
                            playsInline
                            className="storybook-media"
                            aria-hidden="true"
                        />
                    )}
                    {sceneLoading && (
                        <div className="storybook-loading" role="status" aria-live="polite">
                            <div className="storybook-loading-stage" aria-hidden="true">
                                <div className="loading-ribbon" />
                                <div className="loading-wand" />
                                <div className="loading-sparkle sparkle-1" />
                                <div className="loading-sparkle sparkle-2" />
                                <div className="loading-sparkle sparkle-3" />
                                <div className="loading-orbit">
                                    <div className="loading-orb orb-1" />
                                    <div className="loading-orb orb-2" />
                                    <div className="loading-orb orb-3" />
                                </div>
                                <div className="loading-buddy">
                                    <span className="buddy-eye left" />
                                    <span className="buddy-eye right" />
                                    <span className="buddy-mouth" />
                                </div>
                            </div>
                            <div className="loading-dots" aria-hidden="true">
                                <span />
                                <span />
                                <span />
                            </div>
                            <span className="sr-only">Amelia is drawing the picture.</span>
                        </div>
                    )}
                    {sceneError && !sceneLoading && (
                        <div className="storybook-error" role="status" aria-live="polite">
                            {sceneError}
                        </div>
                    )}
                </section>
            )}

            {storybookStatus && phase !== 'theater' && (
                <div className="storybook-assembling" role="status" aria-live="polite">
                    <div className="storybook-assembling-card">
                        <div className="storybook-assembling-spinner" aria-hidden="true" />
                        <div className="storybook-assembling-text">{storybookStatus.message}</div>
                        {etaLabel && <div className="storybook-assembling-eta">{etaLabel}</div>}
                    </div>
                </div>
            )}

            {/* Connection status badge */}
            {connectionState !== 'connected' && (
                <div className="connection-badge" role="status" aria-live="polite">
                    {connectionState === 'reconnecting' ? '🔄 Reconnecting...' : '⚡ Connecting...'}
                </div>
            )}

            {/* Start screen while Amelia loads */}
            {isStarting && (
                <div
                    className="amelia-loading-screen"
                    role="status"
                    aria-live="polite"
                    style={{
                        '--bg-portrait': `url(/splash/portrait_${splashIndex.toString().padStart(2, '0')}.png)`,
                        '--bg-landscape': `url(/splash/landscape_${splashIndex.toString().padStart(2, '0')}.png)`,
                    } as React.CSSProperties}
                >
                    <div className="amelia-loading-card">
                        <div className="amelia-loading-title">Back to someping, back to DODY LAND!</div>
                        <div className="amelia-loading-subtitle">Please wait. Amelia will say hi.</div>
                    </div>
                </div>
            )}



            {/* Orb position adjusted with z-index */}
            <div
                className={`magic-orb magic-orb-display ${isListening ? 'orb-listening' : ''} ${isSpeaking ? 'orb-speaking' : ''}`}
                role="status"
                aria-live="polite"
                aria-label={
                    isStarting
                        ? 'Amelia is getting ready'
                        : isBuffering
                            ? 'Microphone is getting ready'
                            : agentThinking
                                ? 'Amelia is thinking'
                                : isSpeaking
                                    ? 'Amelia is speaking'
                                    : 'Listening — just talk to Amelia'
                }
                style={{ zIndex: 15 }}
            >
                <span className="orb-icon" aria-hidden="true">
                    {isStarting ? '⏳' : isBuffering ? '🎤' : agentThinking ? '✨' : isListening ? '👂' : isSpeaking ? '🌟' : '👂'}
                </span>
                <span className="orb-label">
                    {isStarting
                        ? 'Amelia is getting ready...'
                        : isBuffering
                            ? 'Getting the mic ready...'
                            : agentThinking
                                ? 'Thinking...'
                                : isSpeaking
                                    ? 'Amelia says...'
                                    : 'Just talk! Amelia is listening.'}
                </span>
            </div>



            <div className="mic-toggle-container">
                <button
                    className={`mic-toggle-btn ${isMicMuted ? 'muted' : ''}`}
                    onClick={toggleMic}
                    aria-label={isMicMuted ? 'Unmute microphone' : 'Mute microphone'}
                    title={isMicMuted ? 'Unmute' : 'Mute'}
                >
                    {isMicMuted ? '🔇 Off' : '🎤 On'}
                </button>
            </div>

            <div className="story-controls" aria-label="Story controls">
            </div>

            <button
                className="restart-story-btn"
                onClick={handleRestartStory}
                aria-label="Start a new story"
                title="Restart Story"
            >
                Start Over
            </button>

            {/* End Story — triggers wrap-up + storybook movie (Moved to top right) */}
            <button
                className="end-story-btn"
                onClick={handleEndStory}
                disabled={isEndingStory}
                aria-label="End the story and make the storybook movie"
                aria-busy={isEndingStory}
            >
                🌟 {isEndingStory ? 'Magic happening...' : 'The End'}
            </button>

            {zoomedImageUrl && (
                <div
                    className="zoom-overlay"
                    role="dialog"
                    aria-modal="true"
                    aria-label="Zoomed story image"
                    onClick={() => setZoomedImageUrl(null)}
                >
                    <div className="zoom-stage">
                        <button
                            className="zoom-close-btn"
                            onClick={(e) => {
                                e.stopPropagation();
                                setZoomedImageUrl(null);
                            }}
                            aria-label="Close zoomed image"
                        >
                            ✕
                        </button>
                        <img
                            src={zoomedImageUrl}
                            alt="Zoomed story scene"
                            className="zoomed-image"
                        />
                    </div>
                </div>
            )}

            {/* Kid-Friendly Restart Confirmation Modal */}
            {showRestartConfirm && (
                <div className="restart-modal-overlay" role="dialog" aria-modal="true">
                    <div className="restart-modal-card">
                        <div className="restart-modal-icon">✨</div>
                        <h2 className="restart-modal-title">Start a New Magic Adventure?</h2>
                        <p className="restart-modal-text">This will finish your current story and start a brand new one!</p>
                        <div className="restart-modal-actions">
                            <button className="restart-confirm-btn" onClick={confirmRestart}>
                                🌟 Yes, Start Over!
                            </button>
                            <button className="restart-cancel-btn" onClick={cancelRestart}>
                                ✨ Keep Playing
                            </button>
                        </div>
                    </div>
                </div>
            )}

            {/* Camera sharing disabled for now. */}
        </main>
    );
}
