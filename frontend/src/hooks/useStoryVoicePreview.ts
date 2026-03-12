'use client';

import { useCallback, useEffect, useRef, useState } from 'react';

import { normalizeStoryReaderVoiceId, STORY_READER_VOICE_PREVIEW_TEXT } from '@/components/story/storyVoiceOptions';

function deriveBackendRunAppHost(host: string): string | null {
    const match = host.match(/^storyteller-frontend-(.+\.(?:a\.)?run\.app)$/);
    if (!match) return null;
    return `storyteller-backend-${match[1]}`;
}

function resolvePageReadAloudUrl(): string {
    const configured = process.env.NEXT_PUBLIC_PAGE_READ_ALOUD_URL ?? '';
    if (typeof window === 'undefined') {
        return configured || '/api/page-read-aloud';
    }
    const protocol = window.location.protocol;
    const host = window.location.host;
    const normalized = configured.trim();
    if (!normalized) {
        return `${protocol}//${host}/api/page-read-aloud`;
    }
    if (normalized.includes('localhost:8000') || normalized.includes('storyteller.example.com')) {
        return `${protocol}//${host}/api/page-read-aloud`;
    }
    if (normalized.startsWith('/')) {
        return `${protocol}//${host}${normalized}`;
    }
    return normalized;
}

function getPreviewAudioContext(): AudioContext | null {
    if (typeof window === 'undefined' || typeof window.AudioContext === 'undefined') {
        return null;
    }
    return new window.AudioContext({ latencyHint: 'interactive' });
}

async function readPreviewError(response: Response): Promise<string> {
    try {
        const payload = await response.json();
        const message = typeof payload?.message === 'string' ? payload.message.trim() : '';
        if (message) {
            return message;
        }
    } catch {
        // Ignore JSON parsing failures and fall through to status text.
    }
    return response.status >= 500
        ? 'Voice preview is unavailable right now.'
        : 'Voice preview could not be loaded.';
}

type StoryVoicePreviewOptions = {
    childAge?: number | null;
    storybookMoviePacing?: string | null;
    sessionId?: string | null;
    onPreviewStart?: () => void | Promise<void>;
    onPreviewEnd?: () => void | Promise<void>;
};

export function useStoryVoicePreview(options?: StoryVoicePreviewOptions) {
    const childAge = options?.childAge ?? null;
    const storybookMoviePacing = options?.storybookMoviePacing ?? null;
    const sessionId = options?.sessionId ?? null;
    const onPreviewStart = options?.onPreviewStart;
    const onPreviewEnd = options?.onPreviewEnd;

    const contextRef = useRef<AudioContext | null>(null);
    const gainRef = useRef<GainNode | null>(null);
    const sourceRef = useRef<AudioBufferSourceNode | null>(null);
    const abortControllerRef = useRef<AbortController | null>(null);
    const shouldNotifyEndRef = useRef(false);
    const cacheRef = useRef<Map<string, ArrayBuffer>>(new Map());
    const activePreviewVoiceIdRef = useRef('');
    const [previewVoiceId, setPreviewVoiceId] = useState('');
    const [previewLoading, setPreviewLoading] = useState(false);
    const [previewPlaying, setPreviewPlaying] = useState(false);
    const [previewError, setPreviewError] = useState<string | null>(null);

    const completePreview = useCallback(() => {
        setPreviewLoading(false);
        setPreviewPlaying(false);
        setPreviewVoiceId('');
        activePreviewVoiceIdRef.current = '';
        if (!shouldNotifyEndRef.current) {
            return;
        }
        shouldNotifyEndRef.current = false;
        void onPreviewEnd?.();
    }, [onPreviewEnd]);

    const stopPreview = useCallback(() => {
        abortControllerRef.current?.abort();
        abortControllerRef.current = null;
        const source = sourceRef.current;
        sourceRef.current = null;
        if (source) {
            source.onended = null;
            try {
                source.stop();
            } catch {
                // Ignore redundant stops.
            }
            try {
                source.disconnect();
            } catch {
                // Ignore.
            }
        }
        completePreview();
    }, [completePreview]);

    const previewVoice = useCallback(async (voiceId: string) => {
        const normalizedVoiceId = normalizeStoryReaderVoiceId(voiceId);
        if (activePreviewVoiceIdRef.current === normalizedVoiceId && (previewLoading || previewPlaying)) {
            stopPreview();
            return;
        }

        stopPreview();
        setPreviewError(null);
        setPreviewLoading(true);
        setPreviewPlaying(false);
        setPreviewVoiceId(normalizedVoiceId);
        activePreviewVoiceIdRef.current = normalizedVoiceId;

        try {
            await onPreviewStart?.();
            shouldNotifyEndRef.current = true;

            let context = contextRef.current;
            if (!context || context.state === 'closed') {
                context = getPreviewAudioContext();
                contextRef.current = context;
                if (context) {
                    const gain = context.createGain();
                    gain.gain.value = 0.92;
                    gain.connect(context.destination);
                    gainRef.current = gain;
                }
            }
            if (!context || !gainRef.current) {
                throw new Error('Voice preview is unavailable on this device.');
            }
            if (context.state === 'suspended') {
                await context.resume();
            }

            const cacheKey = [
                normalizedVoiceId,
                String(childAge ?? ''),
                String(storybookMoviePacing ?? ''),
                STORY_READER_VOICE_PREVIEW_TEXT,
            ].join('::');

            let audioBytes = cacheRef.current.get(cacheKey) ?? null;
            if (!audioBytes) {
                const controller = new AbortController();
                abortControllerRef.current = controller;
                const response = await fetch(resolvePageReadAloudUrl(), {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                    },
                    body: JSON.stringify({
                        session_id: sessionId ?? '',
                        text: STORY_READER_VOICE_PREVIEW_TEXT,
                        child_age: childAge,
                        storybook_movie_pacing: storybookMoviePacing,
                        storybook_elevenlabs_voice_id: normalizedVoiceId,
                    }),
                    signal: controller.signal,
                });
                if (!response.ok) {
                    throw new Error(await readPreviewError(response));
                }
                audioBytes = await response.arrayBuffer();
                cacheRef.current.set(cacheKey, audioBytes.slice(0));
            }

            if (activePreviewVoiceIdRef.current !== normalizedVoiceId) {
                return;
            }
            abortControllerRef.current = null;

            const decoded = await context.decodeAudioData(audioBytes.slice(0));
            if (activePreviewVoiceIdRef.current !== normalizedVoiceId) {
                return;
            }

            const source = context.createBufferSource();
            source.buffer = decoded;
            source.connect(gainRef.current);
            sourceRef.current = source;
            setPreviewLoading(false);
            setPreviewPlaying(true);
            source.onended = () => {
                if (sourceRef.current === source) {
                    sourceRef.current = null;
                }
                completePreview();
            };
            source.start();
        } catch (error) {
            if ((error as Error)?.name === 'AbortError') {
                return;
            }
            console.warn('Story voice preview failed:', error);
            setPreviewError(
                error instanceof Error && error.message.trim()
                    ? error.message.trim()
                    : 'Voice preview is unavailable right now.',
            );
            completePreview();
        }
    }, [
        childAge,
        completePreview,
        onPreviewStart,
        previewLoading,
        previewPlaying,
        sessionId,
        stopPreview,
        storybookMoviePacing,
    ]);

    useEffect(() => {
        return () => {
            abortControllerRef.current?.abort();
            const source = sourceRef.current;
            sourceRef.current = null;
            if (source) {
                source.onended = null;
                try {
                    source.stop();
                } catch {
                    // Ignore redundant stops.
                }
                try {
                    source.disconnect();
                } catch {
                    // Ignore.
                }
            }
            if (contextRef.current && contextRef.current.state !== 'closed') {
                void contextRef.current.close().catch(() => {
                    // Ignore close failures during unmount.
                });
            }
            contextRef.current = null;
            gainRef.current = null;
            shouldNotifyEndRef.current = false;
        };
    }, []);

    return {
        previewError,
        previewLoading,
        previewPlaying,
        previewVoiceId,
        previewVoice,
        stopPreview,
    };
}
