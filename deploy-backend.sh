#!/usr/bin/env bash
# deploy-backend.sh — Build, push, and deploy the backend to Cloud Run
set -euo pipefail

PROJECT="interactive-story-gemini"
REGION="us-central1"
TAG="${1:-$(date +%Y%m%d-%H%M%S)}"
BACKEND_IMAGE="gcr.io/$PROJECT/storyteller-backend:$TAG"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "🔨 Building backend ($TAG)..."
docker build --platform linux/amd64 -t "$BACKEND_IMAGE" -f backend/Dockerfile .
echo "📤 Pushing backend..."
docker push "$BACKEND_IMAGE"
echo "🚀 Deploying backend to Cloud Run..."
gcloud run deploy storyteller-backend \
  --image "$BACKEND_IMAGE" \
  --region "$REGION" \
  --project "$PROJECT" \
  --quiet
echo "✅ Backend deployed: $BACKEND_IMAGE"
echo "   URL: $(gcloud run services describe storyteller-backend --region=$REGION --project=$PROJECT --format='value(status.url)' 2>/dev/null)"
