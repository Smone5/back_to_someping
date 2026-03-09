'use client';

import { useCallback, useRef } from 'react';

export type UiSoundName = 'tap' | 'magic' | 'toggle_on' | 'toggle_off' | 'close' | 'celebrate';

interface UseUiSoundsOptions {
    enabled?: boolean;
    volume?: number;
}

interface ToneSpec {
    offsetMs: number;
    durationMs: number;
    startHz: number;
    endHz?: number;
    gain: number;
    type: OscillatorType;
}

let uiContext: AudioContext | null = null;
let uiMasterGain: GainNode | null = null;
let uiMasterFilter: BiquadFilterNode | null = null;

const UI_SOUND_PATTERNS: Record<UiSoundName, ToneSpec[]> = {
    tap: [
        { offsetMs: 0, durationMs: 72, startHz: 420, endHz: 620, gain: 0.05, type: 'triangle' },
        { offsetMs: 18, durationMs: 48, startHz: 740, endHz: 880, gain: 0.018, type: 'sine' },
    ],
    magic: [
        { offsetMs: 0, durationMs: 80, startHz: 523.25, endHz: 659.25, gain: 0.03, type: 'sine' },
        { offsetMs: 48, durationMs: 88, startHz: 659.25, endHz: 783.99, gain: 0.028, type: 'triangle' },
        { offsetMs: 102, durationMs: 118, startHz: 783.99, endHz: 1046.5, gain: 0.024, type: 'sine' },
    ],
    toggle_on: [
        { offsetMs: 0, durationMs: 84, startHz: 392.0, endHz: 523.25, gain: 0.028, type: 'sine' },
        { offsetMs: 36, durationMs: 92, startHz: 523.25, endHz: 783.99, gain: 0.022, type: 'triangle' },
    ],
    toggle_off: [
        { offsetMs: 0, durationMs: 96, startHz: 523.25, endHz: 392.0, gain: 0.026, type: 'sine' },
        { offsetMs: 28, durationMs: 88, startHz: 392.0, endHz: 293.66, gain: 0.018, type: 'triangle' },
    ],
    close: [
        { offsetMs: 0, durationMs: 94, startHz: 460, endHz: 280, gain: 0.02, type: 'triangle' },
    ],
    celebrate: [
        { offsetMs: 0, durationMs: 70, startHz: 523.25, gain: 0.028, type: 'triangle' },
        { offsetMs: 54, durationMs: 72, startHz: 659.25, gain: 0.03, type: 'triangle' },
        { offsetMs: 108, durationMs: 92, startHz: 783.99, endHz: 1046.5, gain: 0.028, type: 'sine' },
    ],
};

function getUiContext(): AudioContext | null {
    if (typeof window === 'undefined' || typeof window.AudioContext === 'undefined') {
        return null;
    }

    if (!uiContext) {
        uiContext = new window.AudioContext({ latencyHint: 'interactive' });
        uiMasterGain = uiContext.createGain();
        uiMasterGain.gain.value = 0.82;
        uiMasterFilter = uiContext.createBiquadFilter();
        uiMasterFilter.type = 'lowpass';
        uiMasterFilter.frequency.value = 2800;
        uiMasterFilter.Q.value = 0.45;
        uiMasterGain.connect(uiMasterFilter);
        uiMasterFilter.connect(uiContext.destination);
    }

    return uiContext;
}

function scheduleTone(ctx: AudioContext, spec: ToneSpec, volume: number) {
    if (!uiMasterGain) return;

    const startedAt = ctx.currentTime + spec.offsetMs / 1000;
    const endedAt = startedAt + spec.durationMs / 1000;
    const oscillator = ctx.createOscillator();
    const envelope = ctx.createGain();

    oscillator.type = spec.type;
    oscillator.frequency.setValueAtTime(spec.startHz, startedAt);
    oscillator.frequency.linearRampToValueAtTime(spec.endHz ?? spec.startHz, endedAt);

    envelope.gain.setValueAtTime(0.0001, startedAt);
    envelope.gain.linearRampToValueAtTime(spec.gain * volume, startedAt + 0.018);
    envelope.gain.exponentialRampToValueAtTime(0.0001, endedAt);

    oscillator.connect(envelope);
    envelope.connect(uiMasterGain);
    oscillator.onended = () => {
        try {
            oscillator.disconnect();
            envelope.disconnect();
        } catch {
            // no-op
        }
    };

    oscillator.start(startedAt);
    oscillator.stop(endedAt + 0.02);
}

function scheduleUiSound(ctx: AudioContext, name: UiSoundName, volume: number) {
    for (const tone of UI_SOUND_PATTERNS[name] ?? UI_SOUND_PATTERNS.tap) {
        scheduleTone(ctx, tone, volume);
    }
}

export function useUiSounds({ enabled = true, volume = 1 }: UseUiSoundsOptions = {}) {
    const lastPlayAtRef = useRef(0);

    const playUiSound = useCallback((name: UiSoundName = 'tap') => {
        if (!enabled) return;

        const now = typeof performance !== 'undefined' ? performance.now() : Date.now();
        if (now - lastPlayAtRef.current < 45) {
            return;
        }
        lastPlayAtRef.current = now;

        const ctx = getUiContext();
        if (!ctx) return;

        const clampedVolume = Math.max(0.15, Math.min(volume, 1));
        const play = () => scheduleUiSound(ctx, name, clampedVolume);

        if (ctx.state === 'suspended') {
            void ctx.resume().then(play).catch(() => {
                // no-op
            });
            return;
        }

        play();
    }, [enabled, volume]);

    return { playUiSound };
}
