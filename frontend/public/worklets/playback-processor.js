/**
 * PCM Playback AudioWorklet Processor
 *
 * Plays PCM audio chunks received from the backend (24kHz 16-bit ElevenLabs audio).
 * Implements a ring buffer jitter buffer to smooth over network jitter and GC pauses.
 *
 * Key design (Iteration 3, Expert Audit #2 — Audio Stuttering fix):
 * - Queues 150ms of audio before playback begins (the "jitter buffer")
 * - Drops oldest frames if queue exceeds 200ms (Iter 10 #2 — Spurious Wakeup fix)
 *   to prevent a garbage-collection-induced "screech" burst
 */

const SAMPLE_RATE = 24000; // ElevenLabs outputs 24kHz
const BUFFER_SIZE = SAMPLE_RATE * 0.15; // 150ms pre-roll jitter buffer
const MAX_BUFFER_SIZE = SAMPLE_RATE * 30; // 30 seconds max — Gemini streams as fast as possible (burst)

class PlaybackProcessor extends AudioWorkletProcessor {
    constructor() {
        super();
        this._queue = new Float32Array(MAX_BUFFER_SIZE * 2); // Ring buffer
        this._writeIdx = 0;
        this._readIdx = 0;
        this._filled = 0;
        this._ready = false; // Don't play until jitter buffer is filled
        this._isFlushing = false;

        this.port.onmessage = (e) => {
            if (e.data && e.data.type === 'flush') {
                this._isFlushing = true;
                return;
            }
            if (e.data && e.data.type === 'interrupt') {
                // Hard flush: drop queued audio immediately for barge-in
                this._filled = 0;
                this._readIdx = 0;
                this._writeIdx = 0;
                this._ready = false;
                this._isFlushing = false;
                this.port.postMessage({ type: 'flushed' });
                return;
            }

            const int16 = new Int16Array(e.data);
            for (let i = 0; i < int16.length; i++) {
                if (this._filled >= MAX_BUFFER_SIZE) {
                    // Spurious Wakeup Fix (Iter 10 #2): drop oldest frame instead of screeching
                    this._readIdx = (this._readIdx + 1) % this._queue.length;
                    this._filled--;
                }
                this._queue[this._writeIdx] = int16[i] / 32768; // int16 -> float32
                this._writeIdx = (this._writeIdx + 1) % this._queue.length;
                this._filled++;
            }
            if (this._filled >= BUFFER_SIZE) {
                this._ready = true;
            }
        };
    }

    process(inputs, outputs) {
        const output = outputs[0];
        if (!output || !output[0]) return true;

        const framesPerBlock = output[0].length;

        if (!this._ready || this._filled === 0) {
            // Silence while buffering
            output[0].fill(0);
            if (output.length > 1) output[1].fill(0);

            if (this._isFlushing) {
                this.port.postMessage({ type: 'flushed' });
                this._isFlushing = false;
            }
            return true;
        }

        for (let frame = 0; frame < framesPerBlock; frame++) {
            if (this._filled > 0) {
                const sample = this._queue[this._readIdx];
                this._readIdx = (this._readIdx + 1) % this._queue.length;
                this._filled--;

                output[0][frame] = sample;
                if (output.length > 1) {
                    output[1][frame] = sample;
                }
            } else {
                output[0][frame] = 0;
                if (output.length > 1) {
                    output[1][frame] = 0;
                }
                this._ready = false; // Re-enter buffering state on starvation
                if (this._isFlushing) {
                    this.port.postMessage({ type: 'flushed' });
                    this._isFlushing = false;
                }
            }
        }

        return true;
    }
}

registerProcessor('playback-processor', PlaybackProcessor);
