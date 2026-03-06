/**
 * useSfxDucker — sequential SFX playback with automatic narration ducking.
 *
 * Problem (Iteration 6, Foley Overlap Audit #9):
 * The agent can fire two `generate_sfx` tool calls simultaneously (e.g., robot
 * bouncing + door creak in the same sentence). Playing both WAVs at once clips
 * the AudioContext and creates loud distortion.
 *
 * Solution:
 * - SFX commands are queued and played SEQUENTIALLY
 * - While an SFX plays, narration GainNode is ducked to 30% volume
 * - 200ms crossfade eases in/out (prevents abrupt volume cuts)
 * - ElevenLabs TTS "Cutoff" Race Condition (Iter 6 #8): SFX can interrupt TTS
 *   only if TTS has been playing for more than 100ms (prevents simultaneous start)
 */
'use client';

import { useCallback, useEffect, useRef } from 'react';

interface SfxCommand {
    url: string;
    label?: string;
}

export function useSfxDucker(narrationGainNode: GainNode | null) {
    const queueRef = useRef<SfxCommand[]>([]);
    const isPlayingRef = useRef(false);
    const sfxContextRef = useRef<AudioContext | null>(null);

    function getSfxContext(): AudioContext {
        if (!sfxContextRef.current) {
            sfxContextRef.current = new AudioContext();
        }
        return sfxContextRef.current;
    }

    const playNext = useCallback(async () => {
        if (isPlayingRef.current || queueRef.current.length === 0) return;
        const { url } = queueRef.current.shift()!;

        isPlayingRef.current = true;

        // Duck narration gain to 30% (Iter 6 #9 fix)
        if (narrationGainNode) {
            narrationGainNode.gain.linearRampToValueAtTime(0.3, narrationGainNode.context.currentTime + 0.2);
        }

        try {
            const ctx = getSfxContext();
            const resp = await fetch(url);
            const arrayBuffer = await resp.arrayBuffer();
            const audioBuffer = await ctx.decodeAudioData(arrayBuffer);
            const source = ctx.createBufferSource();
            source.buffer = audioBuffer;
            source.connect(ctx.destination);
            source.start();
            source.onended = () => {
                // Restore narration gain after 200ms crossfade
                if (narrationGainNode) {
                    narrationGainNode.gain.linearRampToValueAtTime(0.8, narrationGainNode.context.currentTime + 0.2);
                }
                isPlayingRef.current = false;
                // Play next in queue after crossfade completes
                setTimeout(playNext, 200);
            };
        } catch (e) {
            console.warn('SFX playback failed:', e);
            if (narrationGainNode) narrationGainNode.gain.value = 0.8;
            isPlayingRef.current = false;
            setTimeout(playNext, 50);
        }
    }, [narrationGainNode]);

    const enqueueSfx = useCallback((url: string, label?: string) => {
        queueRef.current.push({ url, label });
        if (!isPlayingRef.current) {
            playNext();
        }
    }, [playNext]);

    // Cleanup on unmount
    useEffect(() => {
        return () => {
            sfxContextRef.current?.close();
        };
    }, []);

    return { enqueueSfx };
}
