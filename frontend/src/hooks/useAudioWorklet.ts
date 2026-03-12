/**
 * useAudioWorklet — manages mic capture (16kHz) and PCM playback (24kHz)
 *
 * Architecture:
 * - A single GLOBAL AudioContext is created once (Iter 2 #2 — Memory Leak fix)
 * - Downsampler AudioWorklet: mic -> 16kHz PCM -> onPcmChunk callback
 * - Playback AudioWorklet: receives 24kHz PCM from backend -> GainNode -> speakers
 * - Wake Lock prevents screen sleep (Iter 1 #4)
 * - iOS AudioContext resume on visibilitychange (Iter 5 #2)
 * - GainNode ceiling at 0.8 prevents hearing damage (Iter 5 #5)
 * - Bluetooth latency compensation via outputLatency (Iter 5 #1)
 * - Full-duplex w/ barge-in: mic stays live; narration ducks on user speech
 */
'use client';

import { useCallback, useEffect, useRef, useState } from 'react';

// Singleton AudioContext — NEVER create inside React render
let _globalAudioContext: AudioContext | null = null;
let _audioWorkletModulesPromise: Promise<void> | null = null;

function getAudioContext(): AudioContext {
    if (!_globalAudioContext) {
        // Output format is natively 24kHz.
        _globalAudioContext = new AudioContext({
            sampleRate: 24000,
            latencyHint: 'interactive',
        });
    }
    return _globalAudioContext;
}

function ensureAudioWorkletModules(ctx: AudioContext): Promise<void> {
    if (!_audioWorkletModulesPromise) {
        _audioWorkletModulesPromise = Promise.all([
            ctx.audioWorklet.addModule('/worklets/downsampler-processor.js'),
            ctx.audioWorklet.addModule('/worklets/playback-processor.js'),
        ]).then(() => undefined);
    }
    return _audioWorkletModulesPromise;
}

export type AudioState = 'idle' | 'listening' | 'speaking' | 'buffering';
export interface BufferedClipProgress {
    progress: number;
    elapsedSpeechMs: number;
    speechStartMs: number;
    speechEndMs: number;
    speechDurationMs: number;
}

interface StartListeningOptions {
    deviceId?: string | null;
}

interface UseAudioWorkletOptions {
    onPcmChunk: (pcm: ArrayBuffer) => void;
    onVoiceVolume?: (rms: number) => void; // For Magic Mirror visualizer
    onVoiceActivityStart?: () => void; // Voice turn starts (hands-free VAD)
    onVoiceActivityEnd?: () => void;   // Voice turn ends after silence
    /** Called when playback buffer is flushed (agent finished). Use to re-open mic for next turn. */
    onFlushComplete?: () => void;
}

// Slightly stricter onset gating cuts random bumps/background sounds without
// forcing kids to shout before Amelia listens.
const SPEECH_START_RMS = 0.0082;
const SPEECH_END_RMS = 0.0042;
const SPEECH_MIN_ACTIVE_MS = 160;
const SPEECH_END_SILENCE_MS = 950;
const SPEECH_PREROLL_CHUNKS = 15; // ~300ms with 20ms PCM chunks
const SPEECH_MAX_UTTERANCE_MS = 9000;

function readClientNumber(value: string | undefined, fallback: number): number {
    const parsed = Number(value);
    if (!Number.isFinite(parsed)) {
        return fallback;
    }
    return Math.max(0, parsed);
}

const NORMAL_BARGE_IN_BLOCK_MS = readClientNumber(
    process.env.NEXT_PUBLIC_BARGE_IN_BLOCK_MS,
    220,
);
const GREETING_BARGE_IN_BLOCK_MS = readClientNumber(
    process.env.NEXT_PUBLIC_GREETING_BARGE_IN_BLOCK_MS,
    900,
);
const FLUSH_COMPLETE_GRACE_MS = readClientNumber(
    process.env.NEXT_PUBLIC_FLUSH_COMPLETE_GRACE_MS,
    140,
);

export function useAudioWorklet({
    onPcmChunk,
    onVoiceVolume,
    onVoiceActivityStart,
    onVoiceActivityEnd,
    onFlushComplete,
}: UseAudioWorkletOptions) {
    const [audioState, setAudioState] = useState<AudioState>('idle');
    const [bluetoothLatencyMs, setBluetoothLatencyMs] = useState(0);

    const downsamplerNodeRef = useRef<AudioWorkletNode | null>(null);
    const playbackNodeRef = useRef<AudioWorkletNode | null>(null);
    const gainNodeRef = useRef<GainNode | null>(null);
    const micStreamRef = useRef<MediaStream | null>(null);
    const micSourceRef = useRef<MediaStreamAudioSourceNode | null>(null);
    const wakeLockRef = useRef<WakeLockSentinel | null>(null);
    const isSpeakingRef = useRef(false);
    const audioInitializedRef = useRef(false);
    const bufferedClipSourceRef = useRef<AudioBufferSourceNode | null>(null);
    const bufferedClipProgressRafRef = useRef<number | null>(null);
    const preferredInputDeviceIdRef = useRef<string | null>(null);
    const voiceActiveRef = useRef(false);
    const voiceRiseStartedAtRef = useRef<number | null>(null);
    const silenceStartedAtRef = useRef<number | null>(null);
    const voiceActiveStartedAtRef = useRef<number | null>(null);
    const preRollRef = useRef<ArrayBuffer[]>([]);
    const duckedForUserRef = useRef(false);
    const outputMutedRef = useRef(false);
    const noiseFloorRef = useRef(0.002);
    const captureBoostUntilRef = useRef<number | null>(null);
    const bargeInBlockUntilRef = useRef<number | null>(null);
    const greetingShieldRef = useRef(true);
    const onPcmChunkRef = useRef(onPcmChunk);
    const onVoiceVolumeRef = useRef(onVoiceVolume);
    const onVoiceActivityStartRef = useRef(onVoiceActivityStart);
    const onVoiceActivityEndRef = useRef(onVoiceActivityEnd);
    const onFlushCompleteRef = useRef(onFlushComplete);

    onPcmChunkRef.current = onPcmChunk;
    onVoiceVolumeRef.current = onVoiceVolume;
    onVoiceActivityStartRef.current = onVoiceActivityStart;
    onVoiceActivityEndRef.current = onVoiceActivityEnd;
    onFlushCompleteRef.current = onFlushComplete;

    const getNarrationCeiling = useCallback(() => (outputMutedRef.current ? 0 : 0.8), []);
    const getNarrationDucked = useCallback(() => (outputMutedRef.current ? 0 : 0.25), []);

    const teardownCapture = useCallback(() => {
        if (voiceActiveRef.current) {
            voiceActiveRef.current = false;
            onVoiceActivityEndRef.current?.();
        }
        voiceRiseStartedAtRef.current = null;
        silenceStartedAtRef.current = null;
        voiceActiveStartedAtRef.current = null;
        preRollRef.current = [];
        downsamplerNodeRef.current?.disconnect();
        downsamplerNodeRef.current = null;
        micSourceRef.current?.disconnect();
        micSourceRef.current = null;
        micStreamRef.current?.getTracks().forEach((t) => t.stop());
        micStreamRef.current = null;
    }, []);

    const initAudio = useCallback(async () => {
        if (audioInitializedRef.current) {
            return getAudioContext();
        }
        const ctx = getAudioContext();
        if (ctx.state === 'suspended') await ctx.resume();

        // Load AudioWorklet processors
        await ensureAudioWorkletModules(ctx);

        // Playback path: PlaybackProcessor -> GainNode (ceiling 0.8) -> speakers
        const playbackNode = new AudioWorkletNode(ctx, 'playback-processor');
        const gainNode = ctx.createGain();
        gainNode.gain.value = getNarrationCeiling(); // Hard ceiling (Iter 5 #5 — hearing damage prevention)
        playbackNode.connect(gainNode);
        gainNode.connect(ctx.destination);

        playbackNode.port.onmessage = (e) => {
            if (e.data && e.data.type === 'flushed') {
                isSpeakingRef.current = false;
                if (greetingShieldRef.current) {
                    greetingShieldRef.current = false;
                }
                const flushDelayMs = Math.max(
                    FLUSH_COMPLETE_GRACE_MS,
                    Math.min(400, Math.round((ctx.outputLatency ?? 0) * 1000) + 40),
                );
                window.setTimeout(() => {
                    setAudioState('listening');
                    if (gainNodeRef.current && duckedForUserRef.current) {
                        gainNodeRef.current.gain.linearRampToValueAtTime(
                            getNarrationCeiling(),
                            gainNodeRef.current.context.currentTime + 0.15,
                        );
                        duckedForUserRef.current = false;
                    }
                    onFlushCompleteRef.current?.();
                }, flushDelayMs);
            }
        };

        playbackNodeRef.current = playbackNode;
        gainNodeRef.current = gainNode;

        // Measure Bluetooth output latency (Iter 5 #1)
        const latency = (ctx.outputLatency ?? 0) * 1000;
        setBluetoothLatencyMs(Math.round(latency));

        // Wake lock (Iter 1 #4 — prevent screen sleep)
        try {
            wakeLockRef.current = await navigator.wakeLock?.request('screen');
        } catch {
            // Wake lock not supported on this device — non-critical
        }

        audioInitializedRef.current = true;
        return ctx;
    }, [getNarrationCeiling]);

    const primeAudio = useCallback(async () => {
        await initAudio();
        if (audioState === 'idle') {
            setAudioState('buffering');
        }
    }, [initAudio, audioState]);

    const startListening = useCallback(async (options?: StartListeningOptions) => {
        const activeDeviceId = preferredInputDeviceIdRef.current;
        const requestedDeviceId = options?.deviceId ?? activeDeviceId ?? null;
        if (micStreamRef.current) {
            if (requestedDeviceId === null || requestedDeviceId === activeDeviceId) {
                setAudioState('listening');
                return;
            }
            teardownCapture();
        }
        if (options?.deviceId !== undefined) {
            preferredInputDeviceIdRef.current = options.deviceId ?? null;
        }
        const ctx = await initAudio();

        const buildAudioConstraints = (deviceId: string | null): MediaTrackConstraints => ({
            echoCancellation: true,
            noiseSuppression: true,
            autoGainControl: true,
            sampleRate: { ideal: 48000 },
            channelCount: 1,
            ...(deviceId ? { deviceId: { exact: deviceId } } : {}),
        });

        // Mic capture with echo cancellation (Iter 1 #2 — Audio Feedback Loop fix)
        let stream: MediaStream;
        try {
            stream = await navigator.mediaDevices.getUserMedia({
                audio: buildAudioConstraints(requestedDeviceId),
            });
        } catch (error) {
            if (!requestedDeviceId) {
                throw error;
            }
            stream = await navigator.mediaDevices.getUserMedia({
                audio: buildAudioConstraints(null),
            });
            preferredInputDeviceIdRef.current = null;
        }

        micStreamRef.current = stream;
        const source = ctx.createMediaStreamSource(stream);
        micSourceRef.current = source;
        captureBoostUntilRef.current = performance.now() + 15000;

        // Downsampler: mic -> 16kHz PCM
        const downsamplerNode = new AudioWorkletNode(ctx, 'downsampler-processor', {
            processorOptions: { inputSampleRate: ctx.sampleRate },
        });
        downsamplerNode.port.onmessage = (e) => {
            const pcm = (e.data as ArrayBuffer).slice(0);
            const samples = new Int16Array(pcm);
            let sum = 0;
            for (const s of samples) sum += s * s;
            const rms = Math.sqrt(sum / Math.max(samples.length, 1)) / 32768;

            // Feed volume to visualizer regardless of speaking/listening phase.
            onVoiceVolumeRef.current?.(rms);

            const now = performance.now();
            const noiseFloor = noiseFloorRef.current;
            const boostActive = captureBoostUntilRef.current !== null && now < captureBoostUntilRef.current;
            const startBase = boostActive ? SPEECH_START_RMS * 0.75 : SPEECH_START_RMS;
            const endBase = boostActive ? SPEECH_END_RMS * 0.8 : SPEECH_END_RMS;
            const startMultiplier = boostActive ? 1.8 : 2.2;
            const endMultiplier = boostActive ? 1.3 : 1.5;
            const dynamicStart = Math.max(startBase, noiseFloor * startMultiplier);
            const dynamicEnd = Math.max(endBase, noiseFloor * endMultiplier);
            const bargeInBlocked =
                isSpeakingRef.current &&
                bargeInBlockUntilRef.current !== null &&
                now < bargeInBlockUntilRef.current;

            if (!voiceActiveRef.current) {
                preRollRef.current.push(pcm);
                if (preRollRef.current.length > SPEECH_PREROLL_CHUNKS) {
                    preRollRef.current.shift();
                }

                if (rms < dynamicStart) {
                    // Update noise floor only when we're below the speech threshold.
                    noiseFloorRef.current = noiseFloor * 0.95 + rms * 0.05;
                }

                if (bargeInBlocked) {
                    return;
                }

                if (rms >= dynamicStart) {
                    if (voiceRiseStartedAtRef.current === null) {
                        voiceRiseStartedAtRef.current = now;
                    }
                    if (now - voiceRiseStartedAtRef.current >= SPEECH_MIN_ACTIVE_MS) {
                        voiceActiveRef.current = true;
                        voiceActiveStartedAtRef.current = now;
                        silenceStartedAtRef.current = null;
                        if (isSpeakingRef.current && playbackNodeRef.current) {
                            playbackNodeRef.current.port.postMessage({ type: 'interrupt' });
                        }
                        if (gainNodeRef.current && !duckedForUserRef.current) {
                            gainNodeRef.current.gain.linearRampToValueAtTime(
                                getNarrationDucked(),
                                gainNodeRef.current.context.currentTime + 0.1,
                            );
                            duckedForUserRef.current = true;
                        }
                        onVoiceActivityStartRef.current?.();
                        for (const chunk of preRollRef.current) {
                            onPcmChunkRef.current(chunk);
                        }
                        preRollRef.current = [];
                    }
                } else {
                    voiceRiseStartedAtRef.current = null;
                }
                return;
            }

            // Voice active: stream chunks continuously until end-of-speech silence.
            onPcmChunkRef.current(pcm);
            if (
                voiceActiveStartedAtRef.current !== null &&
                now - voiceActiveStartedAtRef.current >= SPEECH_MAX_UTTERANCE_MS
            ) {
                voiceActiveRef.current = false;
                voiceActiveStartedAtRef.current = null;
                voiceRiseStartedAtRef.current = null;
                silenceStartedAtRef.current = null;
                preRollRef.current = [];
                if (gainNodeRef.current && duckedForUserRef.current) {
                    gainNodeRef.current.gain.linearRampToValueAtTime(
                        getNarrationCeiling(),
                        gainNodeRef.current.context.currentTime + 0.15,
                    );
                    duckedForUserRef.current = false;
                }
                onVoiceActivityEndRef.current?.();
                return;
            }
            if (rms <= dynamicEnd) {
                if (silenceStartedAtRef.current === null) {
                    silenceStartedAtRef.current = now;
                }
                if (now - silenceStartedAtRef.current >= SPEECH_END_SILENCE_MS) {
                    voiceActiveRef.current = false;
                    voiceActiveStartedAtRef.current = null;
                    voiceRiseStartedAtRef.current = null;
                    silenceStartedAtRef.current = null;
                    preRollRef.current = [];
                    if (gainNodeRef.current && duckedForUserRef.current) {
                        gainNodeRef.current.gain.linearRampToValueAtTime(
                            getNarrationCeiling(),
                            gainNodeRef.current.context.currentTime + 0.15,
                        );
                        duckedForUserRef.current = false;
                    }
                    onVoiceActivityEndRef.current?.();
                }
            } else {
                silenceStartedAtRef.current = null;
            }
        };

        source.connect(downsamplerNode);
        downsamplerNodeRef.current = downsamplerNode;

        setAudioState('listening');
    }, [getNarrationCeiling, getNarrationDucked, initAudio, teardownCapture]);

    const stopListening = useCallback(() => {
        teardownCapture();
        if (gainNodeRef.current) {
            gainNodeRef.current.gain.value = getNarrationCeiling();
        }
        duckedForUserRef.current = false;
        setAudioState('idle');
    }, [getNarrationCeiling, teardownCapture]);

    /** Feed raw 24kHz int16 PCM bytes from the backend into the playback worklet. */
    const playPcmChunk = useCallback((pcmBytes: ArrayBuffer) => {
        if (!playbackNodeRef.current) return;
        if (!isSpeakingRef.current) {
            const now = performance.now();
            const blockMs = greetingShieldRef.current
                ? GREETING_BARGE_IN_BLOCK_MS
                : NORMAL_BARGE_IN_BLOCK_MS;
            bargeInBlockUntilRef.current = now + blockMs;
        }
        isSpeakingRef.current = true;
        setAudioState('speaking');
        playbackNodeRef.current.port.postMessage(pcmBytes);
    }, []);

    /** Called when the agent finishes generating text/audio (TURN_COMPLETE signal). */
    const flushPlaybackBuffer = useCallback(() => {
        // Signal playback worklet to drain the buffer and report when it's done.
        // We do NOT instantly unlock the microphone here, or else the mic will pick
        // up Amelia's voice coming out of the speakers (Echo/Double-trigger bug).
        playbackNodeRef.current?.port.postMessage({ type: 'flush' });
    }, []);

    const setNarrationMuted = useCallback((muted: boolean) => {
        outputMutedRef.current = muted;
        const gainNode = gainNodeRef.current;
        if (!gainNode) return;

        const target = muted
            ? 0
            : duckedForUserRef.current
                ? 0.25
                : 0.8;
        gainNode.gain.cancelScheduledValues(gainNode.context.currentTime);
        gainNode.gain.linearRampToValueAtTime(target, gainNode.context.currentTime + 0.08);
    }, []);

    const stopBufferedClip = useCallback(() => {
        if (bufferedClipProgressRafRef.current !== null) {
            cancelAnimationFrame(bufferedClipProgressRafRef.current);
            bufferedClipProgressRafRef.current = null;
        }
        const source = bufferedClipSourceRef.current;
        bufferedClipSourceRef.current = null;
        if (!source) {
            return;
        }
        source.onended = null;
        try {
            source.stop();
        } catch {
            // Ignore redundant stop calls.
        }
        setAudioState((current) => (current === 'speaking' ? 'idle' : current));
    }, []);

    const estimateBufferedClipSpeechWindow = useCallback((buffer: AudioBuffer) => {
        const totalDuration = Math.max(buffer.duration, 0.01);
        if (!buffer.length || buffer.numberOfChannels <= 0) {
            return { startSeconds: 0, endSeconds: totalDuration };
        }

        const blockSize = Math.max(64, Math.floor(buffer.sampleRate / 220));
        const totalBlocks = Math.ceil(buffer.length / blockSize);
        const minActiveBlocks = 3;
        const threshold = 0.008;
        const channels = Array.from({ length: buffer.numberOfChannels }, (_, idx) => buffer.getChannelData(idx));

        let speechStartBlock = -1;
        let speechEndBlock = -1;
        let activeRun = 0;

        for (let blockIndex = 0; blockIndex < totalBlocks; blockIndex += 1) {
            const blockStart = blockIndex * blockSize;
            const blockEnd = Math.min(buffer.length, blockStart + blockSize);
            let blockPeak = 0;

            for (const channel of channels) {
                for (let sampleIndex = blockStart; sampleIndex < blockEnd; sampleIndex += 1) {
                    const amplitude = Math.abs(channel[sampleIndex] ?? 0);
                    if (amplitude > blockPeak) {
                        blockPeak = amplitude;
                    }
                    if (blockPeak >= threshold) {
                        break;
                    }
                }
                if (blockPeak >= threshold) {
                    break;
                }
            }

            if (blockPeak >= threshold) {
                activeRun += 1;
                if (speechStartBlock < 0 && activeRun >= minActiveBlocks) {
                    speechStartBlock = Math.max(0, blockIndex - minActiveBlocks + 1);
                }
                speechEndBlock = blockIndex;
            } else {
                activeRun = 0;
            }
        }

        if (speechStartBlock < 0 || speechEndBlock < speechStartBlock) {
            return { startSeconds: 0, endSeconds: totalDuration };
        }

        const safetyBlocks = 1;
        const startSeconds = Math.max(0, ((speechStartBlock - safetyBlocks) * blockSize) / buffer.sampleRate);
        const endSeconds = Math.min(
            totalDuration,
            ((speechEndBlock + safetyBlocks + 1) * blockSize) / buffer.sampleRate,
        );

        if (endSeconds - startSeconds < 0.12) {
            return { startSeconds: 0, endSeconds: totalDuration };
        }

        return { startSeconds, endSeconds };
    }, []);

    const playBufferedClip = useCallback(async (
        audioBytes: ArrayBuffer,
        options?: { onEnded?: () => void; onProgress?: (progress: BufferedClipProgress) => void },
    ): Promise<boolean> => {
        if (!audioBytes.byteLength) {
            return false;
        }
        const ctx = await initAudio();
        if (ctx.state === 'suspended') {
            await ctx.resume();
        }
        stopBufferedClip();
        try {
            const decoded = await ctx.decodeAudioData(audioBytes.slice(0));
            const source = ctx.createBufferSource();
            source.buffer = decoded;
            source.connect(gainNodeRef.current ?? ctx.destination);
            bufferedClipSourceRef.current = source;
            setAudioState('speaking');
            const startedAt = ctx.currentTime;
            const speechWindow = estimateBufferedClipSpeechWindow(decoded);
            const duration = Math.max(speechWindow.endSeconds - speechWindow.startSeconds, 0.01);
            const emitProgress = (elapsedSeconds: number) => {
                const progress = Math.max(
                    0,
                    Math.min(1, (elapsedSeconds - speechWindow.startSeconds) / duration),
                );
                options?.onProgress?.({
                    progress,
                    elapsedSpeechMs: Math.max(0, (elapsedSeconds - speechWindow.startSeconds) * 1000),
                    speechStartMs: speechWindow.startSeconds * 1000,
                    speechEndMs: speechWindow.endSeconds * 1000,
                    speechDurationMs: duration * 1000,
                });
                return progress;
            };
            emitProgress(0);
            const tickProgress = () => {
                if (bufferedClipSourceRef.current !== source) {
                    return;
                }
                const elapsed = ctx.currentTime - startedAt;
                const progress = emitProgress(elapsed);
                if (progress < 1) {
                    bufferedClipProgressRafRef.current = requestAnimationFrame(tickProgress);
                } else {
                    bufferedClipProgressRafRef.current = null;
                }
            };
            bufferedClipProgressRafRef.current = requestAnimationFrame(tickProgress);
            source.onended = () => {
                if (bufferedClipProgressRafRef.current !== null) {
                    cancelAnimationFrame(bufferedClipProgressRafRef.current);
                    bufferedClipProgressRafRef.current = null;
                }
                if (bufferedClipSourceRef.current === source) {
                    bufferedClipSourceRef.current = null;
                    setAudioState((current) => (current === 'speaking' ? 'idle' : current));
                }
                emitProgress(speechWindow.endSeconds);
                options?.onEnded?.();
            };
            source.start();
            return true;
        } catch (error) {
            if (bufferedClipProgressRafRef.current !== null) {
                cancelAnimationFrame(bufferedClipProgressRafRef.current);
                bufferedClipProgressRafRef.current = null;
            }
            console.warn('Buffered audio playback failed:', error);
            bufferedClipSourceRef.current = null;
            setAudioState((current) => (current === 'speaking' ? 'idle' : current));
            return false;
        }
    }, [estimateBufferedClipSpeechWindow, initAudio, stopBufferedClip]);

    // iOS Background Audio Suppression fix (Iter 5 #2)
    useEffect(() => {
        const handleVisibilityChange = async () => {
            if (document.visibilityState === 'visible') {
                const ctx = getAudioContext();
                if (ctx.state === 'suspended') await ctx.resume();
            }
        };
        document.addEventListener('visibilitychange', handleVisibilityChange);
        return () => document.removeEventListener('visibilitychange', handleVisibilityChange);
    }, []);

    // Cleanup on unmount
    useEffect(() => {
        return () => {
            stopBufferedClip();
            stopListening();
            wakeLockRef.current?.release?.();
        };
    }, [stopBufferedClip, stopListening]);

    return {
        audioState,
        bluetoothLatencyMs,
        narrationGainNode: gainNodeRef.current,
        primeAudio,
        startListening,
        stopListening,
        playPcmChunk,
        playBufferedClip,
        stopBufferedClip,
        flushPlaybackBuffer,
        setNarrationMuted,
    };
}
