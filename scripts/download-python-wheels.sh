#!/usr/bin/env bash
# Скачивает Python-зависимости backend в whl/ для офлайн-сборки (Dockerfile_install_libs).
# Запускать на машине с интернетом, Linux x86_64.

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WHL_DIR="${WHL_DIR:-$ROOT_DIR/whl}"
REQ_FILE="${REQ_FILE:-$ROOT_DIR/requirements1.txt}"
BASE_IMAGE="${BASE_IMAGE:-sazonovanton/ffmpeg-opencv-cuda:12.1.1-cudnn8-runtime-python3.11}"

if [[ ! -f "$REQ_FILE" ]]; then
    grep -iv "opencv" "$ROOT_DIR/requirements.txt" > "$ROOT_DIR/requirements1.txt"
    REQ_FILE="$ROOT_DIR/requirements1.txt"
fi

mkdir -p "$WHL_DIR"

echo "==> Скачиваю wheels в $WHL_DIR"
echo "    requirements: $REQ_FILE"
echo "    base image:   $BASE_IMAGE"

if docker image inspect "$BASE_IMAGE" >/dev/null 2>&1; then
    docker run --rm \
        -v "$WHL_DIR:/whl" \
        -v "$REQ_FILE:/req.txt:ro" \
        "$BASE_IMAGE" \
        bash -lc 'pip download -r /req.txt -d /whl --only-binary=:all: 2>/dev/null || pip download -r /req.txt -d /whl'
else
    echo "    Образ $BASE_IMAGE не найден — скачиваю через pip на хосте (manylinux2014_x86_64, py3.11)"
    if ! command -v pip3 >/dev/null 2>&1; then
        echo "Ошибка: нужен Docker с образом $BASE_IMAGE или pip3 на хосте." >&2
        exit 1
    fi
    pip3 download -r "$REQ_FILE" -d "$WHL_DIR" \
        --platform manylinux2014_x86_64 \
        --python-version 311 \
        --implementation cp \
        --abi cp311 \
        --only-binary=:all: 2>/dev/null \
        || pip3 download -r "$REQ_FILE" -d "$WHL_DIR"
fi

echo ""
echo "Готово. Файлов в whl/: $(find "$WHL_DIR" -maxdepth 1 -type f | wc -l | tr -d ' ')"
ls -lh "$WHL_DIR" | head -20
echo ""
echo "Дальше: ./scripts/offline-prepare.sh"
