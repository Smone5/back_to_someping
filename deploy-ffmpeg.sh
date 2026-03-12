#!/usr/bin/env bash
# deploy-ffmpeg.sh — Build, push, and deploy the FFmpeg worker Cloud Run Job
set -euo pipefail

PROJECT="${GOOGLE_CLOUD_PROJECT:-interactive-story-gemini}"
REGION="${GOOGLE_CLOUD_LOCATION:-us-central1}"
TAG="${1:-$(date +%Y%m%d-%H%M%S)}"
FFMPEG_IMAGE="gcr.io/$PROJECT/storyteller-ffmpeg:$TAG"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "🔨 Building ffmpeg worker ($TAG)..."
docker build --platform linux/amd64 -t "$FFMPEG_IMAGE" -f backend/ffmpeg_worker/Dockerfile .
echo "📤 Pushing ffmpeg worker..."
docker push "$FFMPEG_IMAGE"
echo "🚀 Updating ffmpeg Cloud Run Job..."
gcloud run jobs update storyteller-ffmpeg-assembler \
  --image "$FFMPEG_IMAGE" \
  --region "$REGION" \
  --project "$PROJECT" \
  --quiet
echo "✅ FFmpeg worker deployed: $FFMPEG_IMAGE"
