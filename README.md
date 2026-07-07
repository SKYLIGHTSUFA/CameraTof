# Gige HLS Galaxy

Захват видео с камер Daheng Galaxy GigE и трансляция в HLS.

- **Backend** (`app.py`) — захват через Harvesters/Galaxy SDK, debayer на GPU, кодирование NVENC, запись в `hls_output/`
- **Frontend** (`video_new/nof-front-camera`) — видеостена с вкладками по секциям (90, 91, 93…)

## Требования

- Linux x86_64
- Docker + Docker Compose
- **NVIDIA GPU** + `nvidia-container-toolkit`
- Daheng Galaxy SDK (внутри Docker-образа backend)
- FFmpeg с `h264_nvenc`, OpenCV с CUDA
- `network_mode: host` на backend — для обнаружения GigE-камер
- RAM-диск или быстрый диск для HLS: `/mnt/test_ramdisk` (по умолчанию в `docker-compose.yml`)

---

## Этап 1. Подготовка онлайн (есть интернет)

Выполняется один раз на машине с интернетом (или на CI), чтобы потом работать офлайн.

### Быстрый способ (один скрипт)

```bash
cd /path/to/backend_20260519
chmod +x scripts/*.sh

# Нужен Galaxy SDK в папке Galaxy_Linux-x86_Gige-U3_.../ (см. Dockerfile)
./scripts/offline-prepare.sh
```

Скрипт по порядку:
1. Скачивает Python-пакеты в `whl/` (`download-python-wheels.sh`)
2. Собирает backend (`Dockerfile` → v2, `Dockerfile_install_libs` → v3)
3. Собирает frontend
4. Сохраняет образы в `offline/images/*.tar`

### Пошагово (если нужен контроль)

**1. Python-зависимости в whl/:**

```bash
./scripts/download-python-wheels.sh
```

Скачивает `numpy`, `harvesters`, `genicam`, `pyzmq` в `whl/` через образ `sazonovanton/ffmpeg-opencv-cuda:12.1.1-cudnn8-runtime-python3.11`.

**2. Собрать backend:**

```bash
docker build -f Dockerfile -t gige-hls:galaxysdk-v2 .
docker compose build   # → gige-hls:galaxysdk-v3
```

**3. Собрать frontend:**

```bash
cd video_new/nof-front-camera && docker compose build && cd ../..
```

**4. Сохранить образы:**

```bash
./scripts/docker-save-images.sh
```

### Что копировать на офлайн-сервер

| Обязательно | Зачем |
|-------------|-------|
| `offline/images/` | Docker-образы |
| `app.py`, `config.json`, `docker-compose.yml` | Backend |
| `video_new/nof-front-camera/` | Видеостена |
| `scripts/offline-deploy.sh`, `scripts/docker-load-images.sh` | Запуск |

`whl/` и `Galaxy_Linux-.../` нужны только для **пересборки** образов на сервере. Для обычного запуска достаточно `offline/images/*.tar`.

> Файлы `*.tar` в git не хранятся (см. `.gitignore`).

---

## Этап 2. Запуск офлайн (без интернета)

На production-сервере: Docker, `nvidia-container-toolkit`, сеть к камерам.

### Быстрый способ

```bash
cd /path/to/backend_20260519
chmod +x scripts/*.sh

# Отредактируйте config.json перед запуском
./scripts/offline-deploy.sh
```

Скрипт: загрузит образы → создаст сеть `apps` → смонтирует RAM-диск → поднимет backend и frontend.

Переменные окружения (опционально):

```bash
RAMDISK=/mnt/test_ramdisk RAMDISK_SIZE=8G ./scripts/offline-deploy.sh
```

### Вручную

```bash
./scripts/docker-load-images.sh
docker network create apps 2>/dev/null || true
sudo mount -t tmpfs -o size=4G tmpfs /mnt/test_ramdisk  # если ещё нет
docker compose up -d --no-build
cd video_new/nof-front-camera && docker compose up -d --no-build
```

Открыть: `http://<IP_сервера>:4000/`  
HLS: `/mnt/test_ramdisk/camera_91.1.1/index.m3u8`

---

## Конфигурация (`config.json`)

| Параметр | Описание |
|----------|----------|
| `base_output_dir` | Папка HLS внутри контейнера (`hls_output`) |
| `width`, `height` | Разрешение на выходе в HLS |
| `cam_width`, `cam_height` | ROI на камере (кроп, не resize) |
| `fps_target` | Желаемый FPS |
| `max_camera_fps` | Потолок FPS (защита сети), по умолчанию 10 |
| `software_fps_limit` | Программный throttle в Python, по умолчанию `false` |
| `use_nvenc` | `true` — кодирование на GPU |
| `zmq_enabled` | `false` — отключить ZMQ, если не нужен инференс (foam) |
| `zmq_inference_fps` | Лимит full-frame в ZMQ (2 = 2 кадра/сек на камеру) |
| `zmq_inference_burst_pairs` | `true` — раз в сек парой подряд идущих кадров |
| `camera_mapping` | Имя камеры → ID (`91.1.1` → папка `camera_91.1.1`) |

Секции на видеостене берутся автоматически из первой части ID: `91.1.1` → секция **91**.

### Характеристики камеры (Daheng MER2)

Параметры применяются к самой камере через GenICam при старте (нужен `"update_params": true`).
Задаются глобально в корне `config.json` (для всех камер) и/или индивидуально в `camera_mapping`.

| Параметр | GenICam-узел | Значения / пример |
|----------|--------------|-------------------|
| `exposure_auto` | `ExposureAuto` | `Off` / `Once` / `Continuous` |
| `exposure_time_us` | `ExposureTime` | напр. `30000` (µs), при `exposure_auto: "Off"` |
| `auto_exposure_min_us` / `auto_exposure_max_us` | `AutoExposureTimeMin/Max` | границы для авто-экспозиции |
| `gain_auto` | `GainAuto` | `Off` / `Once` / `Continuous` |
| `gain` | `Gain` | напр. `6.0` (дБ), при `gain_auto: "Off"` |
| `auto_gain_min` / `auto_gain_max` | `AutoGainMin/Max` | границы для авто-gain |
| `balance_white_auto` | `BalanceWhiteAuto` | `Continuous` (авто, убирает синеву) / `Once` / `Off` |
| `balance_ratio` | `BalanceRatioSelector` + `BalanceRatio` | ручной ББ: `{ "Red": 1.6, "Green": 1.0, "Blue": 1.35 }` (только при `balance_white_auto: "Off"`) |
| `gamma_enable` / `gamma` | `GammaEnable` / `Gamma` | `true` + `0.9` (<1 — светлее тени) |
| `black_level` | `BlackLevel` | напр. `2` |
| `sharpness` | `Sharpness` | резкость |
| `saturation` | `Saturation` | насыщенность (color-модели) |
| `digital_shift` | `DigitalShift` | сдвиг битов яркости |
| `genicam` | любой узел | сырой passthrough: `{ "TestPattern": "Off" }` — применяется последним |

**Синий оттенок / неправильные цвета** — это баланс белого. Быстрый способ: `"balance_white_auto": "Continuous"`
(камера сама подстроит). Для стабильного результата — `"balance_white_auto": "Off"` + ручные `balance_ratio`
(увеличить `Red`, уменьшить `Blue`).

**Засветы / пересветы** — уменьшить `exposure_time_us` и/или `gain`; при `exposure_auto: "Continuous"`
ограничить `auto_exposure_max_us`.

Формат `camera_mapping`: значение может быть строкой (только алиас, берёт глобальные настройки) или объектом:

```json
"camera_mapping": {
  "MER2-231-41GC-P(10.228.0.32[...])": "90.1.3",
  "MER2-231-41GC-P(10.228.0.31[...])": {
    "alias": "90.1.1",
    "exposure_auto": "Off",
    "exposure_time_us": 30000,
    "gain": 6.0,
    "balance_white_auto": "Off",
    "balance_ratio": { "Red": 1.6, "Green": 1.0, "Blue": 1.35 },
    "gamma": 0.9,
    "update_params": true
  }
}
```

> Названия GenICam-узлов и допустимые диапазоны видно в Daheng Galaxy Viewer. Если узла нет у модели —
> параметр тихо пропускается, в логах старта камеры печатается, что реально применилось.

---

## Структура HLS

```
/mnt/test_ramdisk/          # на хосте (= /app/hls_output в backend)
├── camera_90.1.1/
│   ├── index.m3u8
│   └── index0.ts
├── camera_91.1.1/
│   └── ...
```

---

## Быстрая шпаргалка

**Тест видеостены без камер (Linux):** см. [video_new/nof-front-camera/README.md](video_new/nof-front-camera/README.md)

**Онлайн (подготовка):**
```bash
./scripts/offline-prepare.sh
```

**Офлайн (production):**
```bash
./scripts/offline-deploy.sh
```

**Проверка:**
```bash
ls /mnt/test_ramdisk/camera_*/
curl -s http://localhost:4000/api/cameras | head
```
