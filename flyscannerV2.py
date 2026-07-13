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


@dataclass
class ActivityResult:
    scan_timestamp: str
    video_path: str
    first_frame_path: str
    ir_enabled: bool
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
    parser.add_argument("--duration", type=float, default=15.0, help="Recording duration in seconds.")
    parser.add_argument("--fps", type=float, default=30.0, help="Requested capture FPS.")
    parser.add_argument("--width", type=int, default=1280, help="Requested frame width.")
    parser.add_argument("--height", type=int, default=720, help="Requested frame height.")
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
        help="Seconds the button must be held to open/confirm IR settings.",
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


def load_ir_capture_setting(state_file: Path, default: bool = True) -> bool:
    if not state_file.exists():
        return default

    try:
        with state_file.open("r", encoding="utf-8") as file:
            value = json.load(file).get("ir_enabled")
        if not isinstance(value, bool):
            raise ValueError("ir_enabled must be true or false")
        return value
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        print(f"Warning: could not read {state_file}: {exc}; using IR on", file=sys.stderr)
        return default


def save_ir_capture_setting(state_file: Path, enabled: bool) -> None:
    state_file.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = state_file.with_name(f".{state_file.name}.tmp")
    try:
        with temporary_path.open("w", encoding="utf-8") as file:
            json.dump({"ir_enabled": enabled}, file, indent=2)
            file.write("\n")
        temporary_path.replace(state_file)
    except OSError:
        try:
            temporary_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def wait_for_idle_action(
    button: ButtonInput,
    lcd: CharacterLcd | NullLcd,
    gpio_number: int,
    debounce_seconds: float,
    long_press_seconds: float,
    ir_enabled: bool,
    last_result: ActivityResult | None = None,
    hold_new_result: bool = False,
    result_hold_seconds: float = 6.0,
    double_press_window: float = 0.6,
) -> str:
    print(
        f"Waiting for button on GPIO{gpio_number} "
        f"(IR during capture: {'on' if ir_enabled else 'off'})..."
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
                lcd.write("Fly scanner", f"IR:{state} Tap/Hold")
            last_lcd_mode = mode

        time.sleep(0.05)

    pressed_at = time.monotonic()
    showed_long_press = False
    while button.is_pressed:
        held_for = time.monotonic() - pressed_at
        if held_for >= long_press_seconds and not showed_long_press:
            lcd.write("IR settings", "Release button")
            showed_long_press = True
        time.sleep(0.02)

    held_for = time.monotonic() - pressed_at
    released_at = time.monotonic()
    if held_for >= long_press_seconds:
        time.sleep(debounce_seconds)
        return "ir_settings"

    # A single press is intentionally delayed by this short window. Ignore the
    # immediate release bounce, then look for one more distinct press.
    time.sleep(min(debounce_seconds, 0.08))
    second_press_deadline = released_at + double_press_window
    while time.monotonic() < second_press_deadline:
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


def configure_ir_capture(
    button: ButtonInput,
    lcd: CharacterLcd | NullLcd,
    current_value: bool,
    state_file: Path,
    debounce_seconds: float,
    long_press_seconds: float,
) -> bool:
    selected = current_value
    print("IR settings: short press toggles; long press saves.")

    while True:
        state = "ON" if selected else "OFF"
        lcd.write(f"IR capture: {state}", "Tap / hold save")

        while not button.is_pressed:
            time.sleep(0.05)

        pressed_at = time.monotonic()
        showed_save_prompt = False
        while button.is_pressed:
            held_for = time.monotonic() - pressed_at
            if held_for >= long_press_seconds and not showed_save_prompt:
                lcd.write("Save IR setting", "Release to save")
                showed_save_prompt = True
            time.sleep(0.02)

        held_for = time.monotonic() - pressed_at
        time.sleep(debounce_seconds)
        if held_for >= long_press_seconds:
            save_ir_capture_setting(state_file, selected)
            lcd.write("IR setting saved", f"Capture: {state}")
            print(f"Saved IR capture setting: {state}")
            time.sleep(1.0)
            return selected

        selected = not selected
        print(f"IR capture selection: {'ON' if selected else 'OFF'}")


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


def make_video_writer(path: Path, fps: float, frame_size: tuple[int, int]) -> cv2.VideoWriter:
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
        self.motion_percentages: list[float] = []
        self.fly_area_percentages: list[float] = []
        self.moving_fly_area_percentages: list[float] = []
        self.brightness_means: list[float] = []
        self.brightness_mins: list[int] = []
        self.brightness_maxs: list[int] = []
        self.kernel = np.ones((3, 3), np.uint8)

    @property
    def frames_analyzed(self) -> int:
        return len(self.motion_percentages)

    def update(self, frame: np.ndarray) -> None:
        gray_raw = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        gray = cv2.GaussianBlur(gray_raw, (5, 5), 0)
        self.brightness_means.append(float(gray_raw.mean()))
        self.brightness_mins.append(int(gray_raw.min()))
        self.brightness_maxs.append(int(gray_raw.max()))

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
        self.motion_percentages.append(100.0 * cv2.countNonZero(motion_mask) / total_pixels)
        self.fly_area_percentages.append(100.0 * cv2.countNonZero(fly_mask) / total_pixels)
        self.moving_fly_area_percentages.append(100.0 * cv2.countNonZero(moving_fly_mask) / total_pixels)

        self.prev_gray = gray

    def result(
        self,
        video_path: Path,
        first_frame_path: Path,
        duration_seconds: float,
        frames_captured: int,
        metadata: dict[str, Any],
    ) -> ActivityResult:
        if self.frames_analyzed == 0:
            raise RuntimeError("Not enough frames were captured to analyze activity.")

        motion_mean = float(np.mean(self.motion_percentages))
        motion_peak = float(np.percentile(self.motion_percentages, 95))
        fly_area_mean = float(np.mean(self.fly_area_percentages))
        moving_fly_area_mean = float(np.mean(self.moving_fly_area_percentages))
        raw_score = (0.7 * moving_fly_area_mean + 0.3 * motion_mean) / 2.0 * 100.0
        # This is an activity index, not a percentage. Do not clamp it to 100:
        # highly active samples must remain distinguishable from one another.
        activity_score = float(raw_score)

        return ActivityResult(
            scan_timestamp=metadata["scan_timestamp"],
            video_path=str(video_path),
            first_frame_path=str(first_frame_path),
            ir_enabled=metadata["ir_enabled"],
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
            brightness_mean=round(float(np.mean(self.brightness_means)), 2),
            brightness_min=min(self.brightness_mins),
            brightness_max=max(self.brightness_maxs),
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

    writer = make_video_writer(video_path, negotiated_fps, (width, height))
    analyzer = LiveActivityAnalyzer(args.motion_threshold, args.min_change_area, args.dark_threshold)

    start = time.monotonic()
    frame_count = 0
    dropped_frames = 0
    last_status_second = -1
    lcd.write("Recording", f"0/{int(args.duration)} sec")
    if dashboard is not None:
        dashboard.set_status("recording", f"0/{args.duration:g} seconds")

    try:
        while True:
            elapsed = time.monotonic() - start
            if elapsed >= args.duration:
                break

            ok, frame = cap.read()
            if not ok or frame is None:
                dropped_frames += 1
                print("Warning: dropped camera frame", file=sys.stderr)
                continue

            writer.write(frame)
            frame_count += 1
            if frame_count % max(args.analyze_every, 1) == 0:
                analysis_frame = analysis_region(
                    frame, args.analysis_shift_x, args.analysis_extend_left
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

            status_second = int(elapsed)
            if status_second != last_status_second:
                print(f"  capture progress: {status_second}/{int(args.duration)}s, {frame_count} frames")
                lcd.write("Recording", f"{status_second}/{int(args.duration)} sec")
                if dashboard is not None:
                    dashboard.set_status(
                        "recording", f"{status_second}/{int(args.duration)} seconds"
                    )
                last_status_second = status_second

            if args.show_preview:
                cv2.imshow("Fruit fly activity capture", frame)
                if cv2.waitKey(1) & 0xFF == ord("q"):
                    break
    finally:
        writer.release()
        cap.release()
        if args.show_preview:
            cv2.destroyAllWindows()

    actual_duration = max(time.monotonic() - start, 0.001)
    if frame_count < 2:
        raise RuntimeError("Not enough frames were captured to analyze activity.")

    metadata: dict[str, Any] = {
        "scan_timestamp": scan_timestamp,
        "ir_enabled": ir_enabled,
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
    """Append one durable summary row without rewriting previous scans."""
    row = asdict(result)
    needs_header = not history_path.exists() or history_path.stat().st_size == 0
    with history_path.open("a", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=list(row.keys()))
        if needs_header:
            writer.writeheader()
        writer.writerow(row)
        file.flush()


def load_recent_history(history_path: Path, limit: int = 20) -> list[dict[str, str]]:
    if not history_path.exists():
        return []
    try:
        with history_path.open("r", newline="", encoding="utf-8") as file:
            return list(deque(csv.DictReader(file), maxlen=limit))
    except (OSError, csv.Error) as exc:
        print(f"Warning: could not load scan history: {exc}", file=sys.stderr)
        return []


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
        self._history: deque[dict[str, Any]] = deque(
            load_recent_history(history_path), maxlen=20
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

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            payload: dict[str, Any] = {
                "status": self._status,
                "detail": self._detail,
                "ir_enabled": self._ir_enabled,
                "latest_result": dict(self._latest_result) if self._latest_result else None,
                "history": [dict(row) for row in self._history],
            }
        payload["latest_image_available"] = self.latest_image_path() is not None
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

        async def history_csv(_request: Any) -> Any:
            if not self.state.history_path.is_file():
                raise web.HTTPNotFound(text="No scan history is available yet")
            return web.FileResponse(self.state.history_path)

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

        app = web.Application()
        app.add_routes(
            [
                web.get("/", index),
                web.get("/ws", websocket),
                web.get("/latest-image", latest_image),
                web.get("/captures/{name}", capture_file),
                web.get("/scan_history.csv", history_csv),
                web.get("/qr/{name}", qr_code),
                web.get("/api/network/status", network_status),
                web.get("/api/network/scan", network_scan),
                web.post("/api/network/connect", network_connect),
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
    match = re.match(r"^(fly_activity_\d{8}_\d{6})(?:_activity|_first_frame)?\.(?:avi|json|csv|jpg)$", path.name)
    if not match:
        return None
    return match.group(1)


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
) -> ActivityResult:
    ensure_cv_deps()

    scan_started = datetime.now()
    timestamp = scan_started.strftime("%Y%m%d_%H%M%S")
    scan_timestamp = scan_started.isoformat(timespec="seconds")
    stem = f"fly_activity_{timestamp}"
    video_path = args.output_dir / f"{stem}.avi"
    first_frame_path = args.output_dir / f"{stem}_first_frame.jpg"

    print(
        f"Recording {args.duration:.1f}s video from webcam {args.camera} "
        f"at {args.width}x{args.height}, {args.fps:g} FPS..."
    )
    if dashboard is not None:
        dashboard.set_status("starting_scan", f"IR {'on' if ir_enabled else 'off'}")
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
    removed_count = cleanup_old_captures(args.output_dir, args.keep_captures, stem)
    if removed_count:
        print(f"Cleaned up {removed_count} old capture files.")

    lcd.write("Activity score", f"{result.activity_score:.2f}")
    if dashboard is not None:
        dashboard.publish_result(result)
    print(json.dumps(asdict(result), indent=2))
    return result


def main() -> int:
    args = parse_args()
    if (
        args.duration <= 0
        or args.fps <= 0
        or args.width <= 0
        or args.height <= 0
        or args.camera_buffers <= 0
    ):
        raise ValueError(
            "duration, fps, width, height and camera-buffers must all be greater than zero"
        )
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
    ir_enabled = False if args.no_ir else load_ir_capture_setting(args.state_file)
    history_path = args.output_dir / "scan_history.csv"
    qr_dir = Path(__file__).resolve().parent / "qr-codes"
    dashboard_state = DashboardState(args.output_dir, history_path, qr_dir)
    dashboard_state.set_ir_enabled(ir_enabled)
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
            run_scan_once(args, lcd, illuminator, ir_enabled, dashboard_state)
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
                    last_result,
                    hold_new_result,
                    args.result_hold,
                    args.double_press_window,
                )
                hold_new_result = False
                if action == "show_network":
                    dashboard_state.set_status("showing_network", "Dashboard address on LCD")
                    show_network_info(
                        lcd,
                        args.web_port,
                        args.network_display_time,
                        args.network_socket,
                    )
                    continue
                if action == "ir_settings":
                    dashboard_state.set_status("ir_settings", "Waiting for selection")
                    if args.no_ir:
                        lcd.write("IR unavailable", "--no-ir active")
                        time.sleep(1.5)
                    else:
                        ir_enabled = configure_ir_capture(
                            button,
                            lcd,
                            ir_enabled,
                            args.state_file,
                            args.button_debounce,
                            args.long_press,
                        )
                        dashboard_state.set_ir_enabled(ir_enabled)
                    continue

                try:
                    last_result = run_scan_once(
                        args, lcd, illuminator, ir_enabled, dashboard_state
                    )
                    hold_new_result = True
                except Exception as exc:
                    dashboard_state.set_status("scan_error", str(exc))
                    lcd.write("Scan error", str(exc)[:16])
                    print(f"Scan failed: {exc}", file=sys.stderr)
                    # Stay alive under systemd; a reconnect or corrected setup can be
                    # tested with the next button press.
                    time.sleep(1.0)
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
