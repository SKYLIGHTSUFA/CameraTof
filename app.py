#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import time
import queue
import threading
import subprocess
import traceback
import json
import zmq

import numpy as np

from harvesters.core import Harvester
import genicam.gentl as gentl

print("=" * 70)
print("ЗАПУСК СИСТЕМЫ ЗАХВАТА ВИДЕО С КАМЕР (HARVESTERS + GALAXY SDK)")
print("=" * 70)

# OpenCV с CUDA (опционально)
try:
    import cv2  # type: ignore
    _CV2_CUDA_AVAILABLE = False
    try:
        if hasattr(cv2, "cuda") and cv2.cuda.getCudaEnabledDeviceCount() > 0:
            _CV2_CUDA_AVAILABLE = True
    except Exception:
        _CV2_CUDA_AVAILABLE = False
        print("⚠️  OpenCV установлен, но CUDA недоступна")
except Exception:
    cv2 = None  # type: ignore
    _CV2_CUDA_AVAILABLE = False
    print("⚠️  OpenCV не установлен")


# =========================
# КОНФИГУРАЦИЯ
# =========================
CONFIG_FILE = os.getenv("CONFIG_FILE", "config.json")

def load_config(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"⚠️ Ошибка загрузки конфига {path}: {e}")
        return {}

config = load_config(CONFIG_FILE)

BASE_OUTPUT_DIR = config.get("base_output_dir", "hls_output")

# CTI file for the GenTL producer (Daheng Galaxy GigE)
CTI_FILE_PATH = config.get("cti_file_path", "/opt/galaxy_sdk/lib/x86_64/GxGVTL.cti")

# Разрешение на выходе (то что уходит в HLS)
WIDTH = config.get("width", 480)
HEIGHT = config.get("height", 240)

# Желаемое разрешение камеры
CAM_WIDTH = config.get("cam_width", 1920)
CAM_HEIGHT = config.get("cam_height", 1200)

PIXEL_FORMAT_TARGET = config.get("pixel_format", "BayerGB8")
EXPOSURE_TIME_US = config.get("exposure_time_us", 10000.0)
UPDATE_PARAMS = config.get("update_params", False)

FPS_TARGET = float(config.get("fps_target", 20))
MAX_CAMERA_FPS = float(config.get("max_camera_fps", 10))
EFFECTIVE_FPS_TARGET = min(FPS_TARGET, MAX_CAMERA_FPS)
SOFTWARE_FPS_LIMIT = config.get("software_fps_limit", False)
MAX_RETRIES = config.get("max_retries", 3)
RETRY_DELAY = config.get("retry_delay", 2)

# GPU (None = автоопределение через nvidia-smi; иначе список ID, например [0, 1])
GPU_DEVICE_IDS: list | None = config.get("gpu_device_ids", None)
USE_NVENC = config.get("use_nvenc", True)

HLS_SEGMENT_SEC = config.get("hls_segment_sec", 1)
HLS_LIST_SIZE = config.get("hls_list_size", 6)

# Размер пула буферов GenTL на камеру (backward-compat: aravis_buffer_count)
GENTL_BUFFER_COUNT = config.get("gentl_buffer_count", config.get("aravis_buffer_count", 16))

# Таймаут ожидания кадра в секундах (backward-compat: aravis_pop_timeout_us)
_timeout_us = config.get("aravis_pop_timeout_us", 1_500_000)
FETCH_TIMEOUT_S = config.get("fetch_timeout_s", _timeout_us / 1_000_000)

ZMQ_ENABLED = config.get("zmq_enabled", True)
ZMQ_PORT_BASE = config.get("zmq_port_base", 5555)
ZMQ_FORMAT = config.get("zmq_format", "raw") # "raw" or "jpeg" (or nvjpeg)
# 0 = send every capture frame; 2 = max 2 full frames/sec per camera (for foam inference)
ZMQ_INFERENCE_FPS = float(config.get("zmq_inference_fps", 0))
# True: every 2/zmq_inference_fps sec send 2 consecutive capture frames back-to-back
ZMQ_INFERENCE_BURST_PAIRS = bool(config.get("zmq_inference_burst_pairs", False))

# =========================
# СООТВЕТСТВИЕ DISPLAY_NAME -> ID
# =========================
CAMERA_MAPPING = config.get("camera_mapping", {})


def get_camera_id(display_name, address=None):
    if display_name in CAMERA_MAPPING:
        return CAMERA_MAPPING[display_name]
    if address and address in CAMERA_MAPPING:
        return CAMERA_MAPPING[address]
    safe_name = ''.join(c for c in display_name if c.isalnum() or c in '._-')
    print(f"⚠️  Камера '{display_name}' не найдена в CAMERA_MAPPING, используется '{safe_name}'")
    return safe_name


# =========================
# ПРОВЕРКА NVENC
# =========================
def check_nvenc_available():
    try:
        result = subprocess.run(
            ['ffmpeg', '-hide_banner', '-encoders'],
            capture_output=True, text=True, timeout=10
        )
        available = 'h264_nvenc' in result.stdout
        if available:
            print("✅ h264_nvenc доступен")
        else:
            print("⚠️  h264_nvenc НЕ найден в ffmpeg, используем libx264")
        return available
    except Exception as e:
        print(f"⚠️  Ошибка проверки ffmpeg: {e}")
        return False


def detect_nvidia_gpus() -> list:
    try:
        result = subprocess.run(
            ['nvidia-smi', '--query-gpu=index,name', '--format=csv,noheader'],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode != 0:
            print(f"⚠️  nvidia-smi rc={result.returncode}: {result.stderr.strip()}")
            return []

        gpus = []
        for line in result.stdout.strip().splitlines():
            parts = [p.strip() for p in line.split(',', 1)]
            if not parts or not parts[0].isdigit():
                continue
            idx = int(parts[0])
            name = parts[1] if len(parts) > 1 else "?"
            gpus.append(idx)
            print(f"   GPU {idx}: {name}")
        return gpus
    except FileNotFoundError:
        print("⚠️  nvidia-smi не найден в PATH")
        return []
    except Exception as e:
        print(f"⚠️  Ошибка nvidia-smi: {e}")
        return []


# =========================
# NODEMAP HELPERS
# =========================
def safe_set_node(nodemap, name: str, value) -> bool:
    """Set a GenICam node value by name, silently skipping on any error."""
    try:
        if hasattr(nodemap, name):
            node = getattr(nodemap, name)
            if hasattr(node, 'value'):
                node.value = value
                return True
    except Exception:
        pass
    return False


def safe_get_node(nodemap, name: str, default="Unknown"):
    """Read a GenICam node value by name without breaking camera startup."""
    try:
        if hasattr(nodemap, name):
            node = getattr(nodemap, name)
            if hasattr(node, 'value'):
                return node.value
    except Exception:
        pass
    return default


def try_set_camera_params(device, camera_name: str) -> bool:
    try:
        nm = device.remote_device.node_map
        if nm is None:
            print(f"[CAM {camera_name}] NodeMap unavailable")
            return False

        if UPDATE_PARAMS:
            safe_set_node(nm, "TriggerMode", "Off")
            safe_set_node(nm, "ExposureAuto", "Off")
            safe_set_node(nm, "GainAuto", "Off")
            fps_mode_ok = safe_set_node(nm, "AcquisitionFrameRateMode", "On")
            fps_enable_ok = safe_set_node(nm, "AcquisitionFrameRateEnable", True)
            fps_ok = safe_set_node(nm, "AcquisitionFrameRate", EFFECTIVE_FPS_TARGET)
            real_fps = safe_get_node(nm, "AcquisitionFrameRate")
            fps_mode = safe_get_node(nm, "AcquisitionFrameRateMode")
            fps_enable = safe_get_node(nm, "AcquisitionFrameRateEnable")
            print(
                f"[CAM {camera_name}] AcquisitionFrameRate target={EFFECTIVE_FPS_TARGET} "
                f"(config={FPS_TARGET}, max={MAX_CAMERA_FPS}), "
                f"actual={real_fps}, mode={fps_mode}, enable={fps_enable}, "
                f"set_ok={fps_ok}, mode_ok={fps_mode_ok}, enable_ok={fps_enable_ok}"
            )

            # ROI
            try:
                nm.Width.value  = CAM_WIDTH
                nm.Height.value = CAM_HEIGHT
                nm.OffsetX.value = 0
                nm.OffsetY.value = 0
                print(f"[CAM {camera_name}] Region = {CAM_WIDTH}x{CAM_HEIGHT}")
            except Exception as e:
                print(f"[CAM {camera_name}] не удалось установить ROI: {e}")

            # Pixel format
            try:
                nm.PixelFormat.value = PIXEL_FORMAT_TARGET
                print(f"[CAM {camera_name}] PixelFormat = {PIXEL_FORMAT_TARGET}")
            except Exception as e:
                print(f"[CAM {camera_name}] PixelFormat ({PIXEL_FORMAT_TARGET}) не удалось: {e}")

            # Exposure
            try:
                if hasattr(nm, "ExposureTime"):
                    nm.ExposureTime.value = float(EXPOSURE_TIME_US)
                elif hasattr(nm, "ExposureTimeAbs"):
                    nm.ExposureTimeAbs.value = float(EXPOSURE_TIME_US)
                elif hasattr(nm, "ExposureTimeRaw"):
                    nm.ExposureTimeRaw.value = int(EXPOSURE_TIME_US)
                print(f"[CAM {camera_name}] ExposureTime = {EXPOSURE_TIME_US} us")
            except Exception as e:
                print(f"[CAM {camera_name}] не удалось установить экспозицию: {e}")

            # Gain
            try:
                if hasattr(nm, "Gain"):
                    nm.Gain.value = 15.0
                elif hasattr(nm, "GainRaw"):
                    nm.GainRaw.value = 150
                print(f"[CAM {camera_name}] Gain = 15.0")
            except Exception as e:
                print(f"[CAM {camera_name}] не удалось установить Gain: {e}")

            safe_set_node(nm, "GevSCPSPacketSize", 8000)
        else:
            print(f"[CAM {camera_name}] ⏩ Обновление параметров пропущено (update_params=false)")

        # Log actual params
        try:    real_fmt = nm.PixelFormat.value
        except Exception: real_fmt = "Unknown"
        try:    exp_value = nm.ExposureTime.value
        except Exception: exp_value = "Unknown"
        try:    gain_value = nm.Gain.value
        except Exception: gain_value = "Unknown"
        try:    w = nm.Width.value; h = nm.Height.value
        except Exception: w = h = "?"

        print(
            f"[CAM {camera_name}] ⚙️ PixFmt={real_fmt}, ROI={w}x{h}, "
            f"Exp={exp_value}us, Gain={gain_value}dB"
        )
        return True

    except Exception as e:
        print(f"[CAM {camera_name}] ⚠️ config failed: {e}")
        return False


def genicam_to_ffmpeg_pixfmt(pf: str) -> str:
    key = (pf or "").upper().replace("_", "")
    pixfmt_map = {
        "BAYERRG8": "bayer_rggb8",
        "BAYERBG8": "bayer_bggr8",
        "BAYERGB8": "bayer_gbrg8",
        "BAYERGR8": "bayer_grbg8",
        "MONO8": "gray",
        "RGB8": "rgb24",
        "BGR8": "bgr24",
        "YUV422PACKED": "yuyv422",
        "YUV422_8_UYVY": "uyvy422",
    }
    if key in pixfmt_map:
        return pixfmt_map[key]
    print(f"⚠️  Неизвестный PixelFormat '{pf}', пробуем bayer_rggb8")
    return "bayer_rggb8"


def detect_frame_shape_and_dtype(pixel_format: str, width: int, height: int, data_len: int):
    key = (pixel_format or "").upper().replace("_", "")

    if key in ("BAYERRG8", "BAYERBG8", "BAYERGB8", "BAYERGR8", "MONO8"):
        expected = width * height
        if data_len < expected:
            print(f"⚠️  buffer too small: {data_len} < {expected}")
        return (height, width), np.uint8

    if key in ("RGB8", "BGR8"):
        expected = width * height * 3
        if data_len < expected:
            print(f"⚠️  buffer too small: {data_len} < {expected}")
        return (height, width, 3), np.uint8

    if key in ("YUV422PACKED", "YUV422_8_UYVY"):
        expected = width * height * 2
        if data_len < expected:
            print(f"⚠️  buffer too small: {data_len} < {expected}")
        return (height, width, 2), np.uint8

    # Fallback
    expected = width * height
    if data_len >= width * height * 2:
        return (height, width, 2), np.uint8
    if data_len >= width * height * 3:
        return (height, width, 3), np.uint8
    return (height, width), np.uint8


# =========================
# HLS STREAMER
# =========================
class HLSStreamer:
    def __init__(self, camera_id, display_name, use_nvenc: bool, gpu_id: int = 0):
        self.camera_id = camera_id
        self.display_name = display_name
        self.use_nvenc = use_nvenc
        self.gpu_id = gpu_id
        self.output_dir = os.path.join(BASE_OUTPUT_DIR, f"camera_{camera_id}")
        os.makedirs(self.output_dir, exist_ok=True)
        self.process = None
        self.running = False
        self.src_pix_fmt = None
        self.src_w = CAM_WIDTH
        self.src_h = CAM_HEIGHT

    def _build_cmd(self):
        gop = max(1, round(EFFECTIVE_FPS_TARGET * HLS_SEGMENT_SEC))

        if self.use_nvenc:
            # Если на входе уже yuv420p (сделан GPU-дебайер в Python) — сразу грузим на GPU и масштабируем.
            # Иначе конвертируем в yuv420p на CPU перед hwupload.
            if self.src_pix_fmt == "yuv420p":
                vf = f"hwupload_cuda,scale_cuda={WIDTH}:{HEIGHT}:interp_algo=bilinear"
            else:
                vf = f"format=yuv420p,hwupload_cuda,scale_cuda={WIDTH}:{HEIGHT}:interp_algo=bilinear"
            encode_args = [
                "-init_hw_device", f"cuda=cu:{self.gpu_id}",
                "-filter_hw_device", "cu",
                "-vf", vf,
                "-c:v", "h264_nvenc",
                "-preset", "p3",
                "-tune", "ull",
                "-gpu", str(self.gpu_id),
                "-b:v", "600k",
                "-g", str(gop),
                "-keyint_min", str(gop),
                "-rc", "cbr", #vbr
                "-rc-lookahead", "0",
                "-delay", "0",
            ]
        else:
            encode_args = [
                "-vf", f"scale={WIDTH}:{HEIGHT}",
                "-c:v", "libx264",
                "-preset", "ultrafast",
                "-tune", "zerolatency",
                "-pix_fmt", "yuv420p",
                "-b:v", "800k",
                "-g", str(gop),
                "-keyint_min", str(gop),
            ]

        return [
            "ffmpeg", "-y",
            "-loglevel", "warning",
            "-fflags", "nobuffer",
            "-flags", "low_delay",
            "-probesize", "128k", #32
            "-analyzeduration", "0",
            "-f", "rawvideo",
            "-pix_fmt", self.src_pix_fmt,
            "-s", f"{self.src_w}x{self.src_h}",
            "-r", str(EFFECTIVE_FPS_TARGET),
            "-i", "-",
            *encode_args,
            "-f", "hls",
            "-hls_time", str(HLS_SEGMENT_SEC),
            "-hls_list_size", str(HLS_LIST_SIZE),
            "-hls_flags", "delete_segments+append_list+omit_endlist",
            os.path.join(self.output_dir, "index.m3u8"),
        ]

    def start(self, src_pix_fmt: str, src_w: int, src_h: int):
        self.src_pix_fmt = src_pix_fmt
        self.src_w = src_w
        self.src_h = src_h

        for f in os.listdir(self.output_dir):
            if f.endswith(".ts") or f == "index.m3u8":
                try:
                    os.remove(os.path.join(self.output_dir, f))
                except Exception:
                    pass

        cmd = self._build_cmd()

        if self.src_pix_fmt == "yuv420p":
            frame_bytes = src_w * src_h * 3 // 2
        elif self.src_pix_fmt in ("rgb24", "bgr24"):
            frame_bytes = src_w * src_h * 3
        elif self.src_pix_fmt in ("yuyv422", "uyvy422"):
            frame_bytes = src_w * src_h * 2
        else:
            frame_bytes = src_w * src_h
        pipe_buf = max(frame_bytes * 8, 1 << 20)

        self.process = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            bufsize=pipe_buf,
        )
        self.running = True

        threading.Thread(
            target=self._drain_stderr,
            daemon=True,
            name=f"FFmpegLog_{self.camera_id}"
        ).start()

        enc = "h264_nvenc (GPU)" if self.use_nvenc else "libx264 (CPU)"
        print(
            f"[CAM {self.display_name} ({self.camera_id})] "
            f"🎬 HLS started [{enc}] src={src_w}x{src_h}:{src_pix_fmt}"
        )

    def _drain_stderr(self):
        try:
            for raw_line in self.process.stderr:
                line = raw_line.decode(errors="replace").strip()
                if line:
                    print(f"[FFmpeg {self.camera_id}] {line}")
        except Exception:
            pass

    def send(self, frame):
        if not self.running or self.process is None or self.process.stdin is None:
            return
        try:
            if isinstance(frame, np.ndarray):
                buf = frame.data if frame.flags["C_CONTIGUOUS"] else memoryview(np.ascontiguousarray(frame))
            else:
                buf = frame
            self.process.stdin.write(buf)
        except (BrokenPipeError, ValueError, OSError):
            self.running = False
        except Exception:
            self.running = False

    def stop(self):
        self.running = False
        if self.process:
            try:
                if self.process.stdin:
                    self.process.stdin.close()
                self.process.terminate()
                self.process.wait(timeout=3)
            except Exception:
                self.process.kill()
        print(f"[CAM {self.display_name} ({self.camera_id})] 🎬 HLS stopped")


# =========================
# CAMERA WORKER
# =========================
class CameraWorker:
    # Маппинг Bayer-паттерна → код OpenCV для demosaicing (Bayer → BGR).
    # ВАЖНО: cv2.cuda.demosaicing ожидает константы из cv2.cuda.COLOR_Bayer*,
    # которые могут отличаться от cv2.COLOR_Bayer* (разные enum'ы).
    # Если в сборке OpenCV их нет — падаем на обычные cv2.COLOR_Bayer* (это ловит probe).
    _BAYER_CV_CODES_BGR = {}
    _BAYER_CV_CODES_CPU = {}
    if cv2 is not None:
        _BAYER_CV_CODES_CPU = {
            "BAYERRG8": cv2.COLOR_BayerRG2BGR,
            "BAYERBG8": cv2.COLOR_BayerBG2BGR,
            "BAYERGB8": cv2.COLOR_BayerGB2BGR,
            "BAYERGR8": cv2.COLOR_BayerGR2BGR,
        }
        _cuda_ns = getattr(cv2, "cuda", None)
        _BAYER_CV_CODES_BGR = {
            "BAYERRG8": getattr(_cuda_ns, "COLOR_BayerRG2BGR", cv2.COLOR_BayerRG2BGR),
            "BAYERBG8": getattr(_cuda_ns, "COLOR_BayerBG2BGR", cv2.COLOR_BayerBG2BGR),
            "BAYERGB8": getattr(_cuda_ns, "COLOR_BayerGB2BGR", cv2.COLOR_BayerGB2BGR),
            "BAYERGR8": getattr(_cuda_ns, "COLOR_BayerGR2BGR", cv2.COLOR_BayerGR2BGR),
        }
        del _cuda_ns

    def __init__(self, device, camera_id: str, display_name: str, use_nvenc: bool, gpu_id: int = 0, zmq_ctx: zmq.Context | None = None):
        self.device       = device      # harvesters ImageAcquirer
        self.camera_id    = camera_id
        self.display_name = display_name
        self.gpu_id       = gpu_id
        self.running      = True
        self.zmq_ctx      = zmq_ctx
        self.zmq_socket   = None

        # Setup ZMQ interface for low latency inference (connecting to inproc proxy)
        if self.zmq_ctx is not None:
            try:
                self.zmq_socket = self.zmq_ctx.socket(zmq.PUB)
                # Setting High Water Mark to drop frames if inference is too slow reading
                self.zmq_socket.setsockopt(zmq.SNDHWM, 2)
                self.zmq_socket.connect("inproc://zmq_workers")
            except Exception as e:
                print(f"[CAM {self.display_name} ({self.camera_id})] ⚠️ ZMQ init failed: {e}")
                self.zmq_socket = None

        self.q = queue.Queue(maxsize=2)
        self.hls = HLSStreamer(camera_id, display_name, use_nvenc, gpu_id)

        self.fps_count = 0
        self.last_fps = time.time()
        self.last_empty_log = 0
        self.fetch_count = 0
        self.last_fetch_log = time.time()
        self._drop_count = 0
        self._last_drop_log = time.time()

        self.frame_interval = 1.0 / EFFECTIVE_FPS_TARGET if EFFECTIVE_FPS_TARGET > 0 else 0.0
        self.last_frame_time = 0.0

        self.pixel_format = "Unknown"
        self._use_cuda_debayer = _CV2_CUDA_AVAILABLE

        self._zmq_prev_capture = None
        self._zmq_last_publish = 0.0

    @staticmethod
    def _copy_for_zmq(raw):
        if isinstance(raw, np.ndarray):
            return raw.copy()
        return bytes(raw)

    def _send_zmq_frame(self, raw, now: float, width: int, height: int) -> bool:
        if self.zmq_socket is None:
            return False
        is_np = isinstance(raw, np.ndarray)
        try:
            if ZMQ_FORMAT in ("jpeg", "nvjpeg"):
                if is_np:
                    img_to_encode = raw
                else:
                    c_shape, _ = detect_frame_shape_and_dtype(self.pixel_format, width, height, len(raw))
                    img_to_encode = np.frombuffer(raw, dtype=np.uint8).reshape(c_shape)

                ret, encoded = cv2.imencode('.jpg', img_to_encode, [int(cv2.IMWRITE_JPEG_QUALITY), 85])
                if not ret:
                    return False
                msg_bytes = encoded.tobytes()
                meta = {
                    "camera_id": self.camera_id,
                    "display_name": self.display_name,
                    "timestamp": now,
                    "format": "jpeg",
                    "pixel_format": self.pixel_format
                }
            else:
                if is_np:
                    c_shape = raw.shape
                    dtype_str = str(raw.dtype)
                    msg_bytes = raw.data if raw.flags["C_CONTIGUOUS"] else np.ascontiguousarray(raw).data
                else:
                    c_shape, _ = detect_frame_shape_and_dtype(self.pixel_format, width, height, len(raw))
                    dtype_str = "uint8"
                    msg_bytes = raw
                meta = {
                    "camera_id": self.camera_id,
                    "display_name": self.display_name,
                    "timestamp": now,
                    "format": "raw",
                    "shape": c_shape,
                    "dtype": dtype_str,
                    "pixel_format": self.pixel_format
                }
            topic = f"cam_{self.camera_id}".encode('ascii')
            self.zmq_socket.send(topic, zmq.SNDMORE)
            self.zmq_socket.send_json(meta, zmq.SNDMORE)
            self.zmq_socket.send(msg_bytes, copy=False)
            return True
        except Exception as e:
            if self.fetch_count == 0:
                print(f"[CAM {self.camera_id}] ZMQ Send error: {e}")
            return False

    def _maybe_publish_zmq(self, raw, now: float, width: int, height: int):
        if self.zmq_socket is None:
            return

        if ZMQ_INFERENCE_FPS <= 0:
            self._send_zmq_frame(raw, now, width, height)
            return

        if ZMQ_INFERENCE_BURST_PAIRS:
            burst_interval = 2.0 / ZMQ_INFERENCE_FPS
            prev = self._zmq_prev_capture
            self._zmq_prev_capture = self._copy_for_zmq(raw)
            if prev is None:
                return
            if now - self._zmq_last_publish < burst_interval:
                return
            self._zmq_last_publish = now
            self._send_zmq_frame(prev, now, width, height)
            self._send_zmq_frame(raw, now, width, height)
            return

        min_interval = 1.0 / ZMQ_INFERENCE_FPS
        if now - self._zmq_last_publish < min_interval:
            return
        self._zmq_last_publish = now
        self._send_zmq_frame(raw, now, width, height)

    def _debayer_cuda(self, bayer: np.ndarray, key: str) -> np.ndarray:
        """GPU demosaicing: raw Bayer uint8 (H,W) → BGR uint8 (H,W,3).

        Выдаём именно BGR24, а не YUV_I420, чтобы избежать багов со
        stride/padding между планами Y/U/V при передаче в ffmpeg -f rawvideo.
        Конвертацию в yuv420p сделает сам ffmpeg (фильтр format=yuv420p
        в _build_cmd), перед hwupload_cuda.
        """
        code_bgr = self._BAYER_CV_CODES_BGR.get(key, cv2.COLOR_BayerRG2BGR)
        gpu_in = cv2.cuda_GpuMat()
        gpu_in.upload(bayer)

        # Python binding: cv2.cuda.demosaicing(src, code[, dst[, dcn[, stream]]]) -> dst
        gpu_bgr = cv2.cuda.demosaicing(gpu_in, code_bgr)

        bgr = gpu_bgr.download()
        if not bgr.flags["C_CONTIGUOUS"]:
            bgr = np.ascontiguousarray(bgr)
        return bgr

    def _debayer_cpu(self, bayer: np.ndarray, key: str) -> np.ndarray:
        """CPU demosaicing через OpenCV: raw Bayer uint8 (H,W) → BGR uint8 (H,W,3).

        Используется как fallback, если CUDA недоступна или произошла ошибка.
        OpenCV на CPU делает debayer корректно (в отличие от swscale в ffmpeg,
        который для сырых Bayer*8 склонен давать диагональные артефакты).
        """
        if cv2 is None:
            raise RuntimeError("OpenCV (cv2) не установлен — CPU debayer недоступен")
        code_bgr = self._BAYER_CV_CODES_CPU.get(key, cv2.COLOR_BayerRG2BGR)
        bgr = cv2.cvtColor(bayer, code_bgr)
        if not bgr.flags["C_CONTIGUOUS"]:
            bgr = np.ascontiguousarray(bgr)
        return bgr

    def capture(self):
        print(f"[CAM {self.display_name} ({self.camera_id})] 📷 START capture")
        try:
            try_set_camera_params(self.device, f"{self.display_name} ({self.camera_id})")

            try:
                self.pixel_format = self.device.remote_device.node_map.PixelFormat.value
            except Exception:
                self.pixel_format = "BayerRG8"

            # Bind GPU context for this thread and probe cv2.cuda.demosaicing
            if self._use_cuda_debayer:
                try:
                    cv2.cuda.setDevice(self.gpu_id)
                    if not hasattr(cv2.cuda, "demosaicing"):
                        print(
                            f"[CAM {self.display_name} ({self.camera_id})] ⚠️ "
                            f"cv2.cuda.demosaicing недоступен — GPU debayer отключён"
                        )
                        self._use_cuda_debayer = False
                    else:
                        try:
                            probe = cv2.cuda_GpuMat()
                            probe.upload(np.zeros((4, 4), dtype=np.uint8))
                            probe_code = self._BAYER_CV_CODES_BGR.get(
                                "BAYERRG8", cv2.COLOR_BayerRG2BGR
                            )
                            cv2.cuda.demosaicing(probe, probe_code)
                        except Exception as e:
                            print(
                                f"[CAM {self.display_name} ({self.camera_id})] ⚠️ "
                                f"cv2.cuda.demosaicing probe failed: {e} — GPU debayer отключён"
                            )
                            self._use_cuda_debayer = False
                except Exception as e:
                    print(f"[CAM {self.display_name} ({self.camera_id})] ⚠️ cv2.cuda.setDevice({self.gpu_id}) failed: {e}")
                    self._use_cuda_debayer = False

            # Configure buffer pool then start acquisition
            self.device.num_buffers = GENTL_BUFFER_COUNT
            self.device.start()
            time.sleep(0.3)

        except Exception as e:
            print(f"[CAM {self.display_name} ({self.camera_id})] ❌ start error: {e}")
            self.running = False
            return

        while self.running:
            try:
                with self.device.fetch(timeout=FETCH_TIMEOUT_S) as buf:
                    if buf is None or not buf.payload.components:
                        continue

                    comp = buf.payload.components[0]
                    width  = comp.width
                    height = comp.height

                    if width <= 0 or height <= 0:
                        continue

                    now = time.time()
                    if (
                        SOFTWARE_FPS_LIMIT
                        and self.frame_interval > 0
                        and now - self.last_frame_time < self.frame_interval
                    ):
                        self.fetch_count += 1
                        continue

                    # comp.data is a numpy 1-D uint8 view on the internal buffer.
                    # Everything that leaves this `with` block MUST be a copy.
                    data = comp.data
                    if data is None or data.size == 0:
                        continue

                    key = (self.pixel_format or "").upper().replace("_", "")
                    bayer_keys = {"BAYERRG8", "BAYERBG8", "BAYERGB8", "BAYERGR8"}
                    is_bayer   = key in bayer_keys and cv2 is not None

                    if not self.hls.running:
                        src_fmt = "bgr24" if is_bayer else genicam_to_ffmpeg_pixfmt(self.pixel_format)
                        self.hls.start(src_fmt, width, height)

                    if is_bayer:
                        total_bytes    = data.size
                        expected_bytes = width * height

                        if total_bytes == expected_bytes:
                            # .copy() because data is a view on Harvesters' internal buffer;
                            # the buffer is returned to the pool on `with` exit.
                            raw_bayer = data.reshape(height, width).copy()
                        elif (
                            total_bytes > expected_bytes
                            and total_bytes % height == 0
                            and (total_bytes // height) >= width
                            and (total_bytes // height) % 4 == 0
                        ):
                            stride_pixels = total_bytes // height
                            padded = data.reshape(height, stride_pixels)
                            # np.ascontiguousarray on a non-contiguous slice already copies
                            raw_bayer = np.ascontiguousarray(padded[:, :width])
                        else:
                            raw_bayer = data[:expected_bytes].reshape(height, width).copy()

                        if self.fetch_count == 0:
                            print(
                                f"[CAM {self.display_name} ({self.camera_id})] "
                                f"Bayer buffer: size={total_bytes}, expected={expected_bytes}, "
                                f"width={width}, height={height}, "
                                f"stride={total_bytes // height if height else 0}"
                            )

                        # Debayer → BGR24 (result is always a fresh array, safe to queue)
                        if self._use_cuda_debayer:
                            try:
                                raw = self._debayer_cuda(raw_bayer, key)
                            except Exception as e:
                                print(
                                    f"[CAM {self.display_name} ({self.camera_id})] "
                                    f"⚠️ GPU debayer failed, fallback to CPU OpenCV: {e}"
                                )
                                self._use_cuda_debayer = False
                                raw = self._debayer_cpu(raw_bayer, key)
                        else:
                            raw = self._debayer_cpu(raw_bayer, key)

                    else:
                        # Non-Bayer: strip stride/padding/chunk bytes, then copy
                        shape, dtype = detect_frame_shape_and_dtype(
                            self.pixel_format, width, height, data.size
                        )
                        bpp       = shape[2] if len(shape) == 3 else 1
                        row_bytes = width * bpp
                        frame_bytes = row_bytes * height
                        total     = data.size

                        if total == frame_bytes:
                            raw = bytes(data)
                        elif (
                            total > frame_bytes
                            and total % height == 0
                            and (total // height) > row_bytes
                            and (total // height) % 4 == 0
                        ):
                            stride_bytes  = total // height
                            stride_pixels = stride_bytes // bpp
                            if len(shape) == 2:
                                padded = data[:stride_pixels * height].reshape(height, stride_pixels)
                                raw = np.ascontiguousarray(padded[:, :width]).tobytes()
                            else:
                                padded = data[:stride_pixels * height * bpp].reshape(height, stride_pixels, bpp)
                                raw = np.ascontiguousarray(padded[:, :width, :]).tobytes()
                        else:
                            raw = bytes(data[:frame_bytes])

                        if self.fetch_count == 0:
                            print(
                                f"[CAM {self.display_name} ({self.camera_id})] "
                                f"CPU buffer: size={total}, frame_bytes={frame_bytes}, "
                                f"width={width}, height={height}, bpp={bpp}, "
                                f"extra={total - frame_bytes}"
                            )

                    self._maybe_publish_zmq(raw, now, width, height)

                    # Atomic drop-oldest + push: avoids the race where encode
                    # drains the queue between our get_nowait and put_nowait.
                    while True:
                        try:
                            self.q.put_nowait(raw)
                            break
                        except queue.Full:
                            try:
                                self.q.get_nowait()
                                self._drop_count += 1
                            except queue.Empty:
                                break

                    self.last_frame_time = now
                    self.fetch_count += 1

                now = time.time()
                if now - self.last_fetch_log >= 5.0:
                    print(
                        f"[CAM {self.display_name} ({self.camera_id})] "
                        f"🔍 Capture FPS: {self.fetch_count / 5.0:.1f}, drops={self._drop_count}"
                    )
                    self.fetch_count  = 0
                    self._drop_count  = 0
                    self.last_fetch_log = now

            except gentl.TimeoutException:
                continue
            except Exception as e:
                if not self.running:
                    break
                print(f"[CAM {self.display_name} ({self.camera_id})] fetch error: {e}")
                time.sleep(0.1)

    def encode(self):
        print(f"[CAM {self.display_name} ({self.camera_id})] 🎞 START encode")
        while self.running:
            try:
                frame = self.q.get(timeout=1.0)
                self.hls.send(frame)
                self.fps_count += 1

                now = time.time()
                if now - self.last_fps >= 5.0:
                    print(
                        f"[CAM {self.display_name} ({self.camera_id})] "
                        f"HLS FPS={self.fps_count / 5.0:.1f}"
                    )
                    self.fps_count = 0
                    self.last_fps = now

            except queue.Empty:
                now = time.time()
                if now - self.last_empty_log > 5.0:
                    print(f"[CAM {self.display_name} ({self.camera_id})] ⏳ нет кадров...")
                    self.last_empty_log = now

    def stop(self):
        self.running = False
        self.hls.stop()
        try:
            self.device.stop()
        except Exception:
            pass
        print(f"[CAM {self.display_name} ({self.camera_id})] Stopped")


# =========================
# ОТКРЫТИЕ КАМЕРЫ С ПОВТОРНЫМИ ПОПЫТКАМИ
# =========================
def open_camera_with_retry(h: Harvester, index: int, max_retries: int = MAX_RETRIES):
    for attempt in range(max_retries):
        try:
            if attempt > 0:
                print(f"   Попытка {attempt + 1}/{max_retries} для камеры {index}...")
                time.sleep(RETRY_DELAY)

            device = h.create(index)
            if device is not None:
                print(f"   Камера {index} открыта")
                return device

        except Exception as e:
            err = str(e)
            print(f"   Ошибка камеры {index}: {err}")

            if "-1005" in err or "denied" in err.lower():
                # Access-denied: force-destroy dangling handle and retry
                try:
                    tmp = h.create(index)
                    if tmp:
                        tmp.destroy()
                        time.sleep(1)
                except Exception:
                    pass

    print(f"   Не удалось открыть камеру {index}")
    return None


# =========================
# ENUMERATE CAMERAS
# =========================
def list_cameras(h: Harvester) -> list:
    cameras = []
    for i, di in enumerate(h.device_info_list):
        display_name = di.display_name or f"camera_{i}"
        camera_id    = get_camera_id(display_name)
        cameras.append({
            "index":        i,
            "display_name": display_name,
            "camera_id":    camera_id,
        })
    return cameras


# =========================
# MAIN
# =========================
def main():
    try:
        subprocess.run(['ffmpeg', '-version'], capture_output=True, check=True)
        print("✅ FFmpeg найден")
    except Exception:
        print("❌ FFmpeg не найден")
        return

    use_nvenc = USE_NVENC and check_nvenc_available()

    if _CV2_CUDA_AVAILABLE:
        try:
            n = cv2.cuda.getCudaEnabledDeviceCount()
            print(f"✅ OpenCV CUDA доступен (GPU count={n}) — debayer на GPU")
        except Exception:
            print("✅ OpenCV CUDA доступен — debayer на GPU")
    else:
        print("⚠️  OpenCV CUDA недоступен — debayer на CPU через OpenCV")

    gpu_ids: list = []
    if use_nvenc:
        if GPU_DEVICE_IDS is None:
            print("\n🔍 Автоопределение GPU...")
            gpu_ids = detect_nvidia_gpus()
            if not gpu_ids:
                print("⚠️  GPU не обнаружены, откатываемся на libx264 (CPU)")
                use_nvenc = False
            else:
                print(f"✅ Найдено GPU: {len(gpu_ids)} → {gpu_ids}")
        else:
            gpu_ids = list(GPU_DEVICE_IDS)
            print(f"✅ Заданы GPU вручную: {gpu_ids}")

    if not gpu_ids:
        gpu_ids = [0]

    print(f"\n📷 Инициализация Harvester (CTI: {CTI_FILE_PATH})...")
    h = Harvester()
    try:
        h.add_file(CTI_FILE_PATH)
        print(f"✅ Драйвер загружен: {CTI_FILE_PATH}")
    except Exception as e:
        print(f"❌ Ошибка загрузки CTI: {e}")
        return

    h.update()

    cameras_info = list_cameras(h)
    camera_count = len(cameras_info)

    print(f"📸 Найдено камер: {camera_count}")
    if camera_count == 0:
        print("❌ Камеры не обнаружены.")
        return

    print("\n📋 Список камер:")
    for cam in cameras_info:
        print(f"   [{cam['index']}] {cam['display_name']} -> ID: {cam['camera_id']}")

    print("\n🔧 Открытие камер...")
    devices_with_info = []
    for cam in cameras_info:
        device = open_camera_with_retry(h, cam["index"])
        if device is not None:
            devices_with_info.append({**cam, "device": device})

    print(f"\n✅ Открыто {len(devices_with_info)} из {camera_count} камер")
    if not devices_with_info:
        print("❌ Нет доступных камер.")
        return

    zmq_ctx = None
    if ZMQ_ENABLED:
        try:
            zmq_ctx = zmq.Context()
            xpub = zmq_ctx.socket(zmq.XPUB)
            xpub.bind(f"tcp://0.0.0.0:{ZMQ_PORT_BASE}")
            xsub = zmq_ctx.socket(zmq.XSUB)
            xsub.bind("inproc://zmq_workers")
            threading.Thread(target=zmq.proxy, args=(xsub, xpub), daemon=True, name="ZMQ_Proxy").start()
            if ZMQ_INFERENCE_FPS > 0:
                mode = (
                    f"burst pairs every {2.0 / ZMQ_INFERENCE_FPS:.2f}s"
                    if ZMQ_INFERENCE_BURST_PAIRS
                    else f"{ZMQ_INFERENCE_FPS:.1f} fps throttle"
                )
                print(f"   ZMQ inference limit: {mode}")
            print(f"\n🚀 ZMQ XPUB bound on port {ZMQ_PORT_BASE}. All cameras publish here via topics.")
        except Exception as e:
            print(f"\n⚠️ ZMQ Proxy init failed: {e}")
            zmq_ctx = None

    print("\n🚀 Запуск потоков...")
    workers = []
    for i, info in enumerate(devices_with_info):
        gpu_id = gpu_ids[i % len(gpu_ids)]

        w = CameraWorker(
            info["device"],
            info["camera_id"],
            info["display_name"],
            use_nvenc,
            gpu_id,
            zmq_ctx=zmq_ctx
        )
        workers.append(w)

        threading.Thread(
            target=w.capture, daemon=True,
            name=f"Capture_{info['camera_id']}"
        ).start()

        threading.Thread(
            target=w.encode, daemon=True,
            name=f"Encode_{info['camera_id']}"
        ).start()

    print("\n" + "=" * 70)
    print("▶️  СИСТЕМА ЗАПУЩЕНА")
    enc_label = "h264_nvenc (GPU)" if use_nvenc else "libx264 (CPU)"
    print(f"🖥️  Энкодер: {enc_label}")
    print(f"📁 HLS: {BASE_OUTPUT_DIR}/")
    for w in workers:
        print(f"   • {w.display_name} -> camera_{w.camera_id}/index.m3u8")
    print("=" * 70 + "\n")

    try:
        while True:
            time.sleep(5)
            active = sum(1 for w in workers if w.running)
            if active < len(workers):
                print(f"⚠️  Активных камер: {active}/{len(workers)}")
    except KeyboardInterrupt:
        print("\n⏹️  Остановка...")
        for w in workers:
            w.stop()
        time.sleep(1)
        for w in workers:
            try:
                w.device.destroy()
            except Exception:
                pass
        try:
            h.reset()
        except Exception:
            pass
        print("✅ Завершено")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"\n❌ КРИТИЧЕСКАЯ ОШИБКА: {e}")
        traceback.print_exc()