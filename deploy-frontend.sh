#!/usr/bin/env bash
# deploy-frontend.sh — Build, push, and deploy the frontend to Cloud Run
set -euo pipefail

PROJECT="interactive-story-gemini"
REGION="us-central1"
TAG="${1:-$(date +%Y%m%d-%H%M%S)}"
FRONTEND_IMAGE="gcr.io/$PROJECT/storyteller-frontend:$TAG"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "🔨 Building frontend ($TAG)..."
docker build --platform linux/amd64 -t "$FRONTEND_IMAGE" -f frontend/Dockerfile frontend/
echo "📤 Pushing frontend..."
docker push "$FRONTEND_IMAGE"
echo "🚀 Deploying frontend to Cloud Run..."
gcloud run deploy storyteller-frontend \
  --image "$FRONTEND_IMAGE" \
  --region "$REGION" \
  --project "$PROJECT" \
  --quiet
echo "✅ Frontend deployed: $FRONTEND_IMAGE"
echo "   URL: $(gcloud run services describe storyteller-frontend --region=$REGION --project=$PROJECT --format='value(status.url)' 2>/dev/null)"
