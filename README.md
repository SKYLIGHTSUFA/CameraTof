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

### 1.1. Собрать backend

```bash
cd /path/to/backend_20260519

# Если базового образа ещё нет — полная сборка с нуля:
# docker build -f Dockerfile -t gige-hls:galaxysdk-v2

# Сборка рабочего образа (поверх v2 или уже загруженного tar):
docker compose build
```

Образ: `gige-hls:galaxysdk-v3`

### 1.2. Собрать frontend (видеостена)

```bash
cd video_new/nof-front-camera
docker compose build
cd ../..
```

Образ: `gige-hls-front:latest`

### 1.3. Сохранить образы для офлайн-сервера

```bash
chmod +x scripts/docker-save-images.sh scripts/docker-load-images.sh
./scripts/docker-save-images.sh
```

Создаст папку `offline/images/`:

```
offline/images/
├── gige-hls-backend.tar   # backend
├── gige-hls-front.tar     # видеостена
└── images.env             # имена образов
```

Скопируйте **всю папку `offline/images/`** на production-сервер (флешка, scp, rsync).

> Файлы `*.tar` в git не хранятся (см. `.gitignore`).

---

## Этап 2. Запуск офлайн (без интернета)

На production-сервере должны быть: Docker, `nvidia-container-toolkit`, сеть к камерам, RAM-диск.

### 2.1. Загрузить образы

```bash
cd /path/to/backend_20260519
./scripts/docker-load-images.sh
```

### 2.2. Подготовить конфиг и RAM-диск

1. Отредактировать `config.json` (`camera_mapping`, `fps_target`, `max_camera_fps`…)
2. Создать RAM-диск (если ещё нет):

```bash
sudo mkdir -p /mnt/test_ramdisk
sudo mount -t tmpfs -o size=4G tmpfs /mnt/test_ramdisk
```

### 2.3. Запустить backend

```bash
docker compose up -d --no-build
docker logs -f gige-hls-galaxysdk
```

HLS пишется в `/mnt/test_ramdisk/camera_91.1.1/index.m3u8` и т.д.

### 2.4. Запустить видеостену

```bash
cd video_new/nof-front-camera
docker compose up -d --no-build
```

Открыть в браузере: `http://<IP_сервера>:4000/`

Поток одной камеры: `http://<IP>:4000/hls/camera_91.1.1/index.m3u8`

### 2.5. Сеть Docker для frontend

Перед первым запуском frontend создайте сеть (если её нет):

```bash
docker network create apps
```

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
| `zmq_enabled` | `false` — отключить ZMQ, если не нужен инференс |
| `camera_mapping` | Имя камеры → ID (`91.1.1` → папка `camera_91.1.1`) |

Секции на видеостене берутся автоматически из первой части ID: `91.1.1` → секция **91**.

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

## Какие файлы нужны, а какие нет

### Нужны для production (GPU)

| Файл / папка | Назначение |
|--------------|------------|
| `app.py` | Backend захвата |
| `config.json` | Конфигурация камер |
| `docker-compose.yml` | Backend на GPU |
| `Dockerfile`, `Dockerfile_install_libs` | Сборка образа backend |
| `requirements.txt`, `whl/` | Python-зависимости |
| `Galaxy_Linux-.../` | SDK Daheng (для сборки) |
| `video_new/nof-front-camera/` | Видеостена |
| `scripts/docker-save-images.sh` | Сохранение образов (онлайн) |
| `scripts/docker-load-images.sh` | Загрузка образов (офлайн) |

### Можно удалить (не нужны для GigE + GPU)

| Файл / папка | Почему |
|--------------|--------|
| `docker-compose.cpu.yml` | Только CPU, без NVENC — вам не нужен |
| `test_config.json` | Тестовый конфиг на 1 камеру |
| `video_new/video/`, `video_2/`, `VIDEO_ALL/` | Старая RTSP-схема |
| `Hls/` | Старые RTSP frontend/go |
| `video_new/*.tar` | Локальные бэкапы образов (держать на сервере, не в git) |
| `app_20260427.py`, `config_20260518.json` и т.п. | Старые бэкапы, если есть |

### Опционально (для отладки, не для production)

| Файл | Назначение |
|------|------------|
| `test_video_capture.py` | Просмотр кадров из ZMQ — только если `zmq_enabled: true` |
| `profile.sh` | Профилирование `py-spy`, не для обычного запуска |

---

## Быстрая шпаргалка

**Онлайн (подготовка):**
```bash
docker compose build
cd video_new/nof-front-camera && docker compose build && cd ../..
./scripts/docker-save-images.sh
```

**Офлайн (production):**
```bash
./scripts/docker-load-images.sh
docker compose up -d --no-build
cd video_new/nof-front-camera && docker compose up -d --no-build
```

**Проверка:**
```bash
ls /mnt/test_ramdisk/camera_*/
curl -s http://localhost:4000/api/cameras | head
```
