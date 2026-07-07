#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Считывает текущие характеристики камеры Daheng MER2 и печатает блок
для вставки в config.json → camera_mapping.

Примеры:
  python3 scripts/dump-camera-config.py --list
  python3 scripts/dump-camera-config.py --ip 10.228.0.31
  python3 scripts/dump-camera-config.py --alias 90.1.1
  python3 scripts/dump-camera-config.py --index 0
  python3 scripts/dump-camera-config.py --all
  python3 scripts/dump-camera-config.py --ip 10.228.0.31 --extra
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

from harvesters.core import Harvester


def load_json(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def safe_get(nm, name: str, default=None):
    try:
        if hasattr(nm, name):
            node = getattr(nm, name)
            if hasattr(node, "value"):
                return node.value
    except Exception:
        pass
    return default


def get_camera_alias(value) -> str | None:
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, dict):
        alias = value.get("alias") or value.get("camera_id")
        return str(alias).strip() if alias else None
    return None


def mapping_key_for_alias(camera_mapping: dict, alias: str) -> str | None:
    for key, value in camera_mapping.items():
        if get_camera_alias(value) == alias:
            return key
    return None


def mapping_key_for_ip(camera_mapping: dict, ip: str) -> str | None:
    for key in camera_mapping:
        if ip in key:
            return key
    return None


def _device_key(di, fallback_index: int) -> str:
    return str(getattr(di, "id_", None) or di.display_name or fallback_index)


def find_devices(h: Harvester, passes: int = 3, delay: float = 1.0) -> list:
    """Прогреваем GigE discovery несколькими проходами, затем берём индексы
    из ФИНАЛЬНОГО списка (индексы меняются между update, поэтому храним ключ)."""
    import time

    seen: set[str] = set()
    for _ in range(max(1, passes)):
        h.update()
        for i, di in enumerate(h.device_info_list):
            seen.add(_device_key(di, i))
        time.sleep(delay)

    # Финальный проход: индексы из него совпадут с тем, что увидит h.create()
    h.update()
    devices = []
    for i, di in enumerate(h.device_info_list):
        devices.append({
            "index": i,
            "display_name": di.display_name or _device_key(di, i),
            "key": _device_key(di, i),
        })
    return sorted(devices, key=lambda d: d["display_name"])


def resolve_current_index(h: Harvester, key: str, fallback_index: int) -> int:
    """Свежий update и поиск актуального индекса камеры по её уникальному ключу."""
    h.update()
    for i, di in enumerate(h.device_info_list):
        if _device_key(di, i) == key:
            return i
    return fallback_index


def pick_device(devices: list[dict], args, camera_mapping: dict) -> list[dict]:
    if args.all:
        return devices

    if args.index is not None:
        for d in devices:
            if d["index"] == args.index:
                return [d]
        raise SystemExit(f"Камера с index={args.index} не найдена")

    if args.ip:
        ip = args.ip.strip()
        for d in devices:
            if ip in d["display_name"]:
                return [d]
        key = mapping_key_for_ip(camera_mapping, ip)
        if key:
            for d in devices:
                if key.split("(")[0] in d["display_name"] and ip in d["display_name"]:
                    return [d]
        raise SystemExit(f"Камера с IP {ip} не найдена")

    if args.alias:
        alias = args.alias.strip()
        key = mapping_key_for_alias(camera_mapping, alias)
        if not key:
            raise SystemExit(f"alias {alias} не найден в camera_mapping")
        ip_start = key.find("(") + 1
        ip_end = key.find("[")
        ip_hint = key[ip_start:ip_end] if ip_start > 0 and ip_end > ip_start else ""
        for d in devices:
            if ip_hint and ip_hint in d["display_name"]:
                return [d]
        for d in devices:
            if alias in d["display_name"]:
                return [d]
        raise SystemExit(f"Камера alias={alias} есть в config, но не обнаружена в сети")

    if args.name:
        needle = args.name.strip().lower()
        matched = [d for d in devices if needle in d["display_name"].lower()]
        if len(matched) == 1:
            return matched
        if len(matched) > 1:
            print("Найдено несколько камер, уточните --ip или --index:", file=sys.stderr)
            for d in matched:
                print(f"  [{d['index']}] {d['display_name']}", file=sys.stderr)
            raise SystemExit(1)
        raise SystemExit(f"Камера по имени '{args.name}' не найдена")

    raise SystemExit("Укажите камеру: --list | --ip | --alias | --index | --name | --all")


def read_balance_ratio(nm) -> dict[str, float] | None:
    ratios: dict[str, float] = {}
    for channel in ("Red", "Green", "Blue"):
        try:
            if not hasattr(nm, "BalanceRatioSelector"):
                return None
            nm.BalanceRatioSelector.value = channel
            val = safe_get(nm, "BalanceRatio")
            if val is not None:
                ratios[channel] = round(float(val), 4)
        except Exception:
            return None
    return ratios or None


def round_num(value: Any) -> Any:
    if isinstance(value, bool):
        return value
    if isinstance(value, float):
        return round(value, 4) if abs(value) < 1e6 else round(value, 2)
    return value


def read_camera_params(nm, include_extra: bool) -> dict:
    cfg: dict[str, Any] = {"update_params": True}

    width = safe_get(nm, "Width")
    height = safe_get(nm, "Height")
    if width is not None:
        cfg["cam_width"] = int(width)
    if height is not None:
        cfg["cam_height"] = int(height)

    pixel_format = safe_get(nm, "PixelFormat")
    if pixel_format is not None:
        cfg["pixel_format"] = str(pixel_format)

    fps = safe_get(nm, "AcquisitionFrameRate")
    if fps is not None:
        cfg["fps_target"] = round_num(float(fps))

    exposure_auto = safe_get(nm, "ExposureAuto")
    if exposure_auto is not None:
        cfg["exposure_auto"] = str(exposure_auto)

    if str(exposure_auto) == "Off":
        for node, key in (
            ("ExposureTime", "exposure_time_us"),
            ("ExposureTimeAbs", "exposure_time_us"),
        ):
            val = safe_get(nm, node)
            if val is not None:
                cfg[key] = round_num(float(val))
                break
    else:
        for node, key in (
            ("AutoExposureTimeMin", "auto_exposure_min_us"),
            ("AutoExposureTimeMax", "auto_exposure_max_us"),
        ):
            val = safe_get(nm, node)
            if val is not None:
                cfg[key] = round_num(float(val))

    gain_auto = safe_get(nm, "GainAuto")
    if gain_auto is not None:
        cfg["gain_auto"] = str(gain_auto)

    if str(gain_auto) == "Off":
        gain = safe_get(nm, "Gain")
        if gain is not None:
            cfg["gain"] = round_num(float(gain))
    else:
        for node, key in (
            ("AutoGainMin", "auto_gain_min"),
            ("AutoGainMax", "auto_gain_max"),
        ):
            val = safe_get(nm, node)
            if val is not None:
                cfg[key] = round_num(float(val))

    wb_auto = safe_get(nm, "BalanceWhiteAuto")
    if wb_auto is not None:
        cfg["balance_white_auto"] = str(wb_auto)

    if str(wb_auto) == "Off":
        ratios = read_balance_ratio(nm)
        if ratios:
            cfg["balance_ratio"] = ratios

    for node, key, cast in (
        ("GammaEnable", "gamma_enable", bool),
        ("Gamma", "gamma", float),
        ("BlackLevel", "black_level", float),
        ("Sharpness", "sharpness", float),
        ("Saturation", "saturation", float),
        ("DigitalShift", "digital_shift", int),
    ):
        val = safe_get(nm, node)
        if val is not None:
            cfg[key] = round_num(cast(val))

    if include_extra:
        extra: dict[str, Any] = {}
        for node in (
            "TriggerMode", "TriggerSource", "AcquisitionMode",
            "AcquisitionFrameRateMode", "AcquisitionFrameRateEnable",
            "GevSCPSPacketSize", "GevSCPD", "ExpectedGrayValue",
            "AWBLampHouse", "ReverseX", "ReverseY",
        ):
            val = safe_get(nm, node)
            if val is not None:
                extra[node] = val if isinstance(val, (bool, int, float)) else str(val)
        if extra:
            cfg["genicam"] = extra

    return cfg


def resolve_mapping_key(display_name: str, camera_mapping: dict, alias: str | None) -> str:
    if display_name in camera_mapping:
        return display_name
    if alias:
        key = mapping_key_for_alias(camera_mapping, alias)
        if key:
            return key
    for key in camera_mapping:
        if key.split("(")[0] in display_name:
            ip_start = key.find("(") + 1
            ip_end = key.find("[")
            if ip_start > 0 and ip_end > ip_start:
                ip = key[ip_start:ip_end]
                if ip in display_name:
                    return key
    return display_name


def dump_one(h: Harvester, device_info: dict, camera_mapping: dict, args) -> None:
    index = resolve_current_index(h, device_info["key"], device_info["index"])
    try:
        ia = h.create(index)
    except Exception as e:
        err = str(e)
        if "-1005" in err or "AccessDenied" in err or "denied" in err.lower():
            raise SystemExit(
                f"Камера занята (AccessDenied -1005): {device_info['display_name']}\n"
                "Остановите backend, который держит камеры открытыми:\n"
                "  docker stop gige-hls-galaxysdk\n"
                "Затем снова запустите этот скрипт и после — backend:\n"
                "  docker start gige-hls-galaxysdk"
            ) from e
        raise
    try:
        nm = ia.remote_device.node_map
        alias = get_camera_alias(camera_mapping.get(device_info["display_name"]))
        if alias is None:
            for key, value in camera_mapping.items():
                if device_info["display_name"] in key or key in device_info["display_name"]:
                    alias = get_camera_alias(value)
                    break

        params = read_camera_params(nm, include_extra=args.extra)
        if alias:
            params = {"alias": alias, **params}
        elif args.alias:
            params = {"alias": args.alias.strip(), **params}

        mapping_key = resolve_mapping_key(
            device_info["display_name"],
            camera_mapping,
            params.get("alias"),
        )

        block = {mapping_key: params}
        print(f"# [{index}] {device_info['display_name']}")
        print(json.dumps(block, ensure_ascii=False, indent=2))
        print()
    finally:
        try:
            ia.destroy()
        except Exception:
            pass


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Считать характеристики камеры Daheng и вывести блок для camera_mapping",
    )
    parser.add_argument(
        "-c", "--config",
        default=os.getenv("CONFIG_FILE", "config.json"),
        help="Путь к config.json (по умолчанию: config.json)",
    )
    parser.add_argument(
        "--cti",
        default=None,
        help="Путь к GxGVTL.cti (по умолчанию из config.json)",
    )
    parser.add_argument("--list", action="store_true", help="Показать обнаруженные камеры")
    parser.add_argument("--all", action="store_true", help="Считать все обнаруженные камеры")
    parser.add_argument("--ip", help="IP камеры, напр. 10.228.0.31")
    parser.add_argument("--alias", help="alias из camera_mapping, напр. 90.1.1")
    parser.add_argument("--index", type=int, help="Индекс камеры из --list")
    parser.add_argument("--name", help="Часть display_name")
    parser.add_argument(
        "--extra",
        action="store_true",
        help="Добавить блок genicam (TriggerMode, GevSCPSPacketSize, ...)",
    )
    parser.add_argument(
        "--passes",
        type=int,
        default=3,
        help="Число проходов discovery (по умолчанию 3)",
    )
    args = parser.parse_args()

    if not os.path.isfile(args.config):
        raise SystemExit(f"Config не найден: {args.config}")

    cfg = load_json(args.config)
    cti = args.cti or cfg.get("cti_file_path", "/opt/galaxy_sdk/lib/x86_64/GxGVTL.cti")
    camera_mapping = cfg.get("camera_mapping", {})

    h = Harvester()
    try:
        h.add_file(cti)
    except Exception as e:
        raise SystemExit(f"Не удалось загрузить CTI {cti}: {e}") from e

    devices = find_devices(h, passes=args.passes)
    if args.list:
        if not devices:
            print("Камеры не найдены")
            return
        for d in devices:
            alias = None
            for key, value in camera_mapping.items():
                if d["display_name"] in key or key in d["display_name"]:
                    alias = get_camera_alias(value)
                    break
            alias_note = f" -> {alias}" if alias else ""
            print(f"[{d['index']:>2}] {d['display_name']}{alias_note}")
        return

    if not devices:
        raise SystemExit("Камеры не найдены")

    selected = pick_device(devices, args, camera_mapping)
    for device_info in selected:
        dump_one(h, device_info, camera_mapping, args)

    try:
        h.reset()
    except Exception:
        pass


if __name__ == "__main__":
    main()
