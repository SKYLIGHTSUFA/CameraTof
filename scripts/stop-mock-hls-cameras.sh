#!/usr/bin/env bash
# Остановить ffmpeg-процессы, запущенные mock-hls-cameras.sh

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HLS_OUTPUT_DIR="${HLS_OUTPUT_DIR:-$ROOT_DIR/hls_output}"
PID_FILE="${PID_FILE:-$HLS_OUTPUT_DIR/.mock-hls.pids}"

if [[ ! -f "$PID_FILE" ]]; then
    echo "PID-файл не найден: $PID_FILE"
    echo "Возможно, mock-потоки уже остановлены."
    exit 0
fi

stopped=0
while read -r pid; do
    [[ -z "$pid" ]] && continue
    if kill -0 "$pid" 2>/dev/null; then
        kill "$pid" 2>/dev/null || true
        stopped=$((stopped + 1))
        echo "Остановлен PID $pid"
    fi
done < "$PID_FILE"

rm -f "$PID_FILE"
echo "Остановлено процессов: $stopped"
