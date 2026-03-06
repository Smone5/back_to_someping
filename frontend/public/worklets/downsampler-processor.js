/**
 * 16kHz PCM Downsampler AudioWorklet Processor
 *
 * This runs inside the browser's AudioWorklet thread (NOT the main JS thread).
 * It receives raw floating-point samples from the microphone (typically 44.1kHz
 * or 48kHz depending on hardware) and outputs 16kHz 16-bit signed integer PCM
 * required by Gemini Live API.
 *
 * Key design (Iteration 9, Integration Audit #2 — Audio Mismatch):
 * The browser NEVER captures at exactly 16kHz. We must downsample here.
 * Not doing this = Gemini hears "chipmunk" audio and fails to transcribe.
 */

class DownsamplerProcessor extends AudioWorkletProcessor {
    constructor(options) {
        super();
        // Input sample rate comes from the AudioContext (hardware rate, 44100 or 48000)
        this._inputSampleRate = options.processorOptions?.inputSampleRate ?? sampleRate;
        this._targetSampleRate = 16000;
        this._ratio = this._inputSampleRate / this._targetSampleRate;
        this._buffer = [];
        // Aggregate to ~20ms frames (320 samples at 16kHz) to reduce WS overhead.
        this._targetChunkSamples = 320;
        this._outBuffer = [];
    }

    process(inputs) {
        const input = inputs[0];
        if (!input || !input[0]) return true;

        const samples = input[0]; // Mono channel

        // Naive linear downsampling — fast enough for real-time use
        for (let i = 0; i < samples.length; i++) {
            this._buffer.push(samples[i]);
        }

        // Output every `ratio` input samples as one output sample
        const outputSamples = [];
        let offset = 0;
        while (offset + this._ratio <= this._buffer.length) {
            // Average a window to reduce aliasing
            let sum = 0;
            const start = Math.floor(offset);
            const end = Math.min(Math.floor(offset + this._ratio), this._buffer.length);
            for (let j = start; j < end; j++) {
                sum += this._buffer[j];
            }
            const avg = sum / (end - start);
            // Convert float32 [-1, 1] to int16 [-32768, 32767]
            const int16 = Math.max(-32768, Math.min(32767, Math.round(avg * 32767)));
            outputSamples.push(int16);
            offset += this._ratio;
        }

        // Keep leftover samples for next process() call
        this._buffer = this._buffer.slice(Math.floor(offset));

        if (outputSamples.length > 0) {
            // Accumulate to a larger chunk size to avoid tiny network frames.
            for (let i = 0; i < outputSamples.length; i++) {
                this._outBuffer.push(outputSamples[i]);
            }
            while (this._outBuffer.length >= this._targetChunkSamples) {
                const chunk = this._outBuffer.slice(0, this._targetChunkSamples);
                this._outBuffer = this._outBuffer.slice(this._targetChunkSamples);
                const pcm = new Int16Array(chunk);
                this.port.postMessage(pcm.buffer, [pcm.buffer]);
            }
        }

        return true; // Keep processor alive
    }
}

registerProcessor('downsampler-processor', DownsamplerProcessor);
