'use client';

/**
 * MagicMirror — WebGL voice visualizer ("Pensieve" / fluid mirror effect)
 *
 * When the agent is thinking/speaking, the screen shows a swirling fluid
 * simulation that reacts to the child's voice volume in real-time.
 *
 * Implementation:
 * - Uses WebGL2 shader with simplex-noise-based fluid distortion
 * - voiceRms (0–1) drives the amplitude of the swirl
 * - WebGL Context Loss handled gracefully (Iter 5 #4 — WebGL Context Loss fix)
 * - Falls back to a CSS radial animation if WebGL is unavailable (Iter 2 #8)
 */

import { useEffect, useRef } from 'react';

export type MagicMirrorMode = 'idle' | 'user-speaking' | 'thinking' | 'speaking';

export interface MagicMirrorProps {
    voiceRms: number;       // 0–1, updated per audio frame
    mode: MagicMirrorMode;
}

const VERTEX_SHADER = `#version 300 es
in vec2 a_position;
void main() {
  gl_Position = vec4(a_position, 0.0, 1.0);
}`;

const FRAGMENT_SHADER = `#version 300 es
precision highp float;
uniform float u_time;
uniform float u_rms;
uniform vec2  u_resolution;
out vec4 fragColor;

// Permutation table for simplex noise
vec3 mod289v3(vec3 x) { return x - floor(x * (1.0/289.0)) * 289.0; }
vec2 mod289v2(vec2 x) { return x - floor(x * (1.0/289.0)) * 289.0; }
vec3 permute(vec3 x) { return mod289v3(((x*34.0)+1.0)*x); }

float snoise(vec2 v) {
  const vec4 C = vec4(0.211324865405187, 0.366025403784439, -0.577350269189626, 0.024390243902439);
  vec2 i = floor(v + dot(v, C.yy));
  vec2 x0 = v - i + dot(i, C.xx);
  vec2 i1;
  i1 = (x0.x > x0.y) ? vec2(1.0, 0.0) : vec2(0.0, 1.0);
  vec4 x12 = x0.xyxy + C.xxzz;
  x12.xy -= i1;
  i = mod289v2(i);
  vec3 p = permute(permute(i.y + vec3(0.0, i1.y, 1.0)) + i.x + vec3(0.0, i1.x, 1.0));
  vec3 m = max(0.5 - vec3(dot(x0, x0), dot(x12.xy, x12.xy), dot(x12.zw, x12.zw)), 0.0);
  m = m * m; m = m * m;
  vec3 x = 2.0 * fract(p * C.www) - 1.0;
  vec3 h = abs(x) - 0.5;
  vec3 ox = floor(x + 0.5);
  vec3 a0 = x - ox;
  m *= 1.79284291400159 - 0.85373472095314 * (a0*a0 + h*h);
  vec3 g;
  g.x  = a0.x  * x0.x  + h.x  * x0.y;
  g.yz = a0.yz * x12.xz + h.yz * x12.yw;
  return 130.0 * dot(m, g);
}

void main() {
  vec2 uv = gl_FragCoord.xy / u_resolution.xy;
  uv = uv * 2.0 - 1.0;
  uv.x *= u_resolution.x / u_resolution.y;

  float amplitude = 0.3 + u_rms * 1.2;
  float speed = 0.5 + u_rms * 2.0;
  float t = u_time * speed;

  float n1 = snoise(uv * 2.0 + vec2(t * 0.3, t * 0.2)) * amplitude;
  float n2 = snoise(uv * 3.0 - vec2(t * 0.2, t * 0.4)) * amplitude * 0.6;
  float n3 = snoise(uv * 5.0 + vec2(t * 0.5, -t * 0.3)) * amplitude * 0.3;

  float combined = n1 + n2 + n3;

  vec3 col1 = vec3(0.4, 0.1, 0.9);  // Deep purple
  vec3 col2 = vec3(0.0, 0.7, 1.0);  // Cyan
  vec3 col3 = vec3(1.0, 0.3, 0.7);  // Pink

  vec3 color = mix(col1, col2, sin(combined + t * 0.5) * 0.5 + 0.5);
  color = mix(color, col3, cos(combined * 1.3 + t * 0.7) * 0.5 + 0.5);

  // Vignette toward edges for "mirror" feel
  float d = length(uv);
  color *= smoothstep(1.5, 0.3, d);

  fragColor = vec4(color, 0.9);
}`;

function deriveVisualEnergy(mode: MagicMirrorMode, voiceRms: number): number {
    const clampedRms = Math.min(1, Math.max(0, voiceRms));
    switch (mode) {
        case 'speaking':
            return Math.min(1, Math.max(0.28, clampedRms * 2.2));
        case 'thinking':
            return Math.min(0.72, Math.max(0.18, clampedRms * 1.6 + 0.08));
        case 'user-speaking':
            return Math.min(0.9, Math.max(0.16, clampedRms * 2.4));
        case 'idle':
        default:
            return Math.min(0.22, Math.max(0.06, clampedRms * 1.2));
    }
}

function getCanvasOpacity(mode: MagicMirrorMode): number {
    switch (mode) {
        case 'speaking':
            return 1;
        case 'thinking':
            return 0.9;
        case 'user-speaking':
            return 0.82;
        case 'idle':
        default:
            return 0.58;
    }
}

export default function MagicMirror({ voiceRms, mode }: MagicMirrorProps) {
    const canvasRef = useRef<HTMLCanvasElement>(null);
    const glRef = useRef<WebGL2RenderingContext | null>(null);
    const programRef = useRef<WebGLProgram | null>(null);
    const rafRef = useRef<number | null>(null);
    const voiceRmsRef = useRef(voiceRms);
    const modeRef = useRef<MagicMirrorMode>(mode);
    const startTimeRef = useRef(0);
    const lowPowerModeRef = useRef(false);
    const forceCssFallbackRef = useRef(false);
    const lastFrameAtRef = useRef(0);

    useEffect(() => {
        voiceRmsRef.current = voiceRms;
    }, [voiceRms]);

    useEffect(() => {
        modeRef.current = mode;
    }, [mode]);

    useEffect(() => {
        const canvas = canvasRef.current;
        if (!canvas) return;
        startTimeRef.current = performance.now();
        lastFrameAtRef.current = 0;
        const connection = (
            navigator as Navigator & {
                connection?: {
                    saveData?: boolean;
                };
            }
        ).connection;
        const prefersReducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
        const isCompact = window.matchMedia('(max-width: 900px)').matches;
        lowPowerModeRef.current = isCompact || prefersReducedMotion || Boolean(connection?.saveData);
        forceCssFallbackRef.current = prefersReducedMotion || Boolean(connection?.saveData);

        const stopRenderLoop = () => {
            if (rafRef.current) {
                cancelAnimationFrame(rafRef.current);
                rafRef.current = null;
            }
        };

        // WebGL2 Context Loss handling (Iter 5 #4)
        const handleContextLost = (e: Event) => {
            e.preventDefault();
            stopRenderLoop();
            glRef.current = null;
        };
        const handleContextRestored = () => {
            initWebGL();
            startRenderLoop();
        };
        canvas.addEventListener('webglcontextlost', handleContextLost);
        canvas.addEventListener('webglcontextrestored', handleContextRestored);

        function initWebGL() {
            if (forceCssFallbackRef.current) {
                canvas!.style.background = 'radial-gradient(circle, #6310e0, #0097d9)';
                canvas!.style.animation = 'pulse 2s ease-in-out infinite';
                programRef.current = null;
                return;
            }
            const gl = canvas!.getContext('webgl2');
            if (!gl) {
                // Fallback: CSS animation (Iter 2 #8)
                canvas!.style.background = 'radial-gradient(circle, #6310e0, #0097d9)';
                canvas!.style.animation = 'pulse 1.5s ease-in-out infinite';
                programRef.current = null;
                return;
            }

            const glCtx = gl;
            glRef.current = glCtx;

            function compileShader(type: number, src: string): WebGLShader | null {
                const s = glCtx.createShader(type)!;
                glCtx.shaderSource(s, src);
                glCtx.compileShader(s);
                if (!glCtx.getShaderParameter(s, glCtx.COMPILE_STATUS)) {
                    console.error('Shader compile error:', glCtx.getShaderInfoLog(s));
                    glCtx.deleteShader(s);
                    return null;
                }
                return s;
            }

            const vs = compileShader(glCtx.VERTEX_SHADER, VERTEX_SHADER);
            const fs = compileShader(glCtx.FRAGMENT_SHADER, FRAGMENT_SHADER);
            if (!vs || !fs) return;

            const prog = glCtx.createProgram()!;
            glCtx.attachShader(prog, vs);
            glCtx.attachShader(prog, fs);
            glCtx.linkProgram(prog);
            if (!glCtx.getProgramParameter(prog, glCtx.LINK_STATUS)) {
                console.error('Program link error:', glCtx.getProgramInfoLog(prog));
                glCtx.deleteProgram(prog);
                glCtx.deleteShader(vs);
                glCtx.deleteShader(fs);
                programRef.current = null;
                return;
            }
            programRef.current = prog;
            glCtx.deleteShader(vs);
            glCtx.deleteShader(fs);

            // Full-screen quad
            const buf = glCtx.createBuffer();
            glCtx.bindBuffer(glCtx.ARRAY_BUFFER, buf);
            glCtx.bufferData(glCtx.ARRAY_BUFFER, new Float32Array([-1, -1, 1, -1, -1, 1, 1, 1]), glCtx.STATIC_DRAW);
            const loc = glCtx.getAttribLocation(prog, 'a_position');
            glCtx.enableVertexAttribArray(loc);
            glCtx.vertexAttribPointer(loc, 2, glCtx.FLOAT, false, 0, 0);
        }

        function startRenderLoop() {
            stopRenderLoop();

            const render = () => {
                const activeCanvas = canvasRef.current;
                const gl = glRef.current;
                const prog = programRef.current;
                if (!activeCanvas) return;
                const now = performance.now();
                const minFrameIntervalMs = lowPowerModeRef.current
                    ? (modeRef.current === 'idle' ? 1000 / 12 : 1000 / 24)
                    : 1000 / 60;
                if (lastFrameAtRef.current && now - lastFrameAtRef.current < minFrameIntervalMs) {
                    rafRef.current = requestAnimationFrame(render);
                    return;
                }
                lastFrameAtRef.current = now;

                if (gl && prog) {
                    const width = Math.max(1, activeCanvas.clientWidth);
                    const height = Math.max(1, activeCanvas.clientHeight);
                    if (activeCanvas.width !== width || activeCanvas.height !== height) {
                        activeCanvas.width = width;
                        activeCanvas.height = height;
                    }

                    gl.viewport(0, 0, activeCanvas.width, activeCanvas.height);
                    gl.useProgram(prog);
                    gl.uniform1f(
                        gl.getUniformLocation(prog, 'u_time'),
                        (now - startTimeRef.current) / 1000
                    );
                    gl.uniform1f(
                        gl.getUniformLocation(prog, 'u_rms'),
                        deriveVisualEnergy(modeRef.current, voiceRmsRef.current)
                    );
                    gl.uniform2f(gl.getUniformLocation(prog, 'u_resolution'), activeCanvas.width, activeCanvas.height);
                    gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
                }

                rafRef.current = requestAnimationFrame(render);
            };

            rafRef.current = requestAnimationFrame(render);
        }

        initWebGL();
        startRenderLoop();

        return () => {
            canvas.removeEventListener('webglcontextlost', handleContextLost);
            canvas.removeEventListener('webglcontextrestored', handleContextRestored);
            stopRenderLoop();
        };
    }, []);

    return (
        <canvas
            ref={canvasRef}
            className="magic-mirror-canvas"
            aria-hidden="true"
            style={{
                position: 'absolute',
                inset: 0,
                width: '100%',
                height: '100%',
                opacity: getCanvasOpacity(mode),
                transition: 'opacity 0.5s ease',
                pointerEvents: 'none',
                borderRadius: '50%',
            }}
        />
    );
}
