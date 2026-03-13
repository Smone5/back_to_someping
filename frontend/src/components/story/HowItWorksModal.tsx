'use client';

import React from 'react';

interface HowItWorksModalProps {
    onClose: () => void;
}

export default function HowItWorksModal({ onClose }: HowItWorksModalProps) {
    return (
        <div className="how-it-works-overlay" role="dialog" aria-modal="true" aria-labelledby="how-it-works-title">
            <div className="how-it-works-card">
                <button className="how-it-works-close-btn" onClick={onClose} aria-label="Close information">×</button>

                <header className="how-it-works-header">
                    <h2 id="how-it-works-title" className="how-it-works-title">Voxitale <span>✨</span></h2>
                    <p className="how-it-works-tagline">Imagine • Create • Tell</p>
                </header>

                <div className="how-it-works-scroll-area">
                    <section className="how-it-works-section">
                        <h3>Concept</h3>
                        <p>
                            Voxitale is an AI-powered storytelling tool that allows children ages 4 to 10 to transform
                            their imagination into illustrated picture stories, helping them learn the art of
                            storytelling before they can read or write.
                        </p>
                    </section>

                    <section className="how-it-works-section">
                        <h3>How the Program Works</h3>
                        <div className="steps-grid">
                            <div className="step-item">
                                <div className="step-icon">💭</div>
                                <h4>Imagine</h4>
                                <p>The child describes an idea (e.g. &quot;A dragon flying over a castle&quot;).</p>
                            </div>
                            <div className="step-item">
                                <div className="step-icon">🎨</div>
                                <h4>Create</h4>
                                <p>AI generates the first illustration instantly.</p>
                            </div>
                            <div className="step-item">
                                <div className="step-icon">🛠️</div>
                                <h4>Build</h4>
                                <p>Amelia asks prompts like &quot;Who is in the story?&quot; and &quot;What happens next?&quot;</p>
                            </div>
                            <div className="step-item">
                                <div className="step-icon">➡️</div>
                                <h4>Continue</h4>
                                <p>Each idea creates another illustrated scene, allowing the story to grow.</p>
                            </div>
                            <div className="step-item">
                                <div className="step-icon">📚</div>
                                <h4>Finish & Read Along</h4>
                                <p>A personal storybook movie emerges. Words are highlighted as Amelia reads them, helping children learn to read.</p>
                            </div>
                        </div>
                    </section>

                    <section className="how-it-works-section">
                        <h3>Educational Benefits</h3>
                        <div className="benefits-grid">
                            <div className="benefit-pill">
                                <strong>📖 Early Literacy</strong>
                                <span>Word recognition via read-along highlighting, vocabulary growth, story structure</span>
                            </div>
                            <div className="benefit-pill">
                                <strong>🧠 Cognitive</strong>
                                <span>Sequencing, cause and effect, problem solving</span>
                            </div>
                            <div className="benefit-pill">
                                <strong>💡 Creativity</strong>
                                <span>Imagination, visual storytelling, idea generation</span>
                            </div>
                            <div className="benefit-pill">
                                <strong>🗣️ Communication</strong>
                                <span>Expressing ideas, describing events, building confidence</span>
                            </div>
                        </div>
                    </section>

                    <section className="how-it-works-section">
                        <h3>A Day in the Life</h3>
                        <div className="story-example">
                            <p>
                                <em>&quot;Maya opens Voxitale. The program asks, &lsquo;What story would you like to create today?&rsquo;
                                    Maya replies, &lsquo;A pink dragon flying over a castle.&rsquo; Within seconds the AI generates an illustration.
                                    Maya continues adding characters and adventures, each idea creating another illustrated page until
                                    she has built her own storybook.&quot;</em>
                            </p>
                        </div>
                    </section>

                    <footer className="how-it-works-footer-note">
                        <p>
                            By combining children&apos;s imagination with artificial intelligence, Voxitale helps young learners
                            become creative thinkers, storytellers, and lifelong learners.
                        </p>
                    </footer>
                </div>

                <div className="how-it-works-actions">
                    <button className="how-it-works-primary-btn" onClick={onClose}>Got it! ✨</button>
                </div>
            </div>
        </div>
    );
}
