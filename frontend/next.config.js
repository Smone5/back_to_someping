const backendUrl =
    process.env.BACKEND_URL?.trim() || process.env.NEXT_PUBLIC_BACKEND_URL?.trim() || '';
const shouldProxyToBackend =
    process.env.NODE_ENV !== 'production' || backendUrl.length > 0;
const backendTarget = backendUrl || 'http://localhost:8000';

/** @type {import('next').NextConfig} */
const nextConfig = {
    // Instructs Next.js to allow large WebSocket frames in dev proxy
    experimental: {
        serverActions: { bodySizeLimit: '5mb' },
    },
    // Environment variables available in the browser
    env: {
        // Use relative defaults so production never hardcodes localhost in client bundles.
        NEXT_PUBLIC_BACKEND_URL:
            process.env.NEXT_PUBLIC_BACKEND_URL ?? process.env.BACKEND_URL ?? '',
        NEXT_PUBLIC_WS_URL: process.env.NEXT_PUBLIC_WS_URL ?? '/ws/story',
        NEXT_PUBLIC_UPLOAD_URL: process.env.NEXT_PUBLIC_UPLOAD_URL ?? '/api/upload',
    },
    // Proxy /api and /ws to the FastAPI backend during local development
    async rewrites() {
        if (!shouldProxyToBackend) {
            // In production without explicit BACKEND_URL, avoid localhost fallthrough.
            // Client runtime host-derivation will call backend directly.
            return [];
        }
        return [
            {
                source: '/api/:path*',
                destination: `${backendTarget}/api/:path*`,
            },
            {
                source: '/ws/:path*',
                destination: `${backendTarget}/ws/:path*`,
            },
        ];
    },
};

module.exports = nextConfig;
