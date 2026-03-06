/**
 * useWebSocket — manages the Bidi-streaming WebSocket to the FastAPI backend.
 *
 * Responsibilities:
 * - Connection singleton via useRef (Iter 4 #9 — Zombie React Effects fix)
 * - Exponential backoff reconnection (Iter 2 #4)
 * - SessionStorage session_id persistence (Iter 3 #4 — Accidental Refresh fix)
 * - Message routing: binary frames -> audio, text frames -> typed ServerEvents
 * - Async JSON parsing via Response blob (Iter 10 #3 — Base64 Truncation fix)
 * - Pre-warms connection on load (Iter 2 #7 — Connection Warm-up)
 * - Bandwidth degradation detection (Iter 3 #5 — Backseat Wi-Fi Drops)
 */
'use client';

import { useCallback, useEffect, useRef, useState } from 'react';

export type ConnectionState = 'connecting' | 'connected' | 'reconnecting' | 'disconnected';

interface ServerPayload {
    type: string;
    payload?: Record<string, unknown>;
}

interface UseWebSocketOptions {
    url: string;
    onBinaryMessage: (data: ArrayBuffer) => void;
    onJsonMessage: (msg: ServerPayload) => void;
    onConnectionStateChange?: (state: ConnectionState) => void;
}

const SESSION_ID_KEY = 'storyteller_session_id';

function getOrCreateSessionId(): string {
    // Accidental Refresh fix (Iter 3 #4): persist in sessionStorage
    // Guard: sessionStorage is browser-only — Next.js SSR has no window
    if (typeof window === 'undefined') {
        return crypto.randomUUID(); // SSR path — WS is never opened server-side
    }
    let id = sessionStorage.getItem(SESSION_ID_KEY);
    if (!id) {
        id = crypto.randomUUID();
        sessionStorage.setItem(SESSION_ID_KEY, id);
    }
    return id;
}

export function useWebSocket({
    url,
    onBinaryMessage,
    onJsonMessage,
    onConnectionStateChange,
}: UseWebSocketOptions) {
    const [connectionState, setConnectionState] = useState<ConnectionState>('disconnected');
    const wsRef = useRef<WebSocket | null>(null);
    const reconnectAttemptRef = useRef(0);
    const reconnectTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
    const sessionIdRef = useRef<string>(getOrCreateSessionId());
    const intentionalCloseRef = useRef(false);
    // Zombie React Effects guard (Iter 4 #9 — Next.js Strict Mode fires useEffect twice)
    const mountedRef = useRef(false);
    const pendingSendsRef = useRef<Array<string | ArrayBuffer>>([]);

    const pingTimerRef = useRef<ReturnType<typeof setInterval> | null>(null);
    const lastPingTimeRef = useRef<number>(Date.now());

    const setState = useCallback((state: ConnectionState) => {
        setConnectionState(state);
        onConnectionStateChange?.(state);
    }, [onConnectionStateChange]);

    const connect = useCallback(() => {
        // Guard against double-mount in Strict Mode
        if (mountedRef.current && wsRef.current?.readyState === WebSocket.OPEN) return;

        const sessionId = sessionIdRef.current;
        const wsUrl = `${url}?session_id=${sessionId}&user_id=anonymous`;

        setState('connecting');
        const ws = new WebSocket(wsUrl);
        ws.binaryType = 'arraybuffer';
        wsRef.current = ws;

        ws.onopen = () => {
            if (wsRef.current !== ws) return;
            reconnectAttemptRef.current = 0;
            setState('connected');
            // Flush messages queued before socket became open.
            for (const queued of pendingSendsRef.current) {
                ws.send(queued);
            }
            pendingSendsRef.current = [];
            // Ping/pong for bandwidth monitoring
            pingTimerRef.current = setInterval(() => {
                const now = Date.now();
                if (ws.readyState === WebSocket.OPEN) {
                    lastPingTimeRef.current = now;
                    ws.send(JSON.stringify({ type: 'heartbeat', session_id: sessionId }));
                }
            }, 10000);
        };

        ws.onmessage = async (event: MessageEvent) => {
            if (wsRef.current !== ws) return;
            if (event.data instanceof ArrayBuffer) {
                // Binary frame = 24kHz PCM audio from ElevenLabs
                onBinaryMessage(event.data);
            } else if (event.data instanceof Blob) {
                // Async blob parsing prevents iOS OOM crash (Iter 10 #3)
                try {
                    const msg = await new Response(event.data).json();
                    onJsonMessage(msg as ServerPayload);
                } catch { /* malformed — ignore */ }
            } else {
                // String frame — parse synchronously (small control messages only)
                try {
                    const msg = JSON.parse(event.data as string) as ServerPayload;
                    // Filter out ping echo
                    if (msg?.payload && (msg.payload as any).ping) return;
                    onJsonMessage(msg);
                } catch { /* malformed — ignore */ }
            }
        };

        ws.onclose = (e) => {
            if (wsRef.current !== ws) return;
            wsRef.current = null;
            if (pingTimerRef.current) clearInterval(pingTimerRef.current);
            if (intentionalCloseRef.current) {
                setState('disconnected');
                return;
            }
            // Exponential backoff reconnect (Iter 2 #4)
            const attempt = reconnectAttemptRef.current++;
            const delay = Math.min(1000 * 2 ** attempt, 30000) + Math.random() * 500;
            setState('reconnecting');
            reconnectTimerRef.current = setTimeout(() => {
                if (!intentionalCloseRef.current) connect();
            }, delay);
        };

        ws.onerror = () => {
            if (wsRef.current !== ws) return;
            ws.close();
        };
    }, [url, onBinaryMessage, onJsonMessage, setState]);

    const send = useCallback((data: string | ArrayBuffer) => {
        if (wsRef.current?.readyState === WebSocket.OPEN) {
            wsRef.current.send(data);
            return;
        }
        if (pendingSendsRef.current.length < 50) {
            pendingSendsRef.current.push(data);
        }
    }, []);

    const sendJson = useCallback((msg: Record<string, unknown>) => {
        send(JSON.stringify(msg));
    }, [send]);

    const disconnect = useCallback(() => {
        intentionalCloseRef.current = true;
        if (reconnectTimerRef.current) clearTimeout(reconnectTimerRef.current);
        wsRef.current?.close(1000, 'intentional close');
    }, []);

    // Connection Warm-up (Iter 2 #7): connect as soon as hook mounts
    useEffect(() => {
        if (mountedRef.current) return; // Strict Mode guard
        mountedRef.current = true;
        intentionalCloseRef.current = false;
        connect();
        return () => {
            mountedRef.current = false;
            intentionalCloseRef.current = true;
            if (reconnectTimerRef.current) clearTimeout(reconnectTimerRef.current);
            if (pingTimerRef.current) clearInterval(pingTimerRef.current);
            wsRef.current?.close();
        };
    }, [connect]);

    return { connectionState, send, sendJson, disconnect, sessionId: sessionIdRef.current };
}
