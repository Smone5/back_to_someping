'use client';

export type HomeAssistantTransport = 'browser' | 'backend';

export interface HomeAssistantConfigLike {
    ha_url: string;
    ha_token: string;
    ha_entity: string;
}

export interface HomeAssistantLightCommand {
    hex_color?: string;
    rgb_color?: [number, number, number];
    entity?: string;
    brightness?: number;
    transition?: number;
    client_should_apply?: boolean;
    backend_applied?: boolean;
    backend_error?: string;
    scene_description?: string;
}

export interface TheaterLightingCue extends HomeAssistantLightCommand {
    start_seconds: number;
    end_seconds?: number;
    scene_index?: number;
    scene_number?: number;
    cue_source?: string;
}

export interface HomeAssistantActionResult {
    ok: boolean;
    reason?: string;
    transport: HomeAssistantTransport;
}

export interface HomeAssistantTestResult extends HomeAssistantActionResult {
    entityName?: string;
    restored?: boolean;
}

export function normalizeHomeAssistantUrl(rawUrl: string): string {
    return String(rawUrl ?? '').trim().replace(/\/+$/, '');
}

export function normalizeEntityId(rawEntity: string): string {
    return String(rawEntity ?? '').trim();
}

export function normalizeHomeAssistantConfig(
    config: HomeAssistantConfigLike,
): HomeAssistantConfigLike {
    return {
        ha_url: normalizeHomeAssistantUrl(config.ha_url),
        ha_token: String(config.ha_token ?? '').trim(),
        ha_entity: normalizeEntityId(config.ha_entity || 'light.living_room') || 'light.living_room',
    };
}

export function isLikelyLocalHomeAssistantUrl(rawUrl: string): boolean {
    const normalized = normalizeHomeAssistantUrl(rawUrl);
    if (!normalized) return false;

    try {
        const parsed = new URL(normalized);
        const host = parsed.hostname.toLowerCase();
        if (
            host === 'localhost' ||
            host === '127.0.0.1' ||
            host === '::1' ||
            host.endsWith('.local') ||
            host.endsWith('.lan') ||
            host.endsWith('.home') ||
            host.endsWith('.internal') ||
            !host.includes('.')
        ) {
            return true;
        }

        if (/^10\./.test(host)) return true;
        if (/^192\.168\./.test(host)) return true;
        if (/^172\.(1[6-9]|2\d|3[01])\./.test(host)) return true;

        return false;
    } catch {
        return false;
    }
}

export function isMixedContentRisk(
    rawUrl: string,
    pageProtocol: string = typeof window !== 'undefined' ? window.location.protocol : 'https:',
): boolean {
    const normalized = normalizeHomeAssistantUrl(rawUrl);
    return pageProtocol === 'https:' && normalized.startsWith('http://');
}

function deriveBackendRunAppHost(host: string): string | null {
    const match = host.match(/^storyteller-frontend-(.+\.(?:a\.)?run\.app)$/);
    if (!match) return null;
    return `storyteller-backend-${match[1]}`;
}

function deriveBackendHttpOriginFromConfiguredUrls(): string | null {
    const candidates = [
        process.env.NEXT_PUBLIC_BACKEND_URL ?? '',
        process.env.NEXT_PUBLIC_PAGE_READ_ALOUD_URL ?? '',
        process.env.NEXT_PUBLIC_UPLOAD_URL ?? '',
        process.env.NEXT_PUBLIC_WS_URL ?? '',
    ];
    for (const raw of candidates) {
        const normalized = raw.trim();
        if (!normalized || normalized.startsWith('/')) continue;
        try {
            const parsed = new URL(normalized);
            if (parsed.protocol === 'http:' || parsed.protocol === 'https:') {
                return parsed.origin;
            }
            if (parsed.protocol === 'ws:' || parsed.protocol === 'wss:') {
                return parsed.origin.replace(/^ws/i, 'http');
            }
        } catch {
            // Ignore malformed configured URLs and keep searching.
        }
    }
    return null;
}

function resolveHomeAssistantRelayUrl(path: string): string {
    if (typeof window === 'undefined') {
        return path;
    }

    const protocol = window.location.protocol;
    const host = window.location.host;
    const backendOrigin = deriveBackendHttpOriginFromConfiguredUrls();
    const backendRunAppHost = deriveBackendRunAppHost(host);
    if (backendOrigin) {
        return `${backendOrigin}${path}`;
    }
    if (backendRunAppHost) {
        return `${protocol}//${backendRunAppHost}${path}`;
    }
    return `${protocol}//${host}${path}`;
}

export function getHomeAssistantTransport(
    config: HomeAssistantConfigLike | null,
): HomeAssistantTransport {
    const normalized = config ? normalizeHomeAssistantConfig(config) : null;
    if (!normalized?.ha_url) {
        return 'browser';
    }
    return isLikelyLocalHomeAssistantUrl(normalized.ha_url) ? 'browser' : 'backend';
}

function rgbFromHex(rawHex?: string): [number, number, number] | null {
    const hex = (rawHex || '').trim().replace(/^#/, '');
    if (!/^[0-9a-fA-F]{6}$/.test(hex)) return null;

    return [
        Number.parseInt(hex.slice(0, 2), 16),
        Number.parseInt(hex.slice(2, 4), 16),
        Number.parseInt(hex.slice(4, 6), 16),
    ];
}

type HomeAssistantStateSnapshot = {
    entityId: string;
    friendlyName: string;
    wasOn: boolean;
    brightness?: number;
    rgbColor?: [number, number, number];
};

function mapHomeAssistantHttpReason(status: number): string {
    switch (status) {
        case 400:
            return 'invalid_request';
        case 401:
            return 'unauthorized';
        case 403:
            return 'forbidden';
        case 404:
            return 'entity_not_found';
        default:
            return 'http_error';
    }
}

function validateHomeAssistantConfig(
    config: HomeAssistantConfigLike | null,
): { ok: true; config: HomeAssistantConfigLike; transport: HomeAssistantTransport } | { ok: false; reason: string } {
    if (!config?.ha_url || !config?.ha_token) {
        return { ok: false, reason: 'not_configured' };
    }

    const normalized = normalizeHomeAssistantConfig(config);
    if (!normalized.ha_url) {
        return { ok: false, reason: 'missing_url' };
    }
    if (!normalized.ha_token) {
        return { ok: false, reason: 'missing_token' };
    }
    if (!normalized.ha_entity) {
        return { ok: false, reason: 'missing_entity' };
    }
    let parsed: URL;
    try {
        parsed = new URL(normalized.ha_url);
    } catch {
        return { ok: false, reason: 'invalid_url' };
    }

    const transport = getHomeAssistantTransport(normalized);
    if (transport === 'browser' && isMixedContentRisk(normalized.ha_url)) {
        return { ok: false, reason: 'mixed_content' };
    }
    if (transport === 'backend' && parsed.protocol !== 'https:') {
        return { ok: false, reason: 'insecure_url' };
    }
    return { ok: true, config: normalized, transport };
}

async function homeAssistantFetch(
    config: HomeAssistantConfigLike,
    path: string,
    init?: RequestInit,
): Promise<Response> {
    return fetch(`${config.ha_url}${path}`, {
        ...init,
        headers: {
            Authorization: `Bearer ${config.ha_token}`,
            'Content-Type': 'application/json',
            ...(init?.headers || {}),
        },
    });
}

async function homeAssistantRelayFetch(
    path: string,
    body: unknown,
): Promise<Response> {
    return fetch(resolveHomeAssistantRelayUrl(path), {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
        },
        body: JSON.stringify(body),
    });
}

async function readRelayPayload(response: Response): Promise<Record<string, unknown> | null> {
    try {
        const payload = await response.json();
        return payload && typeof payload === 'object' ? payload as Record<string, unknown> : null;
    } catch {
        return null;
    }
}

function relayReasonFromResponse(
    response: Response,
    payload: Record<string, unknown> | null,
): string {
    const payloadReason = typeof payload?.reason === 'string'
        ? payload.reason
        : typeof payload?.error === 'string'
            ? payload.error
            : '';
    if (payloadReason) {
        return payloadReason;
    }
    if (response.status === 403) {
        return 'origin_not_allowed';
    }
    if (response.status === 400) {
        return 'invalid_request';
    }
    return 'http_error';
}

function normalizeRgbColor(value: unknown): [number, number, number] | undefined {
    if (!Array.isArray(value) || value.length !== 3) {
        return undefined;
    }
    const rgb = value.map((item) => Number(item));
    if (rgb.some((item) => !Number.isFinite(item))) {
        return undefined;
    }
    return [rgb[0], rgb[1], rgb[2]];
}

async function loadHomeAssistantState(
    config: HomeAssistantConfigLike,
): Promise<HomeAssistantStateSnapshot | { reason: string }> {
    try {
        const entityId = normalizeEntityId(config.ha_entity || 'light.living_room');
        const response = await homeAssistantFetch(
            config,
            `/api/states/${encodeURIComponent(entityId)}`,
            { method: 'GET' },
        );
        if (!response.ok) {
            return { reason: mapHomeAssistantHttpReason(response.status) };
        }
        const payload = await response.json().catch(() => null) as
            | {
                state?: unknown;
                attributes?: {
                    friendly_name?: unknown;
                    brightness?: unknown;
                    rgb_color?: unknown;
                };
            }
            | null;
        if (!payload || typeof payload !== 'object') {
            return { reason: 'invalid_response' };
        }
        return {
            entityId,
            friendlyName: typeof payload.attributes?.friendly_name === 'string'
                ? payload.attributes.friendly_name
                : entityId,
            wasOn: String(payload.state || '').toLowerCase() === 'on',
            brightness: Number.isFinite(Number(payload.attributes?.brightness))
                ? Number(payload.attributes?.brightness)
                : undefined,
            rgbColor: normalizeRgbColor(payload.attributes?.rgb_color),
        };
    } catch {
        return { reason: 'network' };
    }
}

async function restoreHomeAssistantState(
    config: HomeAssistantConfigLike,
    snapshot: HomeAssistantStateSnapshot,
): Promise<boolean> {
    try {
        if (!snapshot.wasOn) {
            const response = await homeAssistantFetch(config, '/api/services/light/turn_off', {
                method: 'POST',
                body: JSON.stringify({
                    entity_id: snapshot.entityId,
                    transition: 0.6,
                }),
            });
            return response.ok;
        }

        const payload: {
            entity_id: string;
            transition: number;
            brightness?: number;
            rgb_color?: [number, number, number];
        } = {
            entity_id: snapshot.entityId,
            transition: 0.6,
        };
        if (typeof snapshot.brightness === 'number' && Number.isFinite(snapshot.brightness)) {
            payload.brightness = snapshot.brightness;
        }
        if (snapshot.rgbColor) {
            payload.rgb_color = snapshot.rgbColor;
        }

        const response = await homeAssistantFetch(config, '/api/services/light/turn_on', {
            method: 'POST',
            body: JSON.stringify(payload),
        });
        return response.ok;
    } catch {
        return false;
    }
}

export function getLightingCommandKey(command: HomeAssistantLightCommand): string {
    const rgb = command.rgb_color?.join(',') || '';
    const entity = normalizeEntityId(command.entity || '');
    const hex = (command.hex_color || '').trim().toLowerCase();
    return `${entity}|${hex}|${rgb}|${command.transition ?? ''}|${command.brightness ?? ''}`;
}

export function normalizeTheaterLightingCues(raw: unknown): TheaterLightingCue[] {
    if (!Array.isArray(raw)) {
        return [];
    }

    const cues: TheaterLightingCue[] = [];
    for (const item of raw) {
        if (!item || typeof item !== 'object') {
            continue;
        }
        const candidate = item as Record<string, unknown>;
        const startSeconds = Number(candidate.start_seconds ?? 0);
        if (!Number.isFinite(startSeconds) || startSeconds < 0) {
            continue;
        }
        const endSecondsRaw = Number(candidate.end_seconds ?? NaN);
        const endSeconds = Number.isFinite(endSecondsRaw) && endSecondsRaw > startSeconds
            ? endSecondsRaw
            : undefined;
        const rgbColor = normalizeRgbColor(candidate.rgb_color);
        const brightness = Number(candidate.brightness ?? NaN);
        const transition = Number(candidate.transition ?? NaN);
        const sceneIndex = Number(candidate.scene_index ?? NaN);
        const sceneNumber = Number(candidate.scene_number ?? NaN);
        cues.push({
            start_seconds: startSeconds,
            end_seconds: endSeconds,
            scene_index: Number.isFinite(sceneIndex) ? sceneIndex : undefined,
            scene_number: Number.isFinite(sceneNumber) ? sceneNumber : undefined,
            cue_source: typeof candidate.cue_source === 'string' ? candidate.cue_source.trim() : undefined,
            hex_color: typeof candidate.hex_color === 'string' ? candidate.hex_color.trim() : undefined,
            rgb_color: rgbColor,
            entity: typeof candidate.entity === 'string' ? candidate.entity.trim() : undefined,
            brightness: Number.isFinite(brightness) ? brightness : undefined,
            transition: Number.isFinite(transition) ? transition : undefined,
            scene_description: typeof candidate.scene_description === 'string'
                ? candidate.scene_description.trim()
                : undefined,
        });
    }

    cues.sort((left, right) => left.start_seconds - right.start_seconds);
    return cues;
}

export function describeHomeAssistantFailure(reason?: string): string {
    switch (reason) {
        case 'not_configured':
            return 'Add the Home Assistant URL, token, and light entity first.';
        case 'missing_url':
            return 'Add the Home Assistant URL first.';
        case 'missing_token':
            return 'Add a Home Assistant long-lived access token first.';
        case 'missing_entity':
            return 'Add the light entity ID first.';
        case 'invalid_url':
            return 'Use a full Home Assistant URL, including http:// or https://.';
        case 'insecure_url':
            return 'Public Home Assistant URLs must use HTTPS.';
        case 'private_url':
            return 'Local Home Assistant URLs can only be used from a browser on the same network.';
        case 'mixed_content':
            return 'This secure page cannot call an HTTP Home Assistant URL. Use HTTPS for Home Assistant.';
        case 'invalid_color':
            return 'The requested light color was invalid.';
        case 'entity_not_found':
            return 'Home Assistant could not find that light entity.';
        case 'unauthorized':
            return 'Home Assistant rejected the token.';
        case 'forbidden':
            return 'Home Assistant denied access to that light.';
        case 'invalid_request':
            return 'Home Assistant rejected that light command.';
        case 'invalid_response':
            return 'Home Assistant sent back an unexpected response.';
        case 'network':
            return 'This browser could not reach Home Assistant from this device.';
        case 'origin_not_allowed':
            return 'This Voxitale site is not allowed to use the Home Assistant relay.';
        case 'restore_failed':
            return 'The light test worked, but restoring the previous light state failed.';
        case 'entity_unavailable':
            return 'That light is unavailable right now.';
        default:
            return 'Home Assistant did not accept that request.';
    }
}

export function describeHomeAssistantTestFailure(
    config: HomeAssistantConfigLike | null,
    transport: HomeAssistantTransport,
    reason?: string,
    pageOrigin: string = typeof window !== 'undefined' ? window.location.origin : '',
): string {
    if (transport === 'backend') {
        if (reason === 'network') {
            return 'Voxitale could not reach Home Assistant from the backend. Check that the URL is public, reachable from the internet, and still accepts this token.';
        }
        return describeHomeAssistantFailure(reason);
    }

    if (reason !== 'network') {
        return describeHomeAssistantFailure(reason);
    }

    const normalized = config ? normalizeHomeAssistantConfig(config) : null;
    if (!normalized?.ha_url) {
        return describeHomeAssistantFailure(reason);
    }

    const originHint = pageOrigin ? ` ${pageOrigin}` : ' this site';
    if (isLikelyLocalHomeAssistantUrl(normalized.ha_url)) {
        return `This browser could not call Home Assistant directly. Check that Home Assistant is reachable on this network and that${originHint} is listed in http.cors_allowed_origins.`;
    }

    return `This browser could not call Home Assistant directly. Home Assistant is likely blocking${originHint} with CORS. Add that origin to http.cors_allowed_origins, or save and verify through the story/backend path instead.`;
}

export async function applyHomeAssistantLighting(
    config: HomeAssistantConfigLike | null,
    command: HomeAssistantLightCommand,
): Promise<HomeAssistantActionResult> {
    const validated = validateHomeAssistantConfig(config);
    if ('reason' in validated) {
        return { ok: false, reason: validated.reason, transport: getHomeAssistantTransport(config) };
    }

    if (validated.transport === 'backend') {
        try {
            const response = await homeAssistantRelayFetch('/api/home-assistant/apply-lighting', {
                config: validated.config,
                command,
            });
            const payload = await readRelayPayload(response);
            if (!response.ok || payload?.ok !== true) {
                return {
                    ok: false,
                    reason: relayReasonFromResponse(response, payload),
                    transport: 'backend',
                };
            }
            return { ok: true, transport: 'backend' };
        } catch {
            return { ok: false, reason: 'network', transport: 'backend' };
        }
    }

    const rgbColor = command.rgb_color ?? rgbFromHex(command.hex_color);
    if (!rgbColor) {
        return { ok: false, reason: 'invalid_color', transport: 'browser' };
    }

    const payload = {
        entity_id: normalizeEntityId(command.entity || validated.config.ha_entity || 'light.living_room'),
        rgb_color: rgbColor,
        brightness: Number.isFinite(command.brightness) ? command.brightness : 200,
        transition: Number.isFinite(command.transition) ? command.transition : 2,
    };

    try {
        const response = await homeAssistantFetch(validated.config, '/api/services/light/turn_on', {
            method: 'POST',
            body: JSON.stringify(payload),
        });
        if (!response.ok) {
            return { ok: false, reason: mapHomeAssistantHttpReason(response.status), transport: 'browser' };
        }
        return { ok: true, transport: 'browser' };
    } catch {
        return { ok: false, reason: 'network', transport: 'browser' };
    }
}

export async function smokeTestHomeAssistantLighting(
    config: HomeAssistantConfigLike | null,
): Promise<HomeAssistantTestResult> {
    const validated = validateHomeAssistantConfig(config);
    if ('reason' in validated) {
        return {
            ok: false,
            reason: validated.reason,
            transport: getHomeAssistantTransport(config),
        };
    }

    if (validated.transport === 'backend') {
        try {
            const response = await homeAssistantRelayFetch('/api/home-assistant/test-light', {
                config: validated.config,
            });
            const payload = await readRelayPayload(response);
            if (!response.ok || payload?.ok !== true) {
                return {
                    ok: false,
                    reason: relayReasonFromResponse(response, payload),
                    entityName: typeof payload?.entityName === 'string' ? payload.entityName : undefined,
                    restored: payload?.restored === true,
                    transport: 'backend',
                };
            }
            return {
                ok: true,
                entityName: typeof payload?.entityName === 'string' ? payload.entityName : undefined,
                restored: payload?.restored === true,
                transport: 'backend',
            };
        } catch {
            return { ok: false, reason: 'network', transport: 'backend' };
        }
    }

    const snapshot = await loadHomeAssistantState(validated.config);
    if ('reason' in snapshot) {
        return { ok: false, reason: snapshot.reason, transport: 'browser' };
    }
    if (!snapshot.entityId) {
        return { ok: false, reason: 'entity_not_found', transport: 'browser' };
    }
    if (snapshot.wasOn === false && snapshot.brightness === undefined && snapshot.rgbColor === undefined) {
        // This still can be a valid off light. Proceed.
    }

    const testResult = await applyHomeAssistantLighting(validated.config, {
        entity: snapshot.entityId,
        rgb_color: [124, 92, 255],
        brightness: 190,
        transition: 0.6,
    });
    if (!testResult.ok) {
        return {
            ok: false,
            reason: testResult.reason,
            entityName: snapshot.friendlyName,
            transport: 'browser',
        };
    }

    await new Promise((resolve) => setTimeout(resolve, 900));
    const restored = await restoreHomeAssistantState(validated.config, snapshot);
    return {
        ok: restored,
        reason: restored ? undefined : 'restore_failed',
        entityName: snapshot.friendlyName,
        restored,
        transport: 'browser',
    };
}
