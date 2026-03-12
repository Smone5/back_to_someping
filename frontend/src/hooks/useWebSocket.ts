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
    const connectTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);
    const sessionIdRef = useRef<string>(getOrCreateSessionId());
    const intentionalCloseRef = useRef(false);
    // Zombie React Effects guard (Iter 4 #9 — Next.js Strict Mode fires useEffect twice)
    const mountedRef = useRef(false);
    const pendingSendsRef = useRef<Array<string | ArrayBuffer>>([]);

    const pingTimerRef = useRef<ReturnType<typeof setInterval> | null>(null);
    const lastPingTimeRef = useRef<number>(Date.now());
    const staleSocketTimerRef = useRef<ReturnType<typeof setInterval> | null>(null);
    const lastServerActivityRef = useRef<number>(Date.now());
    const lastTransportHeartbeatRef = useRef<number>(Date.now());
    const reconnectReasonRef = useRef<string>('initial');

    const setState = useCallback((state: ConnectionState) => {
        setConnectionState(state);
        onConnectionStateChange?.(state);
    }, [onConnectionStateChange]);

    const shouldBufferWhenDisconnected = useCallback((data: string | ArrayBuffer): boolean => {
        if (data instanceof ArrayBuffer) {
            return false;
        }
        try {
            const parsed = JSON.parse(data) as { type?: unknown } | null;
            const msgType = typeof parsed?.type === 'string' ? parsed.type : '';
            if (!msgType) {
                return true;
            }
            if (msgType === 'heartbeat' || msgType === 'activity_start' || msgType === 'activity_end') {
                return false;
            }
        } catch {
            // Non-JSON strings are uncommon here; allow them through as best-effort.
        }
        return true;
    }, []);

    const clearSocketTimers = useCallback(() => {
        if (connectTimeoutRef.current) {
            clearTimeout(connectTimeoutRef.current);
            connectTimeoutRef.current = null;
        }
        if (pingTimerRef.current) {
            clearInterval(pingTimerRef.current);
            pingTimerRef.current = null;
        }
        if (staleSocketTimerRef.current) {
            clearInterval(staleSocketTimerRef.current);
            staleSocketTimerRef.current = null;
        }
    }, []);

    const markTransportHeartbeat = useCallback((msg?: ServerPayload | null) => {
        const msgType = typeof msg?.type === 'string' ? msg.type : '';
        const payload = (msg?.payload ?? {}) as Record<string, unknown>;
        if (
            msgType === 'heartbeat'
            || msgType === 'heartbeat_ack'
            || payload.ping === true
            || payload.pong === true
        ) {
            lastTransportHeartbeatRef.current = Date.now();
        }
    }, []);

    const forceReconnect = useCallback((reason: string) => {
        reconnectReasonRef.current = reason;
        clearSocketTimers();
        const ws = wsRef.current;
        if (!ws) {
            return;
        }
        if (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING) {
            try {
                ws.close(4002, reason);
            } catch {
                // Best effort only; onclose will drive reconnect.
            }
        }
    }, [clearSocketTimers]);

    const connect = useCallback(() => {
        // Guard against double-mount in Strict Mode
        if (
            mountedRef.current
            && (
                wsRef.current?.readyState === WebSocket.OPEN
                || wsRef.current?.readyState === WebSocket.CONNECTING
            )
        ) {
            return;
        }

        const sessionId = sessionIdRef.current;
        const wsUrl = `${url}?session_id=${sessionId}&user_id=anonymous`;

        setState('connecting');
        const ws = new WebSocket(wsUrl);
        ws.binaryType = 'arraybuffer';
        wsRef.current = ws;
        connectTimeoutRef.current = setTimeout(() => {
            if (wsRef.current !== ws) {
                return;
            }
            if (ws.readyState === WebSocket.CONNECTING) {
                reconnectReasonRef.current = 'connect_timeout';
                try {
                    ws.close(4006, 'connect timeout');
                } catch {
                    // Best effort only; onclose will drive reconnect.
                }
            }
        }, 8000);

        ws.onopen = () => {
            if (wsRef.current !== ws) return;
            reconnectAttemptRef.current = 0;
            const now = Date.now();
            lastServerActivityRef.current = now;
            lastTransportHeartbeatRef.current = now;
            setState('connected');
            // Flush messages queued before socket became open.
            for (const queued of pendingSendsRef.current) {
                ws.send(queued);
            }
            pendingSendsRef.current = [];
            ws.send(JSON.stringify({
                type: 'heartbeat',
                session_id: sessionId,
                payload: { client_ts_ms: now, reason: 'open' },
            }));
            // Ping/pong for bandwidth monitoring
            pingTimerRef.current = setInterval(() => {
                if (typeof document !== 'undefined' && document.visibilityState !== 'visible') {
                    return;
                }
                const pingNow = Date.now();
                if (ws.readyState === WebSocket.OPEN) {
                    lastPingTimeRef.current = pingNow;
                    ws.send(JSON.stringify({
                        type: 'heartbeat',
                        session_id: sessionId,
                        payload: { client_ts_ms: pingNow, reason: 'keepalive' },
                    }));
                }
            }, 10000);
            staleSocketTimerRef.current = setInterval(() => {
                if (ws.readyState !== WebSocket.OPEN) {
                    return;
                }
                if (typeof document !== 'undefined' && document.visibilityState !== 'visible') {
                    return;
                }
                const tickNow = Date.now();
                const serverSilentMs = tickNow - lastServerActivityRef.current;
                const heartbeatSilentMs = tickNow - lastTransportHeartbeatRef.current;
                if (serverSilentMs > 30000) {
                    forceReconnect('server_silent');
                    return;
                }
                if (heartbeatSilentMs > 30000 && serverSilentMs > 15000) {
                    forceReconnect('heartbeat_stale');
                }
            }, 5000);
        };

        ws.onmessage = async (event: MessageEvent) => {
            if (wsRef.current !== ws) return;
            lastServerActivityRef.current = Date.now();
            if (event.data instanceof ArrayBuffer) {
                // Binary frame = 24kHz PCM audio from ElevenLabs
                onBinaryMessage(event.data);
            } else if (event.data instanceof Blob) {
                // Async blob parsing prevents iOS OOM crash (Iter 10 #3)
                try {
                    const msg = await new Response(event.data).json();
                    markTransportHeartbeat(msg as ServerPayload);
                    onJsonMessage(msg as ServerPayload);
                } catch { /* malformed — ignore */ }
            } else {
                // String frame — parse synchronously (small control messages only)
                try {
                    const msg = JSON.parse(event.data as string) as ServerPayload;
                    markTransportHeartbeat(msg);
                    // Filter out ping echo
                    if (msg?.payload && (msg.payload as any).ping) return;
                    onJsonMessage(msg);
                } catch { /* malformed — ignore */ }
            }
        };

        ws.onclose = (e) => {
            if (wsRef.current !== ws) return;
            wsRef.current = null;
            clearSocketTimers();
            if (intentionalCloseRef.current) {
                setState('disconnected');
                return;
            }
            if (typeof document !== 'undefined' && document.visibilityState !== 'visible') {
                reconnectReasonRef.current = 'hidden_paused';
                setState('disconnected');
                return;
            }
            // Exponential backoff reconnect (Iter 2 #4)
            const attempt = reconnectAttemptRef.current++;
            const aggressiveRetry = (
                reconnectReasonRef.current === 'heartbeat_stale'
                || reconnectReasonRef.current === 'server_silent'
                || reconnectReasonRef.current === 'connect_timeout'
                || reconnectReasonRef.current === 'visibility_resume_stale'
                || e.code === 4001
                || e.code === 4002
                || e.code === 4006
            );
            const reconnectScheduleMs = aggressiveRetry
                ? [120, 300, 700, 1400, 2600, 4500]
                : [400, 900, 1600, 2800, 4500, 8000];
            const delay = reconnectScheduleMs[Math.min(attempt, reconnectScheduleMs.length - 1)] + Math.random() * 250;
            reconnectReasonRef.current = 'scheduled_retry';
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
        if (!shouldBufferWhenDisconnected(data)) {
            return;
        }
        if (pendingSendsRef.current.length < 50) {
            pendingSendsRef.current.push(data);
        }
    }, [shouldBufferWhenDisconnected]);

    const sendJson = useCallback((msg: Record<string, unknown>) => {
        send(JSON.stringify(msg));
    }, [send]);

    const disconnect = useCallback(() => {
        intentionalCloseRef.current = true;
        if (reconnectTimerRef.current) clearTimeout(reconnectTimerRef.current);
        clearSocketTimers();
        wsRef.current?.close(1000, 'intentional close');
    }, [clearSocketTimers]);

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
            clearSocketTimers();
            wsRef.current?.close();
        };
    }, [clearSocketTimers, connect]);

    useEffect(() => {
        if (typeof window === 'undefined') {
            return;
        }

        const reconnectSoon = (reason: string) => {
            if (intentionalCloseRef.current) {
                return;
            }
            if (typeof document !== 'undefined' && document.visibilityState !== 'visible' && reason !== 'visibility_resume') {
                reconnectReasonRef.current = 'hidden_paused';
                return;
            }
            reconnectReasonRef.current = reason;
            if (reconnectTimerRef.current) {
                clearTimeout(reconnectTimerRef.current);
                reconnectTimerRef.current = null;
            }
            const ws = wsRef.current;
            if (!ws || ws.readyState === WebSocket.CLOSED) {
                reconnectAttemptRef.current = 0;
                connect();
                return;
            }
            if (ws.readyState === WebSocket.OPEN) {
                forceReconnect(reason);
            }
        };

        const handleOnline = () => {
            reconnectSoon('network_online');
        };

        const handleVisibilityChange = () => {
            if (document.visibilityState !== 'visible') {
                reconnectReasonRef.current = 'hidden_paused';
                clearSocketTimers();
                const ws = wsRef.current;
                if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) {
                    try {
                        ws.close(1000, 'hidden');
                    } catch {
                        // Best-effort visibility pause only.
                    }
                }
                return;
            }
            const ws = wsRef.current;
            if (!ws || ws.readyState === WebSocket.CLOSED) {
                reconnectSoon('visibility_resume');
                return;
            }
            if (ws.readyState === WebSocket.OPEN && Date.now() - lastServerActivityRef.current > 15000) {
                reconnectSoon('visibility_resume_stale');
            }
        };

        window.addEventListener('online', handleOnline);
        document.addEventListener('visibilitychange', handleVisibilityChange);
        return () => {
            window.removeEventListener('online', handleOnline);
            document.removeEventListener('visibilitychange', handleVisibilityChange);
        };
    }, [connect, forceReconnect]);

    return { connectionState, send, sendJson, disconnect, sessionId: sessionIdRef.current };
}
