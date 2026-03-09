import type { Metadata, Viewport } from 'next';
import { Inter } from 'next/font/google';
import './globals.css';

const inter = Inter({
    subsets: ['latin'],
    variable: '--font-inter',
    display: 'swap',
});

export const metadata: Metadata = {
    title: 'Back to Some-ping, back to Dody Land!',
    description:
        'A voice-first AI picture-storytelling experience for young children. Imagine, create, tell, and watch the storybook movie.',
    keywords: ['kids stories', 'interactive AI', 'storytelling', 'children', 'educational', 'picture stories', 'early literacy'],
    authors: [{ name: 'Back to Some-ping, back to Dody Land!' }],
    robots: 'noindex, nofollow', // Private app — no public indexing (COPPA)
    manifest: '/manifest.json',
    icons: { icon: '/favicon.ico', apple: '/apple-touch-icon.png' },
};

export const viewport: Viewport = {
    // Mobile-first: keep layout optimized for landscape storybook mode.
    width: 'device-width',
    initialScale: 1,
    maximumScale: 1,
    userScalable: false,
    themeColor: '#1a0533',
    viewportFit: 'cover', // Safe area for iPhone notch
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
    return (
        <html lang="en" className={inter.variable}>
            <head>
                {/* Preload the cold-start greeting audio (Iter 7 #10 — First-Turn silence fix) */}
                <link rel="preload" href="/audio/amelia-thinking.mp3" as="audio" type="audio/mpeg" />
                <link rel="preload" href="/audio/got-it-lets-go.mp3" as="audio" type="audio/mpeg" />
            </head>
            <body className="app-body">
                {children}
            </body>
        </html>
    );
}
