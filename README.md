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
git clone YOUR_REPOSITORY_URL /home/flyscanner/flyscanner
cd /home/flyscanner/flyscanner
```

Then follow [INSTALL_PI5.md](INSTALL_PI5.md). Its setup creates the virtual
environment, installs dependencies, tests the hardware, and enables the systemd
service.

## Files kept in Git

- `flyscannerV2.py` — scanner, analysis, GPIO, history, and web server
- `dashboard.html` — local live dashboard
- `flyscanner.service` — boot service template
- `flyscanner.default` — command-line configuration template
- `requirements-pi5.txt` — Python packages
- `INSTALL_PI5.md` — fresh-device installation and wiring guide

Videos, result files, the virtual environment, Python caches, and the local IR
preference are intentionally excluded. They are generated independently on each
scanner and should not make the source repository grow.

## Dashboard

After installation, open:

```text
http://flyscanner.local:8080
```

Double-press the physical button to display the Pi's current numeric address on
the LCD when `.local` hostname resolution is unavailable.
