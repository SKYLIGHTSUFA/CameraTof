#!/usr/bin/env bash
# Запуск на production-сервере БЕЗ интернета (после копирования offline/images/).

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RAMDISK="${RAMDISK:-/mnt/test_ramdisk}"
RAMDISK_SIZE="${RAMDISK_SIZE:-4G}"
FRONTEND_PORT="${FRONTEND_PORT:-4000}"

cd "$ROOT_DIR"

echo "========== GigE HLS — офлайн-деплой =========="

if [[ ! -f "$ROOT_DIR/config.json" ]]; then
    echo "Ошибка: не найден config.json" >&2
    exit 1
fi

echo ""
echo "==> Загрузка Docker-образов"
bash "$ROOT_DIR/scripts/docker-load-images.sh"

echo ""
echo "==> Docker-сеть apps (для frontend)"
docker network inspect apps >/dev/null 2>&1 || docker network create apps

echo ""
echo "==> RAM-диск для HLS: $RAMDISK"
if mountpoint -q "$RAMDISK" 2>/dev/null; then
    echo "    уже смонтирован"
else
    sudo mkdir -p "$RAMDISK"
    if ! sudo mount -t tmpfs -o "size=$RAMDISK_SIZE" tmpfs "$RAMDISK" 2>/dev/null; then
        echo "    не удалось смонтировать tmpfs — используйте обычную папку или смонтируйте вручную"
        sudo mkdir -p "$RAMDISK"
        sudo chmod 777 "$RAMDISK"
    fi
fi

echo ""
echo "==> Backend"
docker compose -f "$ROOT_DIR/docker-compose.yml" up -d --no-build

echo ""
echo "==> Frontend (видеостена)"
docker compose -f "$ROOT_DIR/video_new/nof-front-camera/docker-compose.yml" up -d --no-build

echo ""
echo "========== Запущено =========="
echo ""
docker ps --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}' | grep -E 'gige-hls|NAMES' || true
echo ""
echo "HLS:  ls $RAMDISK/camera_*/index.m3u8"
echo "UI:   http://$(hostname -I 2>/dev/null | awk '{print $1}' || echo 'localhost'):$FRONTEND_PORT/"
echo ""
echo "Логи backend:  docker logs -f gige-hls-galaxysdk"
echo "Логи frontend: docker logs -f gige-hls-front"
