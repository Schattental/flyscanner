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

If no saved network is reachable, the Pi creates a unique password-protected
setup hotspot. Connect to it and open `http://10.42.0.1:8080` to configure the
deployment Wi-Fi from the dashboard. See [INSTALL_PI5.md](INSTALL_PI5.md) for
installation, credentials, QR payloads, and recovery behavior.

On startup the scanner also generates three device-specific QR-code PNGs for
hotspot connection, hotspot dashboard access, and normal-network dashboard
access. They can be previewed and downloaded from the dashboard's **Printable
QR codes** section and are excluded from Git.
