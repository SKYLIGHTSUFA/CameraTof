#!/usr/bin/env bash
# Загрузка Docker-образов с офлайн-носителя.
# Запускать на сервере без интернета после копирования offline/images/

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
IN_DIR="${IN_DIR:-$ROOT_DIR/offline/images}"

BACKEND_IMAGE="${BACKEND_IMAGE:-gige-hls:galaxysdk-v3}"
FRONTEND_IMAGE="${FRONTEND_IMAGE:-gige-hls-front:latest}"

BACKEND_TAR="${BACKEND_TAR:-$IN_DIR/gige-hls-backend.tar}"
FRONTEND_TAR="${FRONTEND_TAR:-$IN_DIR/gige-hls-front.tar}"

if [[ -f "$IN_DIR/images.env" ]]; then
    # shellcheck disable=SC1090
    source "$IN_DIR/images.env"
    BACKEND_TAR="$IN_DIR/${BACKEND_TAR##*/}"
    FRONTEND_TAR="$IN_DIR/${FRONTEND_TAR##*/}"
fi

for tar_file in "$BACKEND_TAR" "$FRONTEND_TAR"; do
    if [[ ! -f "$tar_file" ]]; then
        echo "Ошибка: не найден файл $tar_file" >&2
        echo "Сначала на машине с интернетом выполните: ./scripts/docker-save-images.sh" >&2
        exit 1
    fi
done

echo "==> Загружаю backend из $BACKEND_TAR"
docker load -i "$BACKEND_TAR"

echo "==> Загружаю frontend из $FRONTEND_TAR"
docker load -i "$FRONTEND_TAR"

# Если после load теги отличаются — переименовать в ожидаемые
if ! docker image inspect "$BACKEND_IMAGE" >/dev/null 2>&1; then
    loaded_backend="$(docker images --format '{{.Repository}}:{{.Tag}}' | grep -m1 'gige-hls' || true)"
    if [[ -n "$loaded_backend" && "$loaded_backend" != "$BACKEND_IMAGE" ]]; then
        echo "    docker tag $loaded_backend $BACKEND_IMAGE"
        docker tag "$loaded_backend" "$BACKEND_IMAGE"
    fi
fi

if ! docker image inspect "$FRONTEND_IMAGE" >/dev/null 2>&1; then
    loaded_front="$(docker images --format '{{.Repository}}:{{.Tag}}' | grep -m1 'gige-hls-front' || true)"
    if [[ -n "$loaded_front" && "$loaded_front" != "$FRONTEND_IMAGE" ]]; then
        echo "    docker tag $loaded_front $FRONTEND_IMAGE"
        docker tag "$loaded_front" "$FRONTEND_IMAGE"
    fi
fi

echo ""
echo "Загруженные образы:"
docker images | grep -E 'gige-hls|REPOSITORY' || true

echo ""
echo "Дальше на сервере:"
echo "  docker compose up -d --no-build"
echo "  cd video_new/nof-front-camera && docker compose up -d --no-build"
