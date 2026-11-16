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
NETWORKMANAGER_STAGING_DIR = Path("/run/NetworkManager/system-connections")
NETWORK_CHANGE_PHASES = {"switching", "forgetting_wifi", "forgetting_all_wifi"}


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
            env={**os.environ, "LC_ALL": "C"},
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
            5.0, float(os.environ.get("FLYSCANNER_HOTSPOT_WAIT_SECONDS", "5"))
        )
        self.retry_seconds = max(
            30.0, float(os.environ.get("FLYSCANNER_WIFI_RETRY_SECONDS", "120"))
        )
        self.connect_timeout = max(
            5, int(os.environ.get("FLYSCANNER_WIFI_CONNECT_TIMEOUT", "20"))
        )
        self._lock = threading.RLock()
        self._phase = "starting"
        self._detail = "Waiting for NetworkManager"
        self._offline_since = time.monotonic()
        self._stop = threading.Event()
        self._hotspot_reconciled = False
        # Scan promptly when fallback mode first becomes available. Later scans
        # use retry_seconds so the hotspot remains quiet and predictable.
        self._last_wifi_attempt = time.monotonic() - self.retry_seconds
        self._scanner_busy = False
        self._scanner_pid: int | None = None
        self._forced_hotspot_path = self.socket_path.with_name("forced-hotspot")
        self._forced_hotspot = self._forced_hotspot_path.exists()
        self._last_logged_state: str | None = None
        self._forget_all_error: str | None = None

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

    def wifi_connection(self) -> tuple[str, str] | None:
        """Return only a fully connected client profile, never an activating one."""
        interface = self.discover_interface()
        state = run_nmcli("-g", "GENERAL.STATE", "device", "show", interface)
        if not state.startswith("100"):
            return None
        name = run_nmcli("-g", "GENERAL.CONNECTION", "device", "show", interface)
        if name and name != "--" and name != HOTSPOT_PROFILE:
            return "wifi", name
        return None

    def ethernet_connection(self) -> tuple[str, str] | None:
        for name, connection_type, _device in self.active_connections():
            if connection_type in {"802-3-ethernet", "ethernet"}:
                return "ethernet", name
        return None

    def wifi_ssid(self, profile_name: str) -> str | None:
        try:
            value = run_nmcli(
                "-g", "802-11-wireless.ssid", "connection", "show", profile_name
            )
        except NmcliError:
            return None
        return value or None

    def saved_wifi_profiles(self) -> list[str]:
        output = run_nmcli(
            "-t", "--escape", "yes", "-f", "NAME,TYPE,AUTOCONNECT", "connection", "show"
        )
        profiles: list[str] = []
        for line in output.splitlines():
            fields = split_terse(line)
            if (
                len(fields) >= 3
                and fields[1] in {"802-11-wireless", "wifi"}
                and fields[0] != HOTSPOT_PROFILE
                and fields[2].lower() in {"yes", "true", "1"}
            ):
                profiles.append(fields[0])
        return profiles

    def hotspot_active(self) -> bool:
        return any(name == HOTSPOT_PROFILE for name, _kind, _device in self.active_connections())

    def all_client_wifi_profiles(self) -> list[str]:
        """UUIDs include disabled autoconnect profiles and avoid ambiguous names."""
        output = run_nmcli(
            "-t", "--escape", "yes", "-f", "UUID,NAME,TYPE", "connection", "show"
        )
        profiles: list[str] = []
        for line in output.splitlines():
            fields = split_terse(line)
            if (
                len(fields) >= 3
                and fields[2] in {"802-11-wireless", "wifi"}
                and fields[1] != HOTSPOT_PROFILE
            ):
                mode = run_nmcli(
                    "-g", "802-11-wireless.mode", "connection", "show", "uuid", fields[0]
                )
                if mode in {"infrastructure", "", "--"}:
                    profiles.append(fields[0])
        return profiles

    def ipv4_addresses(self) -> dict[str, list[str]]:
        output = run_nmcli(
            "-t", "--escape", "yes", "-f", "GENERAL.TYPE,IP4.ADDRESS", "device", "show"
        )
        addresses: dict[str, list[str]] = {"lan_ipv4": [], "wifi_ipv4": []}
        kind = ""
        for line in output.splitlines():
            fields = split_terse(line)
            if len(fields) < 2:
                continue
            if fields[0] == "GENERAL.TYPE":
                kind = fields[1]
            elif fields[0].startswith("IP4.ADDRESS"):
                key = "wifi_ipv4" if kind == "wifi" else "lan_ipv4" if kind == "ethernet" else None
                if key:
                    addresses[key].append(fields[1].split("/", 1)[0])
        return addresses

    def check_network_change_allowed(self) -> None:
        if self._phase in NETWORK_CHANGE_PHASES:
            raise RuntimeError("A Wi-Fi change is already in progress")
        if self.scanner_busy():
            raise RuntimeError("Wait for the recording to finish before changing Wi-Fi")

    def force_hotspot(self) -> None:
        """Latch recovery mode without deleting credentials or changing Ethernet."""
        with self._lock:
            self._forced_hotspot_path.parent.mkdir(parents=True, exist_ok=True)
            self._forced_hotspot_path.touch(mode=0o600)
            self._forced_hotspot = True
            interface = self.discover_interface()
            # Device-level autoconnect also stops NetworkManager's own retries.
            run_nmcli("device", "set", interface, "autoconnect", "no")
            if self.wifi_connection() is not None:
                run_nmcli("device", "disconnect", interface)
            if not self.hotspot_active() or not self._hotspot_reconciled:
                self.start_hotspot()
            self._phase = "forced_hotspot"
            self._detail = "Setup hotspot forced; automatic Wi-Fi reconnection paused"
            print(self._detail, flush=True)

    def resume_wifi_autoconnect(self) -> None:
        with self._lock:
            run_nmcli("device", "set", self.discover_interface(), "autoconnect", "yes")
            self._forced_hotspot_path.unlink(missing_ok=True)
            self._forced_hotspot = False
            print("Automatic Wi-Fi reconnection enabled", flush=True)

    def set_scanner_busy(self, busy: bool, pid: int | None = None) -> None:
        """Track capture activity without blocking the network control socket."""
        if busy and (pid is None or pid <= 0):
            raise ValueError("A positive scanner PID is required while busy")
        self._scanner_busy = busy
        self._scanner_pid = pid if busy else None
        if busy:
            print(f"Scanner recording state active (PID {pid}); Wi-Fi switching paused", flush=True)
        else:
            # Make the next monitor pass scan immediately instead of waiting for
            # the normal retry interval after a long recording.
            self._last_wifi_attempt = time.monotonic() - self.retry_seconds
            print("Scanner recording state idle; Wi-Fi scanning resumed", flush=True)

    def scanner_busy(self) -> bool:
        if not self._scanner_busy:
            return False
        pid = self._scanner_pid
        if pid is None:
            self._scanner_busy = False
            return False
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            self._scanner_busy = False
            self._scanner_pid = None
            print("Scanner process ended; clearing stale recording state", flush=True)
            return False
        except PermissionError:
            # The helper runs as root in production, but treating a live PID as
            # busy is also the safe behavior in restricted test environments.
            pass
        return True

    def visible_saved_wifi_profiles(self) -> list[str]:
        """Return saved profiles whose SSIDs are visible without leaving AP mode."""
        visible_ssids = {str(network["ssid"]) for network in self.scan()}
        matches: list[str] = []
        for profile in self.saved_wifi_profiles():
            if self.wifi_ssid(profile) in visible_ssids:
                matches.append(profile)
        return matches

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
            NETWORKMANAGER_STAGING_DIR.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", prefix="wifi-profile-",
                suffix=".nmconnection", dir=NETWORKMANAGER_STAGING_DIR, delete=False,
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
            if self.wifi_connection() is not None:
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

    def retry_saved_wifi(
        self,
        restore_hotspot: bool = True,
        profiles: list[str] | None = None,
    ) -> bool:
        """Try selected saved profiles, restoring the hotspot on failure."""
        with self._lock:
            profiles = self.saved_wifi_profiles() if profiles is None else list(profiles)
            self._last_wifi_attempt = time.monotonic()
            if not profiles:
                return False
            if self._forced_hotspot:
                return False
            if self.scanner_busy():
                self._phase = "recording"
                self._detail = "Recording active; setup hotspot preserved"
                return False
            self._phase = "retrying_wifi"
            self._detail = f"Connecting to {len(profiles)} visible saved Wi-Fi profile(s)"
            print(f"Trying visible saved Wi-Fi profiles: {', '.join(profiles)}", flush=True)
            # Scanning and matching happened while the hotspot was still up.
            # It is taken down only now, when a known SSID is actually visible.
            run_nmcli("connection", "down", HOTSPOT_PROFILE, check=False)
            run_nmcli("radio", "wifi", "on")
            for profile in profiles:
                try:
                    run_nmcli(
                        "--wait", str(self.connect_timeout),
                        "connection", "up", profile,
                        "ifname", self.discover_interface(),
                        timeout=float(self.connect_timeout + 5),
                    )
                except NmcliError as exc:
                    print(f"Saved Wi-Fi profile {profile!r} failed: {exc}", flush=True)
                    continue
                if self.wifi_connection() is not None:
                    self._phase = "connected"
                    self._detail = f"Connected to Wi-Fi: {profile}"
                    self._offline_since = time.monotonic()
                    print(f"Connected using saved Wi-Fi profile: {profile}", flush=True)
                    return True
            self._phase = "wifi_retry_failed"
            self._detail = (
                "Saved Wi-Fi unavailable; restoring setup hotspot"
                if restore_hotspot
                else "Saved Wi-Fi unavailable"
            )
        if restore_hotspot:
            self.start_hotspot()
        return False

    def retry_visible_saved_wifi(self) -> bool:
        """Scan in AP mode and switch only when an idle scanner sees known Wi-Fi."""
        if self._forced_hotspot:
            return False
        if self.scanner_busy():
            with self._lock:
                self._phase = "recording"
                self._detail = "Recording active; setup hotspot preserved"
            return False
        try:
            profiles = self.visible_saved_wifi_profiles()
        except PermissionError:
            # A concurrent transition may have already removed the hotspot.
            return False
        self._last_wifi_attempt = time.monotonic()
        print(f"Wi-Fi scan: {len(profiles)} visible saved profile(s) matched", flush=True)
        if not profiles:
            with self._lock:
                self._phase = "hotspot"
                self._detail = f"No saved Wi-Fi in range; connect to {self.hotspot_ssid}"
            return False
        return self.retry_saved_wifi(profiles=profiles)

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
            self.check_network_change_allowed()
            if not self.hotspot_active():
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
                    "--wait", str(self.connect_timeout), "connection", "up", profile_name,
                    "ifname", self.discover_interface(),
                    timeout=float(self.connect_timeout + 5),
                )
                self.resume_wifi_autoconnect()
                self._phase = "connected"
                self._detail = f"Connected to {ssid}"
                self._offline_since = time.monotonic()
                self._last_wifi_attempt = time.monotonic()
                print(f"Connected to configured Wi-Fi: {ssid}", flush=True)
        except Exception as exc:
            print(f"Wi-Fi connection failed; restoring hotspot: {exc}", flush=True)
            with self._lock:
                self._phase = "connection_failed"
                self._detail = "Connection failed; restoring setup network"
                self._last_wifi_attempt = time.monotonic()
            try:
                self.start_hotspot()
            except Exception as hotspot_exc:
                print(f"Could not restore fallback hotspot: {hotspot_exc}", flush=True)

    def request_forget_current_wifi(self) -> str:
        with self._lock:
            self.check_network_change_allowed()
            wifi = self.wifi_connection()
            if wifi is None:
                raise RuntimeError("No active Wi-Fi connection can be forgotten")
            profile_name = wifi[1]
            self._phase = "forgetting_wifi"
            self._detail = f"Forgetting Wi-Fi profile: {profile_name}"
        threading.Thread(
            target=self._forget_wifi_after_response,
            args=(profile_name,),
            name="wifi-forget",
            daemon=True,
        ).start()
        return profile_name

    def _forget_wifi_after_response(self, profile_name: str) -> None:
        # Let the browser display the expected-disconnect warning first.
        time.sleep(2.0)
        try:
            with self._lock:
                run_nmcli("connection", "delete", profile_name)
                self._phase = "wifi_forgotten"
                self._detail = "Saved Wi-Fi removed; restoring setup hotspot"
                self._offline_since = time.monotonic() - self.wait_seconds
                self._last_wifi_attempt = time.monotonic() - self.retry_seconds
                print(f"Forgot Wi-Fi profile: {profile_name}", flush=True)
            if not self.hotspot_active():
                self.start_hotspot()
            # The normal scan-first path will now look for any other saved SSID
            # without making the device disappear while none are in range.
            self.retry_visible_saved_wifi()
        except Exception as exc:
            with self._lock:
                self._phase = "forget_error"
                self._detail = str(exc)
            print(f"Could not forget Wi-Fi profile {profile_name!r}: {exc}", flush=True)

    def request_forget_all_wifi(self) -> int:
        with self._lock:
            self.check_network_change_allowed()
            profiles = self.all_client_wifi_profiles()
            self._forget_all_error = None
            self._phase = "forgetting_all_wifi"
            self._detail = "Removing all saved Wi-Fi networks; restoring setup hotspot"
        threading.Thread(
            target=self._forget_all_wifi_after_response,
            args=(profiles,), name="wifi-forget-all", daemon=True,
        ).start()
        return len(profiles)

    def _forget_all_wifi_after_response(self, profiles: list[str]) -> None:
        time.sleep(2.0)
        try:
            with self._lock:
                self.force_hotspot()
                failures = []
                for profile in profiles:
                    try:
                        run_nmcli("connection", "delete", "uuid", profile)
                    except NmcliError:
                        failures.append(profile)
                print(f"Forgot {len(profiles) - len(failures)} saved Wi-Fi profile(s)", flush=True)
                if failures:
                    raise NmcliError(f"Could not remove {len(failures)} Wi-Fi profile(s); retry Forget all networks")
        except Exception as exc:
            with self._lock:
                self._phase = "forget_error"
                self._detail = str(exc)
                self._forget_all_error = str(exc)
            print(f"Forget all Wi-Fi failed: {exc}", flush=True)

    def status(self, include_credentials: bool = False) -> dict[str, Any]:
        with self._lock:
            hotspot = self.hotspot_active()
            wifi = self.wifi_connection()
            ethernet = self.ethernet_connection()
            uplink = wifi or ethernet
            current_wifi_ssid = self.wifi_ssid(wifi[1]) if wifi else None
            payload: dict[str, Any] = {
                "phase": self._phase,
                "detail": self._detail,
                "hotspot_active": hotspot,
                "hotspot_ssid": self.hotspot_ssid if hotspot else None,
                "setup_url": "http://10.42.0.1:8080" if hotspot else None,
                "uplink_type": uplink[0] if uplink else None,
                "connection_name": uplink[1] if uplink else None,
                "wifi_connected": wifi is not None,
                "wifi_ssid": current_wifi_ssid,
                "ethernet_connected": ethernet is not None,
                "saved_wifi_profiles": len(self.saved_wifi_profiles()),
                "wifi_retry_seconds": self.retry_seconds,
                "scanner_busy": self.scanner_busy(),
                "forced_hotspot": self._forced_hotspot,
                "forget_all_error": self._forget_all_error,
                **self.ipv4_addresses(),
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
        if command == "scanner_busy":
            busy = request.get("busy")
            pid = request.get("pid")
            if not isinstance(busy, bool):
                raise ValueError("busy must be true or false")
            if pid is not None and (isinstance(pid, bool) or not isinstance(pid, int)):
                raise ValueError("pid must be an integer")
            self.set_scanner_busy(busy, pid)
            return {"ok": True, "scanner_busy": self.scanner_busy()}
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
        if command == "hotspot":
            with self._lock:
                self.check_network_change_allowed()
                self.force_hotspot()
            return {"ok": True, **self.status()}
        if command == "retry":
            with self._lock:
                self.check_network_change_allowed()
                self.resume_wifi_autoconnect()
                if not self.hotspot_active() and self.wifi_connection() is None:
                    self.start_hotspot()
                connected = self.wifi_connection() is not None or self.retry_visible_saved_wifi()
            return {"ok": True, "connected": connected, **self.status()}
        if command == "forget_all":
            count = self.request_forget_all_wifi()
            return {
                "ok": True, "profiles": count,
                "message": "All saved Wi-Fi networks are being removed. Connect to the setup hotspot at http://10.42.0.1:8080. Ethernet is preserved.",
            }
        if command == "forget":
            profile_name = self.request_forget_current_wifi()
            return {
                "ok": True,
                "profile_name": profile_name,
                "message": (
                    "The saved Wi-Fi is being removed. Flyscanner will restore its "
                    "setup hotspot, then scan for visible saved networks while idle."
                ),
            }
        raise ValueError("Unknown network command")

    def monitor(self) -> None:
        while not self._stop.wait(5.0):
            try:
                with self._lock:
                    if self._phase in NETWORK_CHANGE_PHASES:
                        continue
                    wifi = self.wifi_connection()
                    hotspot = self.hotspot_active()
                    if self._forced_hotspot:
                        if wifi or not hotspot or not self._hotspot_reconciled:
                            self.force_hotspot()
                        else:
                            self._phase = "forced_hotspot"
                            self._detail = "Setup hotspot forced; automatic Wi-Fi reconnection paused"
                    elif wifi:
                        if hotspot:
                            run_nmcli("connection", "down", HOTSPOT_PROFILE, check=False)
                        self._phase = "connected"
                        self._detail = f"Connected to Wi-Fi: {wifi[1]}"
                        self._offline_since = time.monotonic()
                    elif hotspot:
                        if not self._hotspot_reconciled:
                            # Reapply configured credentials after a helper
                            # restart, including values changed in /etc/default.
                            run_nmcli("connection", "down", HOTSPOT_PROFILE, check=False)
                            self.ensure_hotspot_profile()
                            run_nmcli("--wait", "20", "connection", "up", HOTSPOT_PROFILE)
                            self._hotspot_reconciled = True
                        if time.monotonic() - self._last_wifi_attempt >= self.retry_seconds:
                            self.retry_visible_saved_wifi()
                        else:
                            if self.scanner_busy():
                                self._phase = "recording"
                                self._detail = "Recording active; setup hotspot preserved"
                            else:
                                self._phase = "hotspot"
                                self._detail = f"Connect to {self.hotspot_ssid}"
                    elif time.monotonic() - self._offline_since >= self.wait_seconds:
                        # Never spend minutes blindly activating stale profiles.
                        # Establish the reachable fallback first; the next pass
                        # scans in AP mode and only tries visible known SSIDs.
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
            finally:
                # Only transitions are logged; never include credentials or bodies.
                with self._lock:
                    try:
                        addresses = self.ipv4_addresses()
                    except NmcliError:
                        addresses = {}
                    state = json.dumps({
                        "phase": self._phase, "detail": self._detail,
                        "forced_hotspot": self._forced_hotspot, **addresses,
                    }, sort_keys=True)
                    if state != self._last_logged_state:
                        print(f"Network state: {state}", flush=True)
                        self._last_logged_state = state


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
    parser.add_argument(
        "command",
        choices=("daemon", "status", "credentials", "hotspot", "retry", "forget", "forget_all"),
        nargs="?",
        default="daemon",
    )
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
