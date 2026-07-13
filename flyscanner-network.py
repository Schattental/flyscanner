#!/usr/bin/env python3
"""NetworkManager fallback hotspot and Wi-Fi onboarding helper for Flyscanner."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import socket
import socketserver
import subprocess
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any


HOTSPOT_PROFILE = "flyscanner-hotspot"
DEFAULT_SOCKET_PATH = "/run/flyscanner-network/control.sock"


class NmcliError(RuntimeError):
    pass


def run_nmcli(*arguments: str, timeout: float = 40.0, check: bool = True) -> str:
    try:
        result = subprocess.run(
            ["nmcli", *arguments],
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise NmcliError(f"Could not run NetworkManager command: {exc}") from exc
    if check and result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
        raise NmcliError(detail)
    return result.stdout.strip()


def split_terse(line: str) -> list[str]:
    fields: list[str] = []
    current: list[str] = []
    escaped = False
    for character in line:
        if escaped:
            current.append(character)
            escaped = False
        elif character == "\\":
            escaped = True
        elif character == ":":
            fields.append("".join(current))
            current = []
        else:
            current.append(character)
    if escaped:
        current.append("\\")
    fields.append("".join(current))
    return fields


def machine_identity() -> str:
    try:
        value = Path("/etc/machine-id").read_text(encoding="utf-8").strip()
        if value:
            return value
    except OSError:
        pass
    return socket.gethostname()


def keyfile_escape(value: str) -> str:
    """Escape a single GLib keyfile string value without changing its contents."""
    if any(character in value for character in "\0\n\r"):
        raise ValueError("Network credentials cannot contain line breaks or NUL bytes")
    escaped = value.replace("\\", "\\\\").replace("\t", "\\t")
    while escaped.startswith(" "):
        escaped = "\\s" + escaped[1:]
    trailing_spaces = len(escaped) - len(escaped.rstrip(" "))
    if trailing_spaces:
        escaped = escaped[:-trailing_spaces] + "\\s" * trailing_spaces
    return escaped


class NetworkManager:
    def __init__(self) -> None:
        self.socket_path = Path(os.environ.get("FLYSCANNER_NETWORK_SOCKET", DEFAULT_SOCKET_PATH))
        self.interface = os.environ.get("FLYSCANNER_WIFI_INTERFACE", "").strip()
        identity = machine_identity()
        suffix = hashlib.sha256(identity.encode()).hexdigest()[-4:].upper()
        generated_password = f"flyscan-{suffix}"
        self.hotspot_ssid = os.environ.get(
            "FLYSCANNER_HOTSPOT_SSID", f"Flyscanner-{suffix}"
        ).strip()
        self.hotspot_password = os.environ.get(
            "FLYSCANNER_HOTSPOT_PASSWORD", generated_password
        ).strip()
        self.wait_seconds = max(
            5.0, float(os.environ.get("FLYSCANNER_HOTSPOT_WAIT_SECONDS", "45"))
        )
        self._lock = threading.RLock()
        self._phase = "starting"
        self._detail = "Waiting for NetworkManager"
        self._offline_since = time.monotonic()
        self._stop = threading.Event()
        self._hotspot_reconciled = False

        if not self.hotspot_ssid or len(self.hotspot_ssid.encode("utf-8")) > 32:
            raise ValueError("Hotspot SSID must contain 1-32 UTF-8 bytes")
        if not 8 <= len(self.hotspot_password.encode("utf-8")) <= 63:
            raise ValueError("Hotspot password must contain 8-63 characters")

    def discover_interface(self) -> str:
        if self.interface:
            return self.interface
        output = run_nmcli("-t", "--escape", "yes", "-f", "DEVICE,TYPE", "device")
        for line in output.splitlines():
            fields = split_terse(line)
            if len(fields) >= 2 and fields[1] == "wifi" and fields[0]:
                self.interface = fields[0]
                return self.interface
        raise NmcliError("No Wi-Fi interface was found")

    def active_connections(self) -> list[tuple[str, str, str]]:
        output = run_nmcli(
            "-t", "--escape", "yes", "-f", "NAME,TYPE,DEVICE", "connection", "show", "--active"
        )
        active: list[tuple[str, str, str]] = []
        for line in output.splitlines():
            fields = split_terse(line)
            if len(fields) >= 3:
                active.append((fields[0], fields[1], fields[2]))
        return active

    def uplink(self) -> tuple[str, str] | None:
        for name, connection_type, _device in self.active_connections():
            if connection_type in {"802-3-ethernet", "ethernet"}:
                return "ethernet", name
            if connection_type in {"802-11-wireless", "wifi"} and name != HOTSPOT_PROFILE:
                return "wifi", name
        return None

    def hotspot_active(self) -> bool:
        return any(name == HOTSPOT_PROFILE for name, _kind, _device in self.active_connections())

    def install_keyfile_profile(
        self,
        profile_name: str,
        settings: str,
        *,
        autoconnect: bool,
        priority: int = 0,
    ) -> None:
        """Persist a root-only keyfile without putting secrets in process arguments."""
        staged_name = f"flyscanner-staged-{uuid.uuid4().hex}"
        keyfile = (
            "[connection]\n"
            f"id={staged_name}\n"
            f"uuid={uuid.uuid4()}\n"
            "type=wifi\n"
            f"interface-name={keyfile_escape(self.discover_interface())}\n"
            f"autoconnect={'true' if autoconnect else 'false'}\n"
            f"autoconnect-priority={priority}\n\n"
            f"{settings}"
        )
        staged_path: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", prefix="wifi-profile-",
                suffix=".nmconnection", dir=self.socket_path.parent, delete=False,
            ) as secret:
                secret.write(keyfile)
                staged_path = secret.name
            os.chmod(staged_path, 0o600)
            run_nmcli("connection", "load", staged_path)
            # A normal clone persists the root-only staged settings, including
            # secrets, with a fresh UUID and the requested connection name.
            run_nmcli("connection", "clone", staged_name, profile_name)
        finally:
            run_nmcli("connection", "delete", staged_name, check=False)
            if staged_path:
                Path(staged_path).unlink(missing_ok=True)

    def ensure_hotspot_profile(self) -> None:
        run_nmcli("connection", "delete", HOTSPOT_PROFILE, check=False)
        settings = (
            "[wifi]\n"
            f"ssid={keyfile_escape(self.hotspot_ssid)}\n"
            "mode=ap\n"
            "band=bg\n"
            "security=802-11-wireless-security\n\n"
            "[wifi-security]\n"
            "key-mgmt=wpa-psk\n"
            f"psk={keyfile_escape(self.hotspot_password)}\n\n"
            "[ipv4]\n"
            "method=shared\n"
            "address1=10.42.0.1/24\n\n"
            "[ipv6]\n"
            "method=disabled\n"
        )
        self.install_keyfile_profile(HOTSPOT_PROFILE, settings, autoconnect=False)

    def start_hotspot(self) -> None:
        with self._lock:
            if self.uplink() is not None:
                return
            self._phase = "starting_hotspot"
            self._detail = "Creating setup network"
            run_nmcli("radio", "wifi", "on")
            self.ensure_hotspot_profile()
            run_nmcli("--wait", "20", "connection", "up", HOTSPOT_PROFILE)
            self._phase = "hotspot"
            self._detail = f"Connect to {self.hotspot_ssid}"
            self._hotspot_reconciled = True
            print(f"Fallback hotspot active: {self.hotspot_ssid} at 10.42.0.1", flush=True)

    def scan(self) -> list[dict[str, Any]]:
        with self._lock:
            if not self.hotspot_active():
                raise PermissionError("Wi-Fi setup is only available in fallback hotspot mode")
            interface = self.discover_interface()
            arguments = (
                "-t", "--escape", "yes", "-f", "SSID,SIGNAL,SECURITY",
                "device", "wifi", "list", "ifname", interface,
            )
            try:
                output = run_nmcli(*arguments, "--rescan", "yes", timeout=30.0)
            except NmcliError:
                # Some adapters cannot actively scan while they are in AP mode;
                # NetworkManager's cached list still gives useful choices.
                output = run_nmcli(*arguments, "--rescan", "no", timeout=10.0)
        networks: dict[str, dict[str, Any]] = {}
        for line in output.splitlines():
            fields = split_terse(line)
            if len(fields) < 3 or not fields[0] or fields[0] == self.hotspot_ssid:
                continue
            try:
                signal_strength = int(fields[1])
            except ValueError:
                signal_strength = 0
            candidate = {
                "ssid": fields[0],
                "signal": signal_strength,
                "security": fields[2] or "Open",
            }
            if fields[0] not in networks or signal_strength > networks[fields[0]]["signal"]:
                networks[fields[0]] = candidate
        return sorted(networks.values(), key=lambda item: (-item["signal"], item["ssid"].lower()))

    def request_connect(self, ssid: str, password: str) -> None:
        if not ssid or len(ssid.encode("utf-8")) > 32:
            raise ValueError("Wi-Fi name must contain 1-32 UTF-8 bytes")
        if password and not 8 <= len(password.encode("utf-8")) <= 63:
            raise ValueError("Wi-Fi password must contain 8-63 characters")
        with self._lock:
            if not self.hotspot_active() or self._phase == "switching":
                raise PermissionError("Wi-Fi setup is only available in fallback hotspot mode")
            self._phase = "switching"
            self._detail = f"Switching to {ssid}"
        threading.Thread(
            target=self._connect_after_response,
            args=(ssid, password),
            name="wifi-connect",
            daemon=True,
        ).start()

    def _connect_after_response(self, ssid: str, password: str) -> None:
        # Give the dashboard time to display its expected-disconnect message.
        time.sleep(2.0)
        profile_suffix = hashlib.sha256(ssid.encode("utf-8")).hexdigest()[:10]
        profile_name = f"flyscanner-wifi-{profile_suffix}"
        try:
            with self._lock:
                run_nmcli("connection", "down", HOTSPOT_PROFILE, check=False)
                run_nmcli("connection", "delete", profile_name, check=False)
                settings = (
                    "[wifi]\n"
                    f"ssid={keyfile_escape(ssid)}\n"
                    "mode=infrastructure\n"
                    "hidden=true\n"
                )
                if password:
                    settings += (
                        "security=802-11-wireless-security\n\n"
                        "[wifi-security]\n"
                        "key-mgmt=wpa-psk\n"
                        f"psk={keyfile_escape(password)}\n"
                    )
                settings += "\n[ipv4]\nmethod=auto\n\n[ipv6]\nmethod=auto\n"
                self.install_keyfile_profile(
                    profile_name,
                    settings,
                    autoconnect=True,
                    priority=100,
                )
                run_nmcli(
                    "--wait", "35", "connection", "up", profile_name,
                    "ifname", self.discover_interface(), timeout=45.0,
                )
                self._phase = "connected"
                self._detail = f"Connected to {ssid}"
                self._offline_since = time.monotonic()
                print(f"Connected to configured Wi-Fi: {ssid}", flush=True)
        except Exception as exc:
            print(f"Wi-Fi connection failed; restoring hotspot: {exc}", flush=True)
            with self._lock:
                self._phase = "connection_failed"
                self._detail = "Connection failed; restoring setup network"
                run_nmcli("connection", "delete", profile_name, check=False)
            try:
                self.start_hotspot()
            except Exception as hotspot_exc:
                print(f"Could not restore fallback hotspot: {hotspot_exc}", flush=True)

    def status(self, include_credentials: bool = False) -> dict[str, Any]:
        with self._lock:
            hotspot = self.hotspot_active()
            uplink = self.uplink()
            payload: dict[str, Any] = {
                "phase": self._phase,
                "detail": self._detail,
                "hotspot_active": hotspot,
                "hotspot_ssid": self.hotspot_ssid if hotspot else None,
                "setup_url": "http://10.42.0.1:8080" if hotspot else None,
                "uplink_type": uplink[0] if uplink else None,
                "connection_name": uplink[1] if uplink else None,
            }
            if include_credentials:
                payload["hotspot_ssid"] = self.hotspot_ssid
                payload["hotspot_password"] = self.hotspot_password
            return payload

    def handle_request(self, request: dict[str, Any]) -> dict[str, Any]:
        command = request.get("command")
        if command == "status":
            return {"ok": True, **self.status()}
        if command == "credentials":
            return {"ok": True, **self.status(include_credentials=True)}
        if command == "scan":
            return {"ok": True, "networks": self.scan()}
        if command == "connect":
            ssid = request.get("ssid", "")
            password = request.get("password", "")
            if not isinstance(ssid, str) or not isinstance(password, str):
                raise ValueError("Wi-Fi name and password must be text")
            self.request_connect(ssid, password)
            return {
                "ok": True,
                "message": "Settings saved. The hotspot will now disconnect while the scanner joins Wi-Fi.",
            }
        raise ValueError("Unknown network command")

    def monitor(self) -> None:
        while not self._stop.wait(5.0):
            try:
                with self._lock:
                    if self._phase == "switching":
                        continue
                    uplink = self.uplink()
                    hotspot = self.hotspot_active()
                    if uplink:
                        if hotspot:
                            run_nmcli("connection", "down", HOTSPOT_PROFILE, check=False)
                        self._phase = "connected"
                        self._detail = f"Connected via {uplink[0]}: {uplink[1]}"
                        self._offline_since = time.monotonic()
                    elif hotspot:
                        if not self._hotspot_reconciled:
                            # Reapply configured credentials after a helper
                            # restart, including values changed in /etc/default.
                            run_nmcli("connection", "down", HOTSPOT_PROFILE, check=False)
                            self.ensure_hotspot_profile()
                            run_nmcli("--wait", "20", "connection", "up", HOTSPOT_PROFILE)
                            self._hotspot_reconciled = True
                        self._phase = "hotspot"
                        self._detail = f"Connect to {self.hotspot_ssid}"
                    elif time.monotonic() - self._offline_since >= self.wait_seconds:
                        self.start_hotspot()
                    else:
                        remaining = max(0, round(self.wait_seconds - (time.monotonic() - self._offline_since)))
                        self._phase = "waiting"
                        self._detail = f"Waiting {remaining}s for a saved network"
            except Exception as exc:
                with self._lock:
                    self._phase = "error"
                    self._detail = str(exc)
                print(f"Network monitor error: {exc}", flush=True)


class ControlHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        try:
            line = self.rfile.readline(65537)
            if not line or len(line) > 65536:
                raise ValueError("Invalid request size")
            request = json.loads(line.decode("utf-8"))
            if not isinstance(request, dict):
                raise ValueError("Request must be an object")
            response = self.server.manager.handle_request(request)  # type: ignore[attr-defined]
        except Exception as exc:
            response = {"ok": False, "error": str(exc)}
        self.wfile.write((json.dumps(response) + "\n").encode("utf-8"))


class ControlServer(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True
    allow_reuse_address = True


def send_request(socket_path: str, request: dict[str, Any]) -> dict[str, Any]:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(10.0)
        client.connect(socket_path)
        client.sendall((json.dumps(request) + "\n").encode("utf-8"))
        response = client.makefile("rb").readline(65537)
    return json.loads(response.decode("utf-8"))


def run_daemon() -> None:
    manager = NetworkManager()
    manager.socket_path.parent.mkdir(parents=True, exist_ok=True)
    manager.socket_path.unlink(missing_ok=True)
    server = ControlServer(str(manager.socket_path), ControlHandler)
    server.manager = manager  # type: ignore[attr-defined]
    os.chmod(manager.socket_path, 0o660)
    monitor = threading.Thread(target=manager.monitor, name="network-monitor", daemon=True)
    monitor.start()
    print(f"Flyscanner network helper listening on {manager.socket_path}", flush=True)
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        manager._stop.set()
        server.server_close()
        manager.socket_path.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("daemon", "status", "credentials"), nargs="?", default="daemon")
    parser.add_argument("--socket", default=os.environ.get("FLYSCANNER_NETWORK_SOCKET", DEFAULT_SOCKET_PATH))
    args = parser.parse_args()
    if args.command == "daemon":
        run_daemon()
        return 0
    response = send_request(args.socket, {"command": args.command})
    if not response.get("ok"):
        raise RuntimeError(response.get("error", "Network helper request failed"))
    if args.command == "credentials":
        print(f"Hotspot name: {response.get('hotspot_ssid') or 'not active'}")
        print(f"Hotspot password: {response.get('hotspot_password')}")
        print("Dashboard while hotspot is active: http://10.42.0.1:8080")
    else:
        print(json.dumps(response, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
