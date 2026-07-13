# Flyscanner V2 on Raspberry Pi 5

V2 is built for a **64-bit Raspberry Pi OS Lite** installation and the USB
webcam used by the original scanner. It records at 1280x720 and 30 FPS by
default, while analyzing a 746x720 region at 15 FPS. The calibrated region uses
normalized frame coordinates: 17.34375% through 75.625% horizontally and the
full frame height. At 1280x720 this is x=222 through x=967; at 1920x1080 it is
x=333 through x=1451. This preserves the exact relative part of the image when
switching between resolutions with the same camera aspect ratio. The original
file has been renamed to `flyscannerV2.py`.

The camera must support the requested mode. Check its advertised modes before
choosing 1080p or a higher frame rate:

```bash
v4l2-ctl --list-devices
v4l2-ctl --device=/dev/video0 --list-formats-ext
```

This version uses V4L2 for a USB webcam. A CSI ribbon camera should use a
Picamera2/libcamera capture backend instead; `/dev/video0` is not a drop-in CSI
camera interface on current Raspberry Pi OS.

## 1. Install system packages

```bash
sudo apt update
sudo apt full-upgrade -y
sudo apt install -y python3-venv python3-dev python3-opencv python3-numpy \
  python3-gpiozero python3-lgpio v4l-utils
sudo usermod -aG video,gpio flyscanner
```

Log out and back in after changing groups, or reboot. `python3-opencv`, NumPy,
GPIO Zero, and `lgpio` come from Raspberry Pi OS as precompiled packages. This
avoids slow local OpenCV builds and uses the Pi 5-compatible GPIO backend.

## 2. Create the Python environment

Run these commands from `/home/flyscanner/flyscanner`:

```bash
python3 -m venv --system-site-packages .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements-pi5.txt
```

`python3-dev` supplies `Python.h`, which pip needs while building native Blinka
dependencies such as `rpi_ws281x` and `RPi.GPIO`. `--system-site-packages` is
intentional: it exposes the optimized packages
installed by `apt` inside the virtual environment. The two pip packages drive
the optional HD44780-compatible 16x2 LCD. If no LCD is connected, you may skip
the requirements file and run with `--no-lcd`.

## 3. Test hardware before enabling boot startup

The button is expected between **GPIO26 (physical pin 37)** and **GND**. Existing
LCD defaults are RS=GPIO25, EN=GPIO24, D4=GPIO23, D5=GPIO17, D6=GPIO18, and
D7=GPIO22. GPIO uses 3.3 V logic; verify the LCD wiring does not drive 5 V into
the Pi.

### IR illuminator wiring

The IR illuminator control defaults to **GPIO16 (physical pin 36)**. Wire an NPN
transistor as a low-side switch:

- GPIO16 (physical pin 36) -> 330 ohm base resistor -> transistor base
- Transistor emitter -> Pi GND (for example, physical pin 14)
- Transistor collector -> negative/cathode side of the IR LED array
- Positive/anode side of the array -> its correctly rated supply through the
  required LED current-limiting resistor(s)
- If the LED array uses an external supply, connect that supply's ground to Pi
  GND so the transistor base has a common reference

Check the transistor datasheet for its base/collector/emitter pin order; it is
not the same for every NPN part. The 330 ohm **base** resistor protects the GPIO
drive path, but it does not limit current through the IR LEDs. Unless the array
already includes suitable current limiting, each parallel LED branch needs its
own correctly sized series resistor. Never power the LED array from GPIO16.

The service holds GPIO16 low while idle. When IR capture is enabled, it switches
the array on before camera warm-up, keeps it on throughout capture, and switches
it off immediately afterward, including when capture fails. Use `--ir-gpio
NUMBER` to select a different free BCM GPIO or `--no-ir` to disable this feature.

The idle LCD shows the saved IR capture state as `IR:on` or `IR:off`. The one
button has the following behavior:

- Single short press while idle: start a capture after a 0.6-second
  double-press window, using the displayed IR setting.
- Double press while idle: show the dashboard port and current LAN IP address
  on the LCD for eight seconds.
- Long press while idle (1.5 seconds): open the IR setting screen.
- Short press in the setting screen: toggle the selection between ON and OFF.
- Long press in the setting screen: save the displayed selection and return.

The confirmed choice is stored in `flyscanner_state.json` and loaded again after
service restarts and reboots. The LEDs remain physically off while the scanner
is idle even when the displayed capture setting is ON.

Immediately after recording, the LCD switches directly to the activity score
and holds it for six seconds. It then alternates every two seconds between the
score and the `IR:on/off Tap/Hold` control hint. The button remains responsive
during the initial score display. Change its duration with `--result-hold
SECONDS`.

```bash
.venv/bin/python flyscannerV2.py --test-button --no-lcd
.venv/bin/python flyscannerV2.py --run-now --no-lcd
```

Remove `--no-lcd` for an LCD test. A successful scan creates an AVI, first-frame
JPEG, JSON, and CSV under `captures/`. Every successful scan is also appended to
`captures/scan_history.csv`; this master history is not removed by capture
cleanup. The AVI retains the complete camera frame. The first-frame JPEG
contains the exact analysis region so you can check specimen alignment and
framing. Change its position with `--analysis-shift-x PERCENT`; negative values
move left and positive values move right. Change only the extra left coverage
with `--analysis-extend-left PERCENT`. Any resize set by `--analysis-width`
calculates the height from the calibrated region instead of stretching it, so
its aspect ratio is retained.

After camera warm-up, V2 attempts to freeze the webcam's exposure/gain, white
balance, and focus at their settled values. Webcam support varies, so the JSON,
per-scan CSV, and master history record both the successfully locked controls
and their read-back values. Unsupported controls do not fail the scan. Use
`--no-camera-lock` only when troubleshooting a camera that rejects manual
controls.

If capture does not sustain 30 FPS, first confirm that the camera advertises an
MJPG mode at the requested resolution. Full-resolution cropped analysis costs
more CPU; you can reduce analysis load without reducing the recorded frame rate:

```bash
.venv/bin/python flyscannerV2.py --run-now --analyze-every 2 --analysis-width 480
```

For a camera that advertises it, 1080p/30 can be requested with `--width 1920
--height 1080 --fps 30`. The script prints the mode actually negotiated by the
camera, which may differ from the request.

## 4. Install and start the system service

```bash
sudo install -m 0644 flyscanner.service /etc/systemd/system/flyscanner.service
sudo install -m 0644 flyscanner.default /etc/default/flyscanner
sudo systemctl daemon-reload
sudo systemctl enable --now flyscanner.service
systemctl status flyscanner.service
```

The service starts at every boot and waits for the GPIO button. It runs without
a graphical preview and restarts if startup fails. A failed individual scan is
shown on the LCD/log and the process remains ready for another button press.

## 5. Open the local dashboard

The service hosts a read-only dashboard on port 8080. It uses a WebSocket for
live scanner status, result, IR state, temperature, disk space, and recent scan
updates. It also provides the latest alignment image, latest video download, and
the complete master history CSV.

Find the Pi's address:

```bash
hostname -I
```

On a phone or computer connected to the same local network, first try the stable
mDNS hostname:

```text
http://flyscanner.local:8080
```

Raspberry Pi OS advertises its hostname through mDNS. If the client or network
does not support `.local` names, double-press the scanner button to show the
current numeric address on the LCD, or find it manually with `hostname -I`.
Open the numeric address as:

```text
http://PI_ADDRESS:8080
```

For example, `http://192.168.1.42:8080`. The LCD displays `Dashboard :8080` on
its first line and the numeric IP on its second line. Adjust the recognition
window with `--double-press-window SECONDS` and the screen duration with
`--network-display-time SECONDS`.

The dashboard intentionally has no
remote hardware controls or authentication. Keep it on a trusted local network;
do not forward port 8080 from your router to the internet. Change the port with
`--web-port PORT`, bind only to one interface with `--web-host ADDRESS`, or turn
it off with `--no-web` in `/etc/default/flyscanner`.

Useful administration commands:

```bash
journalctl -u flyscanner.service -f
sudo systemctl restart flyscanner.service
sudo systemctl disable --now flyscanner.service
```

To change camera, resolution, frame rate, pins, or disable the LCD, edit
`/etc/default/flyscanner`, set `FLYSCANNER_ARGS`, and restart the service. Keep
the value unquoted, for example:

```text
FLYSCANNER_ARGS=--camera /dev/video0 --width 1920 --height 1080 --fps 30 --analyze-every 2
```
