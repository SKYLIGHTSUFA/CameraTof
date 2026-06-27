#!/usr/bin/env bash
# Эмуляция HLS-потоков из видеофайла для проверки видеостены без GigE-камер.
#
# Пример:
#   VIDEO_FILE=/path/to/sample.mp4 ./scripts/mock-hls-cameras.sh --count 6
#   ./scripts/stop-mock-hls-cameras.sh

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG_FILE="${CONFIG_FILE:-$ROOT_DIR/config.json}"
HLS_OUTPUT_DIR="${HLS_OUTPUT_DIR:-$ROOT_DIR/hls_output}"
VIDEO_FILE="${VIDEO_FILE:-}"
COUNT="${COUNT:-6}"
FPS="${FPS:-5}"
WIDTH="${WIDTH:-480}"
HEIGHT="${HEIGHT:-300}"
PID_FILE="${PID_FILE:-$ROOT_DIR/hls_output/.mock-hls.pids}"
LOG_DIR="${LOG_DIR:-$ROOT_DIR/hls_output/.mock-hls-logs}"

usage() {
    cat <<'EOF'
Использование:
  VIDEO_FILE=/path/to/video.mp4 ./scripts/mock-hls-cameras.sh [опции]

Опции:
  --count N       Запустить первые N камер из config.json (по умолчанию: 6)
  --all           Запустить все камеры из config.json
  --ids ID,...    Явный список ID камер (91.1.1,90.1.1)
  --config PATH   Путь к config.json
  --output DIR    Папка HLS (по умолчанию: ./hls_output)
  -h, --help      Справка

Переменные окружения:
  VIDEO_FILE      Видеофайл-источник (обязательно)
  FPS             FPS потока (по умолчанию: 5)
  WIDTH, HEIGHT   Разрешение (по умолчанию: 480x300)

Примеры:
  VIDEO_FILE=sample.mp4 ./scripts/mock-hls-cameras.sh --count 3
  VIDEO_FILE=sample.mp4 ./scripts/mock-hls-cameras.sh --ids 91.1.1,91.1.3,90.1.1
EOF
}

require_cmd() {
    command -v "$1" >/dev/null 2>&1 || {
        echo "Ошибка: не найдена команда '$1'" >&2
        exit 1
    }
}

load_camera_ids() {
    python3 - "$CONFIG_FILE" <<'PY'
import json, sys
path = sys.argv[1]
with open(path, encoding="utf-8") as f:
    mapping = json.load(f).get("camera_mapping", {})
ids = []
for value in mapping.values():
    if isinstance(value, str) and value.strip():
        ids.append(value.strip())
print("\n".join(ids))
PY
}

MODE="count"
EXPLICIT_IDS=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --count)
            MODE="count"
            COUNT="$2"
            shift 2
            ;;
        --all)
            MODE="all"
            shift
            ;;
        --ids)
            MODE="ids"
            EXPLICIT_IDS="$2"
            shift 2
            ;;
        --config)
            CONFIG_FILE="$2"
            shift 2
            ;;
        --output)
            HLS_OUTPUT_DIR="$2"
            PID_FILE="$HLS_OUTPUT_DIR/.mock-hls.pids"
            LOG_DIR="$HLS_OUTPUT_DIR/.mock-hls-logs"
            shift 2
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "Неизвестный аргумент: $1" >&2
            usage
            exit 1
            ;;
    esac
done

require_cmd ffmpeg
require_cmd python3

if [[ -z "$VIDEO_FILE" ]]; then
    echo "Ошибка: укажите VIDEO_FILE=/path/to/video.mp4" >&2
    exit 1
fi

if [[ ! -f "$VIDEO_FILE" ]]; then
    echo "Ошибка: видеофайл не найден: $VIDEO_FILE" >&2
    exit 1
fi

if [[ ! -f "$CONFIG_FILE" ]]; then
    echo "Ошибка: config не найден: $CONFIG_FILE" >&2
    exit 1
fi

mapfile -t ALL_IDS < <(load_camera_ids)
if [[ ${#ALL_IDS[@]} -eq 0 ]]; then
    echo "Ошибка: в $CONFIG_FILE нет camera_mapping" >&2
    exit 1
fi

CAMERA_IDS=()
case "$MODE" in
    all)
        CAMERA_IDS=("${ALL_IDS[@]}")
        ;;
    ids)
        IFS=',' read -r -a CAMERA_IDS <<< "$EXPLICIT_IDS"
        ;;
    count)
        for ((i = 0; i < COUNT && i < ${#ALL_IDS[@]}; i++)); do
            CAMERA_IDS+=("${ALL_IDS[i]}")
        done
        ;;
esac

mkdir -p "$HLS_OUTPUT_DIR" "$LOG_DIR"
: > "$PID_FILE"

echo "==> Эмуляция ${#CAMERA_IDS[@]} камер"
echo "    Видео:    $VIDEO_FILE"
echo "    HLS:      $HLS_OUTPUT_DIR"
echo "    Config:   $CONFIG_FILE"
echo ""

for camera_id in "${CAMERA_IDS[@]}"; do
    camera_id="${camera_id//[[:space:]]/}"
    [[ -z "$camera_id" ]] && continue

    out_dir="$HLS_OUTPUT_DIR/camera_${camera_id}"
    mkdir -p "$out_dir"

    log_file="$LOG_DIR/camera_${camera_id}.log"
    playlist="$out_dir/index.m3u8"

    # Очистить старые сегменты
    rm -f "$out_dir"/*.ts "$playlist" 2>/dev/null || true

    echo "    camera_${camera_id}"

    nohup ffmpeg -hide_banner -loglevel warning \
        -re -stream_loop -1 -i "$VIDEO_FILE" \
        -an \
        -c:v libx264 -preset ultrafast -tune zerolatency \
        -pix_fmt yuv420p \
        -s "${WIDTH}x${HEIGHT}" -r "$FPS" \
        -f hls \
        -hls_time 1 \
        -hls_list_size 6 \
        -hls_flags delete_segments+append_list+omit_endlist \
        "$playlist" \
        >"$log_file" 2>&1 &

    echo $! >> "$PID_FILE"
    sleep 0.2
done

echo ""
echo "Готово. Проверка:"
echo "  ls $HLS_OUTPUT_DIR/camera_*/index.m3u8"
echo ""
echo "Остановка:"
echo "  ./scripts/stop-mock-hls-cameras.sh"
