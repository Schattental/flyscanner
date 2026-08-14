#!/usr/bin/env python3
"""
Capture a short USB-webcam video of fruit flies and score their activity.

Designed for a Raspberry Pi 5 running 64-bit Raspberry Pi OS Lite.

The full-resolution stream is written to disk while motion analysis runs on a
calibrated crop. Camera controls are locked after warm-up, completed scans are
appended to a master history, and a local WebSocket dashboard reports status.

Dependencies:
    sudo apt install python3-opencv python3-numpy python3-gpiozero python3-lgpio
    .venv/bin/python -m pip install -r requirements-pi5.txt

Example:
    .venv/bin/python flyscannerV2.py --run-now
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import json
import math
import queue
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

cv2 = None
np = None

# Calibrated at 1280x720: x=222 through x=967 (right edge is exclusive 968).
# Keeping these as frame percentages preserves the same field of view when the
# camera resolution changes. Existing shift/extension options remain relative
# adjustments around this calibrated default.
CALIBRATED_ROI_LEFT_PERCENT = 100.0 * 222.0 / 1280.0
CALIBRATED_ROI_RIGHT_PERCENT = 100.0 * 968.0 / 1280.0
CALIBRATED_SHIFT_X_PERCENT = -2.5
CALIBRATED_EXTEND_LEFT_PERCENT = 2.0

QR_CODE_FILES = {
    "1-connect-flyscanner-hotspot.png",
    "2-open-hotspot-dashboard.png",
    "3-open-network-dashboard.png",
}


class ScanCancelled(Exception):
    """Raised when a remote stop request arrives before usable frames exist."""

RUNTIME_PRESETS_SECONDS = (30, 5 * 60, 10 * 60, 24 * 60 * 60)
DEFAULT_RUNTIME_SECONDS = RUNTIME_PRESETS_SECONDS[0]
DEFAULT_VIDEO_BYTES_PER_SECOND = 4_000_000
DEFAULT_H264_BYTES_PER_SECOND = 180_000
STORAGE_RESERVE_BYTES = 1024**3
STORAGE_ESTIMATE_SAFETY_FACTOR = 1.5
LONG_SESSION_SEGMENT_SECONDS = 10 * 60
DASHBOARD_HISTORY_LIMIT = 1000

SCANNER_SETTING_KEYS = {
    "duration_seconds",
    "ir_enabled",
    "camera_device",
    "capture_width",
    "capture_height",
    "capture_fps",
    "camera_buffers",
    "warmup_seconds",
    "camera_lock",
    "fixed_framerate",
    "analysis_width",
    "analyze_every",
    "opencv_threads",
    "motion_threshold",
    "min_change_area",
    "dark_threshold",
    "analysis_shift_x",
    "analysis_extend_left",
    "video_codec",
    "video_roi_only",
    "video_width",
    "video_height",
    "video_grayscale",
    "video_crf",
    "video_preset",
    "video_encoder_threads",
    "keep_captures",
    "result_hold_seconds",
    "button_debounce_seconds",
    "long_press_seconds",
    "double_press_window_seconds",
    "network_display_seconds",
}


@dataclass
class ActivityResult:
    scan_timestamp: str
    video_path: str
    first_frame_path: str
    ir_enabled: bool
    video_codec: str
    video_roi_only: bool
    video_width: int
    video_height: int
    video_grayscale: bool
    video_crf: int | None
    capture_width: int
    capture_height: int
    analysis_roi_left: int
    analysis_roi_top: int
    analysis_roi_width: int
    analysis_roi_height: int
    frames_dropped: int
    camera_locked_controls: str
    camera_exposure: float | None
    camera_gain: float | None
    camera_white_balance: float | None
    camera_focus: float | None
    duration_seconds: float
    frames_analyzed: int
    frames_captured: int
    fps_estimate: float
    analysis_fps_estimate: float
    activity_score: float
    brightness_mean: float
    brightness_min: int
    brightness_max: int
    motion_percent_mean: float
    motion_percent_peak: float
    fly_area_percent_mean: float
    moving_fly_area_percent_mean: float
    threshold_used: int


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Raspberry Pi 5 fly activity scanner (USB/V4L2 camera)."
    )
    parser.add_argument(
        "--camera",
        default="/dev/video0",
        help="USB camera device (recommended: /dev/video0) or numeric OpenCV index.",
    )
    parser.add_argument(
        "--duration",
        type=float,
        default=float(DEFAULT_RUNTIME_SECONDS),
        help="Recording duration in seconds for --run-now; button mode uses the saved preset.",
    )
    parser.add_argument("--fps", type=float, default=30.0, help="Requested capture FPS.")
    parser.add_argument("--width", type=int, default=1280, help="Requested frame width.")
    parser.add_argument("--height", type=int, default=720, help="Requested frame height.")
    parser.add_argument(
        "--video-codec",
        choices=("h264", "mjpeg"),
        default="h264",
        help="Video codec. H.264 MP4 is much smaller; MJPEG AVI is the legacy option.",
    )
    parser.add_argument(
        "--video-crf",
        type=int,
        default=21,
        help="H.264 quality from 0 (lossless/huge) to 51 (lowest); 21 is the tested default.",
    )
    parser.add_argument(
        "--video-width",
        type=int,
        default=746,
        help="Saved H.264 width when keeping the full frame. ROI-only mode derives width from height.",
    )
    parser.add_argument(
        "--video-height",
        type=int,
        default=720,
        help="Saved H.264 video height; 720 preserves the native analysis ROI resolution.",
    )
    parser.add_argument(
        "--video-roi-only",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Save only the calibrated analysis ROI (default); use --no-video-roi-only for the full frame.",
    )
    parser.add_argument(
        "--video-grayscale",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Save grayscale H.264 video by default; use --no-video-grayscale for color.",
    )
    parser.add_argument(
        "--video-preset",
        choices=("ultrafast", "superfast", "veryfast"),
        default="veryfast",
        help="H.264 compression preset; veryfast is the tested Pi 5 default.",
    )
    parser.add_argument(
        "--video-encoder-threads",
        type=int,
        default=2,
        help="CPU threads reserved for live H.264 encoding.",
    )
    parser.add_argument(
        "--camera-buffers",
        type=int,
        default=4,
        help="Number of queued V4L2 camera buffers; four sustains USB webcam throughput.",
    )
    parser.add_argument(
        "--analysis-width",
        type=int,
        default=0,
        help="Resize frames to this width for analysis; 0 analyzes at capture resolution.",
    )
    parser.add_argument(
        "--analysis-shift-x",
        type=float,
        default=-2.5,
        help="Shift the square analysis crop horizontally by a percentage of frame width; negative is left.",
    )
    parser.add_argument(
        "--analysis-extend-left",
        type=float,
        default=2.0,
        help="Extend the shifted analysis region to the left by a percentage of frame width.",
    )
    parser.add_argument(
        "--analyze-every",
        type=int,
        default=1,
        help="Analyze every Nth captured frame; raise further if capture cannot sustain its FPS.",
    )
    parser.add_argument(
        "--opencv-threads",
        type=int,
        default=2,
        help="OpenCV worker threads. Two leaves CPU capacity for capture and system tasks.",
    )
    parser.add_argument(
        "--warmup",
        type=float,
        default=2.0,
        help="Seconds to let the camera auto-exposure settle before recording.",
    )
    parser.add_argument(
        "--no-camera-lock",
        action="store_true",
        help="Leave supported camera exposure, white balance and focus controls automatic.",
    )
    parser.add_argument(
        "--allow-dynamic-framerate",
        action="store_true",
        help="Allow the webcam to lower FPS for longer exposure; fixed FPS is the default.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path(__file__).resolve().parent / "captures",
        help="Directory where video and result files are saved.",
    )
    parser.add_argument(
        "--keep-captures",
        type=int,
        default=5,
        help="Number of most recent capture runs to keep. Use 0 to keep only the current run.",
    )
    parser.add_argument(
        "--motion-threshold",
        type=int,
        default=35,
        help="Pixel-difference threshold for motion. Raise to ignore tiny camera noise.",
    )
    parser.add_argument(
        "--min-change-area",
        type=int,
        default=0,
        help="Minimum changed-pixel blob area to count as real motion. 0 disables this CPU-heavy cleanup.",
    )
    parser.add_argument(
        "--dark-threshold",
        type=int,
        default=0,
        help="Fly/background cutoff. 0 means estimate automatically with Otsu thresholding.",
    )
    parser.add_argument(
        "--show-preview",
        action="store_true",
        help="Show a live preview window while recording. Requires a display.",
    )
    parser.add_argument(
        "--no-lcd",
        action="store_true",
        help="Disable 16x2 LCD status output.",
    )
    parser.add_argument("--lcd-rs", type=int, default=25, help="LCD RS GPIO pin.")
    parser.add_argument("--lcd-en", type=int, default=24, help="LCD enable GPIO pin.")
    parser.add_argument("--lcd-d4", type=int, default=23, help="LCD D4 GPIO pin.")
    parser.add_argument("--lcd-d5", type=int, default=17, help="LCD D5 GPIO pin.")
    parser.add_argument("--lcd-d6", type=int, default=18, help="LCD D6 GPIO pin.")
    parser.add_argument("--lcd-d7", type=int, default=22, help="LCD D7 GPIO pin.")
    parser.add_argument(
        "--button-gpio",
        type=int,
        default=26,
        help="GPIO pin for the scan trigger button. Wire button between this pin and GND.",
    )
    parser.add_argument(
        "--button-debounce",
        type=float,
        default=0.25,
        help="Seconds to debounce the button press/release.",
    )
    parser.add_argument(
        "--long-press",
        type=float,
        default=1.5,
        help="Seconds the button must be held to advance/save capture settings.",
    )
    parser.add_argument(
        "--result-hold",
        type=float,
        default=6.0,
        help="Seconds to show a new activity score before cycling idle messages.",
    )
    parser.add_argument(
        "--double-press-window",
        type=float,
        default=0.6,
        help="Seconds after a short press to wait for a second press that displays network info.",
    )
    parser.add_argument(
        "--network-display-time",
        type=float,
        default=8.0,
        help="Seconds to show dashboard network information on the LCD.",
    )
    parser.add_argument(
        "--ir-gpio",
        type=int,
        default=16,
        help="GPIO driving the IR illuminator transistor base (default: GPIO16, physical pin 36).",
    )
    parser.add_argument(
        "--no-ir",
        action="store_true",
        help="Disable IR illuminator GPIO control.",
    )
    parser.add_argument(
        "--state-file",
        type=Path,
        default=Path(__file__).resolve().parent / "flyscanner_state.json",
        help="Persistent scanner settings file.",
    )
    parser.add_argument(
        "--web-host",
        default="0.0.0.0",
        help="Dashboard listen address (default: all local interfaces).",
    )
    parser.add_argument(
        "--web-port",
        type=int,
        default=8080,
        help="Dashboard HTTP/WebSocket port.",
    )
    parser.add_argument(
        "--no-web",
        action="store_true",
        help="Disable the local web dashboard.",
    )
    parser.add_argument(
        "--network-socket",
        default="/run/flyscanner-network/control.sock",
        help="Local control socket for fallback-hotspot Wi-Fi setup.",
    )
    parser.add_argument(
        "--run-now",
        action="store_true",
        help="Run one scan immediately instead of waiting for button presses.",
    )
    parser.add_argument(
        "--test-button",
        action="store_true",
        help="Watch the button and print/display its state without running scans.",
    )
    return parser.parse_args()


def ensure_cv_deps() -> None:
    global cv2, np
    if cv2 is not None and np is not None:
        return

    try:
        import cv2 as cv2_module
        import numpy as np_module
    except ImportError as exc:
        raise RuntimeError(
            "OpenCV/NumPy could not be imported. Run outside the venv and make sure "
            "python3-opencv and python3-numpy are installed."
        ) from exc

    cv2 = cv2_module
    np = np_module


class NullLcd:
    def write(self, line1: str, line2: str = "") -> None:
        return

    def clear(self) -> None:
        return


class CharacterLcd:
    columns = 16
    rows = 2

    def __init__(self, args: argparse.Namespace) -> None:
        import board
        import digitalio
        import adafruit_character_lcd.character_lcd as characterlcd

        self._lcd_pins = [
            digitalio.DigitalInOut(self._board_pin(board, args.lcd_rs)),
            digitalio.DigitalInOut(self._board_pin(board, args.lcd_en)),
            digitalio.DigitalInOut(self._board_pin(board, args.lcd_d4)),
            digitalio.DigitalInOut(self._board_pin(board, args.lcd_d5)),
            digitalio.DigitalInOut(self._board_pin(board, args.lcd_d6)),
            digitalio.DigitalInOut(self._board_pin(board, args.lcd_d7)),
        ]
        self._lcd = characterlcd.Character_LCD_Mono(*self._lcd_pins, self.columns, self.rows)
        self.clear()

    @staticmethod
    def _board_pin(board_module: object, gpio_number: int) -> object:
        name = f"D{gpio_number}"
        try:
            return getattr(board_module, name)
        except AttributeError as exc:
            raise RuntimeError(f"board.{name} is not available on this Raspberry Pi") from exc

    @staticmethod
    def _fit(text: str) -> str:
        return str(text).replace("\n", " ")[:16].ljust(16)

    def write(self, line1: str, line2: str = "") -> None:
        self._lcd.message = f"{self._fit(line1)}\n{self._fit(line2)}"

    def clear(self) -> None:
        self._lcd.clear()


class ButtonInput:
    def __init__(self, gpio_number: int) -> None:
        # gpiozero selects the Pi 5-compatible lgpio backend on current Pi OS.
        from gpiozero import Button

        self._button = Button(gpio_number, pull_up=True)

    @property
    def is_pressed(self) -> bool:
        return bool(self._button.is_pressed)

    def close(self) -> None:
        self._button.close()


class NullIlluminator:
    def on(self) -> None:
        return

    def off(self) -> None:
        return

    def close(self) -> None:
        return


class IrIlluminator:
    def __init__(self, gpio_number: int) -> None:
        from gpiozero import DigitalOutputDevice

        # An NPN low-side switch turns on when its base GPIO is high. Keeping
        # initial_value low ensures the array remains off while the service waits.
        self._output = DigitalOutputDevice(
            gpio_number,
            active_high=True,
            initial_value=False,
        )

    def on(self) -> None:
        self._output.on()

    def off(self) -> None:
        self._output.off()

    def close(self) -> None:
        self.off()
        self._output.close()


def setup_lcd(args: argparse.Namespace) -> CharacterLcd | NullLcd:
    if args.no_lcd:
        return NullLcd()

    add_local_venv_site_packages()

    try:
        lcd = CharacterLcd(args)
    except Exception as exc:
        print(f"Warning: LCD disabled: {exc}", file=sys.stderr)
        return NullLcd()

    lcd.write("Fly scanner", "Ready")
    return lcd


def setup_button(args: argparse.Namespace) -> ButtonInput:
    try:
        return ButtonInput(args.button_gpio)
    except Exception as exc:
        raise RuntimeError(f"Could not initialize button on GPIO{args.button_gpio}: {exc}") from exc


def setup_illuminator(args: argparse.Namespace) -> IrIlluminator | NullIlluminator:
    if args.no_ir:
        return NullIlluminator()

    try:
        return IrIlluminator(args.ir_gpio)
    except Exception as exc:
        raise RuntimeError(
            f"Could not initialize IR illuminator on GPIO{args.ir_gpio}: {exc}"
        ) from exc


def test_button(button: ButtonInput, lcd: CharacterLcd | NullLcd, gpio_number: int) -> None:
    print(f"Testing button on GPIO{gpio_number}. Press Ctrl+C to stop.")
    last_state: bool | None = None
    while True:
        state = button.is_pressed
        if state != last_state:
            state_text = "PRESSED" if state else "released"
            print(f"Button {state_text}")
            lcd.write("Button test", state_text)
            last_state = state
        time.sleep(0.05)


def runtime_label(duration_seconds: int) -> str:
    labels = {
        30: "30 seconds",
        5 * 60: "5 minutes",
        10 * 60: "10 minutes",
        24 * 60 * 60: "24 hours",
    }
    return labels.get(duration_seconds, f"{duration_seconds} seconds")


def runtime_lcd_label(duration_seconds: int) -> str:
    labels = {30: "30 sec", 5 * 60: "5 min", 10 * 60: "10 min", 24 * 60 * 60: "24 hr"}
    return labels.get(duration_seconds, f"{duration_seconds}s")


def runtime_short_label(duration_seconds: int) -> str:
    labels = {30: "30s", 5 * 60: "5m", 10 * 60: "10m", 24 * 60 * 60: "24h"}
    return labels.get(duration_seconds, f"{duration_seconds}s")


def preset_runtime(value: float | int) -> int:
    numeric = float(value)
    return min(RUNTIME_PRESETS_SECONDS, key=lambda candidate: abs(candidate - numeric))


def state_document(state_file: Path) -> dict[str, Any]:
    if not state_file.exists():
        return {}
    with state_file.open("r", encoding="utf-8") as file:
        document = json.load(file)
    if not isinstance(document, dict):
        raise ValueError("settings must be an object")
    return document


def write_state_document(state_file: Path, document: dict[str, Any]) -> None:
    state_file.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = state_file.with_name(f".{state_file.name}.tmp")
    try:
        with temporary_path.open("w", encoding="utf-8") as file:
            json.dump(document, file, indent=2)
            file.write("\n")
        temporary_path.replace(state_file)
    except OSError:
        temporary_path.unlink(missing_ok=True)
        raise


def scanner_settings_from_args(
    args: argparse.Namespace, ir_enabled: bool, duration_seconds: int
) -> dict[str, Any]:
    return {
        "duration_seconds": int(duration_seconds),
        "ir_enabled": bool(ir_enabled),
        "camera_device": str(args.camera),
        "capture_width": int(args.width),
        "capture_height": int(args.height),
        "capture_fps": float(args.fps),
        "camera_buffers": int(args.camera_buffers),
        "warmup_seconds": float(args.warmup),
        "camera_lock": not bool(args.no_camera_lock),
        "fixed_framerate": not bool(args.allow_dynamic_framerate),
        "analysis_width": int(args.analysis_width),
        "analyze_every": int(args.analyze_every),
        "opencv_threads": int(args.opencv_threads),
        "motion_threshold": int(args.motion_threshold),
        "min_change_area": int(args.min_change_area),
        "dark_threshold": int(args.dark_threshold),
        "analysis_shift_x": float(args.analysis_shift_x),
        "analysis_extend_left": float(args.analysis_extend_left),
        "video_codec": str(args.video_codec),
        "video_roi_only": bool(args.video_roi_only),
        "video_width": int(args.video_width),
        "video_height": int(args.video_height),
        "video_grayscale": bool(args.video_grayscale),
        "video_crf": int(args.video_crf),
        "video_preset": str(args.video_preset),
        "video_encoder_threads": int(args.video_encoder_threads),
        "keep_captures": int(args.keep_captures),
        "result_hold_seconds": float(args.result_hold),
        "button_debounce_seconds": float(args.button_debounce),
        "long_press_seconds": float(args.long_press),
        "double_press_window_seconds": float(args.double_press_window),
        "network_display_seconds": float(args.network_display_time),
    }


def validate_scanner_settings(
    payload: dict[str, Any], current: dict[str, Any]
) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("settings must be an object")
    unknown = set(payload) - SCANNER_SETTING_KEYS
    if unknown:
        raise ValueError(f"Unknown setting: {sorted(unknown)[0]}")
    settings = dict(current)
    settings.update(payload)

    def integer(name: str, minimum: int, maximum: int) -> int:
        value = settings.get(name)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{name} must be a whole number")
        numeric = int(value)
        if numeric != value or not minimum <= numeric <= maximum:
            raise ValueError(f"{name} must be between {minimum} and {maximum}")
        return numeric

    def decimal(name: str, minimum: float, maximum: float) -> float:
        value = settings.get(name)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{name} must be a number")
        numeric = float(value)
        if not math.isfinite(numeric) or not minimum <= numeric <= maximum:
            raise ValueError(f"{name} must be between {minimum:g} and {maximum:g}")
        return numeric

    def boolean(name: str) -> bool:
        value = settings.get(name)
        if not isinstance(value, bool):
            raise ValueError(f"{name} must be true or false")
        return value

    duration = integer("duration_seconds", 1, 24 * 60 * 60)
    if duration not in RUNTIME_PRESETS_SECONDS:
        raise ValueError("duration_seconds must be 30, 300, 600, or 86400")
    settings["duration_seconds"] = duration
    settings["ir_enabled"] = boolean("ir_enabled")
    camera_device = settings.get("camera_device")
    if not isinstance(camera_device, str) or not camera_device.strip():
        raise ValueError("camera_device must be a camera path or numeric index")
    if len(camera_device) > 256 or "\x00" in camera_device:
        raise ValueError("camera_device is not valid")
    settings["camera_device"] = camera_device.strip()
    settings["capture_width"] = integer("capture_width", 320, 3840)
    settings["capture_height"] = integer("capture_height", 240, 2160)
    settings["capture_fps"] = decimal("capture_fps", 1, 120)
    settings["camera_buffers"] = integer("camera_buffers", 1, 16)
    settings["warmup_seconds"] = decimal("warmup_seconds", 0, 30)
    settings["camera_lock"] = boolean("camera_lock")
    settings["fixed_framerate"] = boolean("fixed_framerate")
    settings["analysis_width"] = integer("analysis_width", 0, 3840)
    settings["analyze_every"] = integer("analyze_every", 1, 30)
    settings["opencv_threads"] = integer("opencv_threads", 1, 4)
    settings["motion_threshold"] = integer("motion_threshold", 0, 255)
    settings["min_change_area"] = integer("min_change_area", 0, 100_000)
    settings["dark_threshold"] = integer("dark_threshold", 0, 255)
    settings["analysis_shift_x"] = decimal("analysis_shift_x", -20, 20)
    settings["analysis_extend_left"] = decimal("analysis_extend_left", 0, 30)
    codec = settings.get("video_codec")
    if codec not in {"h264", "mjpeg"}:
        raise ValueError("video_codec must be h264 or mjpeg")
    settings["video_codec"] = codec
    settings["video_roi_only"] = boolean("video_roi_only")
    settings["video_width"] = integer(
        "video_width", 2 if settings["video_roi_only"] else 320, 3840
    )
    settings["video_height"] = integer("video_height", 240, 2160)
    if settings["video_roi_only"] and codec == "h264":
        settings["video_width"], settings["video_height"] = roi_video_output_size(
            settings["capture_width"],
            settings["capture_height"],
            settings["analysis_shift_x"],
            settings["analysis_extend_left"],
            settings["video_height"],
        )
    if codec == "h264" and (settings["video_width"] % 2 or settings["video_height"] % 2):
        raise ValueError("H.264 video width and height must be even")
    settings["video_grayscale"] = boolean("video_grayscale")
    settings["video_crf"] = integer("video_crf", 0, 51)
    preset = settings.get("video_preset")
    if preset not in {"ultrafast", "superfast", "veryfast"}:
        raise ValueError("video_preset must be ultrafast, superfast, or veryfast")
    settings["video_preset"] = preset
    settings["video_encoder_threads"] = integer("video_encoder_threads", 1, 4)
    settings["keep_captures"] = integer("keep_captures", 0, 20)
    settings["result_hold_seconds"] = decimal("result_hold_seconds", 0, 30)
    settings["button_debounce_seconds"] = decimal(
        "button_debounce_seconds", 0, 2
    )
    settings["long_press_seconds"] = decimal("long_press_seconds", 0.5, 5)
    settings["double_press_window_seconds"] = decimal(
        "double_press_window_seconds", 0.2, 2
    )
    settings["network_display_seconds"] = decimal("network_display_seconds", 1, 30)
    analysis_region_geometry(
        settings["capture_height"],
        settings["capture_width"],
        settings["analysis_shift_x"],
        settings["analysis_extend_left"],
    )
    return settings


def apply_scanner_settings(args: argparse.Namespace, settings: dict[str, Any]) -> None:
    args.camera = settings["camera_device"]
    args.width = settings["capture_width"]
    args.height = settings["capture_height"]
    args.fps = settings["capture_fps"]
    args.camera_buffers = settings["camera_buffers"]
    args.warmup = settings["warmup_seconds"]
    args.no_camera_lock = not settings["camera_lock"]
    args.allow_dynamic_framerate = not settings["fixed_framerate"]
    args.analysis_width = settings["analysis_width"]
    args.analyze_every = settings["analyze_every"]
    args.opencv_threads = settings["opencv_threads"]
    args.motion_threshold = settings["motion_threshold"]
    args.min_change_area = settings["min_change_area"]
    args.dark_threshold = settings["dark_threshold"]
    args.analysis_shift_x = settings["analysis_shift_x"]
    args.analysis_extend_left = settings["analysis_extend_left"]
    args.video_codec = settings["video_codec"]
    args.video_roi_only = settings["video_roi_only"]
    args.video_width = settings["video_width"]
    args.video_height = settings["video_height"]
    args.video_grayscale = settings["video_grayscale"]
    args.video_crf = settings["video_crf"]
    args.video_preset = settings["video_preset"]
    args.video_encoder_threads = settings["video_encoder_threads"]
    args.keep_captures = settings["keep_captures"]
    args.result_hold = settings["result_hold_seconds"]
    args.button_debounce = settings["button_debounce_seconds"]
    args.long_press = settings["long_press_seconds"]
    args.double_press_window = settings["double_press_window_seconds"]
    args.network_display_time = settings["network_display_seconds"]
    args.duration = float(settings["duration_seconds"])
    if cv2 is not None:
        cv2.setNumThreads(max(args.opencv_threads, 1))


def load_saved_web_settings(
    state_file: Path, current: dict[str, Any]
) -> dict[str, Any]:
    try:
        document = state_document(state_file)
        saved = document.get("web_settings", {})
        if not isinstance(saved, dict):
            raise ValueError("web_settings must be an object")
        saved = dict(saved)
        if "video_roi_only" not in saved:
            # Migrate the old full-frame 960x540 default to the native analysis
            # ROI at 746x720. Preserve explicitly customized sizes.
            saved["video_roi_only"] = True
            if saved.get("video_width") == 960 and saved.get("video_height") == 540:
                saved["video_width"] = 746
                saved["video_height"] = 720
        return validate_scanner_settings(saved, current)
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        print(f"Warning: could not load advanced web settings: {exc}", file=sys.stderr)
        return validate_scanner_settings({}, current)


def save_full_scanner_settings(state_file: Path, settings: dict[str, Any]) -> None:
    validated = validate_scanner_settings(settings, settings)
    try:
        document = state_document(state_file)
    except (OSError, ValueError, json.JSONDecodeError):
        document = {}
    document["ir_enabled"] = validated["ir_enabled"]
    document["duration_seconds"] = validated["duration_seconds"]
    document["web_settings"] = {
        key: value
        for key, value in validated.items()
        if key not in {"ir_enabled", "duration_seconds"}
    }
    write_state_document(state_file, document)


def load_scanner_settings(
    state_file: Path,
    default_ir: bool = True,
    default_duration: int = DEFAULT_RUNTIME_SECONDS,
) -> tuple[bool, int]:
    if not state_file.exists():
        return default_ir, preset_runtime(default_duration)

    try:
        settings = state_document(state_file)
        ir_enabled = settings.get("ir_enabled", default_ir)
        duration_seconds = settings.get("duration_seconds", default_duration)
        if not isinstance(ir_enabled, bool):
            raise ValueError("ir_enabled must be true or false")
        if isinstance(duration_seconds, bool) or not isinstance(duration_seconds, (int, float)):
            raise ValueError("duration_seconds must be a number")
        duration_preset = int(duration_seconds)
        if duration_preset not in RUNTIME_PRESETS_SECONDS:
            raise ValueError("duration_seconds is not a supported preset")
        return ir_enabled, duration_preset
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        print(
            f"Warning: could not read {state_file}: {exc}; using default capture settings",
            file=sys.stderr,
        )
        return default_ir, preset_runtime(default_duration)


def save_scanner_settings(state_file: Path, ir_enabled: bool, duration_seconds: int) -> None:
    if duration_seconds not in RUNTIME_PRESETS_SECONDS:
        raise ValueError("duration_seconds is not a supported preset")
    try:
        document = state_document(state_file)
    except (OSError, ValueError, json.JSONDecodeError):
        document = {}
    document["ir_enabled"] = ir_enabled
    document["duration_seconds"] = duration_seconds
    write_state_document(state_file, document)


def wait_for_idle_action(
    button: ButtonInput,
    lcd: CharacterLcd | NullLcd,
    gpio_number: int,
    debounce_seconds: float,
    long_press_seconds: float,
    ir_enabled: bool,
    duration_seconds: int,
    last_result: ActivityResult | None = None,
    hold_new_result: bool = False,
    result_hold_seconds: float = 6.0,
    double_press_window: float = 0.6,
    dashboard: DashboardState | None = None,
) -> str | dict[str, Any]:
    print(
        f"Waiting for button on GPIO{gpio_number} "
        f"(runtime: {runtime_label(duration_seconds)}, "
        f"IR during capture: {'on' if ir_enabled else 'off'})..."
    )

    while button.is_pressed:
        time.sleep(0.05)

    cycle_started = time.monotonic()
    score_hold_until = (
        cycle_started + result_hold_seconds
        if last_result is not None and hold_new_result
        else cycle_started
    )
    last_lcd_mode: str | None = None
    while not button.is_pressed:
        remote_command = dashboard.take_idle_command() if dashboard is not None else None
        if remote_command is not None:
            return remote_command
        now = time.monotonic()
        if last_result is None:
            mode = "prompt"
        elif now < score_hold_until:
            mode = "score"
        else:
            # Start with the control hint after the exclusive score hold, then
            # alternate it with the score every two seconds.
            mode = "prompt" if int((now - score_hold_until) / 2) % 2 == 0 else "score"

        if mode != last_lcd_mode:
            if mode == "score" and last_result is not None:
                lcd.write("Activity score", f"{last_result.activity_score:.2f}")
            else:
                state = "on" if ir_enabled else "off"
                lcd.write(
                    "Fly scanner",
                    f"{runtime_short_label(duration_seconds)} IR:{state} T/Hold",
                )
            last_lcd_mode = mode

        time.sleep(0.05)

    pressed_at = time.monotonic()
    showed_long_press = False
    while button.is_pressed:
        held_for = time.monotonic() - pressed_at
        if held_for >= long_press_seconds and not showed_long_press:
            lcd.write("Capture settings", "Release button")
            showed_long_press = True
        time.sleep(0.02)

    held_for = time.monotonic() - pressed_at
    released_at = time.monotonic()
    if held_for >= long_press_seconds:
        time.sleep(debounce_seconds)
        return "capture_settings"

    # A single press is intentionally delayed by this short window. Ignore the
    # immediate release bounce, then look for one more distinct press.
    time.sleep(min(debounce_seconds, 0.08))
    second_press_deadline = released_at + double_press_window
    while time.monotonic() < second_press_deadline:
        remote_command = dashboard.take_idle_command() if dashboard is not None else None
        if remote_command is not None:
            return remote_command
        if button.is_pressed:
            while button.is_pressed:
                time.sleep(0.02)
            time.sleep(debounce_seconds)
            return "show_network"
        time.sleep(0.02)
    return "scan"


def local_ipv4_address() -> str | None:
    """Return the IPv4 address selected by the default LAN route."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as connection:
            # UDP connect selects a route without transmitting application data.
            connection.connect(("1.1.1.1", 80))
            address = str(connection.getsockname()[0])
            if address and not address.startswith("127."):
                return address
    except OSError:
        pass

    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            address = str(info[4][0])
            if address and not address.startswith("127."):
                return address
    except OSError:
        pass
    return None


def show_network_info(
    lcd: CharacterLcd | NullLcd,
    web_port: int,
    display_seconds: float,
    network_socket: str,
) -> None:
    hotspot_active = False
    try:
        network_status = network_helper_request(network_socket, {"command": "status"})
        hotspot_active = bool(network_status.get("ok") and network_status.get("hotspot_active"))
    except Exception:
        pass
    address = "10.42.0.1" if hotspot_active else local_ipv4_address()
    hostname = socket.gethostname().split(".", 1)[0]
    if address is None:
        lcd.write("Network offline", "Try again later")
        print("Dashboard address unavailable: network is offline")
    else:
        lcd.write(f"{'Setup WiFi' if hotspot_active else 'Dashboard'} :{web_port}", address)
        print(f"Dashboard: http://{address}:{web_port}")
        print(f"mDNS shortcut: http://{hostname}.local:{web_port}")
    time.sleep(display_seconds)


def configure_capture_settings(
    button: ButtonInput,
    lcd: CharacterLcd | NullLcd,
    current_duration: int,
    current_ir_value: bool,
    state_file: Path,
    debounce_seconds: float,
    long_press_seconds: float,
    ir_available: bool = True,
) -> tuple[int, bool]:
    selected_duration = preset_runtime(current_duration)
    selected_ir = current_ir_value if ir_available else False
    print("Capture settings: short press changes; long press advances, then saves.")

    while True:
        lcd.write(f"Runtime: {runtime_lcd_label(selected_duration)}", "Tap / hold next")

        while not button.is_pressed:
            time.sleep(0.05)

        pressed_at = time.monotonic()
        showed_next_prompt = False
        while button.is_pressed:
            held_for = time.monotonic() - pressed_at
            if held_for >= long_press_seconds and not showed_next_prompt:
                lcd.write("IR settings next", "Release button")
                showed_next_prompt = True
            time.sleep(0.02)

        held_for = time.monotonic() - pressed_at
        time.sleep(debounce_seconds)
        if held_for >= long_press_seconds:
            break

        selected_index = RUNTIME_PRESETS_SECONDS.index(selected_duration)
        selected_duration = RUNTIME_PRESETS_SECONDS[
            (selected_index + 1) % len(RUNTIME_PRESETS_SECONDS)
        ]
        print(f"Runtime selection: {runtime_label(selected_duration)}")

    while True:
        state = "ON" if selected_ir else "OFF"
        if ir_available:
            lcd.write(f"IR capture: {state}", "Tap / hold save")
        else:
            lcd.write("IR unavailable", "Hold to save")

        while not button.is_pressed:
            time.sleep(0.05)

        pressed_at = time.monotonic()
        showed_save_prompt = False
        while button.is_pressed:
            held_for = time.monotonic() - pressed_at
            if held_for >= long_press_seconds and not showed_save_prompt:
                lcd.write("Save settings", "Release to save")
                showed_save_prompt = True
            time.sleep(0.02)

        held_for = time.monotonic() - pressed_at
        time.sleep(debounce_seconds)
        if held_for >= long_press_seconds:
            save_scanner_settings(state_file, selected_ir, selected_duration)
            lcd.write("Settings saved", f"{runtime_short_label(selected_duration)} IR:{state}")
            print(
                f"Saved capture settings: {runtime_label(selected_duration)}, IR {state}"
            )
            time.sleep(1.0)
            return selected_duration, selected_ir

        if ir_available:
            selected_ir = not selected_ir
            print(f"IR capture selection: {'ON' if selected_ir else 'OFF'}")


def add_local_venv_site_packages() -> None:
    venv_lib = Path(__file__).resolve().parent / ".venv" / "lib"
    if not venv_lib.exists():
        return

    current_version = f"python{sys.version_info.major}.{sys.version_info.minor}"
    candidates = [venv_lib / current_version / "site-packages"]
    candidates.extend(sorted(venv_lib.glob("python*/site-packages")))

    for site_packages in candidates:
        if site_packages.exists():
            path_text = str(site_packages)
            if path_text not in sys.path:
                sys.path.insert(0, path_text)
            return


def camera_source(value: str) -> str | int:
    return int(value) if value.isdecimal() else value


def open_camera(
    camera_index: str,
    width: int,
    height: int,
    fps: float,
    buffer_count: int = 4,
) -> cv2.VideoCapture:
    source = camera_source(camera_index)
    cap = cv2.VideoCapture(source, cv2.CAP_V4L2)
    if not cap.isOpened():
        cap.release()
        cap = cv2.VideoCapture(source)

    if not cap.isOpened():
        raise RuntimeError(
            f"Could not open webcam {camera_index}. Check it with: v4l2-ctl --list-devices"
        )

    cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
    cap.set(cv2.CAP_PROP_FPS, fps)
    # A single V4L2 buffer caused this UVC camera to lose every other USB
    # transfer (15 FPS despite a negotiated 30 FPS mode). Four keeps transfers
    # queued while OpenCV decodes the previous MJPEG frame.
    cap.set(cv2.CAP_PROP_BUFFERSIZE, buffer_count)
    return cap


def disable_camera_dynamic_framerate(camera_index: str, allowed: bool = False) -> bool:
    """Keep supported V4L2 webcams from silently lowering the requested FPS."""
    if allowed:
        return False
    if camera_index.isdecimal():
        device = f"/dev/video{camera_index}"
    elif camera_index.startswith("/dev/video"):
        device = camera_index
    else:
        print(
            "Warning: cannot configure dynamic frame rate for a non-V4L2 camera source",
            file=sys.stderr,
        )
        return False
    try:
        result = subprocess.run(
            [
                "v4l2-ctl",
                f"--device={device}",
                "--set-ctrl=exposure_dynamic_framerate=0",
            ],
            text=True,
            capture_output=True,
            timeout=5.0,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"Warning: could not fix camera frame rate: {exc}", file=sys.stderr)
        return False
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        print(
            f"Camera has no controllable dynamic frame rate; continuing: {detail}",
            file=sys.stderr,
        )
        return False
    print("Disabled webcam exposure-driven frame-rate reduction")
    return True


def valid_camera_value(cap: cv2.VideoCapture, property_id: int | None) -> float | None:
    if property_id is None:
        return None
    value = float(cap.get(property_id))
    return value if np.isfinite(value) else None


def lock_camera_controls(cap: cv2.VideoCapture, disabled: bool = False) -> dict[str, Any]:
    """Freeze supported automatic controls at their post-warm-up values."""
    properties = {
        "exposure": getattr(cv2, "CAP_PROP_EXPOSURE", None),
        "gain": getattr(cv2, "CAP_PROP_GAIN", None),
        "white_balance": getattr(cv2, "CAP_PROP_WB_TEMPERATURE", None),
        "focus": getattr(cv2, "CAP_PROP_FOCUS", None),
    }
    values = {name: valid_camera_value(cap, prop) for name, prop in properties.items()}
    locked: list[str] = []

    if not disabled:
        auto_exposure = getattr(cv2, "CAP_PROP_AUTO_EXPOSURE", None)
        # Native V4L2 uses menu value 1 for manual exposure. Some OpenCV
        # backends instead expose the same setting as normalized value 0.25.
        exposure_locked = auto_exposure is not None and (
            cap.set(auto_exposure, 1.0) or cap.set(auto_exposure, 0.25)
        )
        if exposure_locked:
            locked.append("exposure")
            if values["exposure"] is not None:
                cap.set(properties["exposure"], values["exposure"])
            if values["gain"] is not None:
                cap.set(properties["gain"], values["gain"])

        auto_wb = getattr(cv2, "CAP_PROP_AUTO_WB", None)
        if auto_wb is not None and cap.set(auto_wb, 0.0):
            locked.append("white_balance")
            if values["white_balance"] is not None:
                cap.set(properties["white_balance"], values["white_balance"])

        autofocus = getattr(cv2, "CAP_PROP_AUTOFOCUS", None)
        if autofocus is not None and cap.set(autofocus, 0.0):
            locked.append("focus")
            if values["focus"] is not None:
                cap.set(properties["focus"], values["focus"])

    # Read back the values actually in use. Some cameras accept a control call
    # but silently clamp or ignore the requested value.
    values = {name: valid_camera_value(cap, prop) for name, prop in properties.items()}
    lock_text = ",".join(locked)
    if disabled:
        print("Camera control locking disabled")
    elif lock_text:
        print(f"Locked camera controls: {lock_text}")
    else:
        print("Camera exposes no lockable automatic controls; continuing", file=sys.stderr)

    return {
        "camera_locked_controls": lock_text,
        "camera_exposure": values["exposure"],
        "camera_gain": values["gain"],
        "camera_white_balance": values["white_balance"],
        "camera_focus": values["focus"],
    }


class FfmpegH264Writer:
    def __init__(
        self,
        path: Path,
        fps: float,
        frame_size: tuple[int, int],
        crf: int,
        preset: str,
        threads: int,
        output_size: tuple[int, int],
        grayscale: bool,
    ) -> None:
        ffmpeg = shutil.which("ffmpeg")
        if ffmpeg is None:
            raise RuntimeError("FFmpeg is missing; install it with: sudo apt install ffmpeg")
        width, height = frame_size
        output_width, output_height = output_size
        filters = [f"scale={output_width}:{output_height}:flags=lanczos"]
        if grayscale:
            filters.append("hue=s=0")
        command = [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "bgr24",
            "-video_size",
            f"{width}x{height}",
            "-framerate",
            f"{fps:g}",
            "-i",
            "-",
            "-an",
            "-vf",
            ",".join(filters),
            "-c:v",
            "libx264",
            "-preset",
            preset,
            "-crf",
            str(crf),
            "-threads",
            str(threads),
            "-pix_fmt",
            "yuv420p",
            "-g",
            str(max(1, round(fps * 10))),
            "-movflags",
            "+faststart",
            str(path),
        ]
        self.process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )
        self.path = path
        self._released = False

    def _error_text(self) -> str:
        if self.process.stderr is None:
            return "unknown FFmpeg error"
        return self.process.stderr.read().decode(errors="replace").strip() or "unknown FFmpeg error"

    def write(self, frame: np.ndarray) -> None:
        if self._released or self.process.stdin is None:
            raise RuntimeError("Cannot write to a closed H.264 video")
        if self.process.poll() is not None:
            raise RuntimeError(f"FFmpeg stopped while recording: {self._error_text()}")
        try:
            self.process.stdin.write(np.ascontiguousarray(frame).tobytes())
        except BrokenPipeError as exc:
            raise RuntimeError(f"FFmpeg stopped while recording: {self._error_text()}") from exc

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        if self.process.stdin is not None:
            self.process.stdin.close()
        return_code = self.process.wait()
        if return_code:
            raise RuntimeError(f"FFmpeg could not finalize {self.path.name}: {self._error_text()}")


def make_video_writer(
    args: argparse.Namespace,
    path: Path,
    fps: float,
    frame_size: tuple[int, int],
    output_size: tuple[int, int],
) -> Any:
    if args.video_codec == "h264":
        return FfmpegH264Writer(
            path,
            fps,
            frame_size,
            args.video_crf,
            args.video_preset,
            args.video_encoder_threads,
            output_size,
            args.video_grayscale,
        )

    codecs = ("MJPG", "XVID", "mp4v")
    for codec in codecs:
        writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*codec), fps, frame_size)
        if writer.isOpened():
            return writer
        writer.release()
    raise RuntimeError("Could not create video writer. Check OpenCV video codec support.")


def analysis_region_geometry(
    height: int,
    width: int,
    shift_x_percent: float,
    extend_left_percent: float,
) -> tuple[int, int, int, int]:
    """Return the calibrated ROI scaled to the current frame dimensions."""
    shift_adjustment = shift_x_percent - CALIBRATED_SHIFT_X_PERCENT
    extension_adjustment = extend_left_percent - CALIBRATED_EXTEND_LEFT_PERCENT
    left_percent = (
        CALIBRATED_ROI_LEFT_PERCENT + shift_adjustment - extension_adjustment
    )
    right_percent = CALIBRATED_ROI_RIGHT_PERCENT + shift_adjustment

    left = round(width * left_percent / 100.0)
    right = round(width * right_percent / 100.0)
    if not 0 <= left < right <= width:
        raise ValueError(
            "Configured analysis ROI falls outside the frame; adjust "
            "analysis-shift-x or analysis-extend-left"
        )
    return left, 0, right - left, height


def roi_video_output_size(
    capture_width: int,
    capture_height: int,
    shift_x_percent: float,
    extend_left_percent: float,
    output_height: int,
) -> tuple[int, int]:
    """Return an even H.264 size that preserves the calibrated ROI aspect."""
    _, _, roi_width, roi_height = analysis_region_geometry(
        capture_height,
        capture_width,
        shift_x_percent,
        extend_left_percent,
    )
    scaled_width = output_height * roi_width / max(roi_height, 1)
    even_width = max(2, int(round(scaled_width / 2.0)) * 2)
    return even_width, output_height


def analysis_region(
    frame: np.ndarray, shift_x_percent: float, extend_left_percent: float
) -> np.ndarray:
    """Return the shifted analysis view with its left-side extension."""
    height, width = frame.shape[:2]
    left, top, region_width, region_height = analysis_region_geometry(
        height, width, shift_x_percent, extend_left_percent
    )
    return frame[top : top + region_height, left : left + region_width]


class LiveActivityAnalyzer:
    def __init__(self, motion_threshold: int, min_change_area: int, dark_threshold: int) -> None:
        self.motion_threshold = motion_threshold
        self.min_change_area = min_change_area
        self.dark_threshold = dark_threshold
        self.threshold_used = 0
        self.prev_gray: np.ndarray | None = None
        self.motion_count = 0
        self.motion_sum = 0.0
        self.motion_histogram = np.zeros(10_001, dtype=np.uint64)
        self.fly_area_sum = 0.0
        self.moving_fly_area_sum = 0.0
        self.brightness_count = 0
        self.brightness_sum = 0.0
        self.brightness_min = 255
        self.brightness_max = 0
        self.kernel = np.ones((3, 3), np.uint8)

    @property
    def frames_analyzed(self) -> int:
        # The first frame establishes the motion baseline but is still fully
        # processed for brightness and thresholding, so include it in the
        # displayed analysis throughput.
        return self.brightness_count

    def update(self, frame: np.ndarray) -> None:
        gray_raw = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        gray = cv2.GaussianBlur(gray_raw, (5, 5), 0)
        brightness_mean = float(gray_raw.mean())
        self.brightness_count += 1
        self.brightness_sum += brightness_mean
        self.brightness_min = min(self.brightness_min, int(gray_raw.min()))
        self.brightness_max = max(self.brightness_max, int(gray_raw.max()))

        if self.prev_gray is None:
            self.prev_gray = gray
            self.threshold_used = self.dark_threshold or estimate_dark_threshold(gray_raw)
            return

        diff = cv2.absdiff(self.prev_gray, gray)
        _, motion_mask = cv2.threshold(diff, self.motion_threshold, 255, cv2.THRESH_BINARY)
        motion_mask = cv2.morphologyEx(motion_mask, cv2.MORPH_OPEN, self.kernel)
        motion_mask = remove_small_blobs(motion_mask, self.min_change_area)

        fly_mask = cv2.inRange(gray_raw, 0, self.threshold_used)
        moving_fly_mask = cv2.bitwise_and(motion_mask, fly_mask)

        total_pixels = float(gray.size)
        motion_percent = 100.0 * cv2.countNonZero(motion_mask) / total_pixels
        fly_area_percent = 100.0 * cv2.countNonZero(fly_mask) / total_pixels
        moving_fly_area_percent = (
            100.0 * cv2.countNonZero(moving_fly_mask) / total_pixels
        )
        self.motion_count += 1
        self.motion_sum += motion_percent
        histogram_index = min(10_000, max(0, round(motion_percent * 100.0)))
        self.motion_histogram[histogram_index] += 1
        self.fly_area_sum += fly_area_percent
        self.moving_fly_area_sum += moving_fly_area_percent

        self.prev_gray = gray

    def result(
        self,
        video_path: Path,
        first_frame_path: Path,
        duration_seconds: float,
        frames_captured: int,
        metadata: dict[str, Any],
    ) -> ActivityResult:
        if self.motion_count == 0:
            raise RuntimeError("Not enough frames were captured to analyze activity.")

        motion_mean = self.motion_sum / self.motion_count
        percentile_position = (self.motion_count - 1) * 0.95
        lower_index = int(percentile_position)
        upper_index = min(lower_index + 1, self.motion_count - 1)
        fraction = percentile_position - lower_index
        cumulative_motion = np.cumsum(self.motion_histogram)
        lower_value = np.searchsorted(cumulative_motion, lower_index + 1) / 100.0
        upper_value = np.searchsorted(cumulative_motion, upper_index + 1) / 100.0
        motion_peak = float(lower_value + (upper_value - lower_value) * fraction)
        fly_area_mean = self.fly_area_sum / self.motion_count
        moving_fly_area_mean = self.moving_fly_area_sum / self.motion_count
        raw_score = (0.7 * moving_fly_area_mean + 0.3 * motion_mean) / 2.0 * 100.0
        # This is an activity index, not a percentage. Do not clamp it to 100:
        # highly active samples must remain distinguishable from one another.
        activity_score = float(raw_score)

        return ActivityResult(
            scan_timestamp=metadata["scan_timestamp"],
            video_path=str(video_path),
            first_frame_path=str(first_frame_path),
            ir_enabled=metadata["ir_enabled"],
            video_codec=metadata["video_codec"],
            video_roi_only=metadata["video_roi_only"],
            video_width=metadata["video_width"],
            video_height=metadata["video_height"],
            video_grayscale=metadata["video_grayscale"],
            video_crf=metadata["video_crf"],
            capture_width=metadata["capture_width"],
            capture_height=metadata["capture_height"],
            analysis_roi_left=metadata["analysis_roi_left"],
            analysis_roi_top=metadata["analysis_roi_top"],
            analysis_roi_width=metadata["analysis_roi_width"],
            analysis_roi_height=metadata["analysis_roi_height"],
            frames_dropped=metadata["frames_dropped"],
            camera_locked_controls=metadata["camera_locked_controls"],
            camera_exposure=metadata["camera_exposure"],
            camera_gain=metadata["camera_gain"],
            camera_white_balance=metadata["camera_white_balance"],
            camera_focus=metadata["camera_focus"],
            duration_seconds=round(duration_seconds, 3),
            frames_analyzed=self.frames_analyzed,
            frames_captured=frames_captured,
            fps_estimate=round(frames_captured / max(duration_seconds, 0.001), 3),
            analysis_fps_estimate=round(self.frames_analyzed / max(duration_seconds, 0.001), 3),
            activity_score=round(activity_score, 2),
            brightness_mean=round(self.brightness_sum / max(self.brightness_count, 1), 2),
            brightness_min=self.brightness_min,
            brightness_max=self.brightness_max,
            motion_percent_mean=round(motion_mean, 4),
            motion_percent_peak=round(motion_peak, 4),
            fly_area_percent_mean=round(fly_area_mean, 4),
            moving_fly_area_percent_mean=round(moving_fly_area_mean, 4),
            threshold_used=self.threshold_used,
        )


def capture_video(
    args: argparse.Namespace,
    video_path: Path,
    first_frame_path: Path,
    lcd: CharacterLcd | NullLcd,
    scan_timestamp: str,
    ir_enabled: bool,
    dashboard: DashboardState | None = None,
    session_progress: str = "",
) -> tuple[float, int, ActivityResult]:
    lcd.write("Opening camera", f"Cam {args.camera}")
    if dashboard is not None:
        dashboard.set_status("opening_camera", f"Camera {args.camera}")
    fixed_framerate = disable_camera_dynamic_framerate(
        args.camera, args.allow_dynamic_framerate
    )
    cap = open_camera(
        args.camera,
        args.width,
        args.height,
        args.fps,
        args.camera_buffers,
    )

    if args.warmup > 0:
        if dashboard is not None:
            dashboard.set_status("warming_up", f"{args.warmup:.1f} seconds")
        print(f"Warming camera for {args.warmup:.1f}s so exposure can settle...")
        warmup_end = time.monotonic() + args.warmup
        last_warmup_second = -1
        while time.monotonic() < warmup_end:
            if dashboard is not None and dashboard.cancel_requested():
                cap.release()
                raise ScanCancelled("Scan cancelled during camera warm-up")
            remaining = max(0, int(round(warmup_end - time.monotonic())))
            if remaining != last_warmup_second:
                lcd.write("Warming camera", f"{remaining}s")
                last_warmup_second = remaining
            cap.read()
            time.sleep(0.05)

    camera_metadata = lock_camera_controls(cap, args.no_camera_lock)
    if fixed_framerate:
        locked_controls = camera_metadata["camera_locked_controls"]
        camera_metadata["camera_locked_controls"] = ",".join(
            value for value in (locked_controls, "dynamic_framerate") if value
        )
    if camera_metadata["camera_locked_controls"]:
        # Drain a few buffers after changing controls so the first saved frame
        # reflects the locked settings.
        for _ in range(3):
            cap.read()

    if dashboard is not None and dashboard.cancel_requested():
        cap.release()
        raise ScanCancelled("Scan cancelled before recording")

    ok, frame = cap.read()
    if not ok or frame is None:
        cap.release()
        raise RuntimeError("Camera opened, but no frame could be read.")

    height, width = frame.shape[:2]
    negotiated_fps = float(cap.get(cv2.CAP_PROP_FPS))
    if not np.isfinite(negotiated_fps) or negotiated_fps < 1.0:
        negotiated_fps = args.fps
    print(f"Camera negotiated {width}x{height} at {negotiated_fps:g} FPS")
    if (width, height) != (args.width, args.height):
        print(
            f"Warning: requested {args.width}x{args.height}, camera supplied {width}x{height}",
            file=sys.stderr,
        )
    first_analysis_frame = analysis_region(
        frame, args.analysis_shift_x, args.analysis_extend_left
    )
    crop_height, crop_width = first_analysis_frame.shape[:2]
    crop_left, crop_top, _, _ = analysis_region_geometry(
        height, width, args.analysis_shift_x, args.analysis_extend_left
    )
    print(
        f"Analysis ROI: {crop_width}x{crop_height} at x={crop_left}, y={crop_top} "
        f"(shift {args.analysis_shift_x:+g}%, extend left {args.analysis_extend_left:g}%)"
    )
    if not cv2.imwrite(str(first_frame_path), first_analysis_frame):
        print(f"Warning: could not save first frame snapshot to {first_frame_path}", file=sys.stderr)

    gray_check = cv2.cvtColor(first_analysis_frame, cv2.COLOR_BGR2GRAY)
    print(
        "First analysis frame brightness: "
        f"mean={gray_check.mean():.1f}, min={gray_check.min()}, max={gray_check.max()} "
        f"(0=black, 255=white)"
    )

    video_frame_size = (
        (crop_width, crop_height) if args.video_roi_only else (width, height)
    )
    if args.video_codec == "h264":
        video_output_size = (
            roi_video_output_size(
                width,
                height,
                args.analysis_shift_x,
                args.analysis_extend_left,
                args.video_height,
            )
            if args.video_roi_only
            else (args.video_width, args.video_height)
        )
    else:
        video_output_size = video_frame_size
    print(
        f"Saved video view: {'analysis ROI' if args.video_roi_only else 'full frame'} "
        f"at {video_output_size[0]}x{video_output_size[1]}"
    )
    writer = make_video_writer(
        args,
        video_path,
        negotiated_fps,
        video_frame_size,
        video_output_size,
    )
    analyzer = LiveActivityAnalyzer(args.motion_threshold, args.min_change_area, args.dark_threshold)

    start = time.monotonic()
    frame_count = 0
    dropped_frames = 0
    progress_interval = 60 if args.duration >= 60 * 60 else 1
    last_progress_bucket = -1
    duration_display = format_recording_time(args.duration)
    recording_title = session_progress or "Recording"
    lcd.write(recording_title, f"00:00/{duration_display}"[-16:])
    if dashboard is not None:
        detail_prefix = f"{session_progress} · " if session_progress else ""
        dashboard.set_status("recording", f"{detail_prefix}00:00 / {duration_display}")

    try:
        while True:
            elapsed = time.monotonic() - start
            if elapsed >= args.duration:
                break
            if dashboard is not None and dashboard.cancel_requested():
                print("Remote cancellation requested; finalizing partial recording")
                break

            ok, frame = cap.read()
            if not ok or frame is None:
                dropped_frames += 1
                print("Warning: dropped camera frame", file=sys.stderr)
                continue

            roi_frame = (
                analysis_region(frame, args.analysis_shift_x, args.analysis_extend_left)
                if args.video_roi_only
                else None
            )
            writer.write(roi_frame if roi_frame is not None else frame)
            frame_count += 1
            if frame_count % max(args.analyze_every, 1) == 0:
                analysis_frame = (
                    roi_frame
                    if roi_frame is not None
                    else analysis_region(
                        frame, args.analysis_shift_x, args.analysis_extend_left
                    )
                )
                analysis_height, analysis_width = analysis_frame.shape[:2]
                if 0 < args.analysis_width < analysis_width:
                    resized_height = max(
                        1, round(analysis_height * args.analysis_width / analysis_width)
                    )
                    analysis_frame = cv2.resize(
                        analysis_frame,
                        (args.analysis_width, resized_height),
                        interpolation=cv2.INTER_AREA,
                    )
                analyzer.update(analysis_frame)

            progress_bucket = int(elapsed // progress_interval)
            if progress_bucket != last_progress_bucket:
                elapsed_display = format_recording_time(elapsed)
                print(
                    f"  capture progress: {elapsed_display}/{duration_display}, "
                    f"{frame_count} frames"
                )
                lcd.write(recording_title, f"{elapsed_display}/{duration_display}"[-16:])
                if dashboard is not None:
                    detail_prefix = f"{session_progress} · " if session_progress else ""
                    dashboard.set_status(
                        "recording",
                        f"{detail_prefix}{elapsed_display} / {duration_display}",
                    )
                last_progress_bucket = progress_bucket

            if args.show_preview:
                cv2.imshow("Fruit fly activity capture", frame)
                if cv2.waitKey(1) & 0xFF == ord("q"):
                    break
    finally:
        capture_ended = time.monotonic()
        writer.release()
        cap.release()
        if args.show_preview:
            cv2.destroyAllWindows()

    # Encoder finalization may take a moment and is not part of camera capture.
    actual_duration = max(capture_ended - start, 0.001)
    if frame_count < 2:
        if dashboard is not None and dashboard.cancel_requested():
            raise ScanCancelled("Scan cancelled before enough frames were captured")
        raise RuntimeError("Not enough frames were captured to analyze activity.")

    metadata: dict[str, Any] = {
        "scan_timestamp": scan_timestamp,
        "ir_enabled": ir_enabled,
        "video_codec": args.video_codec,
        "video_roi_only": args.video_roi_only,
        "video_width": video_output_size[0],
        "video_height": video_output_size[1],
        "video_grayscale": args.video_grayscale if args.video_codec == "h264" else False,
        "video_crf": args.video_crf if args.video_codec == "h264" else None,
        "capture_width": width,
        "capture_height": height,
        "analysis_roi_left": crop_left,
        "analysis_roi_top": crop_top,
        "analysis_roi_width": crop_width,
        "analysis_roi_height": crop_height,
        "frames_dropped": dropped_frames,
        **camera_metadata,
    }
    return actual_duration, frame_count, analyzer.result(
        video_path, first_frame_path, actual_duration, frame_count, metadata
    )


def remove_small_blobs(mask: np.ndarray, min_area: int) -> np.ndarray:
    if min_area <= 1:
        return mask

    count, labels, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
    cleaned = np.zeros_like(mask)
    for label in range(1, count):
        if stats[label, cv2.CC_STAT_AREA] >= min_area:
            cleaned[labels == label] = 255
    return cleaned


def estimate_dark_threshold(gray: np.ndarray) -> int:
    blur = cv2.GaussianBlur(gray, (5, 5), 0)
    otsu_threshold, _ = cv2.threshold(blur, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)

    # On a white background with black flies, Otsu normally lands between the two groups.
    # Clamp the value so shadows do not make the fly mask absurdly large.
    return int(np.clip(otsu_threshold, 40, 180))


def save_results(result: ActivityResult, output_dir: Path, stem: str) -> None:
    json_path = output_dir / f"{stem}_activity.json"
    csv_path = output_dir / f"{stem}_activity.csv"

    with json_path.open("w", encoding="utf-8") as file:
        json.dump(asdict(result), file, indent=2)
        file.write("\n")

    with csv_path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=list(asdict(result).keys()))
        writer.writeheader()
        writer.writerow(asdict(result))


def append_scan_history(result: ActivityResult, history_path: Path) -> None:
    """Append one durable row, safely expanding an older history schema."""
    row = asdict(result)
    needs_header = not history_path.exists() or history_path.stat().st_size == 0
    if not needs_header:
        try:
            with history_path.open("r", newline="", encoding="utf-8") as file:
                reader = csv.DictReader(file)
                old_fieldnames = reader.fieldnames or []
                old_rows = list(reader)
            new_fieldnames = list(row.keys())
            if old_fieldnames != new_fieldnames:
                temporary_path = history_path.with_name(f".{history_path.name}.tmp")
                try:
                    with temporary_path.open("w", newline="", encoding="utf-8") as file:
                        writer = csv.DictWriter(file, fieldnames=new_fieldnames)
                        writer.writeheader()
                        writer.writerows(old_rows)
                    temporary_path.replace(history_path)
                finally:
                    temporary_path.unlink(missing_ok=True)
        except (OSError, csv.Error) as exc:
            raise RuntimeError(f"Could not update scan history schema: {exc}") from exc

    with history_path.open("a", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=list(row.keys()))
        if needs_header:
            writer.writeheader()
        writer.writerow(row)
        file.flush()


def load_recent_history(
    history_path: Path, limit: int = DASHBOARD_HISTORY_LIMIT
) -> list[dict[str, str]]:
    if not history_path.exists():
        return []
    try:
        with history_path.open("r", newline="", encoding="utf-8") as file:
            return list(deque(csv.DictReader(file), maxlen=limit))
    except (OSError, csv.Error) as exc:
        print(f"Warning: could not load scan history: {exc}", file=sys.stderr)
        return []


def format_recording_time(seconds: float) -> str:
    total = max(0, int(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def human_file_size(byte_count: float) -> str:
    value = float(byte_count)
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    for unit in units:
        if value < 1024.0 or unit == units[-1]:
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024.0
    return f"{value:.1f} TiB"


def video_extension(video_codec: str) -> str:
    return ".mp4" if video_codec == "h264" else ".avi"


def estimated_video_bytes_per_second(
    output_dir: Path,
    width: int,
    height: int,
    fps: float,
    video_codec: str,
    video_roi_only: bool,
    video_width: int,
    video_height: int,
    video_grayscale: bool,
    video_crf: int,
) -> float:
    measured_rates: list[float] = []
    for metadata_path in sorted(output_dir.glob("fly_activity_*_activity.json"))[-10:]:
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            video_path = Path(str(metadata["video_path"]))
            if not video_path.is_absolute():
                video_path = output_dir / video_path.name
            if video_path.suffix.lower() != video_extension(video_codec):
                continue
            if bool(metadata.get("video_roi_only", False)) != video_roi_only:
                continue
            if video_codec == "h264" and (
                int(metadata.get("video_width", 0)) != video_width
                or int(metadata.get("video_height", 0)) != video_height
                or bool(metadata.get("video_grayscale", False)) != video_grayscale
                or int(metadata.get("video_crf", -1)) != video_crf
            ):
                continue
            duration = float(metadata["duration_seconds"])
            size = video_path.stat().st_size
            if duration > 0 and size > 0:
                recorded_width = max(1, int(metadata.get("video_width", width)))
                recorded_height = max(1, int(metadata.get("video_height", height)))
                recorded_fps = max(1.0, float(metadata.get("fps_estimate", fps)))
                if video_codec == "h264":
                    scale = fps / recorded_fps
                else:
                    if video_roi_only:
                        _, _, current_width, current_height = analysis_region_geometry(
                            height,
                            width,
                            CALIBRATED_SHIFT_X_PERCENT,
                            CALIBRATED_EXTEND_LEFT_PERCENT,
                        )
                    else:
                        current_width, current_height = width, height
                    scale = (current_width * current_height * fps) / (
                        recorded_width * recorded_height * recorded_fps
                    )
                measured_rates.append(size / duration * scale)
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            continue

    if measured_rates:
        measured_rates.sort()
        return measured_rates[len(measured_rates) // 2]

    resolution_scale = (width * height) / float(1280 * 720)
    fps_scale = fps / 30.0
    if video_codec == "h264":
        stored_resolution_scale = (video_width * video_height) / float(960 * 540)
        quality_scale = 2.0 ** ((21 - video_crf) / 6.0)
        color_scale = 1.08 if not video_grayscale else 1.0
        base_rate = (
            DEFAULT_H264_BYTES_PER_SECOND
            * stored_resolution_scale
            * quality_scale
            * color_scale
        )
        resolution_scale = 1.0
    else:
        base_rate = DEFAULT_VIDEO_BYTES_PER_SECOND
    return base_rate * resolution_scale * fps_scale


def ensure_capture_storage(args: argparse.Namespace) -> tuple[int, int]:
    bytes_per_second = estimated_video_bytes_per_second(
        args.output_dir,
        args.width,
        args.height,
        args.fps,
        args.video_codec,
        args.video_roi_only,
        args.video_width,
        args.video_height,
        args.video_grayscale,
        args.video_crf,
    )
    estimated_bytes = int(bytes_per_second * args.duration)
    free_bytes = shutil.disk_usage(args.output_dir).free
    required_bytes = (
        int(estimated_bytes * STORAGE_ESTIMATE_SAFETY_FACTOR) + STORAGE_RESERVE_BYTES
    )
    if required_bytes > free_bytes:
        raise RuntimeError(
            f"{runtime_label(int(args.duration))} needs about "
            f"{human_file_size(estimated_bytes)}; only {human_file_size(free_bytes)} free"
        )
    return estimated_bytes, free_bytes


def system_health(output_dir: Path) -> dict[str, float | None]:
    temperature: float | None = None
    try:
        temperature = round(
            float(Path("/sys/class/thermal/thermal_zone0/temp").read_text().strip()) / 1000.0,
            1,
        )
    except (OSError, ValueError):
        pass

    try:
        disk = shutil.disk_usage(output_dir)
        free_gb = round(disk.free / (1024**3), 2)
        used_percent = round(100.0 * disk.used / max(disk.total, 1), 1)
    except OSError:
        free_gb = None
        used_percent = None

    return {
        "temperature_c": temperature,
        "disk_free_gb": free_gb,
        "disk_used_percent": used_percent,
    }


def network_helper_request(socket_path: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Send one bounded JSON request to the privileged local network helper."""
    encoded = (json.dumps(payload) + "\n").encode("utf-8")
    if len(encoded) > 65536:
        raise ValueError("Network request is too large")
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(35.0)
            client.connect(socket_path)
            client.sendall(encoded)
            response = client.makefile("rb").readline(65537)
    except OSError as exc:
        raise RuntimeError("Network setup helper is unavailable") from exc
    if not response or len(response) > 65536:
        raise RuntimeError("Network setup helper returned an invalid response")
    decoded = json.loads(response.decode("utf-8"))
    if not isinstance(decoded, dict):
        raise RuntimeError("Network setup helper returned an invalid response")
    return decoded


def wifi_qr_escape(value: str) -> str:
    """Escape special characters used by the common Wi-Fi QR payload format."""
    escaped = value.replace("\\", "\\\\")
    for character in (";", ",", ":", '"'):
        escaped = escaped.replace(character, f"\\{character}")
    return escaped


def generate_qr_codes(qr_dir: Path, network_socket: str, web_port: int) -> list[Path]:
    """Create or refresh the three device-specific printable QR PNGs."""
    try:
        import segno
    except ImportError as exc:
        raise RuntimeError(
            "QR generator is missing; run .venv/bin/python -m pip install -r requirements-pi5.txt"
        ) from exc

    credentials = network_helper_request(network_socket, {"command": "credentials"})
    if not credentials.get("ok"):
        raise RuntimeError(str(credentials.get("error", "Could not read hotspot credentials")))
    hotspot_ssid = credentials.get("hotspot_ssid")
    hotspot_password = credentials.get("hotspot_password")
    if not isinstance(hotspot_ssid, str) or not isinstance(hotspot_password, str):
        raise RuntimeError("Network helper returned invalid hotspot credentials")

    hostname = socket.gethostname().split(".", 1)[0]
    payloads = {
        "1-connect-flyscanner-hotspot.png": (
            f"WIFI:T:WPA;S:{wifi_qr_escape(hotspot_ssid)};"
            f"P:{wifi_qr_escape(hotspot_password)};;"
        ),
        "2-open-hotspot-dashboard.png": f"http://10.42.0.1:{web_port}",
        "3-open-network-dashboard.png": f"http://{hostname}.local:{web_port}",
    }
    digests = {
        name: hashlib.sha256(payload.encode("utf-8")).hexdigest()
        for name, payload in payloads.items()
    }
    qr_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        qr_dir.chmod(0o700)
    except OSError:
        pass
    manifest_path = qr_dir / ".manifest.json"
    try:
        existing_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        existing_manifest = {}

    generated: list[Path] = []
    previous_digests = existing_manifest.get("digests", {})
    for name, payload in payloads.items():
        destination = qr_dir / name
        if destination.is_file() and previous_digests.get(name) == digests[name]:
            continue
        temporary = qr_dir / f".{name}.tmp"
        try:
            segno.make(payload, error="m").save(
                temporary,
                kind="png",
                scale=12,
                border=4,
                dark="#000000",
                light="#ffffff",
            )
            temporary.chmod(0o600)
            temporary.replace(destination)
            generated.append(destination)
        finally:
            temporary.unlink(missing_ok=True)

    temporary_manifest = qr_dir / ".manifest.json.tmp"
    try:
        temporary_manifest.write_text(
            json.dumps({"version": 1, "digests": digests}, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary_manifest.chmod(0o600)
        temporary_manifest.replace(manifest_path)
    finally:
        temporary_manifest.unlink(missing_ok=True)
    return generated


class DashboardState:
    def __init__(self, output_dir: Path, history_path: Path, qr_dir: Path) -> None:
        self.output_dir = output_dir.resolve()
        self.history_path = history_path.resolve()
        self.qr_dir = qr_dir.resolve()
        self._lock = threading.Lock()
        self._status = "starting"
        self._detail = "Initializing scanner"
        self._ir_enabled = False
        self._duration_seconds = DEFAULT_RUNTIME_SECONDS
        self._ir_available = True
        self._settings: dict[str, Any] = {}
        self._commands: queue.Queue[dict[str, Any]] = queue.Queue()
        self._scan_active = False
        self._scan_queued = False
        self._cancel_event = threading.Event()
        self._history: deque[dict[str, Any]] = deque(
            load_recent_history(history_path), maxlen=DASHBOARD_HISTORY_LIMIT
        )
        self._latest_result: dict[str, Any] | None = (
            dict(self._history[-1]) if self._history else None
        )

    def set_status(self, status: str, detail: str = "") -> None:
        with self._lock:
            self._status = status
            self._detail = detail

    def set_ir_enabled(self, enabled: bool) -> None:
        with self._lock:
            self._ir_enabled = enabled

    def set_ir_available(self, available: bool) -> None:
        with self._lock:
            self._ir_available = available

    def ir_available(self) -> bool:
        with self._lock:
            return self._ir_available

    def set_duration(self, duration_seconds: int) -> None:
        with self._lock:
            self._duration_seconds = duration_seconds

    def set_settings(self, settings: dict[str, Any]) -> None:
        with self._lock:
            self._settings = dict(settings)
            self._duration_seconds = int(settings["duration_seconds"])
            self._ir_enabled = bool(settings["ir_enabled"])

    def settings_snapshot(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._settings)

    def request_settings(self, settings: dict[str, Any]) -> None:
        self._commands.put({"type": "settings", "settings": dict(settings)})

    def request_scan(self) -> tuple[bool, str]:
        with self._lock:
            if self._scan_active:
                return False, "A scan is already running"
            if self._scan_queued:
                return False, "A remote scan is already queued"
            if self._status not in {"ready"}:
                return False, f"Scanner is currently {self._status.replace('_', ' ')}"
            self._scan_queued = True
        self._commands.put({"type": "scan"})
        return True, "Scan queued"

    def take_idle_command(self) -> dict[str, Any] | None:
        try:
            return self._commands.get_nowait()
        except queue.Empty:
            return None

    def begin_scan(self) -> None:
        with self._lock:
            self._scan_active = True
            self._scan_queued = False
        self._cancel_event.clear()

    def end_scan(self) -> None:
        with self._lock:
            self._scan_active = False
            self._scan_queued = False
        self._cancel_event.clear()

    def request_cancel(self) -> tuple[bool, str]:
        with self._lock:
            if not self._scan_active:
                return False, "No scan is currently running"
            self._status = "cancelling"
            self._detail = "Finishing the current partial recording"
        self._cancel_event.set()
        return True, "Cancellation requested"

    def cancel_requested(self) -> bool:
        return self._cancel_event.is_set()

    def publish_result(self, result: ActivityResult) -> None:
        row = asdict(result)
        with self._lock:
            self._latest_result = row
            self._history.append(row)
            self._status = "ready"
            self._detail = f"Latest activity score: {result.activity_score:.2f}"

    def latest_image_path(self) -> Path | None:
        with self._lock:
            value = None if self._latest_result is None else self._latest_result.get("first_frame_path")
        path = Path(value).resolve() if value else None
        if path is None or not path.is_file() or self.output_dir not in path.parents:
            return None
        return path

    def recent_video_paths(self, limit: int | None = None) -> list[Path]:
        with self._lock:
            history = [dict(row) for row in self._history]
        paths: list[Path] = []
        seen: set[Path] = set()
        for row in reversed(history):
            value = row.get("video_path")
            path = Path(str(value)).resolve() if value else None
            if (
                path is None
                or path in seen
                or not path.is_file()
                or self.output_dir not in path.parents
            ):
                continue
            paths.append(path)
            seen.add(path)
            if limit is not None and len(paths) >= limit:
                break
        return paths

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            payload: dict[str, Any] = {
                "status": self._status,
                "detail": self._detail,
                "ir_enabled": self._ir_enabled,
                "ir_available": self._ir_available,
                "duration_seconds": self._duration_seconds,
                "settings": dict(self._settings),
                "scan_active": self._scan_active,
                "scan_queued": self._scan_queued,
                "cancel_requested": self._cancel_event.is_set(),
                "latest_result": dict(self._latest_result) if self._latest_result else None,
                "history": [dict(row) for row in list(self._history)[-20:]],
            }
        payload["latest_image_available"] = self.latest_image_path() is not None
        payload["available_videos"] = len(self.recent_video_paths())
        payload["health"] = system_health(self.output_dir)
        payload["server_time"] = datetime.now().isoformat(timespec="seconds")
        return payload


class DashboardServer:
    def __init__(
        self,
        state: DashboardState,
        host: str,
        port: int,
        dashboard_path: Path,
        network_socket: str,
    ) -> None:
        self.state = state
        self.host = host
        self.port = port
        self.dashboard_path = dashboard_path
        self.network_socket = network_socket
        self._stop_event = threading.Event()
        self._startup_event = threading.Event()
        self._startup_error: Exception | None = None
        self._thread = threading.Thread(target=self._run, name="flyscanner-web", daemon=True)

    def start(self) -> None:
        self._thread.start()
        self._startup_event.wait(timeout=5.0)
        if self._startup_error is not None:
            raise RuntimeError(f"Could not start dashboard: {self._startup_error}")
        if not self._startup_event.is_set():
            raise RuntimeError("Dashboard startup timed out")
        print(f"Dashboard listening on http://{self.host}:{self.port}")

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread.is_alive():
            self._thread.join(timeout=5.0)

    def _run(self) -> None:
        try:
            asyncio.run(self._serve())
        except Exception as exc:
            self._startup_error = exc
            self.state.set_status("web_error", str(exc))
            self._startup_event.set()

    async def _serve(self) -> None:
        try:
            from aiohttp import WSMsgType, web
        except ImportError as exc:
            raise RuntimeError(
                "aiohttp is missing; run .venv/bin/python -m pip install -r requirements-pi5.txt"
            ) from exc

        async def index(_request: Any) -> Any:
            return web.FileResponse(self.dashboard_path)

        async def websocket(request: Any) -> Any:
            ws = web.WebSocketResponse(heartbeat=30)
            await ws.prepare(request)
            try:
                await ws.send_json(self.state.snapshot())
                while not ws.closed and not self._stop_event.is_set():
                    try:
                        message = await ws.receive(timeout=1.0)
                    except asyncio.TimeoutError:
                        await ws.send_json(self.state.snapshot())
                        continue
                    if message.type in {
                        WSMsgType.CLOSE,
                        WSMsgType.CLOSED,
                        WSMsgType.CLOSING,
                        WSMsgType.ERROR,
                    }:
                        break
            except (ConnectionResetError, asyncio.CancelledError):
                pass
            finally:
                await ws.close()
            return ws

        async def latest_image(_request: Any) -> Any:
            path = self.state.latest_image_path()
            if path is None:
                raise web.HTTPNotFound(text="No completed scan image is available yet")
            return web.FileResponse(path)

        async def capture_file(request: Any) -> Any:
            name = request.match_info["name"]
            if name != Path(name).name:
                raise web.HTTPBadRequest(text="Invalid capture filename")
            path = (self.state.output_dir / name).resolve()
            if self.state.output_dir not in path.parents or not path.is_file():
                raise web.HTTPNotFound()
            return web.FileResponse(path)

        async def download_videos(request: Any) -> Any:
            try:
                count = int(request.query.get("count", "1"))
            except ValueError as exc:
                raise web.HTTPBadRequest(text="count must be a whole number") from exc
            if not 1 <= count <= DASHBOARD_HISTORY_LIMIT:
                raise web.HTTPBadRequest(
                    text=f"count must be between 1 and {DASHBOARD_HISTORY_LIMIT}"
                )

            paths = self.state.recent_video_paths(count)
            if not paths:
                raise web.HTTPNotFound(text="No completed videos are available")
            if len(paths) == 1:
                return web.FileResponse(
                    paths[0],
                    headers={
                        "Content-Disposition": f'attachment; filename="{paths[0].name}"'
                    },
                )

            archive_name = f"flyscanner-last-{len(paths)}-videos.tar"
            process = await asyncio.create_subprocess_exec(
                "tar",
                "-cf",
                "-",
                "-C",
                str(self.state.output_dir),
                *(path.name for path in paths),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            response = web.StreamResponse(
                headers={
                    "Content-Type": "application/x-tar",
                    "Content-Disposition": f'attachment; filename="{archive_name}"',
                }
            )
            await response.prepare(request)
            try:
                assert process.stdout is not None
                while chunk := await process.stdout.read(1024 * 1024):
                    await response.write(chunk)
                return_code = await process.wait()
                if return_code:
                    assert process.stderr is not None
                    error = (await process.stderr.read()).decode(errors="replace").strip()
                    print(f"Video archive failed: {error}", file=sys.stderr)
                await response.write_eof()
            except (ConnectionResetError, asyncio.CancelledError):
                if process.returncode is None:
                    process.terminate()
                    await process.wait()
            return response

        async def history_csv(_request: Any) -> Any:
            if not self.state.history_path.is_file():
                raise web.HTTPNotFound(text="No scan history is available yet")
            return web.FileResponse(self.state.history_path)

        def require_same_origin(request: Any) -> None:
            origin = request.headers.get("Origin")
            if origin and urlsplit(origin).netloc != request.host:
                raise web.HTTPForbidden(text="Cross-origin scanner control is not allowed")

        async def scanner_settings_get(_request: Any) -> Any:
            return web.json_response(
                {
                    "ok": True,
                    "settings": self.state.settings_snapshot(),
                    "runtime_presets": [
                        {"seconds": value, "label": runtime_label(value)}
                        for value in RUNTIME_PRESETS_SECONDS
                    ],
                }
            )

        async def scanner_settings_update(request: Any) -> Any:
            require_same_origin(request)
            try:
                body = await request.json()
                if not isinstance(body, dict):
                    raise ValueError("Request must be an object")
                payload = body.get("settings")
                validated = validate_scanner_settings(
                    payload, self.state.settings_snapshot()
                )
                if validated["ir_enabled"] and not self.state.ir_available():
                    raise ValueError("IR control is disabled on this scanner")
                self.state.request_settings(validated)
            except (ValueError, TypeError, json.JSONDecodeError) as exc:
                return web.json_response({"ok": False, "error": str(exc)}, status=400)
            return web.json_response(
                {
                    "ok": True,
                    "message": "Settings queued and will apply when the scanner is idle",
                    "settings": validated,
                },
                status=202,
            )

        async def scanner_start(request: Any) -> Any:
            require_same_origin(request)
            try:
                body = await request.json()
                if not isinstance(body, dict) or body.get("confirm") is not True:
                    raise ValueError("Explicit confirmation is required")
            except (ValueError, TypeError, json.JSONDecodeError) as exc:
                return web.json_response({"ok": False, "error": str(exc)}, status=400)
            accepted, message = self.state.request_scan()
            return web.json_response(
                {"ok": accepted, "message": message, "error": None if accepted else message},
                status=202 if accepted else 409,
            )

        async def scanner_cancel(request: Any) -> Any:
            require_same_origin(request)
            try:
                body = await request.json()
                if not isinstance(body, dict) or body.get("confirm") is not True:
                    raise ValueError("Explicit confirmation is required")
            except (ValueError, TypeError, json.JSONDecodeError) as exc:
                return web.json_response({"ok": False, "error": str(exc)}, status=400)
            accepted, message = self.state.request_cancel()
            return web.json_response(
                {"ok": accepted, "message": message, "error": None if accepted else message},
                status=202 if accepted else 409,
            )

        async def qr_code(request: Any) -> Any:
            name = request.match_info["name"]
            if name not in QR_CODE_FILES:
                raise web.HTTPNotFound()
            path = self.state.qr_dir / name
            if not path.is_file():
                raise web.HTTPNotFound(text="QR code has not been generated yet")
            return web.FileResponse(path)

        async def network_status(_request: Any) -> Any:
            try:
                response = await asyncio.to_thread(
                    network_helper_request, self.network_socket, {"command": "status"}
                )
            except Exception as exc:
                return web.json_response({"ok": False, "error": str(exc)}, status=503)
            return web.json_response(response, status=200 if response.get("ok") else 400)

        async def network_scan(_request: Any) -> Any:
            try:
                response = await asyncio.to_thread(
                    network_helper_request, self.network_socket, {"command": "scan"}
                )
            except Exception as exc:
                return web.json_response({"ok": False, "error": str(exc)}, status=503)
            return web.json_response(response, status=200 if response.get("ok") else 403)

        async def network_connect(request: Any) -> Any:
            try:
                body = await request.json()
                if not isinstance(body, dict):
                    raise ValueError("Request must be an object")
                ssid = body.get("ssid", "")
                password = body.get("password", "")
                if not isinstance(ssid, str) or not isinstance(password, str):
                    raise ValueError("Wi-Fi name and password must be text")
                response = await asyncio.to_thread(
                    network_helper_request,
                    self.network_socket,
                    {"command": "connect", "ssid": ssid, "password": password},
                )
            except (ValueError, json.JSONDecodeError) as exc:
                return web.json_response({"ok": False, "error": str(exc)}, status=400)
            except Exception as exc:
                return web.json_response({"ok": False, "error": str(exc)}, status=503)
            return web.json_response(response, status=202 if response.get("ok") else 403)

        async def network_forget(request: Any) -> Any:
            try:
                body = await request.json()
                if not isinstance(body, dict) or body.get("confirm") is not True:
                    raise ValueError("Explicit confirmation is required")
                response = await asyncio.to_thread(
                    network_helper_request,
                    self.network_socket,
                    {"command": "forget"},
                )
            except (ValueError, json.JSONDecodeError) as exc:
                return web.json_response({"ok": False, "error": str(exc)}, status=400)
            except Exception as exc:
                return web.json_response({"ok": False, "error": str(exc)}, status=503)
            return web.json_response(response, status=202 if response.get("ok") else 403)

        app = web.Application()
        app.add_routes(
            [
                web.get("/", index),
                web.get("/ws", websocket),
                web.get("/latest-image", latest_image),
                web.get("/captures/{name}", capture_file),
                web.get("/download-videos", download_videos),
                web.get("/scan_history.csv", history_csv),
                web.get("/api/scanner/settings", scanner_settings_get),
                web.put("/api/scanner/settings", scanner_settings_update),
                web.post("/api/scanner/start", scanner_start),
                web.post("/api/scanner/cancel", scanner_cancel),
                web.get("/qr/{name}", qr_code),
                web.get("/api/network/status", network_status),
                web.get("/api/network/scan", network_scan),
                web.post("/api/network/connect", network_connect),
                web.post("/api/network/forget", network_forget),
            ]
        )
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        site = web.TCPSite(runner, self.host, self.port)
        await site.start()
        self._startup_event.set()
        try:
            while not self._stop_event.is_set():
                await asyncio.sleep(0.25)
        finally:
            await runner.cleanup()


def capture_stem_for(path: Path) -> str | None:
    match = re.match(
        r"^(fly_activity_\d{8}_\d{6})(?:(?P<session>_session)_part\d{3})?"
        r"(?:_activity|_first_frame)?\.(?:avi|mp4|json|csv|jpg)$",
        path.name,
    )
    if not match:
        return None
    return match.group(1) + (match.group("session") or "")


def cleanup_old_captures(output_dir: Path, keep_captures: int, current_stem: str) -> int:
    keep_captures = max(keep_captures, 0)
    runs: dict[str, list[Path]] = {}

    for path in output_dir.iterdir():
        if not path.is_file():
            continue
        stem = capture_stem_for(path)
        if stem:
            runs.setdefault(stem, []).append(path)

    stems_newest_first = sorted(runs, reverse=True)
    keep_count = max(keep_captures, 1)
    stems_to_keep = set(stems_newest_first[:keep_count])
    stems_to_keep.add(current_stem)

    removed_count = 0
    for stem, paths in runs.items():
        if stem in stems_to_keep:
            continue
        for path in paths:
            try:
                path.unlink()
                removed_count += 1
            except OSError as exc:
                print(f"Warning: could not remove old capture file {path}: {exc}", file=sys.stderr)

    return removed_count


def run_scan_once(
    args: argparse.Namespace,
    lcd: CharacterLcd | NullLcd,
    illuminator: IrIlluminator | NullIlluminator,
    ir_enabled: bool,
    dashboard: DashboardState | None = None,
    filename_stem: str | None = None,
    session_progress: str = "",
    check_storage: bool = True,
) -> ActivityResult:
    ensure_cv_deps()
    if check_storage:
        estimated_bytes, free_bytes = ensure_capture_storage(args)
        print(
            f"Estimated video size: {human_file_size(estimated_bytes)} "
            f"({human_file_size(free_bytes)} free before capture)"
        )

    scan_started = datetime.now()
    timestamp = scan_started.strftime("%Y%m%d_%H%M%S")
    scan_timestamp = scan_started.isoformat(timespec="seconds")
    stem = filename_stem or f"fly_activity_{timestamp}"
    video_path = args.output_dir / f"{stem}{video_extension(args.video_codec)}"
    first_frame_path = args.output_dir / f"{stem}_first_frame.jpg"

    print(
        f"Recording {args.duration:.1f}s video from webcam {args.camera} "
        f"at {args.width}x{args.height}, {args.fps:g} FPS..."
    )
    if dashboard is not None:
        detail = f"IR {'on' if ir_enabled else 'off'}"
        if session_progress:
            detail = f"{session_progress} · {detail}"
        dashboard.set_status("starting_scan", detail)
    if ir_enabled:
        print(f"Turning IR illuminator on (GPIO{args.ir_gpio})...")
        illuminator.on()
    else:
        print("IR illuminator disabled for this capture")
    try:
        # IR is enabled before camera warm-up so auto-exposure settles under the
        # same lighting used for the recorded frames.
        duration_seconds, frame_count, result = capture_video(
            args,
            video_path,
            first_frame_path,
            lcd,
            scan_timestamp,
            ir_enabled,
            dashboard,
            session_progress,
        )
    finally:
        illuminator.off()
        print("IR illuminator off")
    # Make the result the first post-recording LCD screen. Result files are
    # saved afterward without replacing this display with a saving message.
    lcd.write("Activity score", f"{result.activity_score:.2f}")
    print(f"Saved {frame_count} frames to {video_path}")
    print("Saving activity results...")
    save_results(result, args.output_dir, stem)
    append_scan_history(result, args.output_dir / "scan_history.csv")
    current_group = capture_stem_for(video_path)
    if current_group is None:
        raise RuntimeError(f"Could not determine capture group for {video_path.name}")
    removed_count = cleanup_old_captures(
        args.output_dir, args.keep_captures, current_group
    )
    if removed_count:
        print(f"Cleaned up {removed_count} old capture files.")

    lcd.write("Activity score", f"{result.activity_score:.2f}")
    if dashboard is not None:
        dashboard.publish_result(result)
    print(json.dumps(asdict(result), indent=2))
    return result


def run_selected_scan(
    args: argparse.Namespace,
    lcd: CharacterLcd | NullLcd,
    illuminator: IrIlluminator | NullIlluminator,
    ir_enabled: bool,
    dashboard: DashboardState | None = None,
) -> ActivityResult:
    if int(args.duration) != 24 * 60 * 60:
        return run_scan_once(args, lcd, illuminator, ir_enabled, dashboard)

    estimated_bytes, free_bytes = ensure_capture_storage(args)
    print(
        f"Starting 24-hour segmented session; estimated total video size "
        f"{human_file_size(estimated_bytes)} ({human_file_size(free_bytes)} free)"
    )
    session_timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    session_stem = f"fly_activity_{session_timestamp}_session"
    total_parts = (24 * 60 * 60) // LONG_SESSION_SEGMENT_SECONDS
    original_duration = args.duration
    latest_result: ActivityResult | None = None
    try:
        args.duration = float(LONG_SESSION_SEGMENT_SECONDS)
        for part_number in range(1, total_parts + 1):
            progress = f"Part {part_number:03d}/{total_parts:03d}"
            part_stem = f"{session_stem}_part{part_number:03d}"
            print(f"24-hour session {progress}: {part_stem}")
            latest_result = run_scan_once(
                args,
                lcd,
                illuminator,
                ir_enabled,
                dashboard,
                filename_stem=part_stem,
                session_progress=progress,
                check_storage=False,
            )
            if dashboard is not None and dashboard.cancel_requested():
                print(f"24-hour session cancelled after {progress}")
                break
            if part_number < total_parts:
                lcd.write(f"Part {part_number:03d} score", f"{latest_result.activity_score:.2f}")
                if dashboard is not None:
                    dashboard.set_status(
                        "session_segment_complete",
                        f"{progress} score {latest_result.activity_score:.2f}",
                    )
                time.sleep(1.0)
    finally:
        args.duration = original_duration

    if latest_result is None:
        raise RuntimeError("The 24-hour session did not record any segments")
    return latest_result


def main() -> int:
    args = parse_args()
    if (
        args.duration <= 0
        or args.fps <= 0
        or args.width <= 0
        or args.height <= 0
        or args.video_width <= 0
        or args.video_height <= 0
        or args.camera_buffers <= 0
        or args.video_encoder_threads <= 0
    ):
        raise ValueError(
            "duration, fps, capture/video dimensions, camera-buffers and encoder threads "
            "must all be greater than zero"
        )
    if args.video_codec == "h264" and (
        args.video_width % 2 or args.video_height % 2
    ):
        raise ValueError("H.264 video width and height must be even numbers")
    if not 0 <= args.video_crf <= 51:
        raise ValueError("video-crf must be between 0 and 51")
    if args.analysis_width < 0 or args.analyze_every <= 0 or args.opencv_threads <= 0:
        raise ValueError("analysis-width cannot be negative; analyze-every and threads must be positive")
    if not -100.0 <= args.analysis_shift_x <= 100.0:
        raise ValueError("analysis-shift-x must be between -100 and 100 percent")
    if not 0.0 <= args.analysis_extend_left <= 100.0:
        raise ValueError("analysis-extend-left must be between 0 and 100 percent")
    analysis_region_geometry(
        args.height,
        args.width,
        args.analysis_shift_x,
        args.analysis_extend_left,
    )
    if (
        args.button_debounce < 0
        or args.long_press <= 0
        or args.result_hold < 0
        or args.double_press_window <= 0
        or args.network_display_time < 0
    ):
        raise ValueError(
            "button/result timing values must be non-negative, with long-press "
            "and double-press-window greater than zero"
        )
    if not 1 <= args.web_port <= 65535:
        raise ValueError("web-port must be between 1 and 65535")
    lcd_gpios = {args.lcd_rs, args.lcd_en, args.lcd_d4, args.lcd_d5, args.lcd_d6, args.lcd_d7}
    if not args.no_ir and (args.ir_gpio == args.button_gpio or (not args.no_lcd and args.ir_gpio in lcd_gpios)):
        raise ValueError(f"IR GPIO{args.ir_gpio} conflicts with another configured input/output")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    add_local_venv_site_packages()
    ensure_cv_deps()
    cv2.setNumThreads(max(args.opencv_threads, 1))
    lcd = setup_lcd(args)
    illuminator = setup_illuminator(args)
    saved_ir_enabled, saved_duration = load_scanner_settings(
        args.state_file,
        default_ir=not args.no_ir,
        default_duration=DEFAULT_RUNTIME_SECONDS,
    )
    ir_enabled = False if args.no_ir else saved_ir_enabled
    if not args.run_now:
        base_settings = scanner_settings_from_args(args, ir_enabled, saved_duration)
        scanner_settings = load_saved_web_settings(args.state_file, base_settings)
        scanner_settings["duration_seconds"] = saved_duration
        scanner_settings["ir_enabled"] = ir_enabled
        apply_scanner_settings(args, scanner_settings)
    else:
        scanner_settings = scanner_settings_from_args(
            args, ir_enabled, int(args.duration)
        )
    history_path = args.output_dir / "scan_history.csv"
    qr_dir = Path(__file__).resolve().parent / "qr-codes"
    dashboard_state = DashboardState(args.output_dir, history_path, qr_dir)
    dashboard_state.set_ir_available(not args.no_ir)
    dashboard_state.set_settings(scanner_settings)
    dashboard_server: DashboardServer | None = None

    try:
        if not args.no_web and not args.run_now and not args.test_button:
            qr_error: Exception | None = None
            for attempt in range(5):
                try:
                    generated_qr_codes = generate_qr_codes(
                        qr_dir, args.network_socket, args.web_port
                    )
                    if generated_qr_codes:
                        print(
                            f"Generated {len(generated_qr_codes)} printable QR codes in {qr_dir}"
                        )
                    qr_error = None
                    break
                except Exception as exc:
                    qr_error = exc
                    if attempt < 4:
                        time.sleep(1.0)
            if qr_error is not None:
                print(f"Warning: QR codes were not generated: {qr_error}", file=sys.stderr)
            dashboard_server = DashboardServer(
                dashboard_state,
                args.web_host,
                args.web_port,
                Path(__file__).resolve().parent / "dashboard.html",
                args.network_socket,
            )
            dashboard_server.start()

        if args.run_now:
            run_selected_scan(args, lcd, illuminator, ir_enabled, dashboard_state)
            return 0

        button = setup_button(args)
        try:
            if args.test_button:
                test_button(button, lcd, args.button_gpio)
                return 0

            last_result: ActivityResult | None = None
            hold_new_result = False
            while True:
                dashboard_state.set_status("ready", "Waiting for button")
                action = wait_for_idle_action(
                    button,
                    lcd,
                    args.button_gpio,
                    args.button_debounce,
                    args.long_press,
                    ir_enabled,
                    int(args.duration),
                    last_result,
                    hold_new_result,
                    args.result_hold,
                    args.double_press_window,
                    dashboard_state,
                )
                hold_new_result = False
                if isinstance(action, dict):
                    command_type = action.get("type")
                    if command_type == "settings":
                        try:
                            pending_settings = validate_scanner_settings(
                                action.get("settings"),
                                scanner_settings_from_args(
                                    args, ir_enabled, int(args.duration)
                                ),
                            )
                            if args.no_ir:
                                pending_settings["ir_enabled"] = False
                            apply_scanner_settings(args, pending_settings)
                            ir_enabled = bool(pending_settings["ir_enabled"])
                            save_full_scanner_settings(args.state_file, pending_settings)
                            dashboard_state.set_settings(pending_settings)
                            lcd.write("Web settings", "Saved")
                            print("Applied and saved advanced web settings")
                            time.sleep(0.6)
                        except Exception as exc:
                            dashboard_state.set_status("settings_error", str(exc))
                            print(f"Could not apply web settings: {exc}", file=sys.stderr)
                        continue
                    if command_type == "scan":
                        action = "scan"
                    else:
                        print(f"Ignoring unknown dashboard command: {command_type}", file=sys.stderr)
                        continue
                if action == "show_network":
                    dashboard_state.set_status("showing_network", "Dashboard address on LCD")
                    show_network_info(
                        lcd,
                        args.web_port,
                        args.network_display_time,
                        args.network_socket,
                    )
                    continue
                if action == "capture_settings":
                    dashboard_state.set_status("capture_settings", "Selecting runtime and IR")
                    selected_duration, ir_enabled = configure_capture_settings(
                        button,
                        lcd,
                        int(args.duration),
                        ir_enabled,
                        args.state_file,
                        args.button_debounce,
                        args.long_press,
                        ir_available=not args.no_ir,
                    )
                    args.duration = float(selected_duration)
                    dashboard_state.set_settings(
                        scanner_settings_from_args(
                            args, ir_enabled, selected_duration
                        )
                    )
                    continue

                dashboard_state.begin_scan()
                try:
                    last_result = run_selected_scan(
                        args, lcd, illuminator, ir_enabled, dashboard_state
                    )
                    hold_new_result = True
                    if dashboard_state.cancel_requested():
                        dashboard_state.set_status(
                            "scan_cancelled", "Partial recording saved"
                        )
                        lcd.write("Scan cancelled", "Partial saved")
                        time.sleep(1.0)
                except ScanCancelled as exc:
                    dashboard_state.set_status("scan_cancelled", str(exc))
                    lcd.write("Scan cancelled", "No result saved")
                    print(str(exc))
                    time.sleep(1.0)
                except Exception as exc:
                    dashboard_state.set_status("scan_error", str(exc))
                    lcd.write("Scan error", str(exc)[:16])
                    print(f"Scan failed: {exc}", file=sys.stderr)
                    # Stay alive under systemd; a reconnect or corrected setup can be
                    # tested with the next button press.
                    time.sleep(1.0)
                finally:
                    dashboard_state.end_scan()
        finally:
            button.close()
    finally:
        if dashboard_server is not None:
            dashboard_server.stop()
        # Also forces the output low during Ctrl+C and systemd shutdown.
        illuminator.close()

    return 0


if __name__ == "__main__":
    def stop_service(_signum: int, _frame: object) -> None:
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop_service)
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nStopped by user.", file=sys.stderr)
        raise SystemExit(130)
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1)
