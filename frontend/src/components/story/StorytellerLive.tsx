'use client';

/**
 * StorytellerLive — The main Bidi-streaming storytelling experience.
 *
 * This is the root component that orchestrates:
 * 1. ParentGate (COPPA approval)
 * 2. useWebSocket (multiplexed WS connection)
 * 3. useAudioWorklet (16kHz mic -> WS, 24kHz playback, wake lock)
 * 4. useSfxDucker (sequential Foley SFX queue)
 * 5. MagicMirror (WebGL visualizer)
 * 6. Scene Video display (Veo 3.1 clips)
 * 7. TheaterMode (final movie playback)
 * 8. Rewind wand button (with disable-state debounce)
 * 9. Optional camera capture (physical object integration)
 *
 * Interaction is fully voice-controlled: mic streams to Gemini Live and ADK
 * handles turn-taking via automatic voice activity detection (no tap required).
 */

import dynamic from 'next/dynamic';
import { useCallback, useEffect, useRef, useState } from 'react';

import { useAudioWorklet, type BufferedClipProgress } from '@/hooks/useAudioWorklet';
import { useBackgroundMusic } from '@/hooks/useBackgroundMusic';
import { useStoryVoicePreview } from '@/hooks/useStoryVoicePreview';
import { useUiSounds } from '@/hooks/useUiSounds';
import { useWebSocket } from '@/hooks/useWebSocket';
import { useSfxDucker } from '@/hooks/useSfxDucker';
import type { MagicMirrorMode, MagicMirrorProps } from './MagicMirror';
import {
    applyHomeAssistantLighting,
    getLightingCommandKey,
    HomeAssistantLightCommand,
    normalizeTheaterLightingCues,
    TheaterLightingCue,
} from './homeAssistant';
import ParentGate, { type StoryTone, type StorybookMoviePacing } from './ParentGate';
import type { IoTConfig } from './IoTSettingsModal';
import {
    DEFAULT_STORY_READER_VOICE_ID,
    STORY_READER_VOICE_OPTIONS,
    getStoryReaderVoiceOption,
    normalizeStoryReaderVoiceId,
} from './storyVoiceOptions';

function MagicMirrorFallback({ mode }: MagicMirrorProps) {
    return <div className={`magic-mirror-fallback mirror-${mode}`} aria-hidden="true" />;
}

// Lazy-load MagicMirror (heavy WebGL) — don't block initial render.
// On ChunkLoadError (e.g. stale cache after rebuild), render a lightweight fallback
// so the center orb still feels alive instead of collapsing to an empty ring.
const MagicMirror = dynamic<MagicMirrorProps>(
    () =>
        import('./MagicMirror').catch(() => ({
            default: MagicMirrorFallback,
        })),
    { ssr: false, loading: () => null }
);

const TheaterMode = dynamic(
    () => import('./TheaterMode'),
    { ssr: false, loading: () => null }
);

type AppPhase = 'gate' | 'mic-check' | 'story' | 'theater';
type MicPermissionState = 'unknown' | 'prompt' | 'granted' | 'denied';
type StoryFlowPhase =
    | 'opening'
    | 'chatting'
    | 'drawing_scene'
    | 'waiting_for_child'
    | 'ending_story'
    | 'assembling_movie'
    | 'theater'
    | 'remake';
type StorybookStatus = {
    message: string;
    etaSeconds?: number;
    storyTitle?: string;
    childName?: string;
    kind?: 'initial' | 'remake';
    startedAtMs: number;
};
type ToyShareOverlayOptions = {
    notifyBackend?: boolean;
    autoStartCamera?: boolean;
};
type EndingStoryOptions = {
    notifyBackend?: boolean;
    message?: string;
    etaSeconds?: number;
    kind?: 'initial' | 'remake';
    startedAtMs?: number;
};
type SceneBranchPoint = {
    scene_number: number;
    label?: string | null;
    scene_description?: string | null;
    storybeat_text?: string | null;
    image_url?: string | null;
    is_current?: boolean;
    is_selected?: boolean;
};
type AssemblyMissionOption = {
    key: string;
    label: string;
};
type AssemblyMission = {
    kicker: string;
    title: string;
    helper: string;
    options: [AssemblyMissionOption, AssemblyMissionOption];
};
type TheaterLightingStage = 'open' | 'play' | 'pause' | 'end' | 'close';
type SceneBranchPickerOptions = {
    selectedSceneNumber?: number | null;
    warning?: string | null;
};
type CommitSceneImageOptions = {
    thumbnailB64?: string | null;
    requestId?: string | null;
    onCommitted?: () => void;
};
const MIC_OK_KEY = 'storyteller_mic_setup_v2';
const MIC_DEVICE_KEY = 'storyteller_mic_device_id_v1';
const STORYBOOK_ASSEMBLY_STORAGE_PREFIX = 'storyteller_assembly_ui:';
const STORYBOOK_ASSEMBLY_STORAGE_TTL_MS = 15 * 60 * 1000;
const STORYBOOK_ASSEMBLY_REHYDRATION_GRACE_MS = STORYBOOK_ASSEMBLY_STORAGE_TTL_MS;
const SCENE_PLACEHOLDER_TIMEOUT_MS = 45 * 1000;
const ENABLE_SCENE_BRANCH_UI = false;

type PersistedStorybookAssemblyState = {
    savedAtMs: number;
    status: StorybookStatus;
    storyTitle: string | null;
    childName: string | null;
    storyPhase: StoryFlowPhase;
};

function storybookAssemblyStorageKey(sessionId: string): string {
    return `${STORYBOOK_ASSEMBLY_STORAGE_PREFIX}${sessionId}`;
}

function persistStorybookAssemblyState(
    sessionId: string,
    status: StorybookStatus,
    storyTitle: string | null,
    childName: string | null,
    storyPhase: StoryFlowPhase,
): void {
    if (typeof window === 'undefined' || !sessionId) {
        return;
    }
    const payload: PersistedStorybookAssemblyState = {
        savedAtMs: Date.now(),
        status: {
            message: status.message,
            etaSeconds: typeof status.etaSeconds === 'number' && Number.isFinite(status.etaSeconds)
                ? status.etaSeconds
                : undefined,
            storyTitle: status.storyTitle,
            childName: status.childName,
            kind: status.kind === 'remake' ? 'remake' : 'initial',
            startedAtMs: Number.isFinite(status.startedAtMs) && status.startedAtMs > 0
                ? status.startedAtMs
                : Date.now(),
        },
        storyTitle: storyTitle ?? null,
        childName: childName ?? null,
        storyPhase,
    };
    try {
        window.sessionStorage.setItem(storybookAssemblyStorageKey(sessionId), JSON.stringify(payload));
    } catch {
        // Best-effort client persistence only.
    }
}

function loadPersistedStorybookAssemblyState(sessionId: string): PersistedStorybookAssemblyState | null {
    if (typeof window === 'undefined' || !sessionId) {
        return null;
    }
    try {
        const raw = window.sessionStorage.getItem(storybookAssemblyStorageKey(sessionId));
        if (!raw) {
            return null;
        }
        const parsed = JSON.parse(raw) as Partial<PersistedStorybookAssemblyState> | null;
        const savedAtMs = Number(parsed?.savedAtMs ?? 0);
        if (!Number.isFinite(savedAtMs) || savedAtMs <= 0 || (Date.now() - savedAtMs) > STORYBOOK_ASSEMBLY_STORAGE_TTL_MS) {
            window.sessionStorage.removeItem(storybookAssemblyStorageKey(sessionId));
            return null;
        }
        const rawStatus = parsed?.status;
        if (!rawStatus || typeof rawStatus !== 'object' || typeof rawStatus.message !== 'string') {
            window.sessionStorage.removeItem(storybookAssemblyStorageKey(sessionId));
            return null;
        }
        return {
            savedAtMs,
            status: {
                message: rawStatus.message,
                etaSeconds: typeof rawStatus.etaSeconds === 'number' && Number.isFinite(rawStatus.etaSeconds)
                    ? rawStatus.etaSeconds
                    : undefined,
                storyTitle: typeof rawStatus.storyTitle === 'string' ? rawStatus.storyTitle : undefined,
                childName: typeof rawStatus.childName === 'string' ? rawStatus.childName : undefined,
                kind: rawStatus.kind === 'remake' ? 'remake' : 'initial',
                startedAtMs: typeof rawStatus.startedAtMs === 'number' && Number.isFinite(rawStatus.startedAtMs) && rawStatus.startedAtMs > 0
                    ? rawStatus.startedAtMs
                    : savedAtMs,
            },
            storyTitle: typeof parsed?.storyTitle === 'string' ? parsed.storyTitle : null,
            childName: typeof parsed?.childName === 'string' ? parsed.childName : null,
            storyPhase: normalizeStoryFlowPhase(parsed?.storyPhase),
        };
    } catch {
        try {
            window.sessionStorage.removeItem(storybookAssemblyStorageKey(sessionId));
        } catch {
            // Ignore storage cleanup failures.
        }
        return null;
    }
}

function clearPersistedStorybookAssemblyState(sessionId: string | null | undefined): void {
    if (typeof window === 'undefined' || !sessionId) {
        return;
    }
    try {
        window.sessionStorage.removeItem(storybookAssemblyStorageKey(sessionId));
    } catch {
        // Ignore storage cleanup failures.
    }
}

function normalizeStoryFlowPhase(raw: unknown): StoryFlowPhase {
    const candidate = typeof raw === 'string' ? raw.trim().toLowerCase() : '';
    switch (candidate) {
        case 'chatting':
        case 'drawing_scene':
        case 'waiting_for_child':
        case 'ending_story':
        case 'assembling_movie':
        case 'theater':
        case 'remake':
            return candidate;
        case 'opening':
        default:
            return 'opening';
    }
}

function isAssemblyStoryPhase(phase: StoryFlowPhase): boolean {
    return phase === 'assembling_movie' || phase === 'remake';
}

function isToyShareAllowedForState(
    phase: AppPhase,
    storyPhase: StoryFlowPhase,
    isEndingStory: boolean,
    storybookStatus: StorybookStatus | null,
): boolean {
    return (
        phase === 'story'
        && !isEndingStory
        && !storybookStatus
        && storyPhase !== 'ending_story'
        && !isAssemblyStoryPhase(storyPhase)
    );
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

function resolveFrontendWebSocketUrl(): string {
    const configured = process.env.NEXT_PUBLIC_WS_URL ?? '';
    if (typeof window === 'undefined') {
        return configured || '/ws/story';
    }

    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const host = window.location.host;
    const backendRunAppHost = deriveBackendRunAppHost(host);
    const normalized = configured.trim();
    const isBadDefault =
        !normalized ||
        normalized.includes('localhost:8000') ||
        normalized.includes('storyteller.example.com');

    if (isBadDefault) {
        if (backendRunAppHost) {
            return `${protocol}//${backendRunAppHost}/ws/story`;
        }
        return `${protocol}//${host}/ws/story`;
    }

    if (normalized.startsWith('/')) {
        if (backendRunAppHost && normalized.startsWith('/ws/')) {
            return `${protocol}//${backendRunAppHost}${normalized}`;
        }
        return `${protocol}//${host}${normalized}`;
    }

    if (normalized.startsWith('http://') || normalized.startsWith('https://')) {
        return normalized.replace(/^http/i, 'ws');
    }

    return normalized;
}

function resolveUploadUrl(): string {
    const configured = process.env.NEXT_PUBLIC_UPLOAD_URL ?? '';
    if (typeof window === 'undefined') {
        return configured || '/api/upload';
    }
    const protocol = window.location.protocol;
    const host = window.location.host;
    const backendRunAppHost = deriveBackendRunAppHost(host);
    const normalized = configured.trim();
    if (!normalized) {
        if (backendRunAppHost) return `${protocol}//${backendRunAppHost}/api/upload`;
        return '/api/upload';
    }
    if (normalized.includes('localhost:8000') || normalized.includes('storyteller.example.com')) {
        if (backendRunAppHost) return `${protocol}//${backendRunAppHost}/api/upload`;
        return '/api/upload';
    }
    if (normalized.startsWith('/')) {
        if (backendRunAppHost && normalized.startsWith('/api/')) {
            return `${protocol}//${backendRunAppHost}${normalized}`;
        }
        return `${protocol}//${host}${normalized}`;
    }
    return normalized;
}

function resolvePageReadAloudUrl(): string {
    const configured = process.env.NEXT_PUBLIC_PAGE_READ_ALOUD_URL ?? '';
    if (typeof window === 'undefined') {
        return configured || '/api/page-read-aloud';
    }
    const protocol = window.location.protocol;
    const host = window.location.host;
    const backendOrigin = deriveBackendHttpOriginFromConfiguredUrls();
    const backendRunAppHost = deriveBackendRunAppHost(host);
    const normalized = configured.trim();
    if (!normalized) {
        if (backendOrigin) return `${backendOrigin}/api/page-read-aloud`;
        if (backendRunAppHost) return `${protocol}//${backendRunAppHost}/api/page-read-aloud`;
        return `${protocol}//${host}/api/page-read-aloud`;
    }
    if (normalized.includes('localhost:8000') || normalized.includes('storyteller.example.com')) {
        if (backendOrigin) return `${backendOrigin}/api/page-read-aloud`;
        if (backendRunAppHost) return `${protocol}//${backendRunAppHost}/api/page-read-aloud`;
        return `${protocol}//${host}/api/page-read-aloud`;
    }
    if (normalized.startsWith('/')) {
        if (backendOrigin && normalized.startsWith('/api/')) {
            return `${backendOrigin}${normalized}`;
        }
        if (backendRunAppHost && normalized.startsWith('/api/')) {
            return `${protocol}//${backendRunAppHost}${normalized}`;
        }
        return `${protocol}//${host}${normalized}`;
    }
    return normalized;
}

type StoryCaptionToken = {
    text: string;
    isWord: boolean;
    wordIndex: number;
};
type PageReadAloudPayload = {
    audioBytes: ArrayBuffer;
    wordStartsMs: number[];
};

const STORY_CAPTION_DECORATIVE_RE = /[✨🌟💫🎵🎶🪄🔊⏹️▶️]/g;
const STORY_CAPTION_CHOICE_RE = /(?:\s+)?(?:what should we do(?: next)?|should we|do you want to|or maybe)\b.*$/i;
const STORY_CAPTION_META_PREFIX_RE = /^(?:(?:sure|okay|ok|great|wonderful)[!,. ]+\s*)?here(?:'s| is)\s+(?:(?:a|an|the)\s+)?(?:(?:[\w']+(?:-[\w']+)?\s+){0,6})?(?:illustration|image|picture|page|caption)\s*(?::|-)\s*/i;
const STORY_CAPTION_HTML_TAG_RE = /<[^>]+>/g;
const STORY_CAPTION_ENCODED_HTML_TAG_RE = /&lt;[^&]+&gt;/gi;

function normalizeStoryCaptionText(raw: string | null | undefined): string {
    const normalized = `${raw ?? ''}`
        .normalize('NFKC')
        .replace(/\u2018|\u2019/g, "'")
        .replace(/\u201c|\u201d/g, '"')
        .replace(/\u2013|\u2014/g, '-')
        .replace(/\u2026/g, '...')
        .replace(/<ctrl\d+>/gi, ' ')
        .replace(STORY_CAPTION_HTML_TAG_RE, ' ')
        .replace(STORY_CAPTION_ENCODED_HTML_TAG_RE, ' ')
        .replace(/^(caption|storybeat|scene)\s*:\s*/i, '')
        .replace(STORY_CAPTION_DECORATIVE_RE, ' ')
        .replace(STORY_CAPTION_CHOICE_RE, '')
        .replace(STORY_CAPTION_META_PREFIX_RE, '')
        .replace(/\s+/g, ' ')
        .trim()
        .replace(/^["']+|["']+$/g, '');
    if (!normalized) {
        return '';
    }
    const firstSentence = normalized.match(/.*?[.!?](?=\s|$)/)?.[0]?.trim() ?? normalized;
    const cleanedSentence = firstSentence.replace(/\s+/g, ' ').trim().replace(/[,:;\- ]+$/, '');
    if (!cleanedSentence) {
        return '';
    }
    return /[.!?]$/.test(cleanedSentence) ? cleanedSentence : `${cleanedSentence}.`;
}

function tokenizeStoryCaption(text: string): StoryCaptionToken[] {
    const parts = text.match(/\S+|\s+/g) ?? [text];
    let wordIndex = 0;
    return parts.map((part) => {
        const isWord = /\S/.test(part);
        const token: StoryCaptionToken = {
            text: part,
            isWord,
            wordIndex: isWord ? wordIndex : -1,
        };
        if (isWord) {
            wordIndex += 1;
        }
        return token;
    });
}

function estimateStoryCaptionWordWeight(rawWord: string): number {
    const bareWord = rawWord.replace(/^[^A-Za-z0-9']+|[^A-Za-z0-9'!?.,;:]+$/g, '');
    const lowered = bareWord.toLowerCase();
    const vowelGroups = lowered.match(/[aeiouy]+/g) ?? [];
    let syllables = vowelGroups.length || 1;
    if (syllables > 1 && /(?:e|es|ed)$/.test(lowered) && !/(?:le|ue|ee)$/.test(lowered)) {
        syllables -= 1;
    }

    let weight = 0.92 + syllables * 0.23 + Math.min(Math.max(bareWord.length, 1), 12) * 0.014;
    if (/[,:;]/.test(rawWord)) {
        weight += 0.18;
    }
    if (/[-–—]/.test(rawWord)) {
        weight += 0.12;
    }
    if (/[.!?]/.test(rawWord)) {
        weight += 0.3;
    }
    return weight;
}

function buildStoryCaptionWordBoundaries(tokens: StoryCaptionToken[]): number[] {
    const wordTokens = tokens.filter((token) => token.isWord);
    if (!wordTokens.length) {
        return [];
    }
    let cumulative = 0;
    const boundaries: number[] = [];
    for (const token of wordTokens) {
        const weight = estimateStoryCaptionWordWeight(token.text.trim());
        cumulative += weight;
        boundaries.push(cumulative);
    }
    return boundaries.map((value) => value / cumulative);
}

function highlightWordIndexForProgress(progress: number, boundaries: number[]): number {
    if (!boundaries.length) {
        return -1;
    }
    const adjustedProgress = Math.max(0, Math.min(1, progress));
    for (let index = 0; index < boundaries.length; index += 1) {
        if (adjustedProgress < boundaries[index]) {
            return index;
        }
    }
    return boundaries.length - 1;
}

function parsePageReadAloudWordStartsMs(headerValue: string | null): number[] {
    if (!headerValue) {
        return [];
    }
    return headerValue
        .split(',')
        .map((part) => Number(part.trim()))
        .filter((value): value is number => Number.isFinite(value) && value >= 0)
        .map((value) => Math.round(value));
}

function normalizePageReadAloudWordStartsMs(wordStartsMs: number[], totalWordCount: number): number[] {
    if (!Array.isArray(wordStartsMs) || wordStartsMs.length !== totalWordCount || totalWordCount <= 0) {
        return [];
    }
    const normalized: number[] = [];
    let lastValue = 0;
    for (const value of wordStartsMs) {
        const safeValue = Math.max(lastValue, Math.round(value));
        normalized.push(safeValue);
        lastValue = safeValue;
    }
    return normalized;
}

function highlightWordIndexForElapsedMs(elapsedSpeechMs: number, wordStartsMs: number[]): number {
    if (!wordStartsMs.length) {
        return -1;
    }
    let activeIndex = 0;
    for (let index = 0; index < wordStartsMs.length; index += 1) {
        if (elapsedSpeechMs >= wordStartsMs[index]) {
            activeIndex = index;
        } else {
            break;
        }
    }
    return activeIndex;
}

function mergeStreamingTranscript(previous: string | null, incomingRaw: string): string {
    const incoming = incomingRaw.trim();
    if (!incoming) return previous?.trim() ?? '';

    const prev = previous?.trim() ?? '';
    if (!prev) return incoming;

    // Cumulative update.
    if (incoming.startsWith(prev)) return incoming;
    // Older/shorter update arriving late.
    if (prev.startsWith(incoming) || prev.endsWith(incoming)) return prev;

    // Long duplicate block already present.
    if (incoming.length >= 12 && prev.includes(incoming)) return prev;

    // Try suffix/prefix overlap for delta-style chunks.
    const maxOverlap = Math.min(prev.length, incoming.length);
    let overlap = 0;
    for (let i = maxOverlap; i > 0; i -= 1) {
        if (prev.slice(-i).toLowerCase() === incoming.slice(0, i).toLowerCase()) {
            overlap = i;
            break;
        }
    }

    const merged = overlap > 0
        ? `${prev}${incoming.slice(overlap)}`
        : /[([{'"-]$/.test(prev) || /^[,.;:!?)}\]'"]/.test(incoming)
            ? `${prev}${incoming}`
            : `${prev} ${incoming}`;

    // Collapse accidental exact doubled block: "Sentence.Sentence."
    if (merged.length % 2 === 0) {
        const half = merged.slice(0, merged.length / 2);
        if (half && `${half}${half}` === merged) {
            return half.trim();
        }
    }
    return merged.trim();
}

function normalizeSceneHistory(raw: unknown): SceneBranchPoint[] {
    if (!Array.isArray(raw)) return [];
    const normalized: SceneBranchPoint[] = [];
    for (const item of raw) {
        if (!item || typeof item !== 'object') continue;
        const record = item as Record<string, unknown>;
        const sceneNumber = Number(record.scene_number ?? 0);
        if (!Number.isFinite(sceneNumber) || sceneNumber <= 0) continue;
        normalized.push({
            scene_number: sceneNumber,
            label: typeof record.label === 'string' ? record.label : null,
            scene_description: typeof record.scene_description === 'string' ? record.scene_description : null,
            storybeat_text: typeof record.storybeat_text === 'string' ? record.storybeat_text : null,
            image_url: typeof record.image_url === 'string' ? record.image_url : null,
            is_current: Boolean(record.is_current),
            is_selected: Boolean(record.is_selected),
        });
    }
    return normalized.sort((a, b) => a.scene_number - b.scene_number);
}

const ASSEMBLY_MISSION_ROTATION_SECONDS = 18;
const ASSEMBLY_MISSIONS: AssemblyMission[] = [
    {
        kicker: 'Memory Game',
        title: 'Pick one tiny story memory.',
        helper: 'One short choice is easier for preschoolers than a big open question.',
        options: [
            { key: 'favorite_part', label: 'Best part' },
            { key: 'favorite_sound', label: 'Funny sound' },
        ],
    },
    {
        kicker: 'Cozy Break',
        title: 'Choose a calm little sound game.',
        helper: 'Quiet, voice-first games work better while Amelia cannot see the room.',
        options: [
            { key: 'soft_echo', label: 'Soft echo' },
            { key: 'cozy_breath', label: 'Star breaths' },
        ],
    },
    {
        kicker: 'Giggle Break',
        title: 'Choose a quick silly turn.',
        helper: 'Tiny jokes and soft counting make waiting feel shorter.',
        options: [
            { key: 'tiny_joke', label: 'Tiny joke' },
            { key: 'counting_game', label: 'Count softly' },
        ],
    },
];
const ASSEMBLY_RECENT_ACTIVITY_LIMIT = 6;

function rememberAssemblyActivity(current: string[], key: string): string[] {
    const normalized = key.trim().toLowerCase();
    if (!normalized) return current;
    const next = current.filter((item) => item !== normalized);
    next.push(normalized);
    return next.slice(-ASSEMBLY_RECENT_ACTIVITY_LIMIT);
}

function pickAssemblyMission(elapsedSeconds: number, recentKeys: string[]): AssemblyMission {
    const startIndex = Math.floor(elapsedSeconds / ASSEMBLY_MISSION_ROTATION_SECONDS) % ASSEMBLY_MISSIONS.length;
    for (let offset = 0; offset < ASSEMBLY_MISSIONS.length; offset += 1) {
        const candidate = ASSEMBLY_MISSIONS[(startIndex + offset) % ASSEMBLY_MISSIONS.length];
        const filteredOptions = candidate.options.filter((option) => !recentKeys.includes(option.key));
        if (filteredOptions.length >= 2) {
            return {
                ...candidate,
                options: [filteredOptions[0], filteredOptions[1]],
            };
        }
    }
    return ASSEMBLY_MISSIONS[startIndex];
}

function cloneLightingCommand(command: HomeAssistantLightCommand): HomeAssistantLightCommand {
    return {
        hex_color: command.hex_color,
        rgb_color: Array.isArray(command.rgb_color) && command.rgb_color.length === 3
            ? [command.rgb_color[0], command.rgb_color[1], command.rgb_color[2]]
            : undefined,
        entity: command.entity,
        brightness: command.brightness,
        transition: command.transition,
        scene_description: command.scene_description,
    };
}

function buildTheaterLightingCommand(
    stage: TheaterLightingStage,
    fallback: HomeAssistantLightCommand | null,
): HomeAssistantLightCommand {
    switch (stage) {
        case 'open':
            return {
                rgb_color: [96, 82, 188],
                brightness: 122,
                transition: 1.6,
            };
        case 'play':
            return {
                rgb_color: [58, 46, 122],
                brightness: 84,
                transition: 1.2,
            };
        case 'pause':
            return {
                rgb_color: [92, 78, 180],
                brightness: 130,
                transition: 1.0,
            };
        case 'end':
            return {
                rgb_color: [255, 194, 104],
                brightness: 178,
                transition: 1.2,
            };
        case 'close':
        default:
            return fallback && (fallback.hex_color || fallback.rgb_color)
                ? cloneLightingCommand(fallback)
                : {
                    rgb_color: [255, 187, 120],
                    brightness: 182,
                    transition: 1.0,
                };
    }
}

function storybookMoviePacingHelperCopy(mode: StorybookMoviePacing): string {
    switch (mode) {
        case 'read_to_me':
            return 'Voice-first with shorter page text for pre-readers.';
        case 'fast_movie':
            return 'Brisker page turns for replays and quick sharing.';
        case 'read_with_me':
        default:
            return 'Balanced read-along pacing with extra time for page text.';
    }
}

function storyReaderVoiceHelperCopy(voiceId: string): string {
    return getStoryReaderVoiceOption(voiceId).blurb;
}

function normalizeMicPermissionState(
    state: PermissionState | MicPermissionState | null | undefined,
): MicPermissionState {
    if (state === 'granted' || state === 'denied' || state === 'prompt') {
        return state;
    }
    return 'unknown';
}

function describeMicSetupError(error: unknown): { permissionState: MicPermissionState; message: string } {
    if (error instanceof DOMException) {
        if (error.name === 'NotAllowedError' || error.name === 'SecurityError') {
            return {
                permissionState: 'denied',
                message: 'Microphone access was blocked. Tap the browser mic prompt and choose Allow, or reopen site permissions and enable the microphone.',
            };
        }
        if (error.name === 'NotFoundError' || error.name === 'DevicesNotFoundError') {
            return {
                permissionState: 'prompt',
                message: 'No microphone was found. Plug one in or switch to a different input and try again.',
            };
        }
        if (error.name === 'NotReadableError' || error.name === 'TrackStartError') {
            return {
                permissionState: 'prompt',
                message: 'That microphone is busy in another app. Close the other app or pick a different mic, then try again.',
            };
        }
        if (error.name === 'OverconstrainedError') {
            return {
                permissionState: 'prompt',
                message: 'That microphone is unavailable right now. Pick another one and try again.',
            };
        }
    }
    return {
        permissionState: 'prompt',
        message: 'We could not start the microphone test. Try again and allow access when your browser asks.',
    };
}

export default function StorytellerLive() {
    const [phase, setPhase] = useState<AppPhase>('gate');
    const [storyPhase, setStoryPhase] = useState<StoryFlowPhase>('opening');
    const [splashIndex] = useState(() => Math.floor(Math.random() * 30) + 1);
    const [calmMode, setCalmMode] = useState(false);
    const calmModeRef = useRef(false);
    const storyToneRef = useRef<StoryTone>('cozy');
    const childAgeRef = useRef<number>(4);
    const storybookMoviePacingRef = useRef<StorybookMoviePacing>('read_with_me');
    const storyReaderVoiceIdRef = useRef<string>(DEFAULT_STORY_READER_VOICE_ID);
    const [childAge, setChildAge] = useState(4);
    const [storybookMoviePacing, setStorybookMoviePacing] = useState<StorybookMoviePacing>('read_with_me');
    const [storyReaderVoiceId, setStoryReaderVoiceId] = useState(DEFAULT_STORY_READER_VOICE_ID);
    const { playUiSound } = useUiSounds({ enabled: !calmMode, volume: 0.95 });
    const [voiceRms, setVoiceRms] = useState(0);
    const [currentSceneImageUrl, setCurrentSceneImageUrl] = useState<string | null>(null);
    const [currentSceneThumbnailB64, setCurrentSceneThumbnailB64] = useState<string | null>(null);
    const [currentSceneVideoUrl, setCurrentSceneVideoUrl] = useState<string | null>(null);
    const [currentSceneStorybeatText, setCurrentSceneStorybeatText] = useState<string | null>(null);
    const [sceneHistory, setSceneHistory] = useState<SceneBranchPoint[]>([]);
    const [finalMovieUrl, setFinalMovieUrl] = useState<string | null>(null);
    const [tradingCardUrl, setTradingCardUrl] = useState<string | null>(null);
    const [storybookTitle, setStorybookTitle] = useState<string | null>(null);
    const [storybookChildName, setStorybookChildName] = useState<string | null>(null);
    const [isMicMuted, setIsMicMuted] = useState(false);
    const [showRestartConfirm, setShowRestartConfirm] = useState(false);
    const [showEndStoryConfirm, setShowEndStoryConfirm] = useState(false);
    const [showParentControls, setShowParentControls] = useState(false);
    const [showSceneBranchPicker, setShowSceneBranchPicker] = useState(false);
    const [selectedSceneNumber, setSelectedSceneNumber] = useState<number | null>(null);
    const [sceneBranchWarning, setSceneBranchWarning] = useState<string>('Going back will remove the pages after that scene.');
    const [isNewScene, setIsNewScene] = useState(false);
    const isMicMutedRef = useRef(isMicMuted);
    useEffect(() => {
        isMicMutedRef.current = isMicMuted;
    }, [isMicMuted]);
    const [agentThinking, setAgentThinking] = useState(false);
    const [hasHeardAgent, setHasHeardAgent] = useState(false);
    const [isNarrow, setIsNarrow] = useState(false);
    const [isCompact, setIsCompact] = useState(false);
    const [isLandscapePhone, setIsLandscapePhone] = useState(false);
    const [isPageReadAloudActive, setIsPageReadAloudActive] = useState(false);
    const [pageReadAloudError, setPageReadAloudError] = useState<string | null>(null);
    const [pageReadAloudHighlightWordIndex, setPageReadAloudHighlightWordIndex] = useState(-1);
    const [userSpeaking, setUserSpeaking] = useState(false);
    const connectionStateRef = useRef<'connecting' | 'connected' | 'reconnecting' | 'disconnected'>('connecting');
    const [spyglassStream, setSpyglassStream] = useState<MediaStream | null>(null);
    const [userTranscript, setUserTranscript] = useState<{ text: string, isFinished: boolean } | null>(null);
    const [displayedAgentText, setDisplayedAgentText] = useState("");
    const agentWordsRef = useRef<string[]>([]);
    const revealedCountRef = useRef(0);
    const agentFinishedRef = useRef(false);
    const typewriterTimerRef = useRef<ReturnType<typeof setInterval> | null>(null);
    const agentClearTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
    const userTranscriptTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);
    const [spyglassCapturing, setSpyglassCapturing] = useState(false);
    const [toyShareState, setToyShareState] = useState<'idle' | 'uploading' | 'success' | 'error'>('idle');
    const [toyShareOverlayOpen, setToyShareOverlayOpen] = useState(false);
    const [toyShareCameraError, setToyShareCameraError] = useState<string | null>(null);
    const [toyShareCameraStarting, setToyShareCameraStarting] = useState(false);
    const [toySharePreviewUrl, setToySharePreviewUrl] = useState<string | null>(null);
    const spyglassVideoRef = useRef<HTMLVideoElement | null>(null);
    const toyUploadInputRef = useRef<HTMLInputElement | null>(null);
    const toyShareResetTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);
    const toySharePreviewObjectUrlRef = useRef<string | null>(null);
    const openToyShareOverlayRef = useRef<(options?: ToyShareOverlayOptions) => Promise<void>>(async () => { });
    const closeToyShareOverlayRef = useRef<(options?: { notifyBackend?: boolean }) => void>(() => { });
    const beginEndingStoryRef = useRef<(options?: EndingStoryOptions) => void>(() => { });
    const setMicEnabledRef = useRef<(enabled: boolean) => void>(() => { });
    const restartStoryNowRef = useRef<() => void>(() => { });
    const openSceneBranchPickerRef = useRef<(options?: SceneBranchPickerOptions) => void>(() => { });
    const closeSceneBranchPickerRef = useRef<() => void>(() => { });
    const pendingSceneImageRef = useRef<string | null>(null);
    const currentSceneImageUrlRef = useRef<string | null>(null);
    const activeSceneRequestIdRef = useRef<string | null>(null);
    const pageReadAloudContentKeyRef = useRef('');
    const pageReadAloudRunIdRef = useRef(0);
    const isPageReadAloudActiveRef = useRef(false);
    const stopPageReadAloudRef = useRef<(options?: { resumeMic?: boolean }) => void>(() => { });
    const pageReadAloudStartLockUntilRef = useRef(0);
    const pageReadAloudInterruptIgnoreUntilRef = useRef(0);
    const pageReadAloudResumeMicTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
    const pageReadAloudShouldResumeMicRef = useRef(false);
    const pageReadAloudAbortControllerRef = useRef<AbortController | null>(null);
    const pageReadAloudPrefetchAbortControllerRef = useRef<AbortController | null>(null);
    const pageReadAloudPrefetchPromiseRef = useRef<Promise<PageReadAloudPayload> | null>(null);
    const pageReadAloudPrefetchContentKeyRef = useRef('');
    const pageReadAloudCachedPayloadRef = useRef<PageReadAloudPayload | null>(null);
    const pageReadAloudCachedContentKeyRef = useRef('');
    const placeholderTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);
    const previousSceneLoadingRef = useRef(false);
    const [sceneLoading, setSceneLoading] = useState(false);
    const [sceneError, setSceneError] = useState<string | null>(null);
    const [zoomedImageUrl, setZoomedImageUrl] = useState<string | null>(null);
    const [isEndingStory, setIsEndingStory] = useState(false);
    const [storybookStatus, setStorybookStatus] = useState<StorybookStatus | null>(null);
    const [assemblyRecentActivities, setAssemblyRecentActivities] = useState<string[]>([]);
    const [storybookNarration, setStorybookNarration] = useState<string[] | null>(null);
    const [storybookAudioAvailable, setStorybookAudioAvailable] = useState<boolean | null>(null);
    const [storybookLightingCues, setStorybookLightingCues] = useState<TheaterLightingCue[]>([]);
    const [storybookWaitElapsedSeconds, setStorybookWaitElapsedSeconds] = useState(0);
    const [serverVadEnabled, setServerVadEnabled] = useState(false);
    const [micCheckError, setMicCheckError] = useState<string | null>(null);
    const [micPermissionState, setMicPermissionState] = useState<MicPermissionState>('unknown');
    const [availableMicDevices, setAvailableMicDevices] = useState<MediaDeviceInfo[]>([]);
    const [selectedMicDeviceId, setSelectedMicDeviceId] = useState('');
    const [micSetupBusy, setMicSetupBusy] = useState(false);
    const [micSetupHeardVoice, setMicSetupHeardVoice] = useState(false);
    const permissionStatusRef = useRef<PermissionStatus | null>(null);
    const selectedMicDeviceIdRef = useRef('');
    const iotConfigRef = useRef<IoTConfig | null>(null);
    const phaseRef = useRef<AppPhase>('gate');
    const completeMicCheckRef = useRef<(reason: 'heard' | 'timeout' | 'skip') => void | Promise<void>>(() => { });
    const sendClientReadyRef = useRef<() => void>(() => { });
    const stopStoryReaderVoicePreviewRef = useRef<() => void>(() => { });
    const isStoryReaderVoicePreviewActiveRef = useRef(false);
    const storyReaderVoicePreviewShouldResumeMicRef = useRef(false);
    const hasEverConnectedRef = useRef(false);
    const lastUserTranscriptRef = useRef<string>('');
    const lastAgentTranscriptRef = useRef<string>('');
    const resumeMicOnReconnectRef = useRef(false);
    const lastLightingCommandRef = useRef<string>('');
    const lastStoryLightingCommandRef = useRef<HomeAssistantLightCommand | null>(null);
    const lastTheaterLightingCommandRef = useRef<string>('');
    const theaterLightingStageRef = useRef<TheaterLightingStage | null>(null);
    const isEndingStoryRef = useRef(false);
    const storybookStatusRef = useRef<StorybookStatus | null>(null);

    useEffect(() => {
        isEndingStoryRef.current = isEndingStory;
    }, [isEndingStory]);

    useEffect(() => {
        storybookStatusRef.current = storybookStatus;
    }, [storybookStatus]);

    useEffect(() => {
        selectedMicDeviceIdRef.current = selectedMicDeviceId;
    }, [selectedMicDeviceId]);

    useEffect(() => {
        const wasLoading = previousSceneLoadingRef.current;
        if (sceneLoading && !wasLoading) {
            playUiSound('magic');
        } else if (!sceneLoading && wasLoading && currentSceneImageUrl && !sceneError) {
            playUiSound('tap');
        }
        previousSceneLoadingRef.current = sceneLoading;
    }, [currentSceneImageUrl, playUiSound, sceneError, sceneLoading]);

    const scheduleToyShareReset = useCallback((delayMs = 3500) => {
        if (toyShareResetTimeoutRef.current) {
            clearTimeout(toyShareResetTimeoutRef.current);
        }
        toyShareResetTimeoutRef.current = setTimeout(() => {
            setToyShareState('idle');
            toyShareResetTimeoutRef.current = null;
        }, delayMs);
    }, []);

    const replaceToySharePreview = useCallback((fileOrBlob: Blob | File | null) => {
        if (toySharePreviewObjectUrlRef.current) {
            URL.revokeObjectURL(toySharePreviewObjectUrlRef.current);
            toySharePreviewObjectUrlRef.current = null;
        }
        if (!fileOrBlob) {
            setToySharePreviewUrl(null);
            return;
        }
        const objectUrl = URL.createObjectURL(fileOrBlob);
        toySharePreviewObjectUrlRef.current = objectUrl;
        setToySharePreviewUrl(objectUrl);
    }, []);

    useEffect(() => {
        if (!storybookStatus) {
            setStorybookWaitElapsedSeconds(0);
            return;
        }
        const updateElapsed = () => {
            setStorybookWaitElapsedSeconds(
                Math.max(0, Math.floor((Date.now() - storybookStatus.startedAtMs) / 1000))
            );
        };
        updateElapsed();
        const timer = window.setInterval(updateElapsed, 1000);
        return () => window.clearInterval(timer);
    }, [storybookStatus]);

    useEffect(() => {
        calmModeRef.current = calmMode;
    }, [calmMode]);

    useEffect(() => {
        return () => {
            if (typewriterTimerRef.current) {
                clearInterval(typewriterTimerRef.current);
                typewriterTimerRef.current = null;
            }
            if (agentClearTimerRef.current) {
                clearTimeout(agentClearTimerRef.current);
                agentClearTimerRef.current = null;
            }
            if (userTranscriptTimeoutRef.current) {
                clearTimeout(userTranscriptTimeoutRef.current);
                userTranscriptTimeoutRef.current = null;
            }
            if (placeholderTimeoutRef.current) {
                clearTimeout(placeholderTimeoutRef.current);
                placeholderTimeoutRef.current = null;
            }
            if (toyShareResetTimeoutRef.current) {
                clearTimeout(toyShareResetTimeoutRef.current);
                toyShareResetTimeoutRef.current = null;
            }
            if (toySharePreviewObjectUrlRef.current) {
                URL.revokeObjectURL(toySharePreviewObjectUrlRef.current);
                toySharePreviewObjectUrlRef.current = null;
            }
        };
    }, []);

    const clearAgentSpeechUi = useCallback(() => {
        if (typewriterTimerRef.current) {
            clearInterval(typewriterTimerRef.current);
            typewriterTimerRef.current = null;
        }
        if (agentClearTimerRef.current) {
            clearTimeout(agentClearTimerRef.current);
            agentClearTimerRef.current = null;
        }
        agentWordsRef.current = [];
        revealedCountRef.current = 0;
        agentFinishedRef.current = false;
        lastAgentTranscriptRef.current = '';
        setDisplayedAgentText('');
    }, []);

    const showTransientAgentCue = useCallback((text: string, durationMs = 1600) => {
        const cue = text.trim();
        if (!cue) {
            return;
        }
        clearAgentSpeechUi();
        setDisplayedAgentText(cue);
        setHasHeardAgent(true);
        agentClearTimerRef.current = setTimeout(() => {
            setDisplayedAgentText('');
            agentClearTimerRef.current = null;
        }, durationMs);
    }, [clearAgentSpeechUi]);

    useEffect(() => {
        phaseRef.current = phase;
        streamAudioRef.current = phase === 'story';
    }, [phase]);


    const hasStoredMicOk = useCallback(() => {
        try {
            return typeof window !== 'undefined' && localStorage.getItem(MIC_OK_KEY) === '1';
        } catch {
            return false;
        }
    }, []);

    const markMicOk = useCallback(() => {
        try {
            if (typeof window !== 'undefined') {
                localStorage.setItem(MIC_OK_KEY, '1');
            }
        } catch {
            // ignore
        }
    }, []);

    const clearMicOk = useCallback(() => {
        try {
            if (typeof window !== 'undefined') {
                localStorage.removeItem(MIC_OK_KEY);
            }
        } catch {
            // ignore
        }
    }, []);

    const getStoredMicDeviceId = useCallback(() => {
        try {
            if (typeof window === 'undefined') {
                return '';
            }
            return localStorage.getItem(MIC_DEVICE_KEY) ?? '';
        } catch {
            return '';
        }
    }, []);

    const persistMicDeviceId = useCallback((deviceId: string) => {
        if (!deviceId) {
            return;
        }
        try {
            if (typeof window !== 'undefined') {
                localStorage.setItem(MIC_DEVICE_KEY, deviceId);
            }
        } catch {
            // ignore
        }
    }, []);

    const refreshMicPermissionState = useCallback(async () => {
        if (typeof navigator === 'undefined' || !navigator.permissions?.query) {
            setMicPermissionState((current) => (current === 'granted' || current === 'denied' ? current : 'prompt'));
            return;
        }
        try {
            if (permissionStatusRef.current) {
                permissionStatusRef.current.onchange = null;
            }
            const status = await navigator.permissions.query({ name: 'microphone' as PermissionName });
            permissionStatusRef.current = status;
            const applyState = () => {
                setMicPermissionState(normalizeMicPermissionState(status.state));
            };
            applyState();
            status.onchange = applyState;
        } catch {
            setMicPermissionState((current) => (current === 'granted' || current === 'denied' ? current : 'prompt'));
        }
    }, []);

    const refreshMicDevices = useCallback(async (preferredDeviceId?: string | null) => {
        if (typeof navigator === 'undefined' || !navigator.mediaDevices?.enumerateDevices) {
            setAvailableMicDevices([]);
            return;
        }
        try {
            const devices = await navigator.mediaDevices.enumerateDevices();
            const microphones = devices.filter((device) => device.kind === 'audioinput');
            setAvailableMicDevices(microphones);
            const nextDeviceId = [
                preferredDeviceId ?? null,
                selectedMicDeviceIdRef.current || null,
                getStoredMicDeviceId() || null,
                microphones[0]?.deviceId ?? null,
            ].find((candidate) => candidate && microphones.some((device) => device.deviceId === candidate)) ?? '';
            setSelectedMicDeviceId(nextDeviceId);
            if (nextDeviceId) {
                persistMicDeviceId(nextDeviceId);
            }
        } catch {
            setAvailableMicDevices([]);
        }
    }, [getStoredMicDeviceId, persistMicDeviceId]);

    useEffect(() => {
        const storedDeviceId = getStoredMicDeviceId();
        if (storedDeviceId) {
            setSelectedMicDeviceId(storedDeviceId);
        }
    }, [getStoredMicDeviceId]);

    useEffect(() => {
        if (phase !== 'mic-check') {
            if (permissionStatusRef.current) {
                permissionStatusRef.current.onchange = null;
                permissionStatusRef.current = null;
            }
            return;
        }
        void refreshMicPermissionState();
        void refreshMicDevices(selectedMicDeviceIdRef.current || getStoredMicDeviceId() || null);
        return () => {
            if (permissionStatusRef.current) {
                permissionStatusRef.current.onchange = null;
                permissionStatusRef.current = null;
            }
        };
    }, [getStoredMicDeviceId, phase, refreshMicDevices, refreshMicPermissionState]);

    useEffect(() => {
        currentSceneImageUrlRef.current = currentSceneImageUrl;
    }, [currentSceneImageUrl]);

    // Keep the last scene image visible until a newer image URL is confirmed loadable.
    const commitSceneImage = useCallback((url: string, options?: CommitSceneImageOptions) => {
        if (!url) return;

        const thumbnailB64 = options?.thumbnailB64;
        const requestId = typeof options?.requestId === 'string'
            ? options.requestId.trim() || null
            : null;
        const sceneRequestIsActive = () => (
            !requestId
            || !activeSceneRequestIdRef.current
            || activeSceneRequestIdRef.current === requestId
        );
        const commitCurrentScene = () => {
            if (!sceneRequestIsActive()) {
                return false;
            }
            if (thumbnailB64) {
                setCurrentSceneThumbnailB64(thumbnailB64);
            }
            setCurrentSceneImageUrl(url);
            options?.onCommitted?.();
            return true;
        };
        const pendingToken = requestId ? `${requestId}::${url}` : url;

        if (url.startsWith('data:image')) {
            const isPlaceholder = url.startsWith('data:image/svg+xml');

            // Guard: If we already have a real image on screen, do NOT commit the placeholder URL.
            // This keeps the old scene visible while the 'sceneLoading' state (set elsewhere) 
            // shows the "Amelia is drawing" overlay on top.
            if (isPlaceholder && currentSceneImageUrlRef.current && !currentSceneImageUrlRef.current.startsWith('data:image/svg+xml')) {
                pendingSceneImageRef.current = pendingToken;
                if (placeholderTimeoutRef.current) {
                    clearTimeout(placeholderTimeoutRef.current);
                }
                placeholderTimeoutRef.current = setTimeout(() => {
                    if (pendingSceneImageRef.current === pendingToken) {
                        setSceneLoading(false);
                        setSceneError('Picture is taking a bit longer. Keep talking and we’ll catch up!');
                    }
                }, SCENE_PLACEHOLDER_TIMEOUT_MS);
                console.log('Preserving existing scene image while new one generates.');
                return;
            }

            pendingSceneImageRef.current = pendingToken;
            commitCurrentScene();
            setSceneLoading(isPlaceholder);
            if (placeholderTimeoutRef.current) {
                clearTimeout(placeholderTimeoutRef.current);
                placeholderTimeoutRef.current = null;
            }
            if (isPlaceholder) {
                placeholderTimeoutRef.current = setTimeout(() => {
                    if (pendingSceneImageRef.current === pendingToken) {
                        setSceneLoading(false);
                        setSceneError('Picture is taking a bit longer. Keep talking and we’ll catch up!');
                    }
                }, SCENE_PLACEHOLDER_TIMEOUT_MS);
            }
            return;
        }

        const probe = new window.Image();
        pendingSceneImageRef.current = pendingToken;
        probe.onload = () => {
            if (pendingSceneImageRef.current === pendingToken && sceneRequestIsActive()) {
                if (placeholderTimeoutRef.current) {
                    clearTimeout(placeholderTimeoutRef.current);
                    placeholderTimeoutRef.current = null;
                }
                commitCurrentScene();
                setIsNewScene(true);
                setTimeout(() => setIsNewScene(false), 2000);
                setSceneLoading(false);
                setSceneError(null);
            }
        };
        probe.onerror = () => {
            if (pendingSceneImageRef.current === pendingToken && sceneRequestIsActive()) {
                if (placeholderTimeoutRef.current) {
                    clearTimeout(placeholderTimeoutRef.current);
                    placeholderTimeoutRef.current = null;
                }
                console.warn('Scene image failed to load; preserving previous scene image.');
                setSceneLoading(false);
                setSceneError('Picture unavailable right now.');
            }
        };
        probe.src = url;
    }, []);
    const commitSceneImageRef = useRef(commitSceneImage);
    commitSceneImageRef.current = commitSceneImage;

    // Safety net: never let the UI stay in "Thinking..." forever if a turn terminator is missed 
    // or if the microphone VAD picks up room noise but the LLM ignores it (silence).
    // Keep the signal visible long enough for slower turns, but still avoid a permanent stuck state.
    useEffect(() => {
        if (!agentThinking) return;
        const timer = setTimeout(() => setAgentThinking(false), 12000);
        return () => clearTimeout(timer);
    }, [agentThinking]);

    // Shorten camera label on mobile to prevent bleed
    useEffect(() => {
        const mq = window.matchMedia('(max-width: 500px)');
        const update = () => setIsNarrow(mq.matches);
        update();
        mq.addEventListener('change', update);
        return () => mq.removeEventListener('change', update);
    }, []);

    useEffect(() => {
        const mq = window.matchMedia('(max-width: 900px)');
        const update = () => setIsCompact(mq.matches);
        update();
        mq.addEventListener('change', update);
        return () => mq.removeEventListener('change', update);
    }, []);

    useEffect(() => {
        const mq = window.matchMedia('(orientation: landscape) and (max-height: 560px) and (pointer: coarse)');
        const update = () => setIsLandscapePhone(mq.matches);
        update();
        mq.addEventListener('change', update);
        return () => mq.removeEventListener('change', update);
    }, []);

    // Optional camera: attach stream to preview video and stop tracks on cleanup
    useEffect(() => {
        if (!spyglassStream || !spyglassVideoRef.current) return;
        const video = spyglassVideoRef.current;
        video.srcObject = spyglassStream;
        video.play().catch(() => { });
        return () => {
            spyglassStream.getTracks().forEach((t) => t.stop());
        };
    }, [spyglassStream]);

    // Refs to break circular dependency between hooks:
    // useWebSocket needs playPcmChunk (from useAudioWorklet) and enqueueSfx (from useSfxDucker),
    // but useAudioWorklet needs send (from useWebSocket).
    // Solution: use refs that are updated synchronously after each hook call.
    const playPcmChunkRef = useRef<(data: ArrayBuffer) => void>(() => { });
    const flushPlaybackBufferRef = useRef<() => void>(() => { });
    const interruptPlaybackRef = useRef<() => void>(() => { });
    const enqueueSfxRef = useRef<(url: string, label?: string) => void>(() => { });
    const sendJsonRef = useRef<(msg: Record<string, unknown>) => void>(() => { });
    const sessionIdRef = useRef<string>('');
    const previousSessionIdRef = useRef<string>('');
    const wsUrlRef = useRef<string>(resolveFrontendWebSocketUrl());
    const uploadUrlRef = useRef<string>(resolveUploadUrl());
    const pageReadAloudUrlRef = useRef<string>(resolvePageReadAloudUrl());

    // ── Audio (defined FIRST — provides playPcmChunk) ────────────────────────────
    const sendRef = useRef<(data: string | ArrayBuffer) => void>(() => { });
    const streamAudioRef = useRef(false);
    const startListeningRef = useRef<(options?: { deviceId?: string | null }) => Promise<void>>(async () => { });
    const {
        audioState,
        narrationGainNode,
        primeAudio,
        startListening,
        stopListening,
        playPcmChunk,
        playBufferedClip,
        stopBufferedClip,
        flushPlaybackBuffer,
        interruptPlayback,
        setNarrationMuted,
    } =
        useAudioWorklet({
            onPcmChunk: useCallback((pcm: ArrayBuffer) => {
                if (streamAudioRef.current) {
                    sendRef.current(pcm);
                }
            }, []),
            onVoiceVolume: setVoiceRms,
            onFlushComplete: useCallback(() => {
                if (phaseRef.current === 'story' && connectionStateRef.current === 'connected' && !isMicMutedRef.current) {
                    startListeningRef.current();
                }
            }, []),
            onVoiceActivityStart: useCallback(() => {
                if (
                    isPageReadAloudActiveRef.current
                    && performance.now() < pageReadAloudInterruptIgnoreUntilRef.current
                ) {
                    return;
                }
                if (isPageReadAloudActiveRef.current) {
                    stopPageReadAloudRef.current({ resumeMic: false });
                }
                if (isStoryReaderVoicePreviewActiveRef.current) {
                    stopStoryReaderVoicePreviewRef.current();
                }
                setAgentThinking(false);
                setUserSpeaking(true);
                if (phaseRef.current === 'mic-check') {
                    setMicSetupHeardVoice(true);
                    return;
                }
                if (phaseRef.current !== 'story') {
                    return;
                }
                if (
                    connectionStateRef.current === 'connected'
                    && !serverVadEnabled
                    && sessionIdRef.current
                ) {
                    sendJsonRef.current({
                        type: 'activity_start',
                        session_id: sessionIdRef.current,
                        payload: {},
                    });
                }
            }, [serverVadEnabled]),
            onVoiceActivityEnd: useCallback(() => {
                setAgentThinking(true);
                setUserSpeaking(false);
                if (phaseRef.current !== 'story') {
                    return;
                }
                if (
                    connectionStateRef.current === 'connected'
                    && !serverVadEnabled
                    && sessionIdRef.current
                ) {
                    sendJsonRef.current({
                        type: 'activity_end',
                        session_id: sessionIdRef.current,
                        payload: {},
                    });
                }
            }, [serverVadEnabled]),
        });
    startListeningRef.current = startListening;
    playPcmChunkRef.current = playPcmChunk;
    flushPlaybackBufferRef.current = flushPlaybackBuffer;
    interruptPlaybackRef.current = interruptPlayback;
    const {
        previewError: storyReaderVoicePreviewError,
        previewLoading: storyReaderVoicePreviewLoading,
        previewPlaying: storyReaderVoicePreviewPlaying,
        previewVoiceId: storyReaderVoicePreviewVoiceId,
        previewVoice: previewStoryReaderVoice,
        stopPreview: stopStoryReaderVoicePreview,
    } = useStoryVoicePreview({
        childAge,
        storybookMoviePacing,
        sessionId: sessionIdRef.current || null,
        onPreviewStart: () => {
            if (isPageReadAloudActiveRef.current) {
                stopPageReadAloudRef.current({ resumeMic: false });
            }
            const shouldResumeMic = phaseRef.current === 'story' && !isMicMutedRef.current;
            storyReaderVoicePreviewShouldResumeMicRef.current = shouldResumeMic;
            if (shouldResumeMic) {
                stopListening();
            }
        },
        onPreviewEnd: () => {
            const shouldResumeMic = storyReaderVoicePreviewShouldResumeMicRef.current;
            storyReaderVoicePreviewShouldResumeMicRef.current = false;
            if (shouldResumeMic && phaseRef.current === 'story' && !isMicMutedRef.current) {
                void startListeningRef.current();
            }
        },
    });
    isStoryReaderVoicePreviewActiveRef.current = storyReaderVoicePreviewLoading || storyReaderVoicePreviewPlaying;
    stopStoryReaderVoicePreviewRef.current = () => {
        storyReaderVoicePreviewShouldResumeMicRef.current = false;
        stopStoryReaderVoicePreview();
    };

    useEffect(() => {
        if (!showParentControls) {
            stopStoryReaderVoicePreview();
            return;
        }
        if (showRestartConfirm || showEndStoryConfirm || showSceneBranchPicker || toyShareOverlayOpen || phase !== 'story') {
            setShowParentControls(false);
        }
    }, [
        phase,
        showEndStoryConfirm,
        showParentControls,
        showRestartConfirm,
        showSceneBranchPicker,
        stopStoryReaderVoicePreview,
        toyShareOverlayOpen,
    ]);

    useEffect(() => {
        if (storyReaderVoicePreviewVoiceId && storyReaderVoicePreviewVoiceId !== storyReaderVoiceId) {
            stopStoryReaderVoicePreview();
        }
    }, [stopStoryReaderVoicePreview, storyReaderVoiceId, storyReaderVoicePreviewVoiceId]);

    useEffect(() => {
        if (phase !== 'story') {
            stopStoryReaderVoicePreview();
        }
    }, [phase, stopStoryReaderVoicePreview]);

    const clearPageReadAloudCache = useCallback(() => {
        pageReadAloudPrefetchAbortControllerRef.current?.abort();
        pageReadAloudPrefetchAbortControllerRef.current = null;
        pageReadAloudPrefetchPromiseRef.current = null;
        pageReadAloudPrefetchContentKeyRef.current = '';
        pageReadAloudCachedPayloadRef.current = null;
        pageReadAloudCachedContentKeyRef.current = '';
    }, []);

    const buildPageReadAloudContentKey = useCallback((
        imageUrl: string | null | undefined,
        text: string,
        voiceId: string,
    ): string => `${imageUrl ?? ''}::${normalizeStoryReaderVoiceId(voiceId)}::${text}`, []);

    const fetchPageReadAloudAudio = useCallback(async (
        text: string,
        contentKey: string,
        signal?: AbortSignal,
    ): Promise<PageReadAloudPayload> => {
        const cachedPayload = pageReadAloudCachedContentKeyRef.current === contentKey
            ? pageReadAloudCachedPayloadRef.current
            : null;
        if (cachedPayload?.audioBytes.byteLength) {
            return cachedPayload;
        }

        if (
            pageReadAloudPrefetchContentKeyRef.current === contentKey
            && pageReadAloudPrefetchPromiseRef.current
        ) {
            return pageReadAloudPrefetchPromiseRef.current;
        }

        const response = await fetch(pageReadAloudUrlRef.current, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
            },
            signal,
            body: JSON.stringify({
                session_id: sessionIdRef.current,
                text,
                child_age: childAgeRef.current,
                storybook_movie_pacing: storybookMoviePacingRef.current,
                storybook_elevenlabs_voice_id: storyReaderVoiceIdRef.current,
            }),
        });
        if (!response.ok) {
            let message = 'Read page is unavailable right now.';
            try {
                const payload = await response.json() as { message?: unknown };
                if (typeof payload?.message === 'string' && payload.message.trim()) {
                    message = payload.message.trim();
                }
            } catch {
                // Keep the generic error copy.
            }
            throw new Error(message);
        }
        const audioBytes = await response.arrayBuffer();
        if (!audioBytes.byteLength) {
            throw new Error('page_read_aloud_empty_audio');
        }
        const wordStartsMs = parsePageReadAloudWordStartsMs(
            response.headers.get('X-StorySpark-Word-Starts-Ms'),
        );
        const payload: PageReadAloudPayload = { audioBytes, wordStartsMs };
        clearPageReadAloudCache();
        pageReadAloudCachedPayloadRef.current = payload;
        pageReadAloudCachedContentKeyRef.current = contentKey;
        return payload;
    }, [clearPageReadAloudCache]);

    const schedulePageReadAloudMicResume = useCallback(() => {
        if (pageReadAloudResumeMicTimerRef.current) {
            clearTimeout(pageReadAloudResumeMicTimerRef.current);
            pageReadAloudResumeMicTimerRef.current = null;
        }
        pageReadAloudResumeMicTimerRef.current = setTimeout(() => {
            pageReadAloudResumeMicTimerRef.current = null;
            if (phaseRef.current === 'story' && !isMicMutedRef.current) {
                void startListening();
            }
        }, 650);
    }, [startListening]);

    const notifyPageReadAloudState = useCallback((active: boolean, suppressForMs = 0) => {
        const currentSessionId = sessionIdRef.current;
        if (!currentSessionId) {
            return;
        }
        sendJsonRef.current({
            type: 'page_read_aloud' as any,
            session_id: currentSessionId,
            payload: {
                active,
                suppress_for_ms: Math.max(0, Math.trunc(suppressForMs)),
            },
        });
    }, []);

    const finishPageReadAloud = useCallback((runId: number, shouldResumeMic: boolean) => {
        if (pageReadAloudRunIdRef.current !== runId) {
            return;
        }
        pageReadAloudAbortControllerRef.current = null;
        pageReadAloudInterruptIgnoreUntilRef.current = 0;
        isPageReadAloudActiveRef.current = false;
        setIsPageReadAloudActive(false);
        setPageReadAloudHighlightWordIndex(-1);
        pageReadAloudShouldResumeMicRef.current = false;
        pageReadAloudContentKeyRef.current = '';
        notifyPageReadAloudState(false, shouldResumeMic ? 1800 : 0);
        if (shouldResumeMic && phaseRef.current === 'story' && !isMicMutedRef.current) {
            schedulePageReadAloudMicResume();
        }
    }, [notifyPageReadAloudState, schedulePageReadAloudMicResume]);

    const stopPageReadAloud = useCallback((options?: { resumeMic?: boolean }) => {
        const resumeMic = options?.resumeMic ?? true;
        pageReadAloudRunIdRef.current += 1;
        const shouldResumeMic = resumeMic && pageReadAloudShouldResumeMicRef.current;
        if (pageReadAloudResumeMicTimerRef.current) {
            clearTimeout(pageReadAloudResumeMicTimerRef.current);
            pageReadAloudResumeMicTimerRef.current = null;
        }
        pageReadAloudShouldResumeMicRef.current = false;
        pageReadAloudContentKeyRef.current = '';
        pageReadAloudInterruptIgnoreUntilRef.current = 0;
        pageReadAloudAbortControllerRef.current?.abort();
        pageReadAloudAbortControllerRef.current = null;
        isPageReadAloudActiveRef.current = false;
        stopBufferedClip();
        setIsPageReadAloudActive(false);
        setPageReadAloudHighlightWordIndex(-1);
        notifyPageReadAloudState(false, shouldResumeMic ? 1200 : 0);
        if (shouldResumeMic && phaseRef.current === 'story' && !isMicMutedRef.current) {
            void startListening();
        }
    }, [notifyPageReadAloudState, startListening, stopBufferedClip]);
    stopPageReadAloudRef.current = stopPageReadAloud;

    const readCurrentPageAloud = useCallback(async () => {
        const text = normalizeStoryCaptionText(currentSceneStorybeatText);
        if (!text) {
            return;
        }
        const now = performance.now();
        if (isPageReadAloudActiveRef.current) {
            playUiSound('close');
            stopPageReadAloud({ resumeMic: true });
            return;
        }
        if (now < pageReadAloudStartLockUntilRef.current) {
            return;
        }

        const runId = pageReadAloudRunIdRef.current + 1;
        pageReadAloudRunIdRef.current = runId;
        pageReadAloudStartLockUntilRef.current = now + 500;
        const contentKey = buildPageReadAloudContentKey(
            currentSceneImageUrl,
            text,
            storyReaderVoiceIdRef.current,
        );
        const wordBoundaries = buildStoryCaptionWordBoundaries(tokenizeStoryCaption(text));
        const totalWordCount = wordBoundaries.length;
        const shouldResumeMic = phaseRef.current === 'story' && !isMicMutedRef.current;
        if (pageReadAloudResumeMicTimerRef.current) {
            clearTimeout(pageReadAloudResumeMicTimerRef.current);
            pageReadAloudResumeMicTimerRef.current = null;
        }
        pageReadAloudShouldResumeMicRef.current = shouldResumeMic;
        pageReadAloudContentKeyRef.current = contentKey;
        pageReadAloudInterruptIgnoreUntilRef.current = now + 1500;
        isPageReadAloudActiveRef.current = true;
        notifyPageReadAloudState(true, 2500);
        setIsPageReadAloudActive(true);
        setPageReadAloudError(null);
        setPageReadAloudHighlightWordIndex(totalWordCount > 0 ? 0 : -1);
        const updateHighlight = (
            timing: BufferedClipProgress,
            exactWordStartsMs: number[],
        ) => {
            if (totalWordCount <= 0) {
                return;
            }
            const nextIndex = exactWordStartsMs.length
                ? highlightWordIndexForElapsedMs(timing.elapsedSpeechMs, exactWordStartsMs)
                : highlightWordIndexForProgress(timing.progress, wordBoundaries);
            setPageReadAloudHighlightWordIndex(nextIndex);
        };
        if (shouldResumeMic) {
            stopListening();
        }

        try {
            await primeAudio();
            const cachedPayload = pageReadAloudCachedContentKeyRef.current === contentKey
                ? pageReadAloudCachedPayloadRef.current
                : null;

            if (cachedPayload) {
                const exactWordStartsMs = normalizePageReadAloudWordStartsMs(
                    cachedPayload.wordStartsMs,
                    totalWordCount,
                );
                const played = await playBufferedClip(cachedPayload.audioBytes, {
                    onProgress: (timing) => updateHighlight(timing, exactWordStartsMs),
                    onEnded: () => finishPageReadAloud(runId, shouldResumeMic),
                });
                if (!played) {
                    finishPageReadAloud(runId, shouldResumeMic);
                }
                return;
            }

            const controller = new AbortController();
            pageReadAloudAbortControllerRef.current = controller;
            const payload = await fetchPageReadAloudAudio(text, contentKey, controller.signal);
            if (pageReadAloudRunIdRef.current !== runId) {
                return;
            }

            const exactWordStartsMs = normalizePageReadAloudWordStartsMs(
                payload.wordStartsMs,
                totalWordCount,
            );
            const played = await playBufferedClip(payload.audioBytes, {
                onProgress: (timing) => updateHighlight(timing, exactWordStartsMs),
                onEnded: () => finishPageReadAloud(runId, shouldResumeMic),
            });
            if (!played) {
                finishPageReadAloud(runId, shouldResumeMic);
            }
        } catch (error) {
            if ((error as Error)?.name === 'AbortError' || pageReadAloudRunIdRef.current !== runId) {
                return;
            }
            console.warn('Page read-aloud failed:', error);
            setPageReadAloudError(
                error instanceof Error && error.message.trim()
                    ? error.message.trim()
                    : 'Read page is unavailable right now.',
            );
            finishPageReadAloud(runId, shouldResumeMic);
        }
    }, [
        currentSceneImageUrl,
        currentSceneStorybeatText,
        buildPageReadAloudContentKey,
        fetchPageReadAloudAudio,
        finishPageReadAloud,
        playBufferedClip,
        playUiSound,
        primeAudio,
        notifyPageReadAloudState,
        stopListening,
        stopPageReadAloud,
    ]);

    useEffect(() => {
        const currentCaptionText = normalizeStoryCaptionText(currentSceneStorybeatText);
        const currentContentKey = buildPageReadAloudContentKey(
            currentSceneImageUrl,
            currentCaptionText,
            storyReaderVoiceId,
        );
        if (currentContentKey !== pageReadAloudCachedContentKeyRef.current) {
            clearPageReadAloudCache();
        }
        if (currentContentKey !== pageReadAloudPrefetchContentKeyRef.current) {
            pageReadAloudPrefetchAbortControllerRef.current?.abort();
            pageReadAloudPrefetchAbortControllerRef.current = null;
            pageReadAloudPrefetchPromiseRef.current = null;
            pageReadAloudPrefetchContentKeyRef.current = '';
        }
        if (!isPageReadAloudActive) {
            isPageReadAloudActiveRef.current = false;
            setPageReadAloudHighlightWordIndex(-1);
        }
        if (!isPageReadAloudActive) {
            return;
        }
        if (phase !== 'story' || currentContentKey !== pageReadAloudContentKeyRef.current) {
            stopPageReadAloud({ resumeMic: true });
        }
    }, [
        clearPageReadAloudCache,
        buildPageReadAloudContentKey,
        currentSceneStorybeatText,
        currentSceneImageUrl,
        isPageReadAloudActive,
        phase,
        storyReaderVoiceId,
        stopPageReadAloud,
    ]);

    useEffect(() => {
        setPageReadAloudError(null);
        setPageReadAloudHighlightWordIndex(-1);
    }, [currentSceneImageUrl, currentSceneStorybeatText]);

    useEffect(() => {
        const text = normalizeStoryCaptionText(currentSceneStorybeatText);
        const contentKey = buildPageReadAloudContentKey(
            currentSceneImageUrl,
            text,
            storyReaderVoiceId,
        );
        if (
            phase !== 'story'
            || !text
            || !currentSceneImageUrl
            || pageReadAloudCachedContentKeyRef.current === contentKey
            || pageReadAloudPrefetchContentKeyRef.current === contentKey
        ) {
            return;
        }

        void primeAudio().catch(() => {
            // Browser gesture rules may still block priming; prefetch can continue.
        });

        pageReadAloudPrefetchAbortControllerRef.current?.abort();
        const controller = new AbortController();
        pageReadAloudPrefetchAbortControllerRef.current = controller;
        pageReadAloudPrefetchContentKeyRef.current = contentKey;
        const prefetchPromise = fetchPageReadAloudAudio(text, contentKey, controller.signal);
        pageReadAloudPrefetchPromiseRef.current = prefetchPromise;
        void prefetchPromise.catch((error) => {
            if ((error as Error)?.name !== 'AbortError') {
                console.warn('Page read-aloud prefetch failed:', error);
            }
        }).finally(() => {
            if (pageReadAloudPrefetchPromiseRef.current === prefetchPromise) {
                pageReadAloudPrefetchPromiseRef.current = null;
                pageReadAloudPrefetchAbortControllerRef.current = null;
                pageReadAloudPrefetchContentKeyRef.current = '';
            }
        });

        return () => {
            if (pageReadAloudPrefetchAbortControllerRef.current === controller) {
                controller.abort();
                pageReadAloudPrefetchAbortControllerRef.current = null;
                pageReadAloudPrefetchPromiseRef.current = null;
                pageReadAloudPrefetchContentKeyRef.current = '';
            }
        };
    }, [
        buildPageReadAloudContentKey,
        currentSceneImageUrl,
        currentSceneStorybeatText,
        fetchPageReadAloudAudio,
        phase,
        primeAudio,
        storyReaderVoiceId,
    ]);

    useEffect(() => {
        return () => {
            notifyPageReadAloudState(false, 0);
            pageReadAloudAbortControllerRef.current?.abort();
            pageReadAloudPrefetchAbortControllerRef.current?.abort();
            if (pageReadAloudResumeMicTimerRef.current) {
                clearTimeout(pageReadAloudResumeMicTimerRef.current);
                pageReadAloudResumeMicTimerRef.current = null;
            }
            isPageReadAloudActiveRef.current = false;
            stopBufferedClip();
            clearPageReadAloudCache();
        };
    }, [clearPageReadAloudCache, notifyPageReadAloudState, stopBufferedClip]);

    // ── SFX Ducker (defined SECOND — provides enqueueSfx) ────────────────────────
    const { enqueueSfx } = useSfxDucker(narrationGainNode);
    enqueueSfxRef.current = (url: string, label?: string) => {
        if (calmModeRef.current) return;
        enqueueSfx(url, label);
    };
    const { setMusicMood, setMusicEnabled, setListeningFocus } = useBackgroundMusic();
    const setMusicMoodRef = useRef(setMusicMood);
    const setMusicEnabledRef = useRef(setMusicEnabled);
    const setMusicFocusRef = useRef(setListeningFocus);
    setMusicMoodRef.current = setMusicMood;
    setMusicEnabledRef.current = setMusicEnabled;
    setMusicFocusRef.current = setListeningFocus;

    useEffect(() => {
        setNarrationMuted(calmMode);
        setMusicEnabledRef.current(!calmMode);
    }, [calmMode, setNarrationMuted]);

    useEffect(() => {
        const shouldUseMusicFocus =
            phase === 'story' &&
            !calmMode &&
            (audioState === 'listening' || userSpeaking || agentThinking);
        setMusicFocusRef.current(shouldUseMusicFocus);
    }, [agentThinking, audioState, calmMode, phase, userSpeaking]);

    // Try to lock to landscape storybook orientation when possible.
    useEffect(() => {
        const orientation = screen.orientation as ScreenOrientation & {
            lock?: (orientation: string) => Promise<void>;
            unlock?: () => void;
        };
        if (orientation && orientation.lock) {
            orientation.lock('landscape').catch(() => { });
        }
        return () => {
            orientation?.unlock?.();
        };
    }, []);

    // ── WebSocket (defined THIRD — uses playPcmChunkRef + enqueueSfxRef) ─────────
    const { connectionState, sendJson, send, sessionId } = useWebSocket({
        url: wsUrlRef.current,
        onBinaryMessage: useCallback((data: ArrayBuffer) => {
            if (isPageReadAloudActiveRef.current) {
                stopPageReadAloudRef.current({ resumeMic: false });
            }
            if (isStoryReaderVoicePreviewActiveRef.current) {
                stopStoryReaderVoicePreviewRef.current();
            }
            // PCM audio from Gemini native audio — forward to playback worklet
            playPcmChunkRef.current(data);
            setAgentThinking(false);
            setHasHeardAgent(true);
            setStoryPhase((current) => (
                current === 'assembling_movie' || current === 'remake' || current === 'theater'
                    ? current
                    : 'chatting'
            ));
        }, []),
        onJsonMessage: useCallback((msg: { type: string; payload?: Record<string, unknown> }) => {
            switch (msg.type) {
                case 'TURN_COMPLETE':
                    // Critical: clear "Thinking..." even when the model produced no audible chunk.
                    // Otherwise the UI can get stuck in thinking state after tool-heavy/silent turns.
                    setAgentThinking(false);
                    setStoryPhase((current) => (
                        current === 'assembling_movie' || current === 'remake' || current === 'theater'
                            ? current
                            : 'waiting_for_child'
                    ));
                    lastAgentTranscriptRef.current = '';
                    lastUserTranscriptRef.current = '';
                    flushPlaybackBufferRef.current();
                    break;
                case 'video_ready':
                    {
                        const url = msg.payload?.url as string;
                        if (!url) break;
                        const requestId = typeof msg.payload?.request_id === 'string'
                            ? (msg.payload.request_id as string).trim() || null
                            : null;
                        const nextSceneHistory = normalizeSceneHistory(msg.payload?.scene_history);
                        const hasSceneHistoryPayload = Array.isArray(msg.payload?.scene_history);
                        const isPlaceholder = Boolean(msg.payload?.is_placeholder);
                        setStoryPhase(isPlaceholder ? 'drawing_scene' : 'waiting_for_child');
                        const isFallback = Boolean(msg.payload?.is_fallback);
                        const storybeatText = typeof msg.payload?.storybeat_text === 'string'
                            ? (msg.payload.storybeat_text as string).trim()
                            : '';
                        const mediaType = (msg.payload?.media_type as string | undefined)?.toLowerCase();
                        const inferredType: 'image' | 'video' =
                            mediaType === 'image' ||
                                url?.startsWith('data:image') ||
                                /\.(png|jpe?g|webp|gif|svg)(\?|$)/i.test(url)
                                ? 'image'
                                : 'video';
                        if (inferredType === 'image') {
                            if (requestId) {
                                if (isPlaceholder) {
                                    activeSceneRequestIdRef.current = requestId;
                                } else if (
                                    activeSceneRequestIdRef.current
                                    && activeSceneRequestIdRef.current !== requestId
                                ) {
                                    console.log('Ignoring stale scene image for superseded request.');
                                    break;
                                }
                            }
                            let scenePresentationApplied = false;
                            const applyCommittedScenePresentation = () => {
                                if (scenePresentationApplied) {
                                    return;
                                }
                                scenePresentationApplied = true;
                                if (hasSceneHistoryPayload && nextSceneHistory.length) {
                                    setSceneHistory(nextSceneHistory);
                                }
                                if (storybeatText) {
                                    setCurrentSceneStorybeatText(storybeatText);
                                }
                            };
                            const thumbB64 = msg.payload?.thumbnail_b64 as string | undefined;
                            const thumbMime = typeof msg.payload?.thumbnail_mime === 'string'
                                ? (msg.payload.thumbnail_mime as string).trim() || 'image/jpeg'
                                : 'image/jpeg';
                            if (!isPlaceholder && !isFallback && thumbB64 && !url.startsWith('data:image')) {
                                commitSceneImageRef.current(`data:${thumbMime};base64,${thumbB64}`, {
                                    requestId,
                                    onCommitted: applyCommittedScenePresentation,
                                });
                            }
                            commitSceneImageRef.current(url, {
                                thumbnailB64: thumbB64,
                                requestId,
                                onCommitted: applyCommittedScenePresentation,
                            });
                            setCurrentSceneVideoUrl(null);

                            if (isFallback) {
                                if (placeholderTimeoutRef.current) {
                                    clearTimeout(placeholderTimeoutRef.current);
                                    placeholderTimeoutRef.current = null;
                                }
                                setSceneLoading(false);
                                setSceneError('Picture unavailable right now.');
                            } else if (isPlaceholder || url.startsWith('data:image/svg+xml')) {
                                // Important: even if commitSceneImage ignored the URL (because an image exists),
                                // we still want to show the loading state.
                                setSceneLoading(true);
                                setSceneError(null);
                            } else {
                                // Full image arrived: let commitSceneImage clear loading only after
                                // the new asset has actually been accepted into the scene.
                                setSceneError(null);
                            }
                        } else if (!currentSceneImageUrlRef.current) {
                            // Show video only when no image has been established yet.
                            setCurrentSceneVideoUrl(url);
                            setSceneLoading(false);
                            setSceneError(null);
                        }
                    }
                    break;
                case 'theater_mode':
                    clearPersistedStorybookAssemblyState(sessionIdRef.current);
                    setFinalMovieUrl(msg.payload?.mp4_url as string);
                    {
                        const cardUrl = typeof msg.payload?.trading_card_url === 'string'
                            ? (msg.payload.trading_card_url as string).trim()
                            : '';
                        setTradingCardUrl(cardUrl || null);
                    }
                    {
                        const title = typeof msg.payload?.story_title === 'string'
                            ? (msg.payload.story_title as string).trim()
                            : '';
                        if (title) {
                            setStorybookTitle(title);
                        }
                        const childName = typeof msg.payload?.child_name === 'string'
                            ? (msg.payload.child_name as string).trim()
                            : '';
                        if (childName && childName.toLowerCase() !== 'friend') {
                            setStorybookChildName(childName);
                        }
                        const narrationRaw = msg.payload?.narration_lines;
                        const narration = Array.isArray(narrationRaw)
                            ? narrationRaw.filter((line) => typeof line === 'string' && line.trim().length > 0)
                            : null;
                        setStorybookNarration(narration && narration.length ? narration : null);
                        const audioAvailable = typeof msg.payload?.audio_available === 'boolean'
                            ? (msg.payload?.audio_available as boolean)
                            : null;
                        setStorybookAudioAvailable(audioAvailable);
                        setStorybookLightingCues(normalizeTheaterLightingCues(msg.payload?.lighting_cues));
                    }
                    setStoryPhase(normalizeStoryFlowPhase(msg.payload?.story_phase ?? 'theater'));
                    setPhase('theater');
                    setIsEndingStory(false);
                    setStorybookStatus(null);
                    setAssemblyRecentActivities([]);
                    break;
                case 'trading_card_ready': {
                    const cardUrl = msg.payload?.trading_card_url as string | undefined;
                    if (cardUrl) setTradingCardUrl(cardUrl);
                    break;
                }
                case 'video_generation_started': {
                    const message = (msg.payload?.message as string) || 'Making your storybook movie…';
                    const eta = Number(msg.payload?.eta_seconds ?? 0);
                    const startedAtEpochMs = Number(msg.payload?.started_at_epoch_ms ?? 0);
                    const storyTitle = typeof msg.payload?.story_title === 'string'
                        ? (msg.payload.story_title as string).trim()
                        : '';
                    const childName = typeof msg.payload?.child_name === 'string'
                        ? (msg.payload.child_name as string).trim()
                        : '';
                    const kind = msg.payload?.kind === 'remake' ? 'remake' : 'initial';
                    if (storyTitle) {
                        setStorybookTitle(storyTitle);
                    }
                    if (childName && childName.toLowerCase() !== 'friend') {
                        setStorybookChildName(childName);
                    }
                    setStorybookStatus((current) => {
                        const payloadStartedAtMs = Number.isFinite(startedAtEpochMs) && startedAtEpochMs > 0
                            ? startedAtEpochMs
                            : null;
                        const preservedStartedAtMs = current && current.kind === kind ? current.startedAtMs : null;
                        return {
                            message,
                            etaSeconds: Number.isFinite(eta) && eta > 0 ? eta : undefined,
                            storyTitle: storyTitle || undefined,
                            childName: childName || undefined,
                            kind,
                            startedAtMs: payloadStartedAtMs ?? preservedStartedAtMs ?? Date.now(),
                        };
                    });
                    setStoryPhase(normalizeStoryFlowPhase(msg.payload?.story_phase ?? (kind === 'remake' ? 'remake' : 'assembling_movie')));
                    setIsEndingStory(true);
                    break;
                }
                case 'music_command': {
                    if (calmModeRef.current) {
                        setMusicEnabledRef.current(false);
                        break;
                    }
                    const mood = msg.payload?.mood as string;
                    const intensity = Number(msg.payload?.intensity ?? 5);
                    void setMusicMoodRef.current(mood, intensity);
                    break;
                }
                case 'lighting_command': {
                    const command = (msg.payload ?? {}) as HomeAssistantLightCommand;
                    if (!command.hex_color && !command.rgb_color) {
                        break;
                    }

                    const commandKey = getLightingCommandKey(command);
                    const shouldApplyClientSide =
                        Boolean(command.client_should_apply) || command.backend_applied !== true;

                    if (!shouldApplyClientSide) {
                        if (commandKey) {
                            lastLightingCommandRef.current = commandKey;
                        }
                        lastStoryLightingCommandRef.current = cloneLightingCommand(command);
                        break;
                    }

                    if (commandKey && commandKey === lastLightingCommandRef.current) {
                        break;
                    }

                    void applyHomeAssistantLighting(iotConfigRef.current, command)
                        .then((result) => {
                            if (result.ok && commandKey) {
                                lastLightingCommandRef.current = commandKey;
                                lastStoryLightingCommandRef.current = cloneLightingCommand(command);
                            }
                            if (!result.ok && result.reason !== 'not_configured') {
                                console.warn('Home Assistant lighting skipped:', result.reason, command);
                            }
                        })
                        .catch((error) => {
                            console.warn('Home Assistant lighting command failed:', error);
                        });
                    break;
                }
                case 'sfx_command':
                    enqueueSfxRef.current(msg.payload?.url as string, msg.payload?.label as string);
                    break;
                case 'heartbeat':
                    // Respond to server-side ping to keep proxy connection alive
                    sendJsonRef.current({
                        type: 'heartbeat' as any,
                        session_id: sessionIdRef.current,
                        payload: { pong: true }
                    });
                    break;
                case 'quick_ack':
                    stopPageReadAloudRef.current({ resumeMic: false });
                    stopStoryReaderVoicePreviewRef.current();
                    if (Boolean(msg.payload?.interrupt_audio)) {
                        interruptPlaybackRef.current();
                    }
                    if (typeof msg.payload?.text === 'string' && msg.payload.text.trim()) {
                        showTransientAgentCue(msg.payload.text as string);
                    }
                    setAgentThinking(true);
                    setStoryPhase((current) => (
                        current === 'assembling_movie' || current === 'remake' || current === 'theater'
                            ? current
                            : 'drawing_scene'
                    ));
                    break;
                case 'rewind_complete':
                    clearPersistedStorybookAssemblyState(sessionIdRef.current);
                    activeSceneRequestIdRef.current = null;
                    setStoryPhase('waiting_for_child');
                    setPhase('story');
                    setIsEndingStory(false);
                    setStorybookStatus(null);
                    setSceneLoading(false);
                    setCurrentSceneVideoUrl(null);
                    closeSceneBranchPickerRef.current();
                    if (msg.payload?.current_scene_image_url) {
                        commitSceneImageRef.current(msg.payload.current_scene_image_url as string);
                    }
                    if (typeof msg.payload?.current_scene_storybeat_text === 'string') {
                        const text = (msg.payload.current_scene_storybeat_text as string).trim();
                        setCurrentSceneStorybeatText(text || null);
                    }
                    {
                        const nextSceneHistory = normalizeSceneHistory(msg.payload?.scene_history);
                        if (nextSceneHistory.length || Array.isArray(msg.payload?.scene_history)) {
                            setSceneHistory(nextSceneHistory);
                        }
                    }
                    break;
                case 'session_rehydrated':
                    activeSceneRequestIdRef.current = null;
                    {
                        const rehydratedStoryPhase = normalizeStoryFlowPhase(msg.payload?.story_phase);
                        const shouldStayInTheater =
                            rehydratedStoryPhase === 'theater'
                            || phaseRef.current === 'theater';
                        setStoryPhase(rehydratedStoryPhase);
                        setAgentThinking(Boolean(msg.payload?.pending_response) && !shouldStayInTheater);
                        setServerVadEnabled(Boolean(msg.payload?.server_vad_enabled));
                        setSceneHistory(normalizeSceneHistory(msg.payload?.scene_history));
                        const persistedAssemblyState = sessionIdRef.current
                            ? loadPersistedStorybookAssemblyState(sessionIdRef.current)
                            : null;
                        const canResumeToyShare = isToyShareAllowedForState(
                            'story',
                            rehydratedStoryPhase,
                            Boolean(msg.payload?.ending_story),
                            persistedAssemblyState?.status ?? null,
                        );
                        if (msg.payload?.toy_share_active && canResumeToyShare) {
                            void openToyShareOverlayRef.current({ notifyBackend: false, autoStartCamera: false });
                        } else {
                            closeToyShareOverlayRef.current({ notifyBackend: false });
                        }
                        const rehydratedStoryTitle = typeof msg.payload?.story_title === 'string'
                            ? (msg.payload.story_title as string).trim()
                            : '';
                        if (rehydratedStoryTitle) {
                            setStorybookTitle(rehydratedStoryTitle);
                        }
                        if (typeof msg.payload?.child_name === 'string') {
                            const childName = (msg.payload.child_name as string).trim();
                            setStorybookChildName(childName && childName.toLowerCase() !== 'friend' ? childName : null);
                        }
                        const reconnectConversationAllowed =
                            (Boolean(msg.payload?.story_started) || Boolean(msg.payload?.ending_story))
                            && !Boolean(msg.payload?.assistant_speaking)
                            && !Boolean(msg.payload?.pending_response)
                            && !shouldStayInTheater;
                        resumeMicOnReconnectRef.current = reconnectConversationAllowed;

                        // Recover UI state after reconnect
                        if (msg.payload?.current_scene_image_url) {
                            commitSceneImageRef.current(msg.payload.current_scene_image_url as string);
                        }
                        if (typeof msg.payload?.current_scene_storybeat_text === 'string') {
                            const text = (msg.payload.current_scene_storybeat_text as string).trim();
                            setCurrentSceneStorybeatText(text || null);
                        }
                        const recentPersistedAssembly =
                            persistedAssemblyState
                            && (Date.now() - persistedAssemblyState.savedAtMs) <= STORYBOOK_ASSEMBLY_REHYDRATION_GRACE_MS
                                ? persistedAssemblyState
                                : null;
                        const rehydratedAssemblyStatus = typeof msg.payload?.assembly_status === 'string'
                            ? (msg.payload.assembly_status as string).trim().toLowerCase()
                            : '';
                        const rehydratedAssemblyError = typeof msg.payload?.assembly_error === 'string'
                            ? (msg.payload.assembly_error as string).trim()
                            : '';
                        const assemblyStillRunning =
                            rehydratedAssemblyStatus === 'assembling'
                            || rehydratedAssemblyStatus === 'reviewing_storyboard'
                            || isAssemblyStoryPhase(rehydratedStoryPhase);
                        if (rehydratedAssemblyStatus === 'failed') {
                            clearPersistedStorybookAssemblyState(sessionIdRef.current);
                            setSceneError(rehydratedAssemblyError || 'Movie assembly failed before the final video was created.');
                            setStorybookStatus(null);
                            setIsEndingStory(false);
                            setStoryPhase('waiting_for_child');
                        } else if (msg.payload?.ending_story || assemblyStillRunning) {
                            setIsEndingStory(true);
                            const rehydratedEtaSeconds = Number(msg.payload?.assembly_eta_seconds ?? 0);
                            const rehydratedStartedAtMs = Number(msg.payload?.assembly_started_at_epoch_ms ?? 0);
                            setStorybookStatus((current) => {
                                const validEtaSeconds = Number.isFinite(rehydratedEtaSeconds) && rehydratedEtaSeconds > 0
                                    ? rehydratedEtaSeconds
                                    : undefined;
                                const validStartedAtMs = Number.isFinite(rehydratedStartedAtMs) && rehydratedStartedAtMs > 0
                                    ? rehydratedStartedAtMs
                                    : null;
                                if (current) {
                                    return {
                                        ...current,
                                        etaSeconds: current.etaSeconds ?? validEtaSeconds,
                                        startedAtMs: validStartedAtMs ?? current.startedAtMs,
                                    };
                                }
                                if (recentPersistedAssembly?.status) {
                                    return {
                                        ...recentPersistedAssembly.status,
                                        etaSeconds: recentPersistedAssembly.status.etaSeconds ?? validEtaSeconds,
                                        startedAtMs: validStartedAtMs ?? recentPersistedAssembly.status.startedAtMs,
                                    };
                                }
                                return {
                                    message: 'Making your storybook movie…',
                                    etaSeconds: validEtaSeconds,
                                    storyTitle: rehydratedStoryTitle || storybookTitle || undefined,
                                    childName: storybookChildName || undefined,
                                    kind: 'initial',
                                    startedAtMs: validStartedAtMs ?? Date.now(),
                                };
                            });
                            setStoryPhase(
                                normalizeStoryFlowPhase(
                                    msg.payload?.story_phase
                                    ?? (rehydratedAssemblyStatus === 'reviewing_storyboard' ? 'remake' : 'assembling_movie')
                                )
                            );
                        } else if (!shouldStayInTheater) {
                            if (recentPersistedAssembly) {
                                setIsEndingStory(true);
                                setStorybookStatus((current) => current ?? recentPersistedAssembly.status);
                                setStoryPhase(recentPersistedAssembly.storyPhase);
                                if (recentPersistedAssembly.storyTitle) {
                                    setStorybookTitle(recentPersistedAssembly.storyTitle);
                                }
                                if (recentPersistedAssembly.childName) {
                                    setStorybookChildName(recentPersistedAssembly.childName);
                                }
                            } else {
                                if (persistedAssemblyState) {
                                    clearPersistedStorybookAssemblyState(sessionIdRef.current);
                                }
                                setIsEndingStory(false);
                                setStorybookStatus(null);
                            }
                        }

                        if (shouldStayInTheater) {
                            setPhase('theater');
                            setIsEndingStory(false);
                            stopListening();
                            break;
                        }

                        if (msg.payload?.story_started || msg.payload?.ending_story || recentPersistedAssembly) {
                            setPhase('story');
                            sendClientReadyRef.current();
                            const shouldAutoResumeMic = (
                                resumeMicOnReconnectRef.current
                                && !isMicMutedRef.current
                                && (!recentPersistedAssembly || assemblyStillRunning)
                            );
                            if (shouldAutoResumeMic) {
                                void startListeningRef.current();
                            }
                        }
                    }
                    break;
                case 'ui_command': {
                    const action = String(msg.payload?.action || '').trim();
                    if (!action) break;
                    if (action === 'open_toy_share') {
                        void openToyShareOverlayRef.current({ notifyBackend: false, autoStartCamera: true });
                        break;
                    }
                    if (action === 'close_toy_share') {
                        closeToyShareOverlayRef.current({ notifyBackend: false });
                        break;
                    }
                    if (action === 'open_scene_branch_picker') {
                        if (!ENABLE_SCENE_BRANCH_UI) {
                            break;
                        }
                        const nextSceneHistory = normalizeSceneHistory(msg.payload?.scene_history);
                        if (nextSceneHistory.length || Array.isArray(msg.payload?.scene_history)) {
                            setSceneHistory(nextSceneHistory);
                        }
                        openSceneBranchPickerRef.current({
                            selectedSceneNumber: Number(msg.payload?.scene_number ?? 0) || null,
                            warning: typeof msg.payload?.warning === 'string' ? msg.payload.warning as string : undefined,
                        });
                        break;
                    }
                    if (action === 'close_scene_branch_picker') {
                        closeSceneBranchPickerRef.current();
                        break;
                    }
                    if (action === 'set_mic_enabled') {
                        setMicEnabledRef.current(Boolean(msg.payload?.enabled));
                        break;
                    }
                    if (action === 'restart_story') {
                        restartStoryNowRef.current();
                        break;
                    }
                    if (action === 'story_ending') {
                        const etaSeconds = Number(msg.payload?.eta_seconds ?? 90);
                        beginEndingStoryRef.current({
                            notifyBackend: false,
                            message: typeof msg.payload?.message === 'string' ? msg.payload.message as string : undefined,
                            etaSeconds: Number.isFinite(etaSeconds) && etaSeconds > 0 ? etaSeconds : undefined,
                        });
                    }
                    break;
                }
                case 'error':
                    // Clear thinking spinner on backend recoverable errors.
                    setAgentThinking(false);
                    if (msg.payload?.assembly_failed && typeof msg.payload?.message === 'string') {
                        clearPersistedStorybookAssemblyState(sessionIdRef.current);
                        setStoryPhase('waiting_for_child');
                        setIsEndingStory(false);
                        setStorybookStatus(null);
                        setSceneError(msg.payload.message as string);
                    } else if (!isEndingStoryRef.current && !storybookStatusRef.current) {
                        setIsEndingStory(false);
                        setStorybookStatus(null);
                    }
                    // If the backend is resetting and asks for an auto-resume,
                    // re-trigger listening so the child doesn't get stuck.
                    if (msg.payload?.auto_resume) {
                        console.log('Auto-resume hint received from backend error.');
                        void startListeningRef.current();
                    }
                    break;
                case 'user_transcription':
                    {
                        if (isPageReadAloudActiveRef.current) {
                            if (performance.now() < pageReadAloudInterruptIgnoreUntilRef.current) {
                                break;
                            }
                            stopPageReadAloudRef.current({ resumeMic: false });
                        }
                        if (isStoryReaderVoicePreviewActiveRef.current) {
                            stopStoryReaderVoicePreviewRef.current();
                        }
                        const text = msg.payload?.text as string;
                        const finished = !!msg.payload?.finished;
                        if (text) {
                            const merged = mergeStreamingTranscript(lastUserTranscriptRef.current, text);
                            lastUserTranscriptRef.current = merged;
                            // Show only the current utterance — no accumulation
                            setUserTranscript({ text: merged, isFinished: finished });
                            if (userTranscriptTimeoutRef.current) clearTimeout(userTranscriptTimeoutRef.current);
                            if (finished) {
                                setStoryPhase((current) => (
                                    current === 'assembling_movie' || current === 'remake' || current === 'theater'
                                        ? current
                                        : 'chatting'
                                ));
                                setAgentThinking(true); // fallback: show "thinking" even if VAD misses
                                setUserSpeaking(false);
                                userTranscriptTimeoutRef.current = setTimeout(() => {
                                    setUserTranscript(null);
                                    lastUserTranscriptRef.current = '';
                                }, 2500);
                            }
                        }
                    }
                    break;
                case 'agent_transcription':
                    {
                        if (isPageReadAloudActiveRef.current) {
                            if (performance.now() < pageReadAloudInterruptIgnoreUntilRef.current) {
                                break;
                            }
                            stopPageReadAloudRef.current({ resumeMic: false });
                        }
                        if (isStoryReaderVoicePreviewActiveRef.current) {
                            stopStoryReaderVoicePreviewRef.current();
                        }
                        const text = msg.payload?.text as string;
                        const finished = !!msg.payload?.finished;
                        if (text) {
                            setStoryPhase((current) => (
                                current === 'assembling_movie' || current === 'remake' || current === 'theater'
                                    ? current
                                    : 'chatting'
                            ));
                            setAgentThinking(false);
                            setHasHeardAgent(true);
                            const merged = mergeStreamingTranscript(lastAgentTranscriptRef.current, text);
                            lastAgentTranscriptRef.current = merged;
                            // Feed cumulative words into the queue
                            const words = merged.trim().split(/\s+/);
                            agentWordsRef.current = words;
                            agentFinishedRef.current = finished;

                            // Cancel any pending clear
                            if (agentClearTimerRef.current) {
                                clearTimeout(agentClearTimerRef.current);
                                agentClearTimerRef.current = null;
                            }

                            // Start the typewriter timer if not already running
                            if (!typewriterTimerRef.current) {
                                typewriterTimerRef.current = setInterval(() => {
                                    const pool = agentWordsRef.current;
                                    const count = revealedCountRef.current;
                                    if (count < pool.length) {
                                        revealedCountRef.current = count + 1;
                                        // Show only the last ~12 words (sliding window for small screens)
                                        const revealed = pool.slice(0, count + 1);
                                        const tail = revealed.length > 12 ? revealed.slice(-12) : revealed;
                                        setDisplayedAgentText(tail.join(' '));
                                    } else if (agentFinishedRef.current) {
                                        // All words revealed & narrator finished
                                        clearInterval(typewriterTimerRef.current!);
                                        typewriterTimerRef.current = null;
                                        agentClearTimerRef.current = setTimeout(() => {
                                            setDisplayedAgentText("");
                                            agentWordsRef.current = [];
                                            revealedCountRef.current = 0;
                                            agentFinishedRef.current = false;
                                        }, 2500);
                                    }
                                    // else: waiting for more words from backend
                                }, 150); // ~150ms per word ≈ child reading pace
                            }
                        }
                    }
                    break;
                default:
                    break;
            }
        }, []),
    });
    // Update sendRef now that useWebSocket has returned
    sendRef.current = send;
    sendJsonRef.current = sendJson;
    sessionIdRef.current = sessionId;

    useEffect(() => {
        if (!sessionId) {
            return;
        }
        const previousSessionId = previousSessionIdRef.current;
        if (previousSessionId && previousSessionId !== sessionId) {
            interruptPlaybackRef.current();
            stopPageReadAloudRef.current({ resumeMic: false });
            stopStoryReaderVoicePreviewRef.current();
            clearAgentSpeechUi();
            setUserTranscript(null);
            lastUserTranscriptRef.current = '';
        }
        previousSessionIdRef.current = sessionId;
    }, [clearAgentSpeechUi, sessionId]);

    const sendClientReady = useCallback(() => {
        const useCompactLayout = isCompact || isLandscapePhone;
        const viewport = {
            width: window.innerWidth,
            height: window.innerHeight,
            devicePixelRatio: window.devicePixelRatio || 1,
            isCompact: useCompactLayout,
            isLandscapePhone,
        };
        const connection = (
            navigator as Navigator & {
                connection?: {
                    effectiveType?: string;
                    saveData?: boolean;
                    downlink?: number;
                    rtt?: number;
                };
            }
        ).connection;
        const network = connection
            ? {
                effectiveType: typeof connection.effectiveType === 'string' ? connection.effectiveType : '',
                saveData: Boolean(connection.saveData),
                downlink: typeof connection.downlink === 'number' && Number.isFinite(connection.downlink)
                    ? connection.downlink
                    : undefined,
                rtt: typeof connection.rtt === 'number' && Number.isFinite(connection.rtt)
                    ? connection.rtt
                    : undefined,
            }
            : undefined;
        const panelWidth = useCompactLayout
            ? isLandscapePhone
                ? Math.min(window.innerWidth * 0.72, 520)
                : Math.min(window.innerWidth * 0.96, 560)
            : Math.min(window.innerWidth * 0.94, 860);
        const panelHeight = useCompactLayout
            ? isLandscapePhone
                ? Math.min(window.innerHeight * 0.62, 260)
                : Math.min(window.innerHeight * 0.56, 520)
            : (panelWidth * 9) / 16;
        const panel = {
            width: panelWidth,
            height: panelHeight,
        };
        sendJsonRef.current({
            type: 'client_ready',
            session_id: sessionIdRef.current,
            payload: {
                viewport,
                panel,
                story_tone: storyToneRef.current,
                child_age: childAgeRef.current,
                storybook_movie_pacing: storybookMoviePacingRef.current,
                storybook_elevenlabs_voice_id: storyReaderVoiceIdRef.current,
                network,
            },
        });

        const cfg = iotConfigRef.current;
        if (cfg && cfg.ha_url && cfg.ha_token) {
            sendJsonRef.current({
                type: 'iot_config',
                session_id: sessionIdRef.current,
                payload: { config: cfg },
            });
        }
    }, [isCompact, isLandscapePhone]);
    sendClientReadyRef.current = sendClientReady;

    const applyTheaterLightingStage = useCallback((stage: TheaterLightingStage) => {
        const config = iotConfigRef.current;
        if (!config?.ha_url || !config?.ha_token) {
            return;
        }

        const command = buildTheaterLightingCommand(stage, lastStoryLightingCommandRef.current);
        const commandKey = getLightingCommandKey(command);
        const previousStage = theaterLightingStageRef.current;

        if (stage !== 'close' && stage === previousStage && commandKey === lastTheaterLightingCommandRef.current) {
            return;
        }

        theaterLightingStageRef.current = stage;
        if (stage === 'close') {
            lastTheaterLightingCommandRef.current = '';
        } else {
            lastTheaterLightingCommandRef.current = commandKey;
        }

        void applyHomeAssistantLighting(config, command)
            .then((result) => {
                if (!result.ok && result.reason !== 'not_configured') {
                    console.warn(`Home Assistant theater lighting skipped for ${stage}:`, result.reason);
                    theaterLightingStageRef.current = previousStage;
                    if (stage !== 'close') {
                        lastTheaterLightingCommandRef.current = '';
                    }
                }
            })
            .catch((error) => {
                console.warn(`Home Assistant theater lighting failed for ${stage}:`, error);
                theaterLightingStageRef.current = previousStage;
                if (stage !== 'close') {
                    lastTheaterLightingCommandRef.current = '';
                }
            });
    }, []);

    const applyTheaterLightingCue = useCallback((cue: TheaterLightingCue) => {
        const config = iotConfigRef.current;
        if (!config?.ha_url || !config?.ha_token) {
            return;
        }

        void applyHomeAssistantLighting(config, cue)
            .catch((error) => {
                console.warn('Home Assistant theater cue failed:', error);
            });
    }, []);

    useEffect(() => {
        connectionStateRef.current = connectionState;
        if (connectionState === 'connected') {
            const reconnected = hasEverConnectedRef.current;
            hasEverConnectedRef.current = true;
            if (reconnected && phaseRef.current === 'story') {
                sendClientReadyRef.current();
            }
        }
        if (connectionState === 'disconnected') {
            hasEverConnectedRef.current = false;
            streamAudioRef.current = false;
            setUserSpeaking(false);
            interruptPlaybackRef.current();
            stopPageReadAloudRef.current({ resumeMic: false });
            stopStoryReaderVoicePreviewRef.current();
            clearAgentSpeechUi();
            stopListening();
        }
        if (connectionState === 'reconnecting') {
            resumeMicOnReconnectRef.current = false;
            streamAudioRef.current = false;
            setUserSpeaking(false);
            interruptPlaybackRef.current();
            stopPageReadAloudRef.current({ resumeMic: false });
            stopStoryReaderVoicePreviewRef.current();
            clearAgentSpeechUi();
            stopListening();
        }
    }, [clearAgentSpeechUi, connectionState, stopListening]);

    const requestMicSetupTest = useCallback(async (deviceIdOverride?: string | null): Promise<boolean> => {
        const requestedDeviceId = deviceIdOverride ?? (selectedMicDeviceIdRef.current || getStoredMicDeviceId() || null);
        setMicSetupBusy(true);
        setMicCheckError(null);
        setMicSetupHeardVoice(false);
        setVoiceRms(0);
        try {
            await startListening({ deviceId: requestedDeviceId });
            setMicPermissionState('granted');
            await refreshMicDevices(requestedDeviceId);
            return true;
        } catch (error) {
            console.error('Microphone test failed:', error);
            stopListening();
            clearMicOk();
            setVoiceRms(0);
            const describedError = describeMicSetupError(error);
            setMicPermissionState(describedError.permissionState);
            setMicCheckError(describedError.message);
            await refreshMicDevices(getStoredMicDeviceId() || null);
            return false;
        } finally {
            setMicSetupBusy(false);
        }
    }, [clearMicOk, getStoredMicDeviceId, refreshMicDevices, startListening, stopListening]);

    const completeMicCheck = useCallback(async (_reason: 'heard' | 'timeout' | 'skip') => {
        setMicCheckError(null);
        const ready = audioState === 'listening'
            ? true
            : await requestMicSetupTest(selectedMicDeviceIdRef.current || null);
        if (!ready) {
            return;
        }
        if (selectedMicDeviceIdRef.current) {
            persistMicDeviceId(selectedMicDeviceIdRef.current);
        }
        markMicOk();
        setPhase('story');
        setStoryPhase('opening');
        setHasHeardAgent(false);
        setAgentThinking(true);
        sendClientReady();
    }, [audioState, markMicOk, persistMicDeviceId, requestMicSetupTest, sendClientReady]);

    useEffect(() => {
        if (!sessionId || storybookStatusRef.current || phaseRef.current === 'theater') {
            return;
        }
        const persisted = loadPersistedStorybookAssemblyState(sessionId);
        if (!persisted) {
            return;
        }
        setStorybookStatus(persisted.status);
        setStoryPhase(persisted.storyPhase);
        setIsEndingStory(true);
        if (persisted.storyTitle) {
            setStorybookTitle(persisted.storyTitle);
        }
        if (persisted.childName) {
            setStorybookChildName(persisted.childName);
        }
    }, [sessionId]);

    useEffect(() => {
        if (!sessionId || !storybookStatus || phase === 'theater') {
            return;
        }
        persistStorybookAssemblyState(
            sessionId,
            storybookStatus,
            storybookTitle,
            storybookChildName,
            storyPhase,
        );
    }, [phase, sessionId, storyPhase, storybookChildName, storybookStatus, storybookTitle]);

    useEffect(() => {
        if (storybookStatus && phase !== 'theater' && !isEndingStory) {
            setIsEndingStory(true);
        }
    }, [isEndingStory, phase, storybookStatus]);

    useEffect(() => {
        completeMicCheckRef.current = completeMicCheck;
    }, [completeMicCheck]);

    // ── Parent Gate approval ─────────────────────────────────────────────────────
    const handleGateApproved = useCallback(async (
        calm: boolean,
        iotConfig: IoTConfig | null,
        storyTone: StoryTone,
        childAge: number,
        nextStorybookMoviePacing: StorybookMoviePacing,
        nextStoryReaderVoiceId: string,
    ) => {
        setCalmMode(calm);
        calmModeRef.current = calm;
        storyToneRef.current = storyTone;
        childAgeRef.current = childAge;
        storybookMoviePacingRef.current = nextStorybookMoviePacing;
        storyReaderVoiceIdRef.current = normalizeStoryReaderVoiceId(nextStoryReaderVoiceId);
        clearPageReadAloudCache();
        setChildAge(childAge);
        setStorybookMoviePacing(nextStorybookMoviePacing);
        setStoryReaderVoiceId(storyReaderVoiceIdRef.current);
        setShowParentControls(false);
        setNarrationMuted(calm);
        setMusicEnabledRef.current(!calm);
        setHasHeardAgent(false);
        setMicCheckError(null);
        setMicPermissionState('prompt');
        setMicSetupHeardVoice(false);
        setVoiceRms(0);
        setStoryPhase('opening');
        iotConfigRef.current = iotConfig;
        lastLightingCommandRef.current = '';
        lastStoryLightingCommandRef.current = null;
        lastTheaterLightingCommandRef.current = '';
        theaterLightingStageRef.current = null;
        setStorybookLightingCues([]);
        const skipMicCheck = hasStoredMicOk();
        const storedDeviceId = getStoredMicDeviceId() || null;
        const shouldWarmMicOnGateTap =
            typeof navigator !== 'undefined'
            && typeof navigator.mediaDevices?.getUserMedia === 'function'
            && (
                isCompact
                || isLandscapePhone
                || navigator.maxTouchPoints > 0
                || window.matchMedia?.('(pointer: coarse)').matches === true
            );
        let attemptedMicOnGateTap = false;
        let micReadyFromGateTap = false;

        setPhase('mic-check');

        if (shouldWarmMicOnGateTap) {
            attemptedMicOnGateTap = true;
            micReadyFromGateTap = await requestMicSetupTest(storedDeviceId);
        }

        try {
            // Keep playback ready, but on mobile try to ask for mic access on the
            // very first approved tap so the browser prompt appears reliably.
            await primeAudio();
            setAgentThinking(false);
        } catch (e) {
            console.error('Microphone initialization failed:', e);
            clearMicOk();
            setAgentThinking(false);
            setMicCheckError('Speaker setup could not start. Refresh and try again.');
            return;
        }

        if (micReadyFromGateTap) {
            await refreshMicPermissionState();
            if (skipMicCheck) {
                markMicOk();
                setPhase('story');
                setAgentThinking(true);
                sendClientReady();
            }
            return;
        }

        if (skipMicCheck) {
            try {
                if (attemptedMicOnGateTap) {
                    return;
                }
                await startListening({ deviceId: storedDeviceId });
                await refreshMicPermissionState();
                await refreshMicDevices(storedDeviceId);
                markMicOk();
                setPhase('story');
                setAgentThinking(true);
                sendClientReady();
                return;
            } catch (e) {
                console.error('Microphone warm start failed:', e);
                stopListening();
                clearMicOk();
                const describedError = describeMicSetupError(e);
                setMicPermissionState(describedError.permissionState);
                setMicCheckError(describedError.message);
            }
        }

        await refreshMicPermissionState();
        await refreshMicDevices(selectedMicDeviceIdRef.current || storedDeviceId);
    }, [clearMicOk, clearPageReadAloudCache, getStoredMicDeviceId, hasStoredMicOk, isCompact, isLandscapePhone, markMicOk, primeAudio, refreshMicDevices, refreshMicPermissionState, requestMicSetupTest, sendClientReady, setNarrationMuted, startListening, stopListening]);

    const handleStorybookMoviePacingChange = useCallback((nextMode: StorybookMoviePacing) => {
        storybookMoviePacingRef.current = nextMode;
        setStorybookMoviePacing(nextMode);
        try {
            localStorage.setItem('storyteller_storybook_movie_pacing', nextMode);
        } catch {
            // Ignore storage failures.
        }
        if (phaseRef.current !== 'gate') {
            sendClientReadyRef.current();
        }
    }, []);

    const handleStoryReaderVoiceChange = useCallback((nextVoiceId: string) => {
        const normalized = normalizeStoryReaderVoiceId(nextVoiceId);
        storyReaderVoiceIdRef.current = normalized;
        setStoryReaderVoiceId(normalized);
        clearPageReadAloudCache();
        try {
            localStorage.setItem('storyteller_story_reader_voice_id', normalized);
        } catch {
            // Ignore storage failures.
        }
        if (phaseRef.current !== 'gate') {
            sendClientReadyRef.current();
        }
    }, [clearPageReadAloudCache]);

    // Orb is display-only: turn boundaries are detected automatically from voice activity.

    const beginEndingStory = useCallback((options: EndingStoryOptions = {}) => {
        const {
            notifyBackend = true,
            message = 'Making your storybook movie…',
            etaSeconds = 90,
            kind = 'initial',
            startedAtMs,
        } = options;
        if (notifyBackend && isEndingStory) return;
        setIsEndingStory(true);
        setStoryPhase(kind === 'remake' ? 'remake' : 'assembling_movie');
        setAssemblyRecentActivities([]);
        setStorybookStatus((current) => {
            const requestedStartedAtMs = Number.isFinite(startedAtMs) && (startedAtMs ?? 0) > 0
                ? startedAtMs as number
                : null;
            const preservedStartedAtMs = current && current.kind === kind ? current.startedAtMs : null;
            return {
                message,
                etaSeconds,
                storyTitle: storybookTitle ?? current?.storyTitle ?? undefined,
                childName: storybookChildName ?? current?.childName ?? undefined,
                kind,
                startedAtMs: requestedStartedAtMs ?? preservedStartedAtMs ?? Date.now(),
            };
        });
        setAgentThinking(true);
        flushPlaybackBufferRef.current();
        if (notifyBackend) {
            sendJson({
                type: 'end_story',
                session_id: sessionId,
                payload: {
                    storybook_elevenlabs_voice_id: storyReaderVoiceIdRef.current,
                },
            });
        }
    }, [isEndingStory, sendJson, sessionId, storybookChildName, storybookTitle]);
    beginEndingStoryRef.current = beginEndingStory;

    const handleEndStory = useCallback(() => {
        if (isEndingStory) {
            return;
        }
        setShowEndStoryConfirm(true);
    }, [isEndingStory]);

    const cancelEndStory = useCallback(() => {
        setShowEndStoryConfirm(false);
    }, []);

    const confirmEndStoryMovie = useCallback(() => {
        setShowEndStoryConfirm(false);
        beginEndingStory({ notifyBackend: true });
    }, [beginEndingStory]);

    const confirmEndStoryRestart = useCallback(() => {
        setShowEndStoryConfirm(false);
        restartStoryNowRef.current();
    }, []);

    const closeToyShareOverlay = useCallback((options: { notifyBackend?: boolean } = {}) => {
        const { notifyBackend = true } = options;
        if (spyglassStream) {
            spyglassStream.getTracks().forEach((t) => t.stop());
            setSpyglassStream(null);
        }
        setToyShareOverlayOpen(false);
        setToyShareCameraStarting(false);
        setToyShareCameraError(null);
        replaceToySharePreview(null);
        if (notifyBackend && sessionId) {
            sendJson({ type: 'toy_share_end', session_id: sessionId, payload: {} });
        }
    }, [replaceToySharePreview, sendJson, sessionId, spyglassStream]);
    closeToyShareOverlayRef.current = closeToyShareOverlay;

    // ── Optional camera: open preview (user sees camera, then taps Take photo) ───
    const handleSpyglass = useCallback(async () => {
        if (spyglassStream) return; // already open
        setToyShareCameraStarting(true);
        setToyShareCameraError(null);
        try {
            const stream = await navigator.mediaDevices.getUserMedia({
                video: {
                    facingMode: { ideal: 'environment' },
                    width: { ideal: 1280 },
                    height: { ideal: 720 },
                },
            });
            setSpyglassStream(stream);
        } catch (e) {
            console.warn('Optional camera failed:', e);
            setToyShareCameraError('Camera not available right now. You can still pick a picture.');
        } finally {
            setToyShareCameraStarting(false);
        }
    }, [spyglassStream]);

    const openToyShareOverlay = useCallback(async (options: ToyShareOverlayOptions = {}) => {
        const { notifyBackend = true, autoStartCamera = true } = options;
        if (toyShareState === 'uploading') return;
        if (!sessionId) return;
        if (!isToyShareAllowedForState(phase, storyPhase, isEndingStory, storybookStatus)) {
            return;
        }
        setToyShareOverlayOpen(true);
        setToyShareCameraError(null);
        if (notifyBackend && sessionId) {
            sendJson({ type: 'toy_share_start', session_id: sessionId, payload: {} });
        }
        if (autoStartCamera) {
            await handleSpyglass();
        }
    }, [handleSpyglass, isEndingStory, phase, sendJson, sessionId, storyPhase, storybookStatus, toyShareState]);
    openToyShareOverlayRef.current = openToyShareOverlay;

    const openSceneBranchPicker = useCallback((options: SceneBranchPickerOptions = {}) => {
        if (!ENABLE_SCENE_BRANCH_UI) {
            return;
        }
        const {
            selectedSceneNumber: requestedSceneNumber = null,
        } = options;
        const warningText = typeof options.warning === 'string' && options.warning.trim()
            ? options.warning
            : 'Going back will remove the pages after that scene.';
        setSceneBranchWarning(warningText);
        setSelectedSceneNumber(requestedSceneNumber);
        setShowSceneBranchPicker(true);
    }, []);
    openSceneBranchPickerRef.current = openSceneBranchPicker;

    const closeSceneBranchPicker = useCallback(() => {
        setShowSceneBranchPicker(false);
        setSelectedSceneNumber(null);
        setSceneBranchWarning('Going back will remove the pages after that scene.');
    }, []);
    closeSceneBranchPickerRef.current = closeSceneBranchPicker;

    useEffect(() => {
        if (!ENABLE_SCENE_BRANCH_UI && showSceneBranchPicker) {
            closeSceneBranchPicker();
        }
    }, [closeSceneBranchPicker, showSceneBranchPicker]);

    const handleRestartStory = useCallback(() => {
        setShowRestartConfirm(true);
    }, []);

    const restartStoryNow = useCallback(() => {
        interruptPlaybackRef.current();
        stopPageReadAloudRef.current({ resumeMic: false });
        stopStoryReaderVoicePreviewRef.current();
        if (phaseRef.current === 'theater' && sessionIdRef.current) {
            applyTheaterLightingStage('close');
            sendJson({
                type: 'theater_close',
                session_id: sessionIdRef.current,
                payload: {},
            });
        }
        clearPersistedStorybookAssemblyState(sessionIdRef.current);
        clearMicOk();
        sessionStorage.removeItem('storyteller_session_id');
        window.location.reload();
    }, [applyTheaterLightingStage, clearMicOk, sendJson]);
    restartStoryNowRef.current = restartStoryNow;

    const confirmRestart = useCallback(() => {
        restartStoryNow();
    }, [restartStoryNow]);

    const confirmSceneBranch = useCallback(() => {
        if (!selectedSceneNumber || !sessionId) {
            return;
        }
        sendJson({
            type: 'branch_to_scene' as any,
            session_id: sessionId,
            payload: {
                scene_number: selectedSceneNumber,
                source: 'button',
            },
        });
    }, [selectedSceneNumber, sendJson, sessionId]);

    const handleSceneRewind = useCallback(() => {
        if (!sessionId) {
            return;
        }
        if (sceneHistory.length > 1) {
            openSceneBranchPicker({
                selectedSceneNumber: sceneHistory[sceneHistory.length - 2]?.scene_number ?? null,
            });
            return;
        }
        sendJson({
            type: 'rewind' as any,
            session_id: sessionId,
            payload: {
                source: 'button',
            },
        });
    }, [openSceneBranchPicker, sceneHistory, sendJson, sessionId]);

    const cancelRestart = useCallback(() => {
        setShowRestartConfirm(false);
    }, []);

    const setMicEnabled = useCallback((enabled: boolean) => {
        setIsMicMuted(!enabled);
    }, []);
    setMicEnabledRef.current = setMicEnabled;

    const toggleMic = useCallback(() => {
        setMicEnabled(isMicMuted);
    }, [isMicMuted, setMicEnabled]);

    useEffect(() => {
        if (phase !== 'story') {
            return;
        }

        // Manual mute/unmute should always control the actual capture stream.
        // The old logic only restarted the mic before Amelia had spoken once,
        // which left later unmute actions visually "on" but functionally dead.
        if (isMicMuted) {
            stopListening();
            return;
        }

        void startListening();
    }, [phase, isMicMuted, stopListening, startListening]);

    useEffect(() => {
        if (phase === 'story') {
            return;
        }
        if (toyShareOverlayOpen) {
            closeToyShareOverlay({ notifyBackend: false });
        }
        if (showSceneBranchPicker) {
            closeSceneBranchPicker();
        }
    }, [closeSceneBranchPicker, closeToyShareOverlay, phase, showSceneBranchPicker, toyShareOverlayOpen]);

    useEffect(() => {
        const assemblyActive =
            phase !== 'theater'
            && (
                storyPhase === 'assembling_movie'
                || storyPhase === 'remake'
                || storyPhase === 'ending_story'
                || Boolean(storybookStatus)
            );
        if (!assemblyActive && !isEndingStory) {
            return;
        }
        if (toyShareOverlayOpen) {
            closeToyShareOverlay({ notifyBackend: false });
        }
        if (showSceneBranchPicker) {
            closeSceneBranchPicker();
        }
    }, [
        closeSceneBranchPicker,
        closeToyShareOverlay,
        isEndingStory,
        phase,
        showSceneBranchPicker,
        storyPhase,
        storybookStatus,
        toyShareOverlayOpen,
    ]);

    const handleSpyglassCancel = useCallback(() => {
        closeToyShareOverlay({ notifyBackend: true });
    }, [closeToyShareOverlay]);

    const handleSpyglassCapture = useCallback(async () => {
        const video = spyglassVideoRef.current;
        if (!video || !spyglassStream || spyglassCapturing) return;
        setSpyglassCapturing(true);
        setToyShareState('uploading');
        try {
            const canvas = document.createElement('canvas');
            canvas.width = 640;
            canvas.height = 480;
            const ctx = canvas.getContext('2d')!;
            ctx.drawImage(video, 0, 0);

            const blob = await new Promise<Blob>((res) => canvas.toBlob((b) => res(b!), 'image/jpeg', 0.8));
            replaceToySharePreview(blob);
            const formData = new FormData();
            formData.append('file', blob, 'spyglass.jpg');
            formData.append('session_id', sessionId);
            const resp = await fetch(uploadUrlRef.current, { method: 'POST', body: formData });
            const payload = await resp.json();
            if (!resp.ok || !payload?.gcs_url) {
                throw new Error(payload?.error || 'Upload failed');
            }
            const { gcs_url } = payload;
            sendJson({ type: 'spyglass_image', session_id: sessionId, payload: { gcs_url } });
            setToyShareState('success');
        } catch (e) {
            console.warn('Optional camera capture failed:', e);
            setToyShareState('error');
        } finally {
            setSpyglassCapturing(false);
            scheduleToyShareReset(5000);
        }
    }, [replaceToySharePreview, scheduleToyShareReset, sendJson, sessionId, spyglassCapturing, spyglassStream]);

    const handleToyPickerOpen = useCallback(() => {
        void openToyShareOverlay({ notifyBackend: true, autoStartCamera: true });
    }, [openToyShareOverlay]);

    const handleToyFileBrowserOpen = useCallback(() => {
        if (toyShareState === 'uploading') return;
        toyUploadInputRef.current?.click();
    }, [toyShareState]);

    const handleToyFileSelected = useCallback(async (event: React.ChangeEvent<HTMLInputElement>) => {
        const file = event.target.files?.[0];
        event.target.value = '';
        if (!file) return;
        if (!file.type.startsWith('image/')) {
            setToyShareState('error');
            scheduleToyShareReset();
            return;
        }

        setToyShareState('uploading');
        try {
            replaceToySharePreview(file);
            const formData = new FormData();
            formData.append('file', file, file.name || 'toy.jpg');
            formData.append('session_id', sessionId);

            const resp = await fetch(uploadUrlRef.current, { method: 'POST', body: formData });
            const payload = await resp.json();
            if (!resp.ok || !payload?.gcs_url) {
                throw new Error(payload?.error || 'Upload failed');
            }

            sendJson({ type: 'spyglass_image', session_id: sessionId, payload: { gcs_url: payload.gcs_url } });
            setToyShareState('success');
        } catch (error) {
            console.warn('Toy upload failed:', error);
            setToyShareState('error');
        } finally {
            setToyShareOverlayOpen(true);
            scheduleToyShareReset(5000);
        }
    }, [replaceToySharePreview, scheduleToyShareReset, sendJson, sessionId]);

    const handleMovieFeedbackSubmit = useCallback((payload: {
        rating: 'loved_it' | 'pretty_good' | 'needs_fixing';
        reasons: string[];
        note: string;
    }) => {
        if (!sessionId) {
            return;
        }
        sendJson({
            type: 'movie_feedback',
            session_id: sessionId,
            payload: {
                rating: payload.rating,
                reasons: payload.reasons,
                note: payload.note,
            },
        });
    }, [sendJson, sessionId]);

    const handleMovieRemakeRequest = useCallback((payload: {
        rating: 'loved_it' | 'pretty_good' | 'needs_fixing';
        reasons: string[];
        note: string;
    }) => {
        if (!sessionId) {
            return;
        }
        applyTheaterLightingStage('close');
        setFinalMovieUrl(null);
        setStorybookLightingCues([]);
        setPhase('story');
        setStoryPhase('remake');
        setIsEndingStory(true);
        setAssemblyRecentActivities([]);
        setStorybookStatus({
            message: 'Polishing a better version of your movie…',
            etaSeconds: 45,
            storyTitle: storybookTitle ?? undefined,
            childName: storybookChildName ?? undefined,
            kind: 'remake',
            startedAtMs: Date.now(),
        });
        sendJson({
            type: 'movie_remake',
            session_id: sessionId,
            payload: {
                rating: payload.rating,
                reasons: payload.reasons,
                note: payload.note,
                storybook_elevenlabs_voice_id: storyReaderVoiceIdRef.current,
            },
        });
    }, [applyTheaterLightingStage, sendJson, sessionId, storybookChildName, storybookTitle]);

    const handleAssemblyMissionSelect = useCallback((option: AssemblyMissionOption) => {
        if (!sessionId || !storybookStatus) {
            return;
        }
        setAgentThinking(true);
        setAssemblyRecentActivities((current) => rememberAssemblyActivity(current, option.key));
        sendJson({
            type: 'assembly_play_prompt' as any,
            session_id: sessionId,
            payload: {
                activity: option.key,
                label: option.label,
            },
        });
    }, [sendJson, sessionId, storybookStatus]);

    // ── Theater close ────────────────────────────────────────────────────────────
    const handleTheaterClose = useCallback(() => {
        applyTheaterLightingStage('close');
        clearPersistedStorybookAssemblyState(sessionIdRef.current);
        sendJson({ type: 'theater_close', session_id: sessionId, payload: {} });
        setPhase('story');
        setStoryPhase('waiting_for_child');
        setFinalMovieUrl(null);
        setStorybookNarration(null);
        setStorybookAudioAvailable(null);
        setStorybookLightingCues([]);
        void startListening();
    }, [applyTheaterLightingStage, sendJson, sessionId, startListening]);

    useEffect(() => {
        if (phase === 'theater') {
            stopListening();
        }
    }, [phase, stopListening]);

    // ── Render ───────────────────────────────────────────────────────────────────
    if (phase === 'gate') {
        return <ParentGate onApproved={handleGateApproved} />;
    }

    if (phase === 'mic-check') {
        const compactMicSetup = isCompact || isLandscapePhone;
        const micLevel = audioState === 'listening'
            ? Math.min(1, Math.max(0, (voiceRms - 0.002) / 0.02))
            : 0;
        const micCheckReady = micPermissionState === 'granted'
            && audioState === 'listening'
            && micSetupHeardVoice;
        const permissionLabel = micCheckReady
            ? 'Mic ready'
            : micPermissionState === 'granted'
                ? 'Mic allowed'
                : micPermissionState === 'denied'
                    ? 'Mic blocked'
                    : 'Permission needed';
        const permissionCopy = micCheckReady
            ? 'We heard you. The start button is unlocked, so you can begin the story now.'
            : micPermissionState === 'granted'
                ? 'Great. Say “Hi Amelia!” and watch the level bar move.'
                : micPermissionState === 'denied'
                    ? 'Your browser blocked microphone access. Reopen the mic prompt or site settings, then try again.'
                    : 'Tap the button below. Your browser will still show the microphone prompt, and you should choose Allow.';
        const micSetupTitle = compactMicSetup
            ? 'Check the microphone'
            : 'Check the microphone like a meeting lobby';
        const micSetupSubtitle = compactMicSetup
            ? 'Allow the mic, say “Hi Amelia!”, and then start the story.'
            : 'We can’t bypass the browser microphone prompt with AI or the model. The browser must ask first, but we can make that step clear and easy.';
        return (
            <main className="storyteller-main mic-check-stage" aria-label="Microphone check">
                <div className="mic-setup-card" role="status" aria-live="polite">
                    <section className="mic-setup-copy-column">
                        <div className="mic-check-icon" aria-hidden="true">🎙️</div>
                        <div className="mic-setup-eyebrow">Before Amelia joins</div>
                        <div className="mic-check-title">{micSetupTitle}</div>
                        <div className="mic-check-subtitle">{micSetupSubtitle}</div>
                        {compactMicSetup ? (
                            <details className="mic-setup-help">
                                <summary className="mic-setup-help-summary">Show the full steps</summary>
                                <ol className="mic-setup-steps mic-setup-steps-compact">
                                    <li>Tap <strong>Allow &amp; test microphone</strong>.</li>
                                    <li>Choose <strong>Allow</strong> in the browser popup.</li>
                                    <li>Say <strong>&ldquo;Hi Amelia!&rdquo;</strong> and watch the bar bounce.</li>
                                    <li>Start the story when everything looks good.</li>
                                </ol>
                            </details>
                        ) : (
                            <ol className="mic-setup-steps">
                                <li>Tap <strong>Allow &amp; test microphone</strong>.</li>
                                <li>Choose <strong>Allow</strong> in the browser popup.</li>
                                <li>Say <strong>&ldquo;Hi Amelia!&rdquo;</strong> and watch the bar bounce.</li>
                                <li>Start the story when everything looks good.</li>
                            </ol>
                        )}
                        <div className={`mic-setup-permission-pill is-${micPermissionState}`}>
                            <span className="mic-setup-permission-label">{permissionLabel}</span>
                            <span className="mic-setup-permission-copy">{permissionCopy}</span>
                        </div>
                    </section>

                    <section className="mic-setup-controls-column">
                        <label className="mic-setup-field">
                            <span className="mic-setup-field-label">Microphone</span>
                            <select
                                value={selectedMicDeviceId}
                                onChange={(event) => {
                                    const nextDeviceId = event.target.value;
                                    setSelectedMicDeviceId(nextDeviceId);
                                    persistMicDeviceId(nextDeviceId);
                                    setMicSetupHeardVoice(false);
                                    if (audioState === 'listening') {
                                        void requestMicSetupTest(nextDeviceId);
                                    }
                                }}
                                disabled={micSetupBusy || availableMicDevices.length === 0}
                                aria-label="Choose a microphone"
                            >
                                {availableMicDevices.length === 0 ? (
                                    <option value="">
                                        {micPermissionState === 'granted'
                                            ? 'Using browser default microphone'
                                            : 'Allow mic access to list devices'}
                                    </option>
                                ) : (
                                    availableMicDevices.map((device, index) => (
                                        <option key={device.deviceId || `mic-${index}`} value={device.deviceId}>
                                            {device.label || `Microphone ${index + 1}`}
                                        </option>
                                    ))
                                )}
                            </select>
                        </label>

                        <div className="mic-setup-meter-card">
                            <div className="mic-setup-meter-header">
                                <span className="mic-setup-field-label">Mic level</span>
                                <span className={`mic-setup-meter-status ${micSetupHeardVoice ? 'is-good' : ''}`}>
                                    {micSetupHeardVoice
                                        ? 'We heard you'
                                        : audioState === 'listening'
                                            ? 'Listening now'
                                            : 'Waiting to test'}
                                </span>
                            </div>
                            <div className="mic-check-meter" aria-hidden="true">
                                <div className="mic-check-fill" style={{ width: `${micLevel * 100}%` }} />
                            </div>
                            <p className="mic-setup-meter-copy">
                                {audioState === 'listening'
                                    ? 'Say “Hi Amelia!” or count to three. The bar should bounce as you talk.'
                                    : 'Once the browser prompt appears, allow microphone access to begin the test.'}
                            </p>
                        </div>

                        {micCheckError && <div className="mic-check-error">{micCheckError}</div>}

                        <div className="mic-setup-actions">
                            <button
                                className="mic-setup-primary"
                                onClick={() => {
                                    // Keep this tap focused on the browser mic prompt.
                                    void requestMicSetupTest(selectedMicDeviceIdRef.current || null);
                                }}
                                disabled={micSetupBusy}
                                aria-label={audioState === 'listening' ? 'Retest microphone' : 'Allow and test microphone'}
                            >
                                {micSetupBusy
                                    ? 'Starting…'
                                    : audioState === 'listening'
                                        ? 'Retest microphone'
                                        : 'Allow & test microphone'}
                            </button>
                            <button
                                className={`mic-setup-secondary ${micCheckReady ? 'is-ready' : ''}`}
                                onClick={() => {
                                    playUiSound('tap');
                                    void completeMicCheckRef.current(micSetupHeardVoice ? 'heard' : 'skip');
                                }}
                                disabled={!micCheckReady || micSetupBusy}
                                aria-label="Start the story"
                            >
                                Start the story
                            </button>
                        </div>
                        <p className={`mic-setup-ready-hint ${micCheckReady ? 'is-visible' : ''}`} aria-live="polite">
                            {micCheckReady
                                ? 'Mic check passed. Start the story whenever you are ready.'
                                : audioState === 'listening'
                                    ? 'Say "Hi Amelia!" or count to three to unlock the start button.'
                                    : 'Run the microphone test first to unlock the story.'}
                        </p>
                    </section>
                </div>
            </main>
        );
    }

    if (phase === 'theater' && finalMovieUrl) {
        return (
            <TheaterMode
                mp4Url={finalMovieUrl}
                childName={storybookChildName ?? undefined}
                storyTitle={storybookTitle ?? undefined}
                tradingCardUrl={tradingCardUrl ?? undefined}
                narrationLines={storybookNarration ?? undefined}
                audioAvailable={storybookAudioAvailable ?? undefined}
                lightingCues={storybookLightingCues}
                calmMode={calmMode}
                uiSoundsEnabled={!calmMode}
                onSubmitFeedback={handleMovieFeedbackSubmit}
                onRequestRemake={handleMovieRemakeRequest}
                onTheaterOpened={() => applyTheaterLightingStage('open')}
                onPlaybackStart={() => applyTheaterLightingStage('play')}
                onPlaybackPause={() => applyTheaterLightingStage('pause')}
                onPlaybackEnded={() => applyTheaterLightingStage('end')}
                onLightingCueChange={applyTheaterLightingCue}
                onMakeAnotherStory={restartStoryNow}
                onClose={handleTheaterClose}
            />
        );
    }

    const isStorybookAssembling =
        phase !== 'theater'
        && (
            storyPhase === 'assembling_movie'
            || storyPhase === 'remake'
            || storyPhase === 'ending_story'
            || Boolean(storybookStatus)
        );
    const isListening = audioState === 'listening' || userSpeaking;
    const isSpeaking = audioState === 'speaking' && !userSpeaking;
    const isThinking = agentThinking && !isSpeaking && !userSpeaking;
    const useCompactLayout = isCompact || isLandscapePhone;
    const showBackground = !useCompactLayout;
    const hasScene = Boolean(currentSceneImageUrl || currentSceneVideoUrl);
    const currentSceneCaptionText = normalizeStoryCaptionText(currentSceneStorybeatText);
    const shouldRenderStorybookPanel = hasScene || sceneLoading || Boolean(sceneError);
    const isStorybookPanelWaitingForMedia = !currentSceneImageUrl && !currentSceneVideoUrl;
    const isStarting = phase === 'story' && (storyPhase === 'opening' || !hasHeardAgent);
    const isBuffering = audioState === 'buffering';
    const hearingActive = !isMicMuted && (userSpeaking || (isListening && voiceRms > 0.006));
    const mirrorMode: MagicMirrorMode = isSpeaking
        ? 'speaking'
        : isThinking
            ? 'thinking'
            : userSpeaking
                ? 'user-speaking'
                : 'idle';
    const sceneBranchReady = ENABLE_SCENE_BRANCH_UI
        && phase === 'story'
        && !isEndingStory
        && !isStorybookAssembling
        && !sceneLoading
        && sceneHistory.length > 1;
    const rewindButtonReady = ENABLE_SCENE_BRANCH_UI
        && phase === 'story'
        && !isEndingStory
        && !isStorybookAssembling
        && !sceneLoading
        && Boolean(sessionId)
        && (sceneBranchReady || hasScene);
    const canReadCurrentPage = Boolean(
        currentSceneCaptionText
        && phase === 'story'
        && !isStorybookAssembling
        && !sceneError
    );
    const hasCommittedSceneVisual = Boolean(
        currentSceneVideoUrl
        || (
            currentSceneImageUrl
            && !currentSceneImageUrl.startsWith('data:image/svg+xml')
        )
    );
    const pageReadAloudBusy = isSpeaking || isThinking || userSpeaking || (sceneLoading && !hasCommittedSceneVisual);
    const captionTokens = currentSceneCaptionText
        ? tokenizeStoryCaption(currentSceneCaptionText)
        : [];
    const toySharingReady = isToyShareAllowedForState(phase, storyPhase, isEndingStory, storybookStatus) && Boolean(sessionId);
    const toyShareLabel = toyShareState === 'uploading'
        ? 'Adding Toy...'
        : toyShareState === 'success'
            ? 'Toy Added'
            : toyShareState === 'error'
                ? 'Try Again'
                : 'Share Toy / Pic';
    const toyShareHelperText = toyShareCameraError
        ? toyShareCameraError
        : toyShareState === 'uploading'
            ? 'Amelia is taking a peek at your toy. Keep talking so she can learn about it.'
            : toyShareState === 'success'
                ? 'Amelia saw it. Tell her its name, what it loves, or show another side.'
                : spyglassStream
                    ? 'Hold your toy in the camera window and chat with Amelia about what makes it special.'
                    : toyShareCameraStarting
                        ? 'Opening the toy camera...'
                        : 'Amelia wants to meet your toy. Turn on the camera or pick a picture.';
    const toyShareLiveStatus = userSpeaking
        ? 'Amelia can hear you talking about your toy.'
        : isSpeaking
            ? 'Amelia is talking about your toy.'
            : isThinking
                ? 'Amelia is peeking and thinking.'
                : 'Talk to Amelia while you show your toy.';

    const isYoungChildMode = childAge <= 5;
    const showInlineHudControls = !isYoungChildMode;
    const selectedScene = sceneHistory.find((scene) => scene.scene_number === selectedSceneNumber) ?? null;
    const currentScenePageNumber = sceneHistory.length
        ? sceneHistory[sceneHistory.length - 1]?.scene_number ?? null
        : null;
    const etaSeconds = storybookStatus?.etaSeconds ?? 0;
    const etaLabel = etaSeconds ? `About ${Math.ceil(etaSeconds / 30) * 30} seconds` : null;
    const assemblingMilestones = storybookStatus?.kind === 'remake'
        ? ['Reading feedback', 'Polishing pages', 'Rebuilding the movie', 'Opening the curtain']
        : ['Gathering the pages', 'Adding music and sparkle', 'Smoothing the camera', 'Opening the curtain'];
    const assemblingProgress = etaSeconds
        ? Math.min(0.96, storybookWaitElapsedSeconds / etaSeconds)
        : Math.min(0.9, storybookWaitElapsedSeconds / Math.max(assemblingMilestones.length * 6, 1));
    const assemblingStepIndex = Math.min(
        assemblingMilestones.length - 1,
        Math.floor(assemblingProgress * assemblingMilestones.length)
    );
    const activeAssemblyMission = pickAssemblyMission(storybookWaitElapsedSeconds, assemblyRecentActivities);
    const assemblyMissionBusy = isSpeaking || agentThinking;
    const assemblyHelperText = connectionState === 'connected'
        ? 'Amelia can still chat while the movie gets ready.'
        : connectionState === 'reconnecting'
            ? 'Amelia is reconnecting so she can keep chatting.'
            : 'Amelia is getting her voice link ready.';
    const connectionBadgeText = connectionState === 'reconnecting'
        ? (isStorybookAssembling ? '🔄 Reconnecting to Amelia...' : '🔄 Reconnecting...')
        : (isStorybookAssembling ? '⚡ Connecting Amelia...' : '⚡ Connecting...');
    const orbStatusText = isStorybookAssembling
        ? isSpeaking
            ? 'Amelia is chatting while the movie is being made'
            : isThinking
                ? 'Amelia is polishing the movie'
                : hearingActive
                    ? 'Amelia can hear you while the movie is being made'
                    : 'Pick a magic mission or talk to Amelia'
        : isStarting
        ? 'Amelia is opening the storybook'
        : userTranscript?.text
            ? 'Amelia hears you'
            : displayedAgentText
                ? 'Amelia is speaking'
                : isThinking
                    ? sceneLoading
                        ? 'Amelia is drawing the next page'
                        : 'Amelia is thinking'
                    : hearingActive
                        ? 'Amelia hears you'
                        : isBuffering
                            ? 'Microphone is getting ready'
                            : isListening
                                ? 'Listening'
                                : 'Talk to Amelia';

    return (
        <main
            className={`storyteller-main ${calmMode ? 'calm-mode' : ''} ${(currentSceneImageUrl || currentSceneVideoUrl) ? 'has-scene' : ''} ${isStorybookAssembling ? 'is-assembling' : ''} ${isLandscapePhone ? 'is-mobile-landscape' : ''}`}
            aria-label="Interactive Storytelling Experience"
        >
            {/* Scene background (Immersive Layer) */}
            {showBackground && !currentSceneImageUrl && currentSceneVideoUrl && (
                <video
                    key={currentSceneVideoUrl}
                    src={currentSceneVideoUrl}
                    autoPlay
                    loop
                    muted={calmMode}
                    playsInline
                    className="scene-video-bg"
                    aria-hidden="true"
                />
            )}
            {showBackground && currentSceneThumbnailB64 && (
                <img
                    key={`thumb-${currentSceneThumbnailB64}`}
                    src={`data:image/jpeg;base64,${currentSceneThumbnailB64}`}
                    className="scene-image-bg"
                    style={{ filter: 'blur(30px)', transform: 'scale(1.1)' }}
                    alt=""
                    aria-hidden="true"
                />
            )}
            {showBackground && currentSceneImageUrl && (
                <img
                    key={currentSceneImageUrl}
                    src={currentSceneImageUrl}
                    className="scene-image-bg"
                    alt=""
                    aria-hidden="true"
                />
            )}

            {/* Magic Mirror WebGL visualizer (Center Layer) */}
            <div
                className={`magic-mirror-container mirror-${mirrorMode}`}
                aria-hidden="true"
                style={{ zIndex: 10 }}
            >
                <div className="magic-mirror-shell" />
                <MagicMirror voiceRms={voiceRms} mode={mirrorMode} />
                {agentThinking && (
                    <div className="magic-sparkles">
                        {Array.from({ length: 12 }).map((_, i) => (
                            <div
                                key={i}
                                className="sparkle"
                                style={{
                                    left: `${Math.random() * 100}%`,
                                    top: `${Math.random() * 100}%`,
                                    '--tx': `${(Math.random() - 0.5) * 100}px`,
                                    '--ty': `${(Math.random() - 0.5) * 100}px`,
                                    animationDelay: `${Math.random() * 2}s`
                                } as any}
                            />
                        ))}
                    </div>
                )}
            </div>

            {/* Storybook panel: scene + narration aligned like a page */}
            {shouldRenderStorybookPanel && (
                <section
                    className={`storybook-panel ${isStorybookPanelWaitingForMedia ? 'is-loading-panel' : ''}`}
                    aria-live="polite"
                >
                    <div className="storybook-page-shell">
                        <div className="storybook-page">
                            <div className="storybook-illustration-frame">
                                {isStorybookPanelWaitingForMedia && (
                                    <div className="storybook-loading-backdrop" aria-hidden="true" />
                                )}
                                {currentSceneThumbnailB64 && (
                                    <img
                                        src={`data:image/jpeg;base64,${currentSceneThumbnailB64}`}
                                        alt=""
                                        className="storybook-media"
                                        style={{ filter: 'blur(30px)', position: 'absolute', top: 0, left: 0, zIndex: 0, width: '100%', height: '100%', objectFit: 'cover' }}
                                        aria-hidden="true"
                                    />
                                )}
                                {currentSceneImageUrl && (
                                    <img
                                        src={currentSceneImageUrl}
                                        alt="Story scene illustration"
                                        className={`storybook-media ${isNewScene ? 'arriving' : ''}`}
                                        style={{ position: 'relative', zIndex: 1 }}
                                        loading="eager"
                                        decoding="async"
                                        onLoad={() => {
                                            if (!currentSceneImageUrl?.startsWith('data:image/svg+xml')) {
                                                setSceneLoading(false);
                                                setSceneError(null);
                                            }
                                        }}
                                        onError={() => {
                                            if (!currentSceneImageUrl?.startsWith('data:image/svg+xml')) {
                                                setSceneLoading(false);
                                                setSceneError('Picture unavailable right now.');
                                            }
                                        }}
                                        onClick={() => {
                                            setZoomedImageUrl(currentSceneImageUrl);
                                        }}
                                    />
                                )}
                                {!currentSceneImageUrl && currentSceneVideoUrl && (
                                    <video
                                        src={currentSceneVideoUrl}
                                        autoPlay
                                        loop
                                        muted={calmMode}
                                        playsInline
                                        className="storybook-media"
                                        aria-hidden="true"
                                    />
                                )}
                                {sceneLoading && (
                                    <div className="storybook-loading" role="status" aria-live="polite">
                                        <div className="storybook-loading-stage" aria-hidden="true">
                                            <div className="loading-comet">
                                                <div className="loading-ribbon" />
                                                <div className="loading-wand" />
                                            </div>
                                            <div className="loading-sparkle sparkle-1" />
                                            <div className="loading-sparkle sparkle-2" />
                                            <div className="loading-sparkle sparkle-3" />
                                            <div className="loading-orbit">
                                                <div className="loading-orb orb-1" />
                                                <div className="loading-orb orb-2" />
                                                <div className="loading-orb orb-3" />
                                            </div>
                                            <div className="loading-buddy">
                                                <span className="buddy-eye left" />
                                                <span className="buddy-eye right" />
                                                <span className="buddy-mouth" />
                                            </div>
                                        </div>
                                        <div className="loading-dots" aria-hidden="true">
                                            <span />
                                            <span />
                                            <span />
                                        </div>
                                        <div className="storybook-loading-copy" aria-hidden="true">
                                            <strong>Amelia is drawing the next page.</strong>
                                            <span>Keep talking while the next picture catches up.</span>
                                        </div>
                                        <span className="sr-only">Amelia is drawing the picture.</span>
                                    </div>
                                )}
                                {sceneError && !sceneLoading && (
                                    <div className="storybook-error" role="status" aria-live="polite">
                                        {sceneError}
                                    </div>
                                )}
                            </div>
                        </div>
                        {currentSceneCaptionText && !sceneError && (
                            <div className="storybook-reading-strip" role="status" aria-live="polite">
                                <div className="storybook-reading-meta">
                                    {currentScenePageNumber && (
                                        <span className="storybook-page-chip">Page {currentScenePageNumber}</span>
                                    )}
                                    {canReadCurrentPage && (
                                        <button
                                            type="button"
                                            className={`storybook-read-btn ${isPageReadAloudActive ? 'is-active' : ''}`}
                                            onClick={() => {
                                                playUiSound(isPageReadAloudActive ? 'close' : 'tap');
                                                void readCurrentPageAloud();
                                            }}
                                            disabled={!isPageReadAloudActive && pageReadAloudBusy}
                                            aria-pressed={isPageReadAloudActive}
                                            aria-label={isPageReadAloudActive ? 'Stop reading this page aloud' : 'Read this page aloud'}
                                        >
                                            {isPageReadAloudActive ? '⏹ Stop reading' : '🔊 Read page'}
                                        </button>
                                    )}
                                </div>
                                {pageReadAloudError && (
                                    <div className="storybook-read-error" role="status" aria-live="polite">
                                        {pageReadAloudError}
                                    </div>
                                )}
                                {currentSceneCaptionText && (
                                    <div className="storybook-caption storybook-caption-readalong">
                                        {captionTokens.map((token, index) => {
                                            const isCurrentWord =
                                                isPageReadAloudActive
                                                && token.isWord
                                                && token.wordIndex === pageReadAloudHighlightWordIndex;
                                            const isReadWord =
                                                isPageReadAloudActive
                                                && token.isWord
                                                && pageReadAloudHighlightWordIndex > 0
                                                && token.wordIndex < pageReadAloudHighlightWordIndex;
                                            return (
                                                <span
                                                    key={`${index}-${token.wordIndex}-${token.text}`}
                                                    className={[
                                                        'storybook-caption-token',
                                                        token.isWord ? 'is-word' : 'is-space',
                                                        isCurrentWord ? 'is-current' : '',
                                                        isReadWord ? 'is-read' : '',
                                                    ].filter(Boolean).join(' ')}
                                                >
                                                    {token.text}
                                                </span>
                                            );
                                        })}
                                    </div>
                                )}
                            </div>
                        )}
                    </div>
                </section>
            )}

            {storybookStatus && phase !== 'theater' && (
                <div className="storybook-assembling" role="status" aria-live="polite">
                    <div className="storybook-assembling-card">
                        <div className="storybook-assembling-kicker">
                            {storybookStatus.kind === 'remake' ? 'Director Pass' : 'Premiere in Progress'}
                        </div>
                        {(storybookStatus.storyTitle || storybookTitle) && (
                            <div className="storybook-assembling-title">
                                {storybookStatus.storyTitle || storybookTitle}
                            </div>
                        )}
                        {(storybookStatus.childName || storybookChildName) && (
                            <div className="storybook-assembling-byline">
                                Made for {storybookStatus.childName || storybookChildName}
                            </div>
                        )}
                        <div className="storybook-assembling-stage">
                            <div className="storybook-assembling-spinner" aria-hidden="true" />
                            {currentSceneImageUrl && (
                                <img
                                    src={currentSceneImageUrl}
                                    alt=""
                                    className="storybook-assembling-preview"
                                    aria-hidden="true"
                                />
                            )}
                            <div className="storybook-assembling-copy">
                                <div className="storybook-assembling-text">{storybookStatus.message}</div>
                                {etaLabel && <div className="storybook-assembling-eta">{etaLabel}</div>}
                            </div>
                        </div>
                        <div className="storybook-assembling-progress" aria-hidden="true">
                            <div
                                className="storybook-assembling-progress-fill"
                                style={{ width: `${Math.max(10, Math.round(assemblingProgress * 100))}%` }}
                            />
                        </div>
                        <div className="storybook-assembling-steps">
                            {assemblingMilestones.map((step, index) => (
                                <div
                                    key={step}
                                    className={`storybook-assembling-step ${index <= assemblingStepIndex ? 'active' : ''}`}
                                >
                                    {step}
                                </div>
                            ))}
                        </div>
                        <div className="storybook-assembling-helper">
                            {assemblyHelperText}
                        </div>
                        <div className="storybook-assembling-mission">
                            <div className="storybook-assembling-mission-kicker">
                                {activeAssemblyMission.kicker}
                            </div>
                            <div className="storybook-assembling-mission-title">
                                {activeAssemblyMission.title}
                            </div>
                            <div className="storybook-assembling-mission-copy">
                                {activeAssemblyMission.helper}
                            </div>
                            <div className="storybook-assembling-mission-actions">
                                {activeAssemblyMission.options.map((option) => (
                                    <button
                                        key={option.key}
                                        type="button"
                                        className="storybook-assembling-mission-button"
                                        disabled={assemblyMissionBusy}
                                        onClick={() => handleAssemblyMissionSelect(option)}
                                    >
                                        {option.label}
                                    </button>
                                ))}
                            </div>
                        </div>
                    </div>
                </div>
            )}

            {/* HUD: Controls & Status (Top Layer) */}

            {/* Top Left: Toy Show & Tell */}
            <div className="hud-top-left">
                {toySharingReady && (
                    <button
                        className={`magic-btn magic-btn-cyan ${toyShareState === 'success' ? 'is-success' : ''} ${toyShareState === 'error' ? 'is-error' : ''}`}
                        onClick={() => {
                            playUiSound('tap');
                            handleToyPickerOpen();
                        }}
                        disabled={toyShareState === 'uploading'}
                        aria-label="Open toy show-and-tell and share a toy or picture"
                    >
                        🧸 {toyShareLabel}
                    </button>
                )}
            </div>

            {/* Top Right: End Story */}
            <div className="hud-top-right">
                {showInlineHudControls ? (
                    !isStorybookAssembling && (
                        <button
                            className="magic-btn magic-btn-gold"
                            onClick={() => {
                                playUiSound('celebrate');
                                handleEndStory();
                            }}
                            disabled={isEndingStory}
                            aria-label="End the story and make the storybook movie"
                            aria-busy={isEndingStory}
                        >
                            🌟 {isEndingStory ? 'Magic...' : 'The End'}
                        </button>
                    )
                ) : (
                    <div className={`adult-tools-tray ${showParentControls ? 'is-open' : ''}`}>
                        <button
                            className="magic-btn magic-btn-secondary adult-tools-trigger"
                            onClick={() => {
                                playUiSound(showParentControls ? 'close' : 'tap');
                                setShowParentControls((current) => !current);
                            }}
                            aria-expanded={showParentControls}
                            aria-haspopup="dialog"
                            aria-label={showParentControls ? 'Hide grown-up controls' : 'Show grown-up controls'}
                        >
                            🔒 Grown-Ups
                        </button>
                        {showParentControls && (
                            <div className="adult-tools-panel" role="dialog" aria-label="Grown-up controls">
                                <div className="adult-tools-copy">
                                    <strong>Grown-up controls</strong>
                                    <span>Keep the child screen simple while you handle the big buttons.</span>
                                </div>
                                <div className="adult-tools-setting">
                                    <label className="adult-tools-setting-label" htmlFor="adult-storybook-movie-pacing">
                                        Final movie pace
                                    </label>
                                    <select
                                        id="adult-storybook-movie-pacing"
                                        className="adult-tools-setting-select"
                                        value={storybookMoviePacing}
                                        disabled={isEndingStory}
                                        onChange={(event) => {
                                            playUiSound('tap');
                                            handleStorybookMoviePacingChange(event.target.value as StorybookMoviePacing);
                                        }}
                                    >
                                        <option value="read_to_me">Read to Me</option>
                                        <option value="read_with_me">Read with Me</option>
                                        <option value="fast_movie">Fast Movie</option>
                                    </select>
                                    <span className="adult-tools-setting-copy">
                                        {storybookMoviePacingHelperCopy(storybookMoviePacing)}
                                    </span>
                                </div>
                                <div className="adult-tools-setting">
                                    <label className="adult-tools-setting-label" htmlFor="adult-story-reader-voice">
                                        Reader voice
                                    </label>
                                    <div className="story-reader-voice-row">
                                        <select
                                            id="adult-story-reader-voice"
                                            className="adult-tools-setting-select"
                                            value={storyReaderVoiceId}
                                            disabled={isEndingStory}
                                            onChange={(event) => {
                                                playUiSound('tap');
                                                handleStoryReaderVoiceChange(event.target.value);
                                            }}
                                        >
                                            {STORY_READER_VOICE_OPTIONS.map((voice) => (
                                                <option key={voice.id} value={voice.id}>
                                                    {voice.name}
                                                </option>
                                            ))}
                                        </select>
                                        <button
                                            type="button"
                                            className={`story-reader-voice-preview-btn ${(storyReaderVoicePreviewLoading || storyReaderVoicePreviewPlaying) && storyReaderVoicePreviewVoiceId === storyReaderVoiceId ? 'is-active' : ''}`}
                                            disabled={isEndingStory}
                                            onClick={() => {
                                                playUiSound((storyReaderVoicePreviewLoading || storyReaderVoicePreviewPlaying) && storyReaderVoicePreviewVoiceId === storyReaderVoiceId ? 'close' : 'tap');
                                                void previewStoryReaderVoice(storyReaderVoiceId);
                                            }}
                                            aria-label={(storyReaderVoicePreviewLoading || storyReaderVoicePreviewPlaying) && storyReaderVoicePreviewVoiceId === storyReaderVoiceId
                                                ? 'Stop voice preview'
                                                : `Preview ${getStoryReaderVoiceOption(storyReaderVoiceId).name} voice`}
                                            aria-pressed={(storyReaderVoicePreviewLoading || storyReaderVoicePreviewPlaying) && storyReaderVoicePreviewVoiceId === storyReaderVoiceId}
                                        >
                                            {storyReaderVoicePreviewLoading && storyReaderVoicePreviewVoiceId === storyReaderVoiceId
                                                ? 'Loading...'
                                                : storyReaderVoicePreviewPlaying && storyReaderVoicePreviewVoiceId === storyReaderVoiceId
                                                    ? 'Stop'
                                                    : 'Preview'}
                                        </button>
                                    </div>
                                    <span className="adult-tools-setting-copy">
                                        {storyReaderVoiceHelperCopy(storyReaderVoiceId)}
                                    </span>
                                    {storyReaderVoicePreviewError ? (
                                        <span className="story-reader-voice-preview-error" role="status">
                                            {storyReaderVoicePreviewError}
                                        </span>
                                    ) : null}
                                </div>
                                <div className="adult-tools-actions">
                                    {rewindButtonReady && (
                                        <button
                                            className="magic-btn magic-btn-secondary"
                                            onClick={() => {
                                                playUiSound('tap');
                                                setShowParentControls(false);
                                                handleSceneRewind();
                                            }}
                                            aria-label={sceneBranchReady
                                                ? 'Choose an earlier scene and branch the story from there'
                                                : 'Go back to the previous story moment'}
                                        >
                                            ↩ {sceneBranchReady ? 'Scenes' : 'Back'}
                                        </button>
                                    )}
                                    <button
                                        className="magic-btn"
                                        style={{ background: 'rgba(255,255,255,0.1)', border: '2px solid rgba(255,255,255,0.2)' }}
                                        onClick={() => {
                                            playUiSound('magic');
                                            setShowParentControls(false);
                                            handleRestartStory();
                                        }}
                                        aria-label="Start a new story"
                                    >
                                        ✨ Restart
                                    </button>
                                    {!isStorybookAssembling && (
                                        <button
                                            className="magic-btn magic-btn-gold"
                                            onClick={() => {
                                                playUiSound('celebrate');
                                                setShowParentControls(false);
                                                handleEndStory();
                                            }}
                                            disabled={isEndingStory}
                                            aria-label="End the story and make the storybook movie"
                                            aria-busy={isEndingStory}
                                        >
                                            🌟 {isEndingStory ? 'Magic...' : 'The End'}
                                        </button>
                                    )}
                                    <button
                                        className={`magic-btn ${isMicMuted ? 'muted' : ''}`}
                                        style={{ background: isMicMuted ? 'var(--color-error)' : 'var(--color-accent-green)' }}
                                        onClick={() => {
                                            playUiSound(isMicMuted ? 'toggle_on' : 'toggle_off');
                                            toggleMic();
                                        }}
                                        aria-label={isMicMuted ? 'Unmute microphone' : 'Mute microphone'}
                                    >
                                        {isMicMuted ? '🔇 Mic Off' : '🎤 Mic On'}
                                    </button>
                                </div>
                            </div>
                        )}
                    </div>
                )}
            </div>

            {/* Center Bottom: Interaction Orb */}
            <div
                className={`magic-orb magic-orb-display ${isListening ? 'orb-listening' : ''} ${isSpeaking ? 'orb-speaking' : ''} ${isThinking ? 'orb-thinking' : ''} ${hearingActive ? 'orb-hearing' : ''} ${isStorybookAssembling ? 'orb-premiere-mode' : ''}`}
                role="status"
                aria-live="polite"
                aria-label={orbStatusText}
            >
                <span className={`orb-icon-shell ${hearingActive ? 'is-hearing' : ''}`} aria-hidden="true">
                    <span className="orb-wave orb-wave-left" />
                    <span className="orb-icon">
                        {isStarting ? '⏳' : isBuffering ? '🎤' : isThinking ? '✨' : isSpeaking ? '🌟' : '👂'}
                    </span>
                    <span className="orb-wave orb-wave-right" />
                </span>
                <span className="sr-only">{orbStatusText}</span>
            </div>

            {showInlineHudControls && (
                <>
                    {/* Bottom Left: Start Over */}
                    <div className="hud-bottom-left">
                        <div className="hud-control-stack">
                            {rewindButtonReady && (
                                <button
                                    className="magic-btn magic-btn-secondary"
                                    onClick={() => {
                                        playUiSound('tap');
                                        handleSceneRewind();
                                    }}
                                    aria-label={sceneBranchReady
                                        ? 'Choose an earlier scene and branch the story from there'
                                        : 'Go back to the previous story moment'}
                                >
                                    ↩ {sceneBranchReady ? 'Scenes' : 'Back'}
                                </button>
                            )}
                            <button
                                className="magic-btn"
                                style={{ background: 'rgba(255,255,255,0.1)', border: '2px solid rgba(255,255,255,0.2)' }}
                                onClick={() => {
                                    playUiSound('magic');
                                    handleRestartStory();
                                }}
                                aria-label="Start a new story"
                            >
                                ✨ Restart
                            </button>
                        </div>
                    </div>

                    {/* Bottom Right: Mic Toggle */}
                    <div className="hud-bottom-right">
                        <button
                            className={`magic-btn ${isMicMuted ? 'muted' : ''}`}
                            style={{ background: isMicMuted ? 'var(--color-error)' : 'var(--color-accent-green)' }}
                            onClick={() => {
                                playUiSound(isMicMuted ? 'toggle_on' : 'toggle_off');
                                toggleMic();
                            }}
                            aria-label={isMicMuted ? 'Unmute microphone' : 'Mute microphone'}
                        >
                            {isMicMuted ? '🔇 Off' : '🎤 On'}
                        </button>
                    </div>
                </>
            )}

            {/* Connection status badge */}
            {connectionState !== 'connected' && (
                <div className="connection-badge" role="status" aria-live="polite">
                    {connectionBadgeText}
                </div>
            )}

            {/* Start screen while Amelia loads */}
            {isStarting && (
                <div
                    className="amelia-loading-screen"
                    role="status"
                    aria-live="polite"
                    style={{
                        '--bg-portrait': `url(/splash/portrait_${splashIndex.toString().padStart(2, '0')}.png)`,
                        '--bg-landscape': `url(/splash/landscape_${splashIndex.toString().padStart(2, '0')}.png)`,
                    } as React.CSSProperties}
                >
                    <div className="amelia-loading-card">
                        <div className="amelia-loading-title">Amelia is opening the storybook...</div>
                        <div className="amelia-loading-subtitle">One moment while she gets ready to say hello.</div>
                    </div>
                </div>
            )}

            {zoomedImageUrl && (
                <div
                    className="zoom-overlay"
                    role="dialog"
                    aria-modal="true"
                    aria-label="Zoomed story image"
                    onClick={() => setZoomedImageUrl(null)}
                >
                    <div className="zoom-stage">
                        <button
                            className="zoom-close-btn"
                            onClick={(e) => {
                                e.stopPropagation();
                                playUiSound('close');
                                setZoomedImageUrl(null);
                            }}
                            aria-label="Close zoomed image"
                        >
                            ✕
                        </button>
                        <img
                            src={zoomedImageUrl}
                            alt="Zoomed story scene"
                            className="zoomed-image"
                        />
                    </div>
                </div>
            )}

            {toyShareOverlayOpen && (
                <div
                    className="spyglass-overlay toy-share-overlay"
                    role="dialog"
                    aria-modal="true"
                    aria-label="Share your toy with Amelia"
                >
                    <div className="toy-share-shell">
                        <div className="toy-share-header">
                            <div className="toy-share-kicker">Show-and-Tell Time</div>
                            <button
                                className="toy-share-close-btn"
                                onClick={() => {
                                    playUiSound('close');
                                    closeToyShareOverlay({ notifyBackend: true });
                                }}
                                aria-label="Close toy sharing"
                            >
                                ✕
                            </button>
                        </div>

                        <div className="toy-share-copy">
                            <h2 className="toy-share-title">Let Amelia meet your toy</h2>
                            <p className="toy-share-helper">{toyShareHelperText}</p>
                            <p className="toy-share-live-status" role="status" aria-live="polite">
                                {toyShareLiveStatus}
                            </p>
                        </div>

                        <div className="toy-share-stage">
                            {spyglassStream ? (
                                <video
                                    ref={spyglassVideoRef}
                                    className="spyglass-preview-video toy-share-video"
                                    playsInline
                                    muted
                                />
                            ) : toySharePreviewUrl ? (
                                <img
                                    src={toySharePreviewUrl}
                                    alt="Shared toy preview"
                                    className="toy-share-still"
                                />
                            ) : (
                                <div className="toy-share-empty" aria-hidden="true">
                                    <div className="toy-share-empty-icon">🧸</div>
                                    <div className="toy-share-empty-text">
                                        Turn on the camera or pick a picture so Amelia can see your special friend.
                                    </div>
                                </div>
                            )}

                            {toySharePreviewUrl && (
                                <div className="toy-share-last-shot">
                                    <span className="toy-share-last-shot-label">Last peek</span>
                                    <img src={toySharePreviewUrl} alt="" aria-hidden="true" />
                                </div>
                            )}
                        </div>

                        <div className="spyglass-overlay-actions toy-share-actions">
                            <button
                                className="spyglass-overlay-btn spyglass-capture"
                                onClick={() => {
                                    playUiSound(spyglassStream ? 'magic' : 'tap');
                                    if (spyglassStream) {
                                        handleSpyglassCapture();
                                        return;
                                    }
                                    handleSpyglass();
                                }}
                                disabled={toyShareCameraStarting || spyglassCapturing || toyShareState === 'uploading'}
                            >
                                {spyglassStream
                                    ? (spyglassCapturing || toyShareState === 'uploading' ? '⏳ Amelia is looking...' : '📸 Show This Toy')
                                    : (toyShareCameraStarting ? '⏳ Opening Camera...' : '🎥 Turn On Camera')}
                            </button>
                            <button
                                className="spyglass-overlay-btn toy-share-pick-btn"
                                onClick={() => {
                                    playUiSound('tap');
                                    handleToyFileBrowserOpen();
                                }}
                                disabled={toyShareState === 'uploading'}
                            >
                                🖼 Pick a Picture
                            </button>
                            <button
                                className="spyglass-overlay-btn spyglass-cancel"
                                onClick={() => {
                                    playUiSound('close');
                                    handleSpyglassCancel();
                                }}
                            >
                                ✨ Back to Story
                            </button>
                        </div>
                    </div>
                </div>
            )}

            {ENABLE_SCENE_BRANCH_UI && showSceneBranchPicker && (
                <div className="scene-branch-overlay" role="dialog" aria-modal="true" aria-label="Choose a story scene">
                    <div className="scene-branch-card">
                        <div className="scene-branch-header">
                            <div>
                                <div className="scene-branch-kicker">Story Fork</div>
                                <h2 className="scene-branch-title">Go back to an earlier scene</h2>
                            </div>
                            <button
                                className="toy-share-close-btn"
                                onClick={() => {
                                    playUiSound('close');
                                    closeSceneBranchPicker();
                                }}
                                aria-label="Close scene picker"
                            >
                                ✕
                            </button>
                        </div>
                        <p className="scene-branch-warning">{sceneBranchWarning}</p>
                        <div className="scene-branch-grid">
                            {sceneHistory.map((scene) => {
                                const isSelected = scene.scene_number === selectedSceneNumber;
                                const text = (scene.storybeat_text || scene.scene_description || scene.label || `Scene ${scene.scene_number}`).trim();
                                return (
                                    <button
                                        key={scene.scene_number}
                                        className={`scene-branch-scene ${isSelected ? 'selected' : ''}`}
                                        onClick={() => {
                                            playUiSound('tap');
                                            setSelectedSceneNumber(scene.scene_number);
                                        }}
                                        aria-pressed={isSelected}
                                    >
                                        <div className="scene-branch-scene-number">Scene {scene.scene_number}</div>
                                        {scene.image_url ? (
                                            <img
                                                src={scene.image_url}
                                                alt=""
                                                className="scene-branch-scene-thumb"
                                                aria-hidden="true"
                                            />
                                        ) : (
                                            <div className="scene-branch-scene-placeholder" aria-hidden="true">
                                                ✨
                                            </div>
                                        )}
                                        <div className="scene-branch-scene-text">{text}</div>
                                        {scene.is_current && <div className="scene-branch-scene-pill">Current</div>}
                                    </button>
                                );
                            })}
                        </div>
                        <div className="scene-branch-footer">
                            <div className="scene-branch-summary">
                                {selectedScene
                                    ? `Go back to scene ${selectedScene.scene_number} and erase the pages after it.`
                                    : 'Pick a scene to branch the story from there.'}
                            </div>
                            <div className="scene-branch-actions">
                                <button
                                    className="restart-cancel-btn"
                                    onClick={() => {
                                        playUiSound('close');
                                        closeSceneBranchPicker();
                                    }}
                                >
                                    Keep This Story
                                </button>
                                <button
                                    className="restart-confirm-btn"
                                    onClick={() => {
                                        playUiSound('magic');
                                        confirmSceneBranch();
                                    }}
                                    disabled={!selectedScene || selectedScene.is_current}
                                >
                                    ↩ Go Back Here
                                </button>
                            </div>
                        </div>
                    </div>
                </div>
            )}

            {/* Kid-Friendly Restart Confirmation Modal */}
            {showRestartConfirm && (
                <div className="restart-modal-overlay" role="dialog" aria-modal="true">
                    <div className="restart-modal-card">
                        <div className="restart-modal-icon">✨</div>
                        <h2 className="restart-modal-title">Start a New Magic Adventure?</h2>
                        <p className="restart-modal-text">This will finish your current story and start a brand new one!</p>
                        <div className="restart-modal-actions">
                            <button
                                className="restart-confirm-btn"
                                onClick={() => {
                                    playUiSound('magic');
                                    confirmRestart();
                                }}
                            >
                                🌟 Yes, Start Over!
                            </button>
                            <button
                                className="restart-cancel-btn"
                                onClick={() => {
                                    playUiSound('close');
                                    cancelRestart();
                                }}
                            >
                                ✨ Keep Playing
                            </button>
                        </div>
                    </div>
                </div>
            )}

            {showEndStoryConfirm && (
                <div className="restart-modal-overlay" role="dialog" aria-modal="true">
                    <div className="restart-modal-card">
                        <div className="restart-modal-icon">🎬</div>
                        <h2 className="restart-modal-title">Finish This Adventure?</h2>
                        <p className="restart-modal-text">
                            You can make the movie now, start a brand new story instead, or keep this one going.
                        </p>
                        <div className="restart-modal-actions">
                            <button
                                className="restart-confirm-btn"
                                onClick={() => {
                                    playUiSound('celebrate');
                                    confirmEndStoryMovie();
                                }}
                            >
                                🌟 Make the Movie
                            </button>
                            <button
                                className="restart-cancel-btn"
                                onClick={() => {
                                    playUiSound('magic');
                                    confirmEndStoryRestart();
                                }}
                            >
                                ✨ Start a New Story
                            </button>
                            <button
                                className="restart-cancel-btn"
                                onClick={() => {
                                    playUiSound('close');
                                    cancelEndStory();
                                }}
                            >
                                💫 Keep Playing
                            </button>
                        </div>
                    </div>
                </div>
            )}

            {/* Hidden inputs / refs */}
            <input
                ref={toyUploadInputRef}
                type="file"
                accept="image/*"
                style={{
                    position: 'absolute',
                    width: '1px',
                    height: '1px',
                    padding: 0,
                    margin: '-1px',
                    overflow: 'hidden',
                    clip: 'rect(0, 0, 0, 0)',
                    whiteSpace: 'nowrap',
                    border: 0,
                    opacity: 0,
                    pointerEvents: 'none'
                }}
                onChange={handleToyFileSelected}
                tabIndex={-1}
                aria-hidden="true"
            />
        </main>
    );
}
