#!/usr/bin/env bash
# Полная подготовка офлайн-пакета на машине С интернетом:
#   1) whl/ — Python-зависимости
#   2) docker build backend (v2 + v3) и frontend
#   3) offline/images/*.tar — готовые образы
#
# Скопируйте на production-сервер:
#   offline/images/          — образы Docker
#   app.py, config.json, docker-compose.yml, video_new/nof-front-camera/

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
GALAXY_DIR="$ROOT_DIR/Galaxy_Linux-x86_Gige-U3_32bits-64bits_2.4.2507.9231"
BASE_V2="${BASE_V2:-gige-hls:galaxysdk-v2}"
BACKEND_V3="${BACKEND_V3:-gige-hls:galaxysdk-v3}"
FRONTEND_IMAGE="${FRONTEND_IMAGE:-gige-hls-front:latest}"
SKIP_WHEELS="${SKIP_WHEELS:-0}"
SKIP_BUILD="${SKIP_BUILD:-0}"

cd "$ROOT_DIR"

echo "========== GigE HLS — подготовка офлайн-пакета =========="

if [[ ! -d "$GALAXY_DIR/Galaxy_camera/lib/x86_64" ]]; then
    echo "Ошибка: не найден Galaxy SDK:" >&2
    echo "  $GALAXY_DIR/Galaxy_camera/lib/x86_64/" >&2
    echo "Скачайте SDK с Daheng Imaging и распакуйте рядом с Dockerfile." >&2
    exit 1
fi

if [[ "$SKIP_WHEELS" != "1" ]]; then
    echo ""
    bash "$ROOT_DIR/scripts/download-python-wheels.sh"
else
    echo ""
    echo "==> SKIP_WHEELS=1 — пропускаю download-python-wheels.sh"
    if [[ ! -d "$ROOT_DIR/whl" ]] || [[ -z "$(ls -A "$ROOT_DIR/whl" 2>/dev/null)" ]]; then
        echo "Ошибка: whl/ пуст — сначала запустите ./scripts/download-python-wheels.sh" >&2
        exit 1
    fi
fi

if [[ "$SKIP_BUILD" != "1" ]]; then
    echo ""
    echo "==> Backend base image: $BASE_V2"
    if ! docker image inspect "$BASE_V2" >/dev/null 2>&1; then
        docker build -f "$ROOT_DIR/Dockerfile" -t "$BASE_V2" "$ROOT_DIR"
    else
        echo "    уже есть, пропускаю (удалите образ для пересборки)"
    fi

    echo ""
    echo "==> Backend runtime image: $BACKEND_V3"
    docker compose -f "$ROOT_DIR/docker-compose.yml" build

    echo ""
    echo "==> Frontend: $FRONTEND_IMAGE"
    docker compose -f "$ROOT_DIR/video_new/nof-front-camera/docker-compose.yml" build
else
    echo ""
    echo "==> SKIP_BUILD=1 — пропускаю docker build"
fi

echo ""
bash "$ROOT_DIR/scripts/docker-save-images.sh"

echo ""
echo "========== Готово =========="
echo ""
echo "Скопируйте на офлайн-сервер:"
echo "  offline/images/                    — Docker-образы (*.tar)"
echo "  app.py, config.json"
echo "  docker-compose.yml"
echo "  video_new/nof-front-camera/        — видеостена"
echo "  scripts/docker-load-images.sh"
echo "  scripts/offline-deploy.sh"
echo ""
echo "На офлайн-сервере:"
echo "  ./scripts/offline-deploy.sh"
