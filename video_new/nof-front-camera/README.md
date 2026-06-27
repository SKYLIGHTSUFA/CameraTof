# Видеостена GigE HLS — запуск и тест на Linux

Полная инструкция для **видеостены** (`video_new/nof-front-camera`).

Можно проверить стену **без GigE-камер и без backend** — достаточно сгенерировать фейковые HLS-потоки из видеофайла через `ffmpeg`.

---

## Что делает видеостена

| Компонент | Роль |
|-----------|------|
| `server.js` | HTTP-сервер, API камер, раздача HLS |
| `public/index.html` | Сетка плееров, вкладки по секциям |
| `config.json` | Список камер и секций (`91.1.1` → секция **91**) |
| `hls_output/` | Папка с потоками `camera_91.1.1/index.m3u8` |

**Backend (`app.py`) для проверки стены не нужен.**

---

## Требования (Linux)

```bash
# Ubuntu / Debian
sudo apt update
sudo apt install -y ffmpeg curl jq

# Node.js 18+ (если без Docker)
curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -
sudo apt install -y nodejs

# Docker (опционально)
sudo apt install -y docker.io docker-compose-plugin
```

---

## Быстрый старт (без камер, без Docker)

Выполнять из **корня репозитория** `backend_20260519/`.

### Шаг 1. Подготовить тестовое видео

Любой `.mp4` подойдёт. Можно скачать тестовый ролик:

```bash
# пример: короткий открытый sample (нужен интернет)
curl -L -o sample.mp4 "https://filesamples.com/samples/video/mp4/sample_640x360.mp4"
```

Или использовать свой файл:

```bash
export VIDEO_FILE=/path/to/your/video.mp4
```

### Шаг 2. Запустить эмуляцию камер

```bash
chmod +x scripts/mock-hls-cameras.sh scripts/stop-mock-hls-cameras.sh

# 6 камер из config.json (по умолчанию)
VIDEO_FILE=sample.mp4 ./scripts/mock-hls-cameras.sh --count 6

# или конкретные ID
VIDEO_FILE=sample.mp4 ./scripts/mock-hls-cameras.sh --ids 91.1.1,91.1.3,90.1.1

# или все камеры из config.json (осторожно: много ffmpeg-процессов)
VIDEO_FILE=sample.mp4 ./scripts/mock-hls-cameras.sh --all
```

Проверка:

```bash
ls hls_output/camera_*/index.m3u8
```

### Шаг 3. Запустить видеостену

```bash
cd video_new/nof-front-camera
npm install
npm start
```

По умолчанию сервер использует:
- `../../hls_output` — папка с HLS
- `../../config.json` — список камер
- `./data` — сохранённые раскладки

При необходимости можно переопределить:

```bash
export HLS_OUTPUT_DIR=../../hls_output
export CONFIG_FILE=../../config.json
export PORT=4000
npm start
```

### Шаг 4. Открыть в браузере

```
http://localhost:4000/
```

или с другого ПК в сети:

```
http://<IP_сервера>:4000/
```

Должны появиться:
- вкладка **Все камеры**
- вкладки **Секция 90**, **Секция 91**, …
- камеры с `index.m3u8` — **ONLINE** с видео
- камеры без потока — **OFFLINE**

### Шаг 5. Остановить эмуляцию

```bash
./scripts/stop-mock-hls-cameras.sh
```

---

## Запуск через Docker (Linux)

### Вариант A: тест без RAM-диска

Используйте `docker-compose.test.yml` — монтирует локальную папку `hls_output/`:

```bash
# 1. Эмуляция потоков (из корня репозитория)
VIDEO_FILE=sample.mp4 ./scripts/mock-hls-cameras.sh --count 6

# 2. Сеть Docker (один раз)
docker network create apps 2>/dev/null || true

# 3. Видеостена
cd video_new/nof-front-camera
docker compose -f docker-compose.test.yml up -d --build

# Логи
docker logs -f gige-hls-front
```

Открыть: `http://localhost:4000/`

Остановка:

```bash
docker compose -f docker-compose.test.yml down
cd ../..
./scripts/stop-mock-hls-cameras.sh
```

### Вариант B: production (как на сервере с backend)

Когда backend пишет HLS в RAM-диск:

```bash
sudo mkdir -p /mnt/test_ramdisk
sudo mount -t tmpfs -o size=4G tmpfs /mnt/test_ramdisk

cd video_new/nof-front-camera
docker network create apps 2>/dev/null || true
docker compose up -d --build
```

---

## Проверка без браузера

```bash
# Список камер (JSON)
curl -s http://localhost:4000/api/cameras | jq '.[] | {name, section, hasStream}'

# Секции
curl -s http://localhost:4000/api/sections | jq '.[].name'

# Плейлист одной камеры
curl -s http://localhost:4000/hls/camera_91.1.1/index.m3u8 | head
```

`hasStream: true` — камера должна показывать видео на стене.

---

## Как устроены пути

Backend (когда камеры будут) пишет:

```
/mnt/test_ramdisk/camera_91.1.1/index.m3u8   # на хосте
/app/hls_output/camera_91.1.1/index.m3u8    # в контейнере backend
```

Видеостена читает ту же папку как `/hls_output`:

```
/hls_output/camera_91.1.1/index.m3u8
```

URL в браузере:

```
http://<IP>:4000/hls/camera_91.1.1/index.m3u8
```

---

## Секции и имена камер

Из `config.json`:

```json
"MER2-231-41GC-P(...)": "91.1.1"
```

| Поле | Значение |
|------|----------|
| ID камеры | `91.1.1` |
| Папка HLS | `camera_91.1.1` |
| Секция на стене | `91` (первая часть до точки) |
| Имя на плитке | `91.1.1` |

Секции создаются **автоматически**, вручную прописывать `video` / `video_2` не нужно.

---

## Уменьшить число камер для теста

Скопируйте минимальный конфиг:

```bash
cp config.json config.test.json
```

Оставьте 3–6 камер в `camera_mapping`, затем:

```bash
CONFIG_FILE=../../config.test.json npm start
```

или в Docker:

```yaml
volumes:
  - ../../config.test.json:/app/config.json:ro
```

---

## Пользовательские раскладки («+ Добавить»)

На стене можно сохранить до 7 своих конфигураций (выбор камер и порядок).

Файл хранится в:

```
video_new/nof-front-camera/data/configs.json
```

При перезапуске Docker эта папка сохраняется через volume `./data`.

---

## Что проверяет mock, а что — нет

| Проверяется mock + стена | Не проверяется без камер |
|--------------------------|-------------------------|
| Вкладки по секциям | Захват GigE |
| Сетка 10–60 плиток | Debayer / NVENC в app.py |
| HLS.js в браузере | Нагрузка GPU на 60 камер |
| ONLINE / OFFLINE | ZMQ |
| Пользовательские конфиги | Сеть GigE |

---

## Типичные проблемы

### Все камеры OFFLINE

```bash
ls hls_output/camera_*/index.m3u8
```

Если пусто — перезапустите mock:

```bash
VIDEO_FILE=sample.mp4 ./scripts/mock-hls-cameras.sh --count 6
```

### 404 на `/hls/...`

- Проверьте, что `HLS_OUTPUT_DIR` указывает на ту же папку, куда пишет mock
- В Docker: volume должен быть `../../hls_output:/hls_output:ro`

### Видео не играет, но hasStream=true

```bash
tail hls_output/.mock-hls-logs/camera_91.1.1.log
ffmpeg -version
```

### Порт 4000 занят

```bash
PORT=4001 npm start
```

### Docker: network apps not found

```bash
docker network create apps
```

### Много ffmpeg — высокая нагрузка CPU

Для теста используйте `--count 3` или `--ids`, не `--all` на слабом ПК.

---

## Полный сценарий на Linux (шпаргалка)

```bash
# === Подготовка ===
cd /path/to/backend_20260519
curl -L -o sample.mp4 "https://filesamples.com/samples/video/mp4/sample_640x360.mp4"
chmod +x scripts/*.sh

# === Mock-потоки ===
VIDEO_FILE=sample.mp4 ./scripts/mock-hls-cameras.sh --count 6

# === Стена (без Docker) ===
cd video_new/nof-front-camera
npm install
HLS_OUTPUT_DIR=../../hls_output CONFIG_FILE=../../config.json npm start

# === Проверка ===
curl -s http://localhost:4000/api/cameras | jq length
# Открыть http://localhost:4000/

# === Остановка ===
./scripts/stop-mock-hls-cameras.sh   # из корня репозитория
```

---

## Связь с production

Когда появятся камеры:

1. Остановить mock: `./scripts/stop-mock-hls-cameras.sh`
2. Запустить backend: `docker compose up -d` (из корня)
3. Запустить стену с `/mnt/test_ramdisk` (см. `docker-compose.yml`)

Общая документация по production и офлайн-образам: [README.md](../../README.md) в корне репозитория.
