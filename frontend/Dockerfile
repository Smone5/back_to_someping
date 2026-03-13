FROM node:20-alpine AS deps
WORKDIR /app
COPY package*.json ./
RUN npm ci

FROM node:20-alpine AS builder
WORKDIR /app
ARG NEXT_PUBLIC_REQUIRE_MATH=false
ARG NEXT_PUBLIC_BACKEND_URL=
ARG NEXT_PUBLIC_WS_URL=/ws/story
ARG NEXT_PUBLIC_UPLOAD_URL=/api/upload
ARG NEXT_PUBLIC_SITE_URL=
ENV NEXT_PUBLIC_REQUIRE_MATH=${NEXT_PUBLIC_REQUIRE_MATH} \
    NEXT_PUBLIC_BACKEND_URL=${NEXT_PUBLIC_BACKEND_URL} \
    NEXT_PUBLIC_WS_URL=${NEXT_PUBLIC_WS_URL} \
    NEXT_PUBLIC_UPLOAD_URL=${NEXT_PUBLIC_UPLOAD_URL} \
    NEXT_PUBLIC_SITE_URL=${NEXT_PUBLIC_SITE_URL}
COPY --from=deps /app/node_modules ./node_modules
COPY . .
RUN npm run build

FROM node:20-alpine AS runner
WORKDIR /app

ENV NODE_ENV=production \
    NEXT_PUBLIC_REQUIRE_MATH=false \
    NEXT_PUBLIC_BACKEND_URL= \
    NEXT_PUBLIC_WS_URL=/ws/story \
    NEXT_PUBLIC_UPLOAD_URL=/api/upload \
    NEXT_PUBLIC_SITE_URL= \
    PORT=8080 \
    HOSTNAME=0.0.0.0

COPY --from=builder /app/.next ./.next
COPY --from=builder /app/public ./public
COPY --from=builder /app/package.json ./package.json
COPY --from=builder /app/next.config.js ./next.config.js
COPY --from=builder /app/node_modules ./node_modules

EXPOSE 8080

CMD ["sh", "-c", "node_modules/.bin/next start --hostname 0.0.0.0 --port ${PORT:-8080}"]
