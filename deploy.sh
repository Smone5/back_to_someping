#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# deploy.sh — Build, push, deploy the Interactive Storyteller to Cloud Run
#              AND apply Terraform config changes automatically.
#
# Usage:
#   ./deploy.sh           # deploy all (backend + frontend + ffmpeg + terraform)
#   ./deploy.sh backend   # deploy only backend + terraform
#   ./deploy.sh frontend  # deploy only frontend + terraform
#   ./deploy.sh ffmpeg    # deploy only ffmpeg + terraform
#   ./deploy.sh code      # deploy all code but skip terraform
#   ./deploy.sh terraform # only run terraform apply (no Docker builds)
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

PROJECT="interactive-story-gemini"
REGION="us-central1"
TAG=$(date +%Y%m%d-%H%M%S)
TARGET="${1:-all}"

BACKEND_IMAGE="gcr.io/$PROJECT/storyteller-backend:$TAG"
FRONTEND_IMAGE="gcr.io/$PROJECT/storyteller-frontend:$TAG"
FFMPEG_IMAGE="gcr.io/$PROJECT/storyteller-ffmpeg:$TAG"
REQUIRE_MATH="${NEXT_PUBLIC_REQUIRE_MATH:-false}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

TFVARS="$SCRIPT_DIR/google_terraform/terraform.tfvars"

# ── Helper: update an image tag in terraform.tfvars ──────────────────────────
update_tfvar_image() {
  local var_name="$1"
  local new_image="$2"
  if grep -q "^${var_name}" "$TFVARS" 2>/dev/null; then
    # macOS-compatible sed -i
    sed -i '' "s|^${var_name}.*|${var_name} = \"${new_image}\"|" "$TFVARS"
    echo "   📝 Updated $var_name → $new_image"
  else
    echo "${var_name} = \"${new_image}\"" >> "$TFVARS"
    echo "   📝 Added $var_name → $new_image"
  fi
}

# ── Build + push functions ───────────────────────────────────────────────────
deploy_backend() {
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
  update_tfvar_image "backend_image" "$BACKEND_IMAGE"
  echo "✅ Backend deployed: $BACKEND_IMAGE"
}

deploy_frontend() {
  echo "🔨 Building frontend ($TAG)..."
  docker build \
    --platform linux/amd64 \
    --build-arg NEXT_PUBLIC_REQUIRE_MATH="$REQUIRE_MATH" \
    -t "$FRONTEND_IMAGE" \
    -f frontend/Dockerfile \
    frontend/
  echo "📤 Pushing frontend..."
  docker push "$FRONTEND_IMAGE"
  echo "🚀 Deploying frontend to Cloud Run..."
  gcloud run deploy storyteller-frontend \
    --image "$FRONTEND_IMAGE" \
    --region "$REGION" \
    --project "$PROJECT" \
    --quiet
  update_tfvar_image "frontend_image" "$FRONTEND_IMAGE"
  echo "✅ Frontend deployed: $FRONTEND_IMAGE"
}

deploy_ffmpeg() {
  echo "🔨 Building ffmpeg ($TAG)..."
  docker build --platform linux/amd64 -t "$FFMPEG_IMAGE" -f backend/ffmpeg_worker/Dockerfile .
  echo "📤 Pushing ffmpeg..."
  docker push "$FFMPEG_IMAGE"
  echo "🚀 Deploying ffmpeg Cloud Run Job..."
  gcloud run jobs update storyteller-ffmpeg-assembler \
    --image "$FFMPEG_IMAGE" \
    --region "$REGION" \
    --project "$PROJECT" \
    --quiet 2>/dev/null || \
  gcloud run jobs create storyteller-ffmpeg-assembler \
    --image "$FFMPEG_IMAGE" \
    --region "$REGION" \
    --project "$PROJECT" \
    --quiet
  update_tfvar_image "ffmpeg_image" "$FFMPEG_IMAGE"
  echo "✅ FFmpeg deployed: $FFMPEG_IMAGE"
}

# ── Terraform apply ──────────────────────────────────────────────────────────
apply_terraform() {
  echo ""
  echo "🏗️  Applying Terraform (env vars, IAM, infra)..."
  cd "$SCRIPT_DIR/google_terraform"
  terraform apply -auto-approve
  cd "$SCRIPT_DIR"
  echo "✅ Terraform apply complete."
}

# ── Main dispatch ────────────────────────────────────────────────────────────
case "$TARGET" in
  backend)
    deploy_backend
    apply_terraform
    ;;
  frontend)
    deploy_frontend
    apply_terraform
    ;;
  ffmpeg)
    deploy_ffmpeg
    apply_terraform
    ;;
  code)
    deploy_backend
    deploy_frontend
    deploy_ffmpeg
    echo ""
    echo "⏭️  Skipping terraform (use './deploy.sh all' to include it)."
    ;;
  terraform)
    apply_terraform
    ;;
  all)
    deploy_backend
    deploy_frontend
    deploy_ffmpeg
    apply_terraform
    ;;
  *)
    echo "Unknown target: $TARGET. Use: all | backend | frontend | ffmpeg | code | terraform"
    exit 1
    ;;
esac

echo ""
echo "🎉 Deploy complete! Tag: $TAG"
echo "   Backend:  $(gcloud run services describe storyteller-backend --region=$REGION --project=$PROJECT --format='value(status.url)' 2>/dev/null)"
echo "   Frontend: $(gcloud run services describe storyteller-frontend --region=$REGION --project=$PROJECT --format='value(status.url)' 2>/dev/null)"
