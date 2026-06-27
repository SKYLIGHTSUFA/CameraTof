#!/usr/bin/env bash
# Подготовка офлайн-пакета: собрать образы (если нужно) и сохранить в offline/images/
# Запускать на машине с интернетом и Docker.

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT_DIR="${OUT_DIR:-$ROOT_DIR/offline/images}"

BACKEND_IMAGE="${BACKEND_IMAGE:-gige-hls:galaxysdk-v3}"
FRONTEND_IMAGE="${FRONTEND_IMAGE:-gige-hls-front:latest}"

BACKEND_TAR="${OUT_DIR}/gige-hls-backend.tar"
FRONTEND_TAR="${OUT_DIR}/gige-hls-front.tar"

mkdir -p "$OUT_DIR"

echo "==> Backend: $BACKEND_IMAGE"
if ! docker image inspect "$BACKEND_IMAGE" >/dev/null 2>&1; then
    echo "    Образ не найден, собираю (docker compose build)..."
    docker compose -f "$ROOT_DIR/docker-compose.yml" build
fi

echo "==> Frontend: $FRONTEND_IMAGE"
if ! docker image inspect "$FRONTEND_IMAGE" >/dev/null 2>&1; then
    echo "    Образ не найден, собираю..."
    docker compose -f "$ROOT_DIR/video_new/nof-front-camera/docker-compose.yml" build
fi

echo "==> Сохраняю образы в $OUT_DIR"
docker save "$BACKEND_IMAGE" -o "$BACKEND_TAR"
docker save "$FRONTEND_IMAGE" -o "$FRONTEND_TAR"

ls -lh "$BACKEND_TAR" "$FRONTEND_TAR"

cat > "$OUT_DIR/images.env" <<EOF
BACKEND_IMAGE=$BACKEND_IMAGE
FRONTEND_IMAGE=$FRONTEND_IMAGE
BACKEND_TAR=$(basename "$BACKEND_TAR")
FRONTEND_TAR=$(basename "$FRONTEND_TAR")
EOF

echo ""
echo "Готово. Скопируйте на сервер папку:"
echo "  $OUT_DIR"
echo ""
echo "На офлайн-сервере:"
echo "  ./scripts/docker-load-images.sh"
