# Workspace context

## Project

Flyscanner is a button-operated fruit-fly activity scanner for Raspberry Pi 5
running 64-bit Raspberry Pi OS Lite. It captures USB-camera video, analyzes
motion and dark regions, controls IR illumination and a 16x2 LCD, saves results,
and serves a local dashboard. Read `README.md` for product behavior and
`INSTALL_PI5.md` for deployment, wiring, hardware checks, and recovery.

## Repository map

- `flyscannerV2.py`: main application. Includes CLI parsing, GPIO/LCD control,
  settings validation and persistence, camera capture, activity analysis,
  FFmpeg encoding, results/history, retention, QR generation, and the aiohttp
  HTTP/WebSocket server.
- `dashboard.html`: dashboard with inline CSS and JavaScript; no frontend build
  step. Connects to `/ws` and the scanner/network HTTP APIs defined in Python.
- `flyscanner-network.py`: privileged NetworkManager helper using `nmcli`;
  provides fallback hotspot, Wi-Fi onboarding, and a Unix-socket control API at
  `/run/flyscanner-network/control.sock` by default.
- `flyscanner.service`: scanner systemd template, running as `flyscanner` with
  `video` and `gpio` supplementary groups from `/home/flyscanner/flyscanner`.
- `flyscanner-network.service`: root network-helper service; executes the
  installed copy at `/usr/local/lib/flyscanner/flyscanner-network.py`.
- `flyscanner.default` and `flyscanner-network.default`: configuration templates
  for `/etc/default/flyscanner` and `/etc/default/flyscanner-network`.
- `requirements-pi5.txt`: pip dependencies. OpenCV, NumPy, GPIO Zero, and lgpio
  are installed through the OS package manager.

## Setup and validation

Follow `INSTALL_PI5.md` for the required OS packages, including FFmpeg,
`v4l-utils`, and NetworkManager. The virtual environment intentionally exposes
the system Python packages:

```bash
python3 -m venv --system-site-packages .venv
.venv/bin/python -m pip install -r requirements-pi5.txt
```

Network recovery regression tests use unittest and mocked hardware/network
commands: `.venv/bin/python -m unittest discover -s tests -v`. There is no
build/lint configuration. Basic checks that do not start capture or alter networking:

```bash
.venv/bin/python -m py_compile flyscannerV2.py flyscanner-network.py
.venv/bin/python flyscannerV2.py --help
.venv/bin/python flyscanner-network.py --help
git diff --check
```

Hardware checks in the installation guide include `--test-button --no-lcd`
and `--run-now --no-lcd`. These use the actual device; a scan writes output and
can drive the IR illuminator. Use them when hardware validation is part of the
task. Report hardware behavior as unverified when only static checks ran.

## Behavior to preserve

- Default capture is 1280x720 at 30 FPS from `/dev/video0`. The calibrated
  analysis ROI is x=222 through x=967 inclusive at that resolution, with full
  frame height; normalized coordinates preserve the relative region at other
  resolutions. Saved video defaults to ROI-only grayscale H.264 MP4 via FFmpeg.
- Runtime presets are 30 seconds, 5 minutes, 10 minutes, and 24 hours. A 24-hour
  session produces 144 separately scored ten-minute parts sharing a session
  timestamp. Retention counts and removes whole sessions, including all parts.
- Saved-video resizing must remain separate from native-resolution analysis.
- Settings persist in `flyscanner_state.json` and apply between recordings.
  Save-and-start must persist settings successfully before starting capture.
  Cancellation finalizes a partial recording when enough frames exist.
- IR defaults to GPIO16, the button to GPIO26. The illuminator is off while
  idle and must be switched off on capture failure and shutdown.
- The dashboard defaults to port 8080 at `http://flyscanner.local:8080`;
  fallback hotspot access uses `http://10.42.0.1:8080`.
- Triple press while idle forces the setup hotspot without deleting credentials;
  recovery mode pauses automatic reconnection until explicit connection/retry or
  reboot. Double press cycles separate wired LAN and Wi-Fi address pages.
- Forget all networks deletes client Wi-Fi profiles, including disconnected and
  non-autoconnect profiles, while preserving Ethernet and access-point profiles.
- `flyscanner-journald.conf` enables persistent logs capped at 64 MiB/30 days.
  Keep credentials, HTTP request bodies, and query strings out of logs.
- Automatic Wi-Fi changes pause during recording. While disconnected, fallback
  hotspot access remains available even with Ethernet connected. Failed Wi-Fi
  connections restore the hotspot.
- Keep dashboard payloads, backend validation, CLI/configuration defaults,
  service templates, and user documentation consistent when changing behavior.

## Local data

`captures/`, `qr-codes/`, `flyscanner_state.json`, `.venv/`, and Python caches
are generated, device-specific, and ignored by Git. Captures include videos,
alignment images, JSON/CSV results, and `scan_history.csv`. The master history
survives capture retention cleanup. Preserve existing recordings and preferences
when working on source. Keep credentials and device-specific configuration out
of committed context and documentation.
