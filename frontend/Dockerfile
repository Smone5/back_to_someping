FROM node:20-alpine AS deps
WORKDIR /app
COPY package*.json ./
RUN npm ci

FROM node:20-alpine AS builder
WORKDIR /app
ARG NEXT_PUBLIC_REQUIRE_MATH=false
ENV NEXT_PUBLIC_REQUIRE_MATH=${NEXT_PUBLIC_REQUIRE_MATH}
COPY --from=deps /app/node_modules ./node_modules
COPY . .
RUN npm run build

FROM node:20-alpine AS runner
WORKDIR /app

ENV NODE_ENV=production \
    NEXT_PUBLIC_REQUIRE_MATH=false \
    PORT=8080 \
    HOSTNAME=0.0.0.0

COPY --from=builder /app/.next ./.next
COPY --from=builder /app/public ./public
COPY --from=builder /app/package.json ./package.json
COPY --from=builder /app/next.config.js ./next.config.js
COPY --from=builder /app/node_modules ./node_modules

EXPOSE 8080

CMD ["sh", "-c", "node_modules/.bin/next start --hostname 0.0.0.0 --port ${PORT:-8080}"]
