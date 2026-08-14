# Flyscanner

Button-operated Raspberry Pi 5 fruit-fly activity scanner with USB-camera
capture, IR illumination control, a 16x2 LCD, persistent CSV results, and a
local WebSocket dashboard.

## Deploy on a new Raspberry Pi

Use 64-bit Raspberry Pi OS Lite and clone this repository to the service's
expected location:

```bash
sudo apt update
sudo apt install -y git
git clone git@github.com:Schattental/flyscanner.git /home/flyscanner/flyscanner
cd /home/flyscanner/flyscanner
```

Then follow [INSTALL_PI5.md](INSTALL_PI5.md). Its setup creates the virtual
environment, installs dependencies, tests the hardware, and enables the systemd
service.

## Files kept in Git

- `flyscannerV2.py` — scanner, analysis, GPIO, history, and web server
- `dashboard.html` — local live dashboard
- `flyscanner.service` — boot service template
- `flyscanner-network.py` — fallback hotspot and Wi-Fi onboarding helper
- `flyscanner-network.service` — privileged network helper service template
- `flyscanner-network.default` — optional hotspot settings template
- `flyscanner.default` — command-line configuration template
- `requirements-pi5.txt` — Python packages
- `INSTALL_PI5.md` — fresh-device installation and wiring guide

Videos, result files, the virtual environment, Python caches, and the local
scanner preferences are intentionally excluded. They are generated
independently on each scanner and should not make the source repository grow.

## Dashboard

After installation, open:

```text
http://flyscanner.local:8080
```

Double-press the physical button to display the Pi's current numeric address on
the LCD when `.local` hostname resolution is unavailable.

Long-press the button to select a 30-second, 5-minute, 10-minute, or 24-hour
runtime; long-press again to advance to IR settings, then long-press once more
to save both choices. The website provides the same runtime and IR choices plus
advanced camera, analysis, compression, retention, and interface-timing
settings. Choose **Save settings** to persist changes, or **Save and start
scan** to persist them and queue a capture from the network. Runtime remains
restricted to the same four physical-device presets. **Stop current scan**
gracefully finalizes and scores a partial recording when enough frames exist.

Settings are applied between recordings and survive reboots. The dashboard can
also download any requested number of the most recent completed videos (one by
default). Multiple videos are delivered as a streaming `.tar` archive.

The 24-hour choice creates 144 individually scored 10-minute H.264 MP4 parts.
Every filename carries the same session timestamp plus `part001` through
`part144`, and cleanup retains or removes the complete set as one session. The
saved video now contains only the calibrated analysis ROI, not the unused sides
of the camera frame. The default native 746x720 ROI is the same frame and
resolution supplied to analysis and measured about 9.95 GiB for 144 parts.
Selecting 540 saved height produces a 560x540 ROI and measured about 2.79 GiB
for 144 parts while analysis remains native resolution. Both matched tests
sustained about 30.23 FPS with zero drops. The scanner adds 50% estimation
headroom and preserves another 1 GiB before starting.

If no saved network is reachable, the Pi creates a unique password-protected
setup hotspot. Connect to it and open `http://10.42.0.1:8080` to configure the
deployment Wi-Fi from the dashboard. It keeps failed profiles and periodically
retries all saved Wi-Fi networks until one succeeds, restoring the hotspot after
each unsuccessful retry. See [INSTALL_PI5.md](INSTALL_PI5.md) for installation,
credentials, QR payloads, and recovery behavior.

On startup the scanner also generates three device-specific QR-code PNGs for
hotspot connection, hotspot dashboard access, and normal-network dashboard
access. They can be previewed and downloaded from the dashboard's **Printable
QR codes** section and are excluded from Git.
