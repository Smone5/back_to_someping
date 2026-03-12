import type { Metadata, Viewport } from 'next';
import { headers } from 'next/headers';
import { Inter } from 'next/font/google';
import './globals.css';

const inter = Inter({
    subsets: ['latin'],
    variable: '--font-inter',
    display: 'swap',
});

async function resolveSiteUrl(): Promise<string> {
    const fallback = process.env.NEXT_PUBLIC_SITE_URL?.trim() || 'http://localhost:3000';
    try {
        const requestHeaders = await headers();
        const host = requestHeaders.get('x-forwarded-host') || requestHeaders.get('host');
        if (!host) {
            return fallback;
        }
        const protocol = requestHeaders.get('x-forwarded-proto') || 'https';
        return `${protocol}://${host}`;
    } catch {
        return fallback;
    }
}

export async function generateMetadata(): Promise<Metadata> {
    const siteUrl = await resolveSiteUrl();
    return {
        metadataBase: new URL(siteUrl),
        title: 'StorySpark',
        description:
            'StorySpark is a voice-first AI picture-storytelling experience for young children. Imagine, create, tell, and watch the storybook movie.',
        applicationName: 'StorySpark',
        keywords: ['StorySpark', 'kids stories', 'interactive AI', 'storytelling', 'children', 'educational', 'picture stories', 'early literacy'],
        authors: [{ name: 'StorySpark' }],
        robots: 'noindex, nofollow',
        manifest: '/manifest.json',
        icons: { icon: '/favicon.ico', apple: '/apple-touch-icon.png' },
        alternates: {
            canonical: '/',
        },
        openGraph: {
            type: 'website',
            url: '/',
            siteName: 'StorySpark',
            title: 'StorySpark',
            description: 'Interactive AI storybooks that weave voice, pictures, and a final movie together for young children.',
            images: [
                {
                    url: '/splash/landscape_16.png',
                    width: 1536,
                    height: 1024,
                    alt: 'StorySpark interactive storybook preview',
                },
            ],
        },
        twitter: {
            card: 'summary_large_image',
            title: 'StorySpark',
            description: 'Interactive AI storybooks that turn a child’s imagination into pictures, narration, and a shareable movie.',
            images: ['/splash/landscape_16.png'],
        },
    };
}

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
