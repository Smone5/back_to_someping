'use client';

import { useState, useEffect, ChangeEvent } from 'react';
import {
    describeHomeAssistantFailure,
    isLikelyLocalHomeAssistantUrl,
    isMixedContentRisk,
    normalizeHomeAssistantConfig,
    smokeTestHomeAssistantLighting,
} from './homeAssistant';

export interface IoTConfig {
    ha_url: string;
    ha_token: string;
    ha_entity: string;
}

interface IoTSettingsModalProps {
    onClose: () => void;
    onSave: (config: IoTConfig) => void;
}

export default function IoTSettingsModal({ onClose, onSave }: IoTSettingsModalProps) {
    const [config, setConfig] = useState<IoTConfig>({
        ha_url: '',
        ha_token: '',
        ha_entity: 'light.living_room'
    });
    const [testState, setTestState] = useState<'idle' | 'running' | 'success' | 'error'>('idle');
    const [testMessage, setTestMessage] = useState('');

    // Load from localStorage on mount
    useEffect(() => {
        try {
            const saved = localStorage.getItem('storyteller_iot_config');
            if (saved) {
                setConfig(normalizeHomeAssistantConfig(JSON.parse(saved)));
            }
        } catch (e) {
            console.error('Failed to parse IoT config from localStorage:', e);
        }
    }, []);

    const handleChange = (e: ChangeEvent<HTMLInputElement>) => {
        setTestState('idle');
        setTestMessage('');
        setConfig((prev: IoTConfig) => ({ ...prev, [e.target.name]: e.target.value }));
    };

    const handleTest = async () => {
        if (testState === 'running') {
            return;
        }
        const normalizedConfig = normalizeHomeAssistantConfig(config);
        setConfig(normalizedConfig);
        setTestState('running');
        setTestMessage('Sending a quick purple shimmer to your light…');

        const result = await smokeTestHomeAssistantLighting(normalizedConfig);
        if (result.ok) {
            setTestState('success');
            setTestMessage(
                result.entityName
                    ? `${result.entityName} responded and returned to its earlier state.`
                    : 'The light responded and returned to its earlier state.'
            );
            return;
        }

        const message = describeHomeAssistantFailure(result.reason);
        setTestState('error');
        setTestMessage(
            result.reason === 'restore_failed'
                ? `${message} The shimmer worked, but the old light state did not restore.`
                : message
        );
    };

    const handleSave = () => {
        const normalizedConfig = normalizeHomeAssistantConfig(config);
        try {
            localStorage.setItem('storyteller_iot_config', JSON.stringify(normalizedConfig));
        } catch (e) {
            console.error('Failed to save IoT config to localStorage:', e);
        }
        onSave(normalizedConfig);
        onClose();
    };

    const showLocalNetworkHint = Boolean(config.ha_url) && isLikelyLocalHomeAssistantUrl(config.ha_url);
    const showMixedContentHint = Boolean(config.ha_url) && isMixedContentRisk(config.ha_url);

    return (
        <div className="iot-modal-overlay" role="dialog" aria-modal="true" aria-labelledby="iot-modal-title">
            <div className="iot-modal-card">
                <button className="iot-close-btn" onClick={onClose} aria-label="Close settings">×</button>
                <h2 id="iot-modal-title" className="iot-modal-title">Smart Home Magic 🪄</h2>
                <p className="iot-modal-subtitle">
                    Connect Amelia to <strong>Home Assistant</strong> to change your room lights to match the story.
                    If you use Google Home, you can add your lights to Home Assistant (or use a bridge) so Amelia can control them too.
                </p>

                <div className="iot-form">
                    <label className="iot-label">
                        Home Assistant URL
                        <input
                            type="url"
                            name="ha_url"
                            className="iot-input"
                            placeholder="http://homeassistant.local:8123"
                            value={config.ha_url}
                            onChange={handleChange}
                        />
                    </label>

                    <label className="iot-label">
                        Long-Lived Access Token
                        <input
                            type="password"
                            name="ha_token"
                            className="iot-input"
                            placeholder="eyJhbGciOiJIUzI1NiIsInR5cCI..."
                            value={config.ha_token}
                            onChange={handleChange}
                        />
                    </label>

                    <label className="iot-label">
                        Light Entity ID
                        <input
                            type="text"
                            name="ha_entity"
                            className="iot-input"
                            placeholder="light.living_room"
                            value={config.ha_entity}
                            onChange={handleChange}
                        />
                    </label>
                </div>

                {showLocalNetworkHint && (
                    <p className="iot-modal-subtitle">
                        Local-network Home Assistant detected. Amelia will send light commands through this browser during the story, which is the reliable path when the app backend is running in the cloud.
                    </p>
                )}

                {showMixedContentHint && (
                    <p className="iot-modal-subtitle">
                        This page is running over HTTPS but your Home Assistant URL is HTTP. Most browsers block that direct light-control request, so use an HTTPS Home Assistant URL if possible.
                    </p>
                )}

                <p className="iot-modal-subtitle">
                    Browser control also requires Home Assistant to allow this site origin in <code>http.cors_allowed_origins</code>.
                </p>
                <p className="iot-modal-subtitle">
                    Test Light sends a short purple shimmer, then restores the light so you can confirm the exact browser path Amelia will use during the story and the final movie.
                </p>

                {testState !== 'idle' && (
                    <div className={`iot-test-status iot-test-status-${testState}`} role="status" aria-live="polite">
                        {testMessage}
                    </div>
                )}

                <div className="iot-actions">
                    <button className="iot-btn iot-btn-secondary" onClick={onClose}>Cancel</button>
                    <button
                        className="iot-btn iot-btn-test"
                        onClick={() => {
                            void handleTest();
                        }}
                        disabled={testState === 'running'}
                    >
                        {testState === 'running' ? 'Testing…' : 'Test Light'}
                    </button>
                    <button className="iot-btn iot-btn-primary" onClick={handleSave}>Save Magic</button>
                </div>
            </div>
        </div>
    );
}
