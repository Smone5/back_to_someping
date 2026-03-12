'use client';

export type StoryReaderVoiceOption = {
    id: string;
    name: string;
    blurb: string;
};

export const DEFAULT_STORY_READER_VOICE_ID = 'aTbnroHRGIomiKpqAQR8';
export const STORY_READER_VOICE_PREVIEW_TEXT = 'Hello, story friend. I am ready to read our next adventure aloud.';

export const STORY_READER_VOICE_OPTIONS: StoryReaderVoiceOption[] = [
    {
        id: DEFAULT_STORY_READER_VOICE_ID,
        name: 'Felicity (UK)',
        blurb: 'Warm British female storyteller voice with gentle bedtime-story pacing.',
    },
    {
        id: '4u5cJuSmHP9d6YRolsOu',
        name: 'Jonathan (UK)',
        blurb: 'Warm British male narrator with a lighter fantasy-story feel.',
    },
    {
        id: 'XfNU2rGpBa01ckF309OY',
        name: 'Nichalia (US)',
        blurb: 'Gentle American female narrator with very clear pacing and pronunciation.',
    },
    {
        id: 'yl2ZDV1MzN4HbQJbMihG',
        name: 'Alex (US)',
        blurb: 'Friendly American male voice with upbeat, easy-to-follow story energy.',
    },
];

export function normalizeStoryReaderVoiceId(raw: unknown): string {
    const cleaned = String(raw ?? '').trim();
    const matched = STORY_READER_VOICE_OPTIONS.find((option) => option.id === cleaned);
    return matched?.id || DEFAULT_STORY_READER_VOICE_ID;
}

export function getStoryReaderVoiceOption(id: unknown): StoryReaderVoiceOption {
    const normalized = normalizeStoryReaderVoiceId(id);
    return STORY_READER_VOICE_OPTIONS.find((option) => option.id === normalized) || STORY_READER_VOICE_OPTIONS[0];
}
