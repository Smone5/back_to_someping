import Link from 'next/link';

export const metadata = {
    title: 'Privacy Policy — Voxitale',
    description: 'Privacy policy for Voxitale. We do not store audio or collect personal information.',
};

export default function PrivacyPage() {
    return (
        <main className="privacy-page">
            <div className="privacy-content">
                <h1>Privacy Policy</h1>
                <p className="privacy-updated">Last updated: March 2026</p>

                <section>
                    <h2>Overview</h2>
                    <p>
                        Voxitale (&quot;the app&quot;) is designed with children and families in mind.
                        We take privacy seriously and collect as little as possible.
                    </p>
                </section>

                <section>
                    <h2>What we do not do</h2>
                    <ul>
                        <li><strong>We do not store your voice or audio.</strong> Microphone input is used only in real time to run the story experience and is not recorded or saved.</li>
                        <li><strong>We do not collect personal information</strong> such as names, emails, or identifiers for advertising or tracking.</li>
                        <li>We do not sell or share user data with third parties for marketing.</li>
                    </ul>
                </section>

                <section>
                    <h2>Optional features</h2>
                    <ul>
                        <li><strong>Room lights (Home Assistant):</strong> If you connect smart lights, configuration is stored on your device. For publicly reachable Home Assistant URLs, Voxitale may relay light commands through the backend during the active session, but we do not persist your Home Assistant token or home setup after the request completes.</li>
                    </ul>
                </section>

                <section>
                    <h2>Contact</h2>
                    <p>
                        If you have questions about this privacy policy or the app, please contact the app provider
                        through the channel you used to access the app.
                    </p>
                </section>

                <p className="privacy-back">
                    <Link href="/">← Back to Voxitale</Link>
                </p>
            </div>
        </main>
    );
}
