#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import time
import queue
import threading
import subprocess
import traceback
import json
import signal
import zmq

import numpy as np

from harvesters.core import Harvester
import genicam.gentl as gentl

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

CAMERA_SOURCE = str(config.get("camera_source", "gige")).strip().lower()
RTSP_CAMERAS: list = config.get("rtsp_cameras", [])
RTSP_TRANSPORT = str(config.get("rtsp_transport", "tcp")).strip().lower()

if CAMERA_SOURCE == "rtsp":
    os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = f"rtsp_transport;{RTSP_TRANSPORT}"

print("=" * 70)
if CAMERA_SOURCE == "rtsp":
    print("ЗАПУСК СИСТЕМЫ ЗАХВАТА ВИДЕО С RTSP-КАМЕР")
else:
    print("ЗАПУСК СИСТЕМЫ ЗАХВАТА ВИДЕО С КАМЕР (HARVESTERS + GALAXY SDK)")
print("=" * 70)

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

# Нет кадров дольше этого порога — очистка HLS и переподключение камеры
STREAM_STALE_SEC = float(config.get("stream_stale_sec", 30))
RECONNECT_ENABLED = config.get("reconnect_enabled", True)
RECONNECT_RETRY_SEC = float(config.get("reconnect_retry_sec", 120))

# GigE discovery: several passes merged by MAC (id_) to reduce missed cameras
DISCOVERY_PASSES = int(config.get("discovery_passes", 5))
DISCOVERY_PASS_DELAY_SEC = float(config.get("discovery_pass_delay_sec", 3.0))

ZMQ_ENABLED = config.get("zmq_enabled", True)
ZMQ_PORT_BASE = config.get("zmq_port_base", 5555)
ZMQ_FORMAT = config.get("zmq_format", "raw") # "raw" or "jpeg" (or nvjpeg)
# 0 = send every capture frame; 2 = max 2 full frames/sec per camera (for foam inference)
ZMQ_INFERENCE_FPS = float(config.get("zmq_inference_fps", 0))
# True: every 2/zmq_inference_fps sec send 2 consecutive capture frames back-to-back
ZMQ_INFERENCE_BURST_PAIRS = bool(config.get("zmq_inference_burst_pairs", False))

# =========================
# СООТВЕТСТВИЕ DISPLAY_NAME -> ID + ПАРАМЕТРЫ КАМЕРЫ
# =========================
CAMERA_MAPPING = config.get("camera_mapping", {})

# snake_case-ключи camera_mapping, которые обрабатываются отдельно и НЕ передаются
# в GenICam node map как есть (у них своя логика применения).
_INTERNAL_CAMERA_CONFIG_KEYS = frozenset({
    "alias", "camera_id", "genicam", "genicam_extra", "width", "height",
    "cam_width", "cam_height", "fps_target", "max_camera_fps", "pixel_format",
    "update_params",
    "exposure_auto", "exposure_time_us", "auto_exposure_min_us", "auto_exposure_max_us",
    "gain_auto", "gain", "auto_gain_min", "auto_gain_max",
    "balance_white_auto", "balance_ratio",
    "black_level", "gamma", "gamma_enable", "sharpness", "saturation", "digital_shift",
})


def build_global_camera_defaults() -> dict:
    """Глобальные значения по умолчанию для всех камер (из корня config.json)."""
    defaults = config.get("camera_defaults", {})
    merged = {
        "cam_width": CAM_WIDTH,
        "cam_height": CAM_HEIGHT,
        "fps_target": FPS_TARGET,
        "max_camera_fps": MAX_CAMERA_FPS,
        "pixel_format": PIXEL_FORMAT_TARGET,
        "update_params": UPDATE_PARAMS,
        "exposure_time_us": EXPOSURE_TIME_US,
    }
    # Необязательные глобальные характеристики камеры
    for key in (
        "exposure_auto", "auto_exposure_min_us", "auto_exposure_max_us",
        "gain_auto", "gain", "auto_gain_min", "auto_gain_max",
        "balance_white_auto", "balance_ratio",
        "black_level", "gamma", "gamma_enable", "sharpness", "saturation", "digital_shift",
    ):
        if key in config:
            merged[key] = config[key]
    if isinstance(defaults, dict):
        merged.update(defaults)
    return merged


def _mapping_value_for(display_name: str, address=None):
    if display_name in CAMERA_MAPPING:
        return CAMERA_MAPPING[display_name]
    if address and address in CAMERA_MAPPING:
        return CAMERA_MAPPING[address]
    return None


def get_camera_alias(mapping_value) -> str | None:
    if isinstance(mapping_value, str):
        alias = mapping_value.strip()
        return alias or None
    if isinstance(mapping_value, dict):
        alias = mapping_value.get("alias") or mapping_value.get("camera_id")
        if alias is not None:
            alias = str(alias).strip()
            return alias or None
    return None


def resolve_camera_config(mapping_value) -> dict:
    """Глобальные настройки + переопределения для конкретной камеры."""
    cfg = build_global_camera_defaults()
    if not isinstance(mapping_value, dict):
        return cfg
    for key, value in mapping_value.items():
        if key in ("alias", "camera_id"):
            continue
        if key == "balance_ratio" and isinstance(value, dict):
            base_wb = cfg.get("balance_ratio")
            if isinstance(base_wb, dict):
                cfg["balance_ratio"] = {**base_wb, **value}
            else:
                cfg["balance_ratio"] = dict(value)
        elif key == "genicam" and isinstance(value, dict):
            extra = cfg.get("genicam_extra")
            if not isinstance(extra, dict):
                extra = {}
            extra.update(value)
            cfg["genicam_extra"] = extra
        else:
            cfg[key] = value
    return cfg


def get_camera_id(display_name, address=None):
    alias = get_camera_alias(_mapping_value_for(display_name, address))
    if alias:
        return alias
    safe_name = ''.join(c for c in display_name if c.isalnum() or c in '._-')
    print(f"⚠️  Камера '{display_name}' не найдена в CAMERA_MAPPING, используется '{safe_name}'")
    return safe_name


def get_camera_config(display_name, address=None) -> dict:
    return resolve_camera_config(_mapping_value_for(display_name, address))


def get_all_expected_camera_ids() -> set:
    ids: set = set()
    for value in CAMERA_MAPPING.values():
        alias = get_camera_alias(value)
        if alias:
            ids.add(alias)
    return ids


def get_all_rtsp_camera_ids() -> set:
    ids: set = set()
    for entry in RTSP_CAMERAS:
        if not isinstance(entry, dict):
            continue
        cam_id = entry.get("id") or entry.get("alias")
        if cam_id is not None:
            cam_id = str(cam_id).strip()
            if cam_id:
                ids.add(cam_id)
    return ids


def resolve_rtsp_camera_config(cam_entry: dict) -> dict:
    """Глобальные настройки + переопределения для конкретной RTSP-камеры."""
    cfg = build_global_camera_defaults()
    if not isinstance(cam_entry, dict):
        return cfg
    for key, value in cam_entry.items():
        if key in ("id", "alias", "rtsp_url"):
            continue
        cfg[key] = value
    return cfg


def effective_fps_for_config(cam_cfg: dict) -> float:
    fps = float(cam_cfg.get("fps_target", FPS_TARGET))
    cap = float(cam_cfg.get("max_camera_fps", MAX_CAMERA_FPS))
    return min(fps, cap) if fps > 0 else 0.0


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


def _cfg_has_genicam_key(cam_cfg: dict, name: str) -> bool:
    extra = cam_cfg.get("genicam_extra")
    return isinstance(extra, dict) and name in extra


def _set_and_log(nm, camera_name: str, node: str, value) -> bool:
    ok = safe_set_node(nm, node, value)
    if ok:
        print(f"[CAM {camera_name}] {node} = {value}")
    else:
        print(f"[CAM {camera_name}] ⚠️ узел {node} недоступен/не принял {value}")
    return ok


def _set_exposure(nm, camera_name: str, cfg: dict) -> None:
    mode = cfg.get("exposure_auto", "Off")
    _set_and_log(nm, camera_name, "ExposureAuto", mode)
    if str(mode) != "Off":
        if "auto_exposure_min_us" in cfg:
            safe_set_node(nm, "AutoExposureTimeMin", float(cfg["auto_exposure_min_us"]))
        if "auto_exposure_max_us" in cfg:
            safe_set_node(nm, "AutoExposureTimeMax", float(cfg["auto_exposure_max_us"]))
        return
    exposure_us = float(cfg.get("exposure_time_us", EXPOSURE_TIME_US))
    try:
        if hasattr(nm, "ExposureTime"):
            nm.ExposureTime.value = exposure_us
        elif hasattr(nm, "ExposureTimeAbs"):
            nm.ExposureTimeAbs.value = exposure_us
        elif hasattr(nm, "ExposureTimeRaw"):
            nm.ExposureTimeRaw.value = int(exposure_us)
        print(f"[CAM {camera_name}] ExposureTime = {exposure_us} us")
    except Exception as e:
        print(f"[CAM {camera_name}] не удалось установить экспозицию: {e}")


def _set_gain(nm, camera_name: str, cfg: dict) -> None:
    mode = cfg.get("gain_auto", "Off")
    _set_and_log(nm, camera_name, "GainAuto", mode)
    if str(mode) != "Off":
        if "auto_gain_min" in cfg:
            safe_set_node(nm, "AutoGainMin", float(cfg["auto_gain_min"]))
        if "auto_gain_max" in cfg:
            safe_set_node(nm, "AutoGainMax", float(cfg["auto_gain_max"]))
        return
    if "gain" not in cfg:
        return
    gain_value = float(cfg["gain"])
    try:
        if hasattr(nm, "Gain"):
            nm.Gain.value = gain_value
        elif hasattr(nm, "GainRaw"):
            nm.GainRaw.value = int(gain_value * 10)
        print(f"[CAM {camera_name}] Gain = {gain_value} dB")
    except Exception as e:
        print(f"[CAM {camera_name}] не удалось установить Gain: {e}")


def _set_white_balance(nm, camera_name: str, cfg: dict) -> None:
    """Баланс белого на камере (color-модели). Убирает синий/цветной оттенок."""
    mode = cfg.get("balance_white_auto")
    ratios = cfg.get("balance_ratio")

    if mode is not None:
        _set_and_log(nm, camera_name, "BalanceWhiteAuto", mode)

    # Ручные коэффициенты применимы только когда авто выключен
    if isinstance(ratios, dict) and ratios:
        if mode is None:
            _set_and_log(nm, camera_name, "BalanceWhiteAuto", "Off")
        elif str(mode) != "Off":
            print(
                f"[CAM {camera_name}] ⚠️ balance_ratio игнорируется, "
                f"т.к. balance_white_auto={mode}"
            )
            return
        for channel in ("Red", "Green", "Blue"):
            if channel not in ratios:
                continue
            if not safe_set_node(nm, "BalanceRatioSelector", channel):
                print(f"[CAM {camera_name}] ⚠️ BalanceRatioSelector недоступен")
                break
            val = float(ratios[channel])
            if safe_set_node(nm, "BalanceRatio", val):
                print(f"[CAM {camera_name}] BalanceRatio[{channel}] = {val}")
            else:
                print(f"[CAM {camera_name}] ⚠️ BalanceRatio[{channel}] не принят")


def _set_image_quality(nm, camera_name: str, cfg: dict) -> None:
    """Гамма, black level, резкость, насыщенность, digital shift — на самой камере."""
    if "gamma_enable" in cfg:
        _set_and_log(nm, camera_name, "GammaEnable", bool(cfg["gamma_enable"]))
    if "gamma" in cfg:
        if "gamma_enable" not in cfg:
            safe_set_node(nm, "GammaEnable", True)
        _set_and_log(nm, camera_name, "Gamma", float(cfg["gamma"]))
    if "black_level" in cfg:
        _set_and_log(nm, camera_name, "BlackLevel", float(cfg["black_level"]))
    if "sharpness" in cfg:
        _set_and_log(nm, camera_name, "Sharpness", cfg["sharpness"])
    if "saturation" in cfg:
        _set_and_log(nm, camera_name, "Saturation", cfg["saturation"])
    if "digital_shift" in cfg:
        _set_and_log(nm, camera_name, "DigitalShift", int(cfg["digital_shift"]))


def _apply_genicam_extra(nm, cam_cfg: dict, camera_name: str) -> None:
    """Произвольные GenICam-узлы из блока genicam{} (сырой passthrough)."""
    extra = cam_cfg.get("genicam_extra")
    if not isinstance(extra, dict):
        return
    for name, value in extra.items():
        _set_and_log(nm, camera_name, str(name), value)


def try_set_camera_params(device, camera_name: str, cam_cfg: dict | None = None) -> bool:
    try:
        cfg = cam_cfg or build_global_camera_defaults()
        nm = device.remote_device.node_map
        if nm is None:
            print(f"[CAM {camera_name}] NodeMap unavailable")
            return False

        update_params = bool(cfg.get("update_params", UPDATE_PARAMS))
        cam_width = int(cfg.get("cam_width", CAM_WIDTH))
        cam_height = int(cfg.get("cam_height", CAM_HEIGHT))
        pixel_format = cfg.get("pixel_format", PIXEL_FORMAT_TARGET)
        target_fps = effective_fps_for_config(cfg)

        if update_params:
            if not _cfg_has_genicam_key(cfg, "TriggerMode"):
                safe_set_node(nm, "TriggerMode", "Off")

            if not _cfg_has_genicam_key(cfg, "AcquisitionFrameRate"):
                fps_mode_ok = safe_set_node(nm, "AcquisitionFrameRateMode", "On")
                fps_enable_ok = safe_set_node(nm, "AcquisitionFrameRateEnable", True)
                fps_ok = safe_set_node(nm, "AcquisitionFrameRate", target_fps)
                real_fps = safe_get_node(nm, "AcquisitionFrameRate")
                fps_mode = safe_get_node(nm, "AcquisitionFrameRateMode")
                fps_enable = safe_get_node(nm, "AcquisitionFrameRateEnable")
                print(
                    f"[CAM {camera_name}] AcquisitionFrameRate target={target_fps} "
                    f"(config={cfg.get('fps_target', FPS_TARGET)}, "
                    f"max={cfg.get('max_camera_fps', MAX_CAMERA_FPS)}), "
                    f"actual={real_fps}, mode={fps_mode}, enable={fps_enable}, "
                    f"set_ok={fps_ok}, mode_ok={fps_mode_ok}, enable_ok={fps_enable_ok}"
                )

            # ROI
            try:
                nm.Width.value = cam_width
                nm.Height.value = cam_height
                nm.OffsetX.value = 0
                nm.OffsetY.value = 0
                print(f"[CAM {camera_name}] Region = {cam_width}x{cam_height}")
            except Exception as e:
                print(f"[CAM {camera_name}] не удалось установить ROI: {e}")

            # Pixel format
            try:
                nm.PixelFormat.value = pixel_format
                print(f"[CAM {camera_name}] PixelFormat = {pixel_format}")
            except Exception as e:
                print(f"[CAM {camera_name}] PixelFormat ({pixel_format}) не удалось: {e}")

            # Характеристики изображения на самой камере
            _set_exposure(nm, camera_name, cfg)
            _set_gain(nm, camera_name, cfg)
            _set_white_balance(nm, camera_name, cfg)
            _set_image_quality(nm, camera_name, cfg)

            if not _cfg_has_genicam_key(cfg, "GevSCPSPacketSize"):
                safe_set_node(nm, "GevSCPSPacketSize", 8000)

            # Произвольные узлы из genicam{} — применяются последними и перекрывают всё
            _apply_genicam_extra(nm, cfg, camera_name)
        else:
            print(f"[CAM {camera_name}] ⏩ Обновление параметров пропущено (update_params=false)")

        # Log actual params
        try:    real_fmt = nm.PixelFormat.value
        except Exception: real_fmt = "Unknown"
        try:    exp_value = nm.ExposureTime.value
        except Exception: exp_value = "Unknown"
        try:    gain_actual = nm.Gain.value
        except Exception: gain_actual = "Unknown"
        try:    w = nm.Width.value; h = nm.Height.value
        except Exception: w = h = "?"
        wb_actual = safe_get_node(nm, "BalanceWhiteAuto", "n/a")

        print(
            f"[CAM {camera_name}] ⚙️ PixFmt={real_fmt}, ROI={w}x{h}, "
            f"Exp={exp_value}us, Gain={gain_actual}dB, WB={wb_actual}"
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

    def purge_files(self):
        for f in os.listdir(self.output_dir):
            if f.endswith(".ts") or f == "index.m3u8":
                try:
                    os.remove(os.path.join(self.output_dir, f))
                except Exception:
                    pass

    def start(self, src_pix_fmt: str, src_w: int, src_h: int):
        self.src_pix_fmt = src_pix_fmt
        self.src_w = src_w
        self.src_h = src_h

        self.purge_files()

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

    def __init__(
        self,
        device,
        camera_id: str,
        display_name: str,
        use_nvenc: bool,
        gpu_id: int = 0,
        zmq_ctx: zmq.Context | None = None,
        harvester: Harvester | None = None,
        device_index: int = -1,
        device_key: str | None = None,
        cam_cfg: dict | None = None,
    ):
        self.device       = device      # harvesters ImageAcquirer
        self.camera_id    = camera_id
        self.display_name = display_name
        self.gpu_id       = gpu_id
        self.running      = True
        self.zmq_ctx      = zmq_ctx
        self.zmq_socket   = None
        self.harvester    = harvester
        self.device_index = device_index
        self.device_key   = device_key
        self._device_lock = threading.Lock()
        self._stale_lock = threading.Lock()
        self._acquisition_start_time = 0.0
        self._last_stale_action = 0.0
        self.cam_cfg = cam_cfg or build_global_camera_defaults()

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

        self.frame_interval = (
            1.0 / effective_fps_for_config(self.cam_cfg)
            if effective_fps_for_config(self.cam_cfg) > 0
            else 0.0
        )
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

    def _drain_frame_queue(self):
        while True:
            try:
                self.q.get_nowait()
            except queue.Empty:
                break

    def _setup_device(self) -> bool:
        try:
            label = f"{self.display_name} ({self.camera_id})"
            try_set_camera_params(self.device, label, self.cam_cfg)

            try:
                self.pixel_format = self.device.remote_device.node_map.PixelFormat.value
            except Exception:
                self.pixel_format = "BayerRG8"

            self.device.num_buffers = GENTL_BUFFER_COUNT
            self.device.start()
            time.sleep(0.3)
            self._acquisition_start_time = time.time()
            return True
        except Exception as e:
            print(f"[CAM {self.display_name} ({self.camera_id})] ❌ device setup error: {e}")
            return False

    def _reconnect_device(self) -> bool:
        if not RECONNECT_ENABLED or self.harvester is None:
            return False
        if self.device_key is None and self.device_index < 0:
            return False

        with self._device_lock:
            try:
                self.device.stop()
            except Exception:
                pass
            try:
                self.device.destroy()
            except Exception:
                pass

            device = None
            if self.device_key:
                device, index = open_camera_by_key(
                    self.harvester,
                    self.device_key,
                    label=self.display_name,
                )
                if index is not None:
                    self.device_index = index
            elif self.device_index >= 0:
                try:
                    self.harvester.update()
                except Exception as e:
                    print(
                        f"[CAM {self.display_name} ({self.camera_id})] "
                        f"⚠️ harvester.update failed: {e}"
                    )
                device = open_camera_with_retry(self.harvester, self.device_index)

            if device is None:
                return False

            self.device = device
            return self._setup_device()

    def _check_stream_stale(self, now: float):
        if not RECONNECT_ENABLED or STREAM_STALE_SEC <= 0:
            return

        if not self._stale_lock.acquire(blocking=False):
            return
        try:
            if self.last_frame_time > 0:
                ref_time = self.last_frame_time
            elif self._acquisition_start_time > 0:
                ref_time = self._acquisition_start_time
            else:
                return

            stale_for = now - ref_time
            if stale_for < STREAM_STALE_SEC:
                return

            if now - self._last_stale_action < RECONNECT_RETRY_SEC:
                return
            self._last_stale_action = now

            print(
                f"[CAM {self.display_name} ({self.camera_id})] "
                f"⚠️ поток устарел ({stale_for:.0f}s без кадров) — очистка HLS и переподключение..."
            )
            self.hls.stop()
            self.hls.purge_files()
            self._drain_frame_queue()

            if self._reconnect_device():
                print(
                    f"[CAM {self.display_name} ({self.camera_id})] "
                    f"✅ камера переподключена"
                )
                self.last_frame_time = 0.0
                self._acquisition_start_time = time.time()
            else:
                print(
                    f"[CAM {self.display_name} ({self.camera_id})] "
                    f"❌ переподключение не удалось, повтор через {RECONNECT_RETRY_SEC:.0f}s"
                )
        finally:
            self._stale_lock.release()

    def capture(self):
        print(f"[CAM {self.display_name} ({self.camera_id})] 📷 START capture")
        try:
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

            if not self._setup_device():
                self.running = False
                return

        except Exception as e:
            print(f"[CAM {self.display_name} ({self.camera_id})] ❌ start error: {e}")
            self.running = False
            return

        while self.running:
            self._check_stream_stale(time.time())
            try:
                with self._device_lock:
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
                self._check_stream_stale(time.time())
                continue
            except Exception as e:
                if not self.running:
                    break
                print(f"[CAM {self.display_name} ({self.camera_id})] fetch error: {e}")
                self._check_stream_stale(time.time())
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
                self._check_stream_stale(now)
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
# RTSP CAMERA WORKER
# =========================
class RtspCameraWorker:
    def __init__(
        self,
        camera_id: str,
        rtsp_url: str,
        use_nvenc: bool,
        gpu_id: int = 0,
        cam_cfg: dict | None = None,
        max_reconnect_attempts: int = 10,
        max_backoff_seconds: int = 300,
        zmq_ctx: zmq.Context | None = None,
    ):
        self.camera_id = camera_id
        self.rtsp_url = rtsp_url
        self.display_name = rtsp_url
        self.gpu_id = gpu_id
        self.running = True
        self.cam_cfg = cam_cfg or build_global_camera_defaults()
        self.max_reconnect_attempts = max(1, int(max_reconnect_attempts))
        self.max_backoff_seconds = max(1, int(max_backoff_seconds))
        self._reconnect_count = 0

        self.zmq_ctx = zmq_ctx
        self.zmq_socket = None
        if self.zmq_ctx is not None:
            try:
                self.zmq_socket = self.zmq_ctx.socket(zmq.PUB)
                self.zmq_socket.setsockopt(zmq.SNDHWM, 2)
                self.zmq_socket.connect("inproc://zmq_workers")
            except Exception as e:
                print(f"[CAM {self.camera_id}] ⚠️ ZMQ init failed: {e}")
                self.zmq_socket = None

        self.q = queue.Queue(maxsize=2)
        self.hls = HLSStreamer(camera_id, self.display_name, use_nvenc, gpu_id)
        self._capture: object | None = None
        self._capture_lock = threading.Lock()
        self._stale_lock = threading.Lock()
        self._last_stale_action = 0.0
        self._stream_start_time = 0.0

        self.fps_count = 0
        self.last_fps = time.time()
        self.last_empty_log = 0
        self.fetch_count = 0
        self.last_fetch_log = time.time()
        self._drop_count = 0
        self.last_frame_time = 0.0

        eff_fps = effective_fps_for_config(self.cam_cfg)
        self.frame_interval = 1.0 / eff_fps if eff_fps > 0 else 0.0
        self.pixel_format = "BGR8"

        self._zmq_prev_capture = None
        self._zmq_last_publish = 0.0

    def _send_zmq_frame(self, frame: np.ndarray, now: float) -> bool:
        if self.zmq_socket is None:
            return False
        try:
            if ZMQ_FORMAT in ("jpeg", "nvjpeg"):
                ret, encoded = cv2.imencode('.jpg', frame, [int(cv2.IMWRITE_JPEG_QUALITY), 85])
                if not ret:
                    return False
                msg_bytes = encoded.tobytes()
                meta = {
                    "camera_id": self.camera_id,
                    "display_name": self.display_name,
                    "timestamp": now,
                    "format": "jpeg",
                    "pixel_format": self.pixel_format,
                }
            else:
                msg_bytes = frame.data if frame.flags["C_CONTIGUOUS"] else np.ascontiguousarray(frame).data
                meta = {
                    "camera_id": self.camera_id,
                    "display_name": self.display_name,
                    "timestamp": now,
                    "format": "raw",
                    "shape": frame.shape,
                    "dtype": str(frame.dtype),
                    "pixel_format": self.pixel_format,
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

    def _maybe_publish_zmq(self, frame: np.ndarray, now: float):
        if self.zmq_socket is None:
            return
        if ZMQ_INFERENCE_FPS <= 0:
            self._send_zmq_frame(frame, now)
            return
        if ZMQ_INFERENCE_BURST_PAIRS:
            burst_interval = 2.0 / ZMQ_INFERENCE_FPS
            prev = self._zmq_prev_capture
            self._zmq_prev_capture = frame.copy()
            if prev is None:
                return
            if now - self._zmq_last_publish < burst_interval:
                return
            self._zmq_last_publish = now
            self._send_zmq_frame(prev, now)
            self._send_zmq_frame(frame, now)
            return
        min_interval = 1.0 / ZMQ_INFERENCE_FPS
        if now - self._zmq_last_publish < min_interval:
            return
        self._zmq_last_publish = now
        self._send_zmq_frame(frame, now)

    def _release_capture(self):
        with self._capture_lock:
            if self._capture is not None:
                try:
                    self._capture.release()
                except Exception:
                    pass
                self._capture = None

    def _connect(self) -> bool:
        if cv2 is None:
            print(f"[CAM {self.camera_id}] ❌ OpenCV не установлен — RTSP недоступен")
            return False
        self._release_capture()
        try:
            cap = cv2.VideoCapture(self.rtsp_url)
            if not cap.isOpened():
                return False
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            if hasattr(cv2, "CAP_PROP_OPEN_TIMEOUT_MSEC"):
                cap.set(cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, 5000)
            if hasattr(cv2, "CAP_PROP_READ_TIMEOUT_MSEC"):
                cap.set(cv2.CAP_PROP_READ_TIMEOUT_MSEC, 5000)
            with self._capture_lock:
                self._capture = cap
            self._reconnect_count = 0
            self._stream_start_time = time.time()
            print(f"[CAM {self.camera_id}] ✅ RTSP подключён: {self.rtsp_url}")
            return True
        except Exception as e:
            print(f"[CAM {self.camera_id}] ❌ RTSP connect error: {e}")
            self._release_capture()
            return False

    def _reconnect(self) -> bool:
        self._reconnect_count += 1
        base_delay = min(2 ** self._reconnect_count, self.max_backoff_seconds)
        delay = min(base_delay, self.max_backoff_seconds)
        print(
            f"[CAM {self.camera_id}] 🔄 переподключение RTSP "
            f"(попытка {self._reconnect_count}/{self.max_reconnect_attempts}) через {delay:.1f}s..."
        )
        time.sleep(delay)
        if self._reconnect_count > self.max_reconnect_attempts:
            print(f"[CAM {self.camera_id}] ❌ лимит попыток RTSP переподключения")
            return False
        return self._connect()

    def _read_frame(self) -> np.ndarray | None:
        with self._capture_lock:
            cap = self._capture
        if cap is None or not cap.isOpened():
            return None
        try:
            if not cap.grab():
                return None
            retrieved, frame = cap.retrieve()
            if not retrieved or frame is None or frame.size == 0:
                return None
            if not frame.flags["C_CONTIGUOUS"]:
                frame = np.ascontiguousarray(frame)
            return frame
        except Exception as e:
            if self.fetch_count < 3:
                print(f"[CAM {self.camera_id}] ⚠️ read error: {e}")
            return None

    def _check_stream_stale(self, now: float):
        if not RECONNECT_ENABLED or STREAM_STALE_SEC <= 0:
            return
        if not self._stale_lock.acquire(blocking=False):
            return
        try:
            if self.last_frame_time > 0:
                ref_time = self.last_frame_time
            elif self._stream_start_time > 0:
                ref_time = self._stream_start_time
            else:
                return
            stale_for = now - ref_time
            if stale_for < STREAM_STALE_SEC:
                return
            if now - self._last_stale_action < RECONNECT_RETRY_SEC:
                return
            self._last_stale_action = now
            print(
                f"[CAM {self.camera_id}] ⚠️ RTSP поток устарел "
                f"({stale_for:.0f}s без кадров) — очистка HLS и переподключение..."
            )
            self.hls.stop()
            self.hls.purge_files()
            while True:
                try:
                    self.q.get_nowait()
                except queue.Empty:
                    break
            self._release_capture()
            if self._reconnect():
                self.last_frame_time = 0.0
                self._stream_start_time = time.time()
            else:
                print(
                    f"[CAM {self.camera_id}] ❌ RTSP переподключение не удалось, "
                    f"повтор через {RECONNECT_RETRY_SEC:.0f}s"
                )
        finally:
            self._stale_lock.release()

    def capture(self):
        print(f"[CAM {self.camera_id}] 📷 START RTSP capture ({self.rtsp_url})")
        if not self._connect():
            self.running = False
            return

        while self.running:
            self._check_stream_stale(time.time())
            frame = self._read_frame()
            if frame is None:
                self._release_capture()
                if not self.running:
                    break
                if not self._reconnect():
                    time.sleep(RECONNECT_RETRY_SEC)
                continue

            height, width = frame.shape[:2]
            now = time.time()
            if self.frame_interval > 0 and now - self.last_frame_time < self.frame_interval:
                continue

            if not self.hls.running:
                self.hls.start("bgr24", width, height)

            self._maybe_publish_zmq(frame, now)

            while True:
                try:
                    self.q.put_nowait(frame)
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
                    f"[CAM {self.camera_id}] 🔍 RTSP capture FPS: "
                    f"{self.fetch_count / 5.0:.1f}, drops={self._drop_count}"
                )
                self.fetch_count = 0
                self._drop_count = 0
                self.last_fetch_log = now

    def encode(self):
        print(f"[CAM {self.camera_id}] 🎞 START RTSP encode")
        while self.running:
            try:
                frame = self.q.get(timeout=1.0)
                self.hls.send(frame)
                self.fps_count += 1
                now = time.time()
                if now - self.last_fps >= 5.0:
                    print(f"[CAM {self.camera_id}] HLS FPS={self.fps_count / 5.0:.1f}")
                    self.fps_count = 0
                    self.last_fps = now
            except queue.Empty:
                now = time.time()
                self._check_stream_stale(now)
                if now - self.last_empty_log > 5.0:
                    print(f"[CAM {self.camera_id}] ⏳ нет кадров RTSP...")
                    self.last_empty_log = now

    def stop(self):
        self.running = False
        self.hls.stop()
        self._release_capture()
        print(f"[CAM {self.camera_id}] Stopped")


# =========================
# ОТКРЫТИЕ КАМЕРЫ С ПОВТОРНЫМИ ПОПЫТКАМИ
# =========================
def _find_device_index(h: Harvester, device_key: str) -> int | None:
    for i, di in enumerate(h.device_info_list):
        if _device_unique_key(di) == device_key:
            return i
    return None


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


def open_camera_by_key(
    h: Harvester,
    device_key: str,
    max_retries: int = MAX_RETRIES,
    label: str = "",
) -> tuple[object | None, int | None]:
    tag = label or device_key
    for attempt in range(max_retries):
        try:
            if attempt > 0:
                print(f"   Попытка {attempt + 1}/{max_retries} для {tag}...")
                time.sleep(RETRY_DELAY)

            h.update()
            index = _find_device_index(h, device_key)
            if index is None:
                continue

            device = h.create(index)
            if device is not None:
                print(f"   Камера {tag} открыта (index={index})")
                return device, index

        except Exception as e:
            err = str(e)
            print(f"   Ошибка {tag}: {err}")

            if "-1005" in err or "denied" in err.lower():
                try:
                    index = _find_device_index(h, device_key)
                    if index is not None:
                        tmp = h.create(index)
                        if tmp:
                            tmp.destroy()
                            time.sleep(1)
                except Exception:
                    pass

    print(f"   Не удалось открыть {tag}")
    return None, None


# =========================
# ENUMERATE CAMERAS
# =========================
def _device_unique_key(di) -> str:
    id_ = getattr(di, "id_", None)
    if id_:
        return str(id_)
    display_name = getattr(di, "display_name", None)
    if display_name:
        return str(display_name)
    return repr(di)


def _device_access_status(di) -> int | None:
    try:
        return int(getattr(di, "access_status"))
    except (TypeError, ValueError):
        return None


def _config_ip_for_camera_id(camera_id: str) -> str:
    for key, value in CAMERA_MAPPING.items():
        if get_camera_alias(value) != camera_id:
            continue
        if "10.228." not in key:
            return ""
        start = key.find("(") + 1
        end = key.find("[")
        if start > 0 and end > start:
            return key[start:end]
    return ""


def _cameras_from_device_list(h: Harvester) -> list:
    cameras = []
    for i, di in enumerate(h.device_info_list):
        display_name = di.display_name or f"camera_{i}"
        cameras.append({
            "index":         i,
            "display_name":  display_name,
            "camera_id":     get_camera_id(display_name),
            "access_status": _device_access_status(di),
            "device_key":    _device_unique_key(di),
        })
    return cameras


def discover_cameras(h: Harvester) -> list:
    merged: dict[str, dict] = {}
    pass_counts: list[int] = []

    print(
        f"\n🔍 GigE discovery: {DISCOVERY_PASSES} passes, "
        f"{DISCOVERY_PASS_DELAY_SEC:.1f}s delay..."
    )
    for pass_num in range(1, DISCOVERY_PASSES + 1):
        h.update()
        count = len(h.device_info_list)
        pass_counts.append(count)
        new_this_pass = 0

        for di in h.device_info_list:
            key = _device_unique_key(di)
            access = _device_access_status(di)
            entry = merged.get(key)
            if entry is None:
                merged[key] = {
                    "display_name":  di.display_name or key,
                    "access_status": access,
                    "passes_seen":   1,
                }
                new_this_pass += 1
            else:
                entry["passes_seen"] += 1
                prev = entry.get("access_status")
                if access == 1 or prev not in (1, None):
                    if access == 1 or prev is None:
                        entry["access_status"] = access

        print(
            f"   pass {pass_num}/{DISCOVERY_PASSES}: {count} devices, "
            f"+{new_this_pass} new, merged total={len(merged)}"
        )
        if pass_num < DISCOVERY_PASSES:
            time.sleep(DISCOVERY_PASS_DELAY_SEC)

    cameras_info: list[dict] = []
    missing_after_final: list[str] = []

    for refresh_attempt in range(2):
        h.update()
        index_by_key = {
            _device_unique_key(di): i for i, di in enumerate(h.device_info_list)
        }
        cameras_info = []
        missing_after_final = []

        for key in sorted(merged.keys(), key=lambda k: merged[k]["display_name"]):
            info = merged[key]
            idx = index_by_key.get(key)
            display_name = info["display_name"]
            if idx is None:
                missing_after_final.append(display_name)
            cameras_info.append({
                "index":           idx,
                "display_name":    display_name,
                "camera_id":       get_camera_id(display_name),
                "access_status":   info.get("access_status"),
                "passes_seen":     info.get("passes_seen", 1),
                "device_key":      key,
                "discovery_stale": idx is None,
            })

        if not missing_after_final or refresh_attempt == 1:
            break

        print(
            f"⚠️  Final refresh: {len(missing_after_final)} camera(s) missing, "
            f"retry in {DISCOVERY_PASS_DELAY_SEC:.1f}s..."
        )
        time.sleep(DISCOVERY_PASS_DELAY_SEC)

    online_count = sum(1 for cam in cameras_info if cam["index"] is not None)
    stale_count = len(missing_after_final)
    print(
        f"📸 Discovery merged: {len(merged)} unique, "
        f"{online_count} online, {stale_count} stale (retry at open) "
        f"(pass counts: {pass_counts})"
    )

    if missing_after_final:
        print(
            f"⚠️  {stale_count} camera(s) seen earlier but missing "
            f"after final refresh (will retry by MAC):"
        )
        for name in missing_after_final:
            print(f"      - {name}")

    expected_ids = get_all_expected_camera_ids()
    found_ids = {cam["camera_id"] for cam in cameras_info}
    missing_config = sorted(expected_ids - found_ids)
    if missing_config:
        print(
            f"⚠️  {len(missing_config)} camera(s) from config.json not discovered:"
        )
        for cid in missing_config:
            ip_hint = _config_ip_for_camera_id(cid)
            suffix = f" ({ip_hint})" if ip_hint else ""
            print(f"      - {cid}{suffix}")

    busy = [cam for cam in cameras_info if cam.get("access_status") == 3]
    if busy:
        print(
            f"⚠️  {len(busy)} camera(s) report access_status=NOACCESS "
            f"(may fail to open):"
        )
        for cam in busy:
            print(f"      - {cam['camera_id']} {cam['display_name']}")

    return cameras_info


def _start_gige_camera_worker(
    info: dict,
    device,
    use_nvenc: bool,
    gpu_id: int,
    zmq_ctx,
    harvester: Harvester,
) -> CameraWorker:
    cam_cfg = get_camera_config(info["display_name"])
    w = CameraWorker(
        device,
        info["camera_id"],
        info["display_name"],
        use_nvenc,
        gpu_id,
        zmq_ctx=zmq_ctx,
        harvester=harvester,
        device_index=info["index"],
        device_key=info.get("device_key"),
        cam_cfg=cam_cfg,
    )
    threading.Thread(
        target=w.capture, daemon=True,
        name=f"Capture_{info['camera_id']}",
    ).start()
    threading.Thread(
        target=w.encode, daemon=True,
        name=f"Encode_{info['camera_id']}",
    ).start()
    return w


def _pending_camera_opener(
    harvester: Harvester,
    pending: list[dict],
    workers: list,
    workers_lock: threading.Lock,
    gpu_ids: list,
    use_nvenc: bool,
    zmq_ctx,
):
    while pending:
        time.sleep(RECONNECT_RETRY_SEC)
        still_pending: list[dict] = []
        for cam in pending:
            device, index = open_camera_by_key(
                harvester,
                cam["device_key"],
                max_retries=3,
                label=cam["display_name"],
            )
            if device is None or index is None:
                still_pending.append(cam)
                continue

            cam = {**cam, "index": index}
            with workers_lock:
                gpu_id = gpu_ids[len(workers) % len(gpu_ids)]
            w = _start_gige_camera_worker(
                cam, device, use_nvenc, gpu_id, zmq_ctx, harvester
            )
            with workers_lock:
                workers.append(w)
            print(
                f"✅ Фоновое подключение: {cam['display_name']} "
                f"-> camera_{cam['camera_id']}/index.m3u8"
            )
        pending = still_pending


def list_cameras(h: Harvester) -> list:
    return _cameras_from_device_list(h)


# =========================
# MAIN (RTSP)
# =========================
def main_rtsp():
    def _shutdown_handler(signum, _frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, _shutdown_handler)
    signal.signal(signal.SIGINT, _shutdown_handler)

    if cv2 is None:
        print("❌ OpenCV не установлен — RTSP режим недоступен")
        return

    try:
        subprocess.run(['ffmpeg', '-version'], capture_output=True, check=True)
        print("✅ FFmpeg найден")
    except Exception:
        print("❌ FFmpeg не найден")
        return

    use_nvenc = USE_NVENC and check_nvenc_available()

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

    cameras = [c for c in RTSP_CAMERAS if isinstance(c, dict) and c.get("rtsp_url")]
    if not cameras:
        print("❌ В config.json нет rtsp_cameras (или пустой список)")
        return

    print(f"\n📸 RTSP камер в конфиге: {len(cameras)}")
    for cam in cameras:
        cam_id = str(cam.get("id") or cam.get("alias") or "?")
        print(f"   • {cam_id} -> {cam['rtsp_url']}")

    zmq_ctx = None
    if ZMQ_ENABLED:
        try:
            zmq_ctx = zmq.Context()
            xpub = zmq_ctx.socket(zmq.XPUB)
            xpub.bind(f"tcp://0.0.0.0:{ZMQ_PORT_BASE}")
            xsub = zmq_ctx.socket(zmq.XSUB)
            xsub.bind("inproc://zmq_workers")
            threading.Thread(
                target=zmq.proxy, args=(xsub, xpub), daemon=True, name="ZMQ_Proxy"
            ).start()
            print(f"\n🚀 ZMQ XPUB bound on port {ZMQ_PORT_BASE}")
        except Exception as e:
            print(f"\n⚠️ ZMQ Proxy init failed: {e}")
            zmq_ctx = None

    print("\n🚀 Запуск RTSP потоков...")
    workers = []
    for i, cam in enumerate(cameras):
        cam_id = str(cam.get("id") or cam.get("alias") or f"rtsp_{i}")
        gpu_id = gpu_ids[i % len(gpu_ids)]
        cam_cfg = resolve_rtsp_camera_config(cam)
        w = RtspCameraWorker(
            camera_id=cam_id,
            rtsp_url=str(cam["rtsp_url"]),
            use_nvenc=use_nvenc,
            gpu_id=gpu_id,
            cam_cfg=cam_cfg,
            max_reconnect_attempts=int(cam.get("max_reconnect_attempts", 10)),
            max_backoff_seconds=int(cam.get("max_backoff_seconds", 300)),
            zmq_ctx=zmq_ctx,
        )
        workers.append(w)
        threading.Thread(
            target=w.capture, daemon=True, name=f"RTSP_Capture_{cam_id}"
        ).start()
        threading.Thread(
            target=w.encode, daemon=True, name=f"RTSP_Encode_{cam_id}"
        ).start()
        time.sleep(0.05)

    print("\n" + "=" * 70)
    print("▶️  RTSP СИСТЕМА ЗАПУЩЕНА")
    enc_label = "h264_nvenc (GPU)" if use_nvenc else "libx264 (CPU)"
    print(f"🖥️  Энкодер: {enc_label}")
    print(f"📡 RTSP transport: {RTSP_TRANSPORT}")
    if RECONNECT_ENABLED and STREAM_STALE_SEC > 0:
        print(
            f"🔄 Автопереподключение: вкл "
            f"(порог {STREAM_STALE_SEC:.0f}s без кадров, повтор {RECONNECT_RETRY_SEC:.0f}s)"
        )
    print(f"📁 HLS: {BASE_OUTPUT_DIR}/")
    for w in workers:
        print(f"   • {w.camera_id} -> camera_{w.camera_id}/index.m3u8")
    print("=" * 70 + "\n")

    try:
        while True:
            time.sleep(5)
            active = sum(1 for w in workers if w.running)
            if active < len(workers):
                print(f"⚠️  Активных RTSP камер: {active}/{len(workers)}")
    except KeyboardInterrupt:
        print("\n⏹️  Остановка RTSP...")
        for w in workers:
            w.stop()
        time.sleep(1)
        print("✅ Завершено")


# =========================
# MAIN (GigE)
# =========================
def main_gige():
    def _shutdown_handler(signum, _frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, _shutdown_handler)
    signal.signal(signal.SIGINT, _shutdown_handler)

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

    cameras_info = discover_cameras(h)
    camera_count = len(cameras_info)

    print(f"\n📸 Найдено камер: {camera_count}")
    stale_count = sum(1 for cam in cameras_info if cam.get("discovery_stale"))
    if stale_count:
        print(
            f"   ({stale_count} с нестабильным ответом — "
            f"попытка подключения при старте и в фоне)"
        )
    if camera_count == 0:
        print("❌ Камеры не обнаружены.")
        return

    print("\n📋 Список камер:")
    for cam in cameras_info:
        access = cam.get("access_status")
        access_note = ""
        if cam.get("discovery_stale"):
            access_note = " [STALE]"
        elif access == 3:
            access_note = " [NOACCESS]"
        elif access == 1:
            access_note = " [OK]"
        index_label = cam["index"] if cam["index"] is not None else "?"
        print(
            f"   [{index_label}] {cam['display_name']} -> "
            f"ID: {cam['camera_id']}{access_note}"
        )

    print("\n🔧 Открытие камер...")
    devices_with_info = []
    pending_cameras: list[dict] = []
    stale_open_retries = max(MAX_RETRIES, DISCOVERY_PASSES + 2)

    for cam in cameras_info:
        device = None
        resolved_index = cam.get("index")

        if cam.get("discovery_stale") or resolved_index is None:
            print(f"   ⏳ {cam['display_name']} — повторное обнаружение по MAC...")
            device, resolved_index = open_camera_by_key(
                harvester=h,
                device_key=cam["device_key"],
                max_retries=stale_open_retries,
                label=cam["display_name"],
            )
        else:
            device = open_camera_with_retry(h, resolved_index)

        if device is not None and resolved_index is not None:
            devices_with_info.append({
                **cam,
                "device": device,
                "index": resolved_index,
            })
        else:
            pending_cameras.append(cam)
            print(
                f"   ⚠️ {cam['display_name']} — не открыта сейчас, "
                f"фоновое подключение каждые {RECONNECT_RETRY_SEC:.0f}s"
            )

    print(f"\n✅ Открыто {len(devices_with_info)} из {camera_count} камер")
    if pending_cameras:
        print(
            f"⏳ В очереди фонового подключения: {len(pending_cameras)} камер(ы)"
        )
    if not devices_with_info and not pending_cameras:
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
    workers_lock = threading.Lock()
    for i, info in enumerate(devices_with_info):
        gpu_id = gpu_ids[i % len(gpu_ids)]
        w = _start_gige_camera_worker(
            info, info["device"], use_nvenc, gpu_id, zmq_ctx, h
        )
        workers.append(w)

    if pending_cameras:
        threading.Thread(
            target=_pending_camera_opener,
            args=(
                h,
                list(pending_cameras),
                workers,
                workers_lock,
                gpu_ids,
                use_nvenc,
                zmq_ctx,
            ),
            daemon=True,
            name="PendingCameraOpener",
        ).start()

    print("\n" + "=" * 70)
    print("▶️  СИСТЕМА ЗАПУЩЕНА")
    enc_label = "h264_nvenc (GPU)" if use_nvenc else "libx264 (CPU)"
    print(f"🖥️  Энкодер: {enc_label}")
    if RECONNECT_ENABLED and STREAM_STALE_SEC > 0:
        print(
            f"🔄 Автопереподключение: вкл "
            f"(порог {STREAM_STALE_SEC:.0f}s без кадров, повтор {RECONNECT_RETRY_SEC:.0f}s)"
        )
    print(f"📁 HLS: {BASE_OUTPUT_DIR}/")
    for w in workers:
        print(f"   • {w.display_name} -> camera_{w.camera_id}/index.m3u8")
    print("=" * 70 + "\n")

    try:
        while True:
            time.sleep(5)
            with workers_lock:
                active_workers = list(workers)
            active = sum(1 for w in active_workers if w.running)
            total = len(cameras_info)
            if active < total:
                print(f"⚠️  Активных камер: {active}/{total}")
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
        if CAMERA_SOURCE == "rtsp":
            main_rtsp()
        elif CAMERA_SOURCE == "gige":
            main_gige()
        else:
            print(f"❌ Неподдерживаемый camera_source: {CAMERA_SOURCE!r} (ожидается gige или rtsp)")
    except Exception as e:
        print(f"\n❌ КРИТИЧЕСКАЯ ОШИБКА: {e}")
        traceback.print_exc()