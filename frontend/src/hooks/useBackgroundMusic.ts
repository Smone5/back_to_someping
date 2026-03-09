'use client';

import { useCallback, useEffect, useRef } from 'react';

type Mood = 'playful' | 'magical' | 'suspenseful' | 'triumphant_celebration' | string;

export function useBackgroundMusic() {
    const ctxRef = useRef<AudioContext | null>(null);
    const masterGainRef = useRef<GainNode | null>(null);
    const filterRef = useRef<BiquadFilterNode | null>(null);
    const highpassRef = useRef<BiquadFilterNode | null>(null);
    const intervalRef = useRef<ReturnType<typeof setInterval> | null>(null);
    const activeOscillatorsRef = useRef<OscillatorNode[]>([]);
    const stepRef = useRef(0);
    const lastMoodRef = useRef<Mood | null>(null);
    const lastIntensityRef = useRef(5);
    const lastMoodChangeAtRef = useRef(0);
    const lastTickAtRef = useRef(0);
    const musicEnabledRef = useRef(true);
    const resetInProgressRef = useRef(false);
    const bedGainRef = useRef(0);
    const listeningFocusRef = useRef(false);
    const MIN_MOOD_HOLD_MS = 7000;

    const applyVoicing = useCallback((ctx: AudioContext, rampSeconds = 0.18) => {
        const master = masterGainRef.current;
        const lowpass = filterRef.current;
        const highpass = highpassRef.current;
        if (!master || !lowpass || !highpass) return;

        const focusMultiplier = listeningFocusRef.current ? 0.2 : 1;
        const targetGain = musicEnabledRef.current ? bedGainRef.current * focusMultiplier : 0;
        const targetLowpass = listeningFocusRef.current ? 820 : 1400;
        const targetHighpass = listeningFocusRef.current ? 180 : 120;

        master.gain.cancelScheduledValues(ctx.currentTime);
        master.gain.setTargetAtTime(targetGain, ctx.currentTime, rampSeconds);

        lowpass.frequency.cancelScheduledValues(ctx.currentTime);
        lowpass.frequency.setTargetAtTime(targetLowpass, ctx.currentTime, rampSeconds);

        highpass.frequency.cancelScheduledValues(ctx.currentTime);
        highpass.frequency.setTargetAtTime(targetHighpass, ctx.currentTime, rampSeconds);
    }, []);

    const ensureContext = useCallback(async () => {
        if (!ctxRef.current) {
            ctxRef.current = new AudioContext();
            masterGainRef.current = ctxRef.current.createGain();
            masterGainRef.current.gain.value = 0.0;
            highpassRef.current = ctxRef.current.createBiquadFilter();
            highpassRef.current.type = 'highpass';
            highpassRef.current.frequency.value = 120;
            highpassRef.current.Q.value = 0.5;
            filterRef.current = ctxRef.current.createBiquadFilter();
            filterRef.current.type = 'lowpass';
            filterRef.current.frequency.value = 1400;
            filterRef.current.Q.value = 0.7;
            masterGainRef.current.connect(highpassRef.current);
            highpassRef.current.connect(filterRef.current);
            filterRef.current.connect(ctxRef.current.destination);
        }
        if (ctxRef.current.state === 'suspended') {
            await ctxRef.current.resume();
        }
        return ctxRef.current;
    }, []);

    const playOneShot = useCallback((ctx: AudioContext, freq: number, durationMs: number, type: OscillatorType, gain: number) => {
        const osc = ctx.createOscillator();
        const env = ctx.createGain();
        osc.type = type;
        osc.frequency.value = freq;
        env.gain.setValueAtTime(0.0001, ctx.currentTime);
        env.gain.linearRampToValueAtTime(gain, ctx.currentTime + 0.03);
        env.gain.exponentialRampToValueAtTime(0.0001, ctx.currentTime + durationMs / 1000);
        osc.connect(env);
        env.connect(masterGainRef.current!);
        osc.onended = () => {
            try {
                osc.disconnect();
                env.disconnect();
            } catch {
                // no-op
            }
        };
        osc.start();
        osc.stop(ctx.currentTime + durationMs / 1000 + 0.02);
        lastTickAtRef.current = performance.now();
    }, []);

    const stopMusic = useCallback(() => {
        if (intervalRef.current) {
            clearInterval(intervalRef.current);
            intervalRef.current = null;
        }
        for (const osc of activeOscillatorsRef.current) {
            try {
                osc.stop();
                osc.disconnect();
            } catch {
                // no-op
            }
        }
        activeOscillatorsRef.current = [];
        if (masterGainRef.current && ctxRef.current) {
            masterGainRef.current.gain.cancelScheduledValues(ctxRef.current.currentTime);
            masterGainRef.current.gain.setValueAtTime(0.0, ctxRef.current.currentTime);
        }
        bedGainRef.current = 0;
        lastTickAtRef.current = 0;
    }, []);

    const hardReset = useCallback(async () => {
        if (resetInProgressRef.current) return;
        resetInProgressRef.current = true;
        stopMusic();
        if (ctxRef.current && ctxRef.current.state !== 'closed') {
            try {
                await ctxRef.current.close();
            } catch {
                // ignore
            }
        }
        ctxRef.current = null;
        masterGainRef.current = null;
        highpassRef.current = null;
        filterRef.current = null;
        resetInProgressRef.current = false;
    }, [stopMusic]);

    const setMusicMood = useCallback(async (mood: Mood, intensity: number = 5) => {
        const previousMood = lastMoodRef.current;
        const previousIntensity = lastIntensityRef.current;
        if (!musicEnabledRef.current) {
            lastMoodRef.current = mood;
            lastIntensityRef.current = intensity;
            stopMusic();
            return;
        }

        const now = performance.now();
        if (
            previousMood === mood &&
            previousIntensity === intensity &&
            intervalRef.current
        ) {
            return;
        }
        if (
            intervalRef.current &&
            previousMood &&
            mood !== previousMood &&
            now - lastMoodChangeAtRef.current < MIN_MOOD_HOLD_MS
        ) {
            return;
        }
        const ctx = await ensureContext();
        stopMusic();

        lastMoodRef.current = mood;
        lastIntensityRef.current = intensity;
        lastMoodChangeAtRef.current = now;

        stepRef.current = 0;
        const normalizedIntensity = Math.max(1, Math.min(10, intensity));
        // Louder bed so music is audible under narration on mobile/tablet speakers.
        const bedGain = Math.min(0.16, 0.04 + normalizedIntensity * 0.012);
        bedGainRef.current = bedGain;
        applyVoicing(ctx, 0.25);

        if (mood === 'suspenseful') {
            const droneA = ctx.createOscillator();
            const droneB = ctx.createOscillator();
            droneA.type = 'sine';
            droneB.type = 'triangle';
            droneA.frequency.value = 98;
            droneB.frequency.value = 146.83;
            const droneGain = ctx.createGain();
            droneGain.gain.value = bedGain * 0.5;
            droneA.connect(droneGain);
            droneB.connect(droneGain);
            droneGain.connect(masterGainRef.current!);
            droneA.start();
            droneB.start();
            activeOscillatorsRef.current.push(droneA, droneB);
            lastTickAtRef.current = performance.now();
            intervalRef.current = setInterval(() => {
                const pulseFreq = [220, 233.08, 246.94, 196][stepRef.current % 4];
                playOneShot(ctx, pulseFreq, 190, 'triangle', 0.06);
                stepRef.current += 1;
            }, 850);
            return;
        }

        const patterns: Record<string, { notes: number[]; ms: number; type: OscillatorType; gain: number }> = {
            playful: {
                notes: [261.63, 329.63, 392.0, 523.25, 392.0, 329.63],
                ms: 360,
                type: 'sine',
                gain: 0.07,
            },
            magical: {
                notes: [329.63, 392.0, 493.88, 659.25, 587.33, 493.88],
                ms: 520,
                type: 'sine',
                gain: 0.065,
            },
            triumphant_celebration: {
                notes: [261.63, 329.63, 392.0, 523.25, 659.25, 784.0],
                ms: 280,
                type: 'triangle',
                gain: 0.07,
            },
        };

        const selected = patterns[mood] ?? patterns.playful;
        intervalRef.current = setInterval(() => {
            const note = selected.notes[stepRef.current % selected.notes.length];
            playOneShot(ctx, note, Math.max(180, selected.ms - 40), selected.type, selected.gain);
            if (mood === 'magical') {
                playOneShot(ctx, note * 2, 140, 'sine', selected.gain * 0.35);
            }
            stepRef.current += 1;
        }, selected.ms);
    }, [applyVoicing, ensureContext, playOneShot, stopMusic]);

    const reviveMusic = useCallback(async () => {
        if (!musicEnabledRef.current) return;
        if (!ctxRef.current) return;
        if (ctxRef.current.state === 'suspended') {
            await ctxRef.current.resume();
        }
        if (!intervalRef.current && lastMoodRef.current) {
            void setMusicMood(lastMoodRef.current, lastIntensityRef.current);
        }
    }, [setMusicMood]);

    useEffect(() => {
        const onVisibility = () => {
            if (document.visibilityState === 'visible') {
                void reviveMusic();
            }
        };
        const onPointer = () => {
            void reviveMusic();
        };
        document.addEventListener('visibilitychange', onVisibility);
        document.addEventListener('pointerdown', onPointer);
        const watchdog = window.setInterval(() => {
            if (!musicEnabledRef.current) {
                stopMusic();
                return;
            }
            const now = performance.now();
            if (document.visibilityState !== 'visible') {
                return;
            }
            if (ctxRef.current?.state === 'suspended') {
                void reviveMusic();
                return;
            }
            if (intervalRef.current && now - lastTickAtRef.current > 6000 && lastMoodRef.current) {
                void hardReset().then(() => {
                    void setMusicMood(lastMoodRef.current!, lastIntensityRef.current);
                });
                return;
            }
            if (activeOscillatorsRef.current.length > 0 && now - lastTickAtRef.current > 6000) {
                void hardReset().then(() => {
                    if (lastMoodRef.current) {
                        void setMusicMood(lastMoodRef.current!, lastIntensityRef.current);
                    }
                });
            }
        }, 8000);
        return () => {
            document.removeEventListener('visibilitychange', onVisibility);
            document.removeEventListener('pointerdown', onPointer);
            window.clearInterval(watchdog);
        };
    }, [hardReset, reviveMusic, setMusicMood, stopMusic]);

    useEffect(() => {
        return () => {
            stopMusic();
            if (ctxRef.current && ctxRef.current.state !== 'closed') {
                void ctxRef.current.close();
            }
        };
    }, [stopMusic]);

    const setMusicEnabled = useCallback((enabled: boolean) => {
        musicEnabledRef.current = enabled;
        if (!enabled) {
            stopMusic();
            return;
        }
        if (lastMoodRef.current) {
            void setMusicMood(lastMoodRef.current, lastIntensityRef.current);
        }
    }, [setMusicMood, stopMusic]);

    const setListeningFocus = useCallback((focused: boolean) => {
        listeningFocusRef.current = focused;
        if (ctxRef.current) {
            void ensureContext().then((ctx) => {
                applyVoicing(ctx, 0.12);
            });
        }
    }, [applyVoicing, ensureContext]);

    return { setMusicMood, stopMusic, setMusicEnabled, setListeningFocus };
}
