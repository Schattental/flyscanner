"""Recovery regressions; all NetworkManager, GPIO, and socket operations are mocked."""
import asyncio
import importlib.util
from html.parser import HTMLParser
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, call, patch

ROOT = Path(__file__).resolve().parents[1]


def load_module(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


network = load_module("scanner_network_test", "flyscanner-network.py")
scanner = load_module("scanner_test", "flyscannerV2.py")


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = patch.dict(os.environ, {
            "FLYSCANNER_NETWORK_SOCKET": str(Path(self.tmp.name) / "control.sock"),
            "FLYSCANNER_WIFI_INTERFACE": "wlan0",
        })
        env.start()
        self.addCleanup(env.stop)
        nm = patch.object(network, "run_nmcli", return_value="")
        self.nm = nm.start()
        self.addCleanup(nm.stop)
        self.manager = network.NetworkManager()
        self.manager._hotspot_reconciled = True
        self.manager.wifi_connection = Mock(return_value=None)
        self.manager.hotspot_active = Mock(return_value=True)

    def test_force_disconnects_client_blocks_autoconnect_and_preserves_profiles(self):
        self.manager.wifi_connection.return_value = ("wifi", "saved")
        self.manager.hotspot_active.return_value = False
        self.manager.start_hotspot = Mock()
        self.manager.force_hotspot()
        self.assertTrue(self.manager._forced_hotspot_path.exists())
        self.nm.assert_has_calls([
            call("device", "set", "wlan0", "autoconnect", "no"),
            call("device", "disconnect", "wlan0"),
        ])
        self.manager.start_hotspot.assert_called_once()
        self.assertFalse(any("delete" in c.args for c in self.nm.call_args_list))
        self.assertTrue(network.NetworkManager()._forced_hotspot)

    def test_forced_monitor_does_not_scan_or_drop_existing_hotspot(self):
        self.manager.force_hotspot()
        self.nm.reset_mock()
        self.manager._stop = Mock()
        self.manager._stop.wait.side_effect = [False, True]
        self.manager.ipv4_addresses = Mock(return_value={})
        self.manager.retry_visible_saved_wifi = Mock()
        self.manager.monitor()
        self.manager.retry_visible_saved_wifi.assert_not_called()
        self.nm.assert_not_called()
        self.assertEqual(self.manager._phase, "forced_hotspot")

    def test_force_is_rejected_during_recording_or_another_change(self):
        self.manager.scanner_busy = Mock(return_value=True)
        for command in ("hotspot", "forget_all", "retry"):
            with self.assertRaisesRegex(RuntimeError, "recording"):
                self.manager.handle_request({"command": command})
        self.manager.scanner_busy.return_value = False
        self.manager._phase = "switching"
        with self.assertRaisesRegex(RuntimeError, "already in progress"):
            self.manager.handle_request({"command": "forget_all"})
        self.nm.assert_not_called()

    def test_retry_explicitly_releases_recovery_mode(self):
        self.manager.force_hotspot()
        self.manager.status = Mock(return_value={})
        self.manager.retry_visible_saved_wifi = Mock(return_value=False)
        self.manager.handle_request({"command": "retry"})
        self.assertFalse(self.manager._forced_hotspot)
        self.assertFalse(self.manager._forced_hotspot_path.exists())
        self.nm.assert_any_call("device", "set", "wlan0", "autoconnect", "yes")

    def test_failed_explicit_connect_keeps_forced_mode_success_releases_it(self):
        self.manager.force_hotspot()
        self.manager.install_keyfile_profile = Mock()
        self.manager.start_hotspot = Mock()

        def fail_connection(*args, **kwargs):
            if "--wait" in args:
                raise network.NmcliError("connection failed")
            return ""

        with patch.object(network.time, "sleep"), patch.object(network, "run_nmcli", side_effect=fail_connection):
            self.manager._connect_after_response("Example Wi-Fi", "test-password")
        self.assertTrue(self.manager._forced_hotspot)
        self.assertTrue(self.manager._forced_hotspot_path.exists())
        with patch.object(network.time, "sleep"):
            self.manager._connect_after_response("Example Wi-Fi", "test-password")
        self.assertFalse(self.manager._forced_hotspot)
        self.assertFalse(self.manager._forced_hotspot_path.exists())

    def test_forget_all_enumerates_disabled_profiles_but_preserves_access_points(self):
        listing = "a:Office:wifi\nb:Disabled:wifi\nc:flyscanner-hotspot:wifi\nd:Other AP:wifi\ne:Wired:ethernet"

        def query(*args, **kwargs):
            if "UUID,NAME,TYPE" in args:
                return listing
            return "ap" if args[-1] == "d" else "infrastructure"

        self.nm.side_effect = query
        self.assertEqual(self.manager.all_client_wifi_profiles(), ["a", "b"])

    def test_forget_all_queues_response_then_deletes_by_uuid_and_keeps_hotspot(self):
        self.manager.all_client_wifi_profiles = Mock(return_value=["a", "b"])
        with patch.object(network.threading, "Thread") as thread:
            self.assertEqual(self.manager.request_forget_all_wifi(), 2)
            thread.return_value.start.assert_called_once()
        self.assertEqual(self.manager._phase, "forgetting_all_wifi")
        self.nm.assert_not_called()
        with patch.object(network.time, "sleep"):
            self.manager._forget_all_wifi_after_response(["a", "b"])
        self.assertTrue(self.manager._forced_hotspot)
        self.assertEqual([c for c in self.nm.call_args_list if "delete" in c.args], [
            call("connection", "delete", "uuid", "a"),
            call("connection", "delete", "uuid", "b"),
        ])

    def test_forget_all_attempts_remaining_profiles_after_partial_failure(self):
        def delete(*args, **kwargs):
            if args == ("connection", "delete", "uuid", "a"):
                raise network.NmcliError("failure")
            return ""

        self.nm.side_effect = delete
        with patch.object(network.time, "sleep"):
            self.manager._forget_all_wifi_after_response(["a", "b"])
        self.nm.assert_any_call("connection", "delete", "uuid", "b")
        self.assertTrue(self.manager._forced_hotspot)
        self.assertEqual(self.manager._phase, "forget_error")

    def test_addresses_keep_wired_and_wifi_separate(self):
        self.nm.return_value = "GENERAL.TYPE:ethernet\nIP4.ADDRESS[1]:192.168.1.4/24\nGENERAL.TYPE:wifi\nIP4.ADDRESS[1]:10.42.0.1/24\nGENERAL.TYPE:loopback\nIP4.ADDRESS[1]:127.0.0.1/8"
        self.assertEqual(self.manager.ipv4_addresses(), {"lan_ipv4": ["192.168.1.4"], "wifi_ipv4": ["10.42.0.1"]})


class ButtonAndDisplayTests(unittest.TestCase):
    def action(self, intervals):
        clock = SimpleNamespace(now=0.0)

        class Button:
            @property
            def is_pressed(self):
                return any(start <= clock.now < end for start, end in intervals)

        def sleep(seconds):
            clock.now += seconds
            if clock.now > 5:
                raise AssertionError("button detection did not finish")

        with patch.object(scanner.time, "monotonic", side_effect=lambda: clock.now), patch.object(scanner.time, "sleep", side_effect=sleep):
            return scanner.wait_for_idle_action(Button(), Mock(), 26, 0.25, 1.5, False, 30)

    def test_single_double_triple_and_hold_are_distinct(self):
        for intervals, expected in (
            ([(0.1, 0.2)], "scan"),
            ([(0.1, 0.2), (0.32, 0.42)], "show_network"),
            ([(0.1, 0.2), (0.32, 0.42), (0.54, 0.64)], "force_hotspot"),
            ([(0.1, 1.8)], "capture_settings"),
            ([(0.1, 0.2), (0.21, 0.23)], "scan"),
        ):
            with self.subTest(expected=expected, intervals=intervals):
                self.assertEqual(self.action(intervals), expected)

    def test_double_press_displays_both_addresses_and_labels(self):
        lcd = Mock()
        status = {"ok": True, "hotspot_active": True, "lan_ipv4": ["192.168.1.4"], "wifi_ipv4": ["10.42.0.1"]}
        with patch.object(scanner, "network_helper_request", return_value=status), patch.object(scanner.time, "sleep") as sleep:
            scanner.show_network_info(lcd, 8080, 8, "unused")
        self.assertEqual(lcd.write.call_args_list, [call("LAN :8080", "192.168.1.4"), call("Setup WiFi :8080", "10.42.0.1")])
        self.assertEqual(sleep.call_args_list, [call(8), call(8)])

    def test_address_display_works_when_helper_is_down(self):
        lcd = Mock()
        with patch.object(scanner, "network_helper_request", side_effect=RuntimeError), patch.object(scanner, "local_interface_ipv4_addresses", return_value={"lan_ipv4": [], "wifi_ipv4": ["192.168.1.5"]}), patch.object(scanner.time, "sleep"):
            scanner.show_network_info(lcd, 8080, 8, "unused")
        self.assertEqual(lcd.write.call_args_list, [call("LAN :8080", "Not connected"), call("WiFi :8080", "192.168.1.5")])

    def test_forget_all_button_is_outside_conditional_network_sections(self):
        class Buttons(HTMLParser):
            def __init__(self):
                super().__init__()
                self.hidden = []
                self.visible = None

            def handle_starttag(self, tag, attributes):
                attrs = dict(attributes)
                hidden = "hidden" in attrs or any(value for _, value in self.hidden)
                if attrs.get("id") == "forgetAllWifi":
                    self.visible = not hidden
                if tag not in {"input", "img", "br", "hr", "meta", "link"}:
                    self.hidden.append((tag, hidden))

            def handle_endtag(self, tag):
                if self.hidden and self.hidden[-1][0] == tag:
                    self.hidden.pop()

        parser = Buttons()
        parser.feed((ROOT / "dashboard.html").read_text())
        self.assertTrue(parser.visible)


class DashboardRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from aiohttp import web
        self.web = web
        server = scanner.DashboardServer(Mock(), "127.0.0.1", 0, ROOT / "dashboard.html", "unused")
        server._stop_event.set()
        with patch.object(web, "TCPSite") as site, patch.object(web, "AppRunner", wraps=web.AppRunner) as runner:
            site.return_value.start = AsyncMock()
            await server._serve()
            self.app = site.call_args.args[0].app
            self.access_logger_class = runner.call_args.kwargs["access_log_class"]

    async def request(self, body, origin=None):
        from aiohttp.test_utils import make_mocked_request
        headers = {"Host": "scanner:8080"}
        if origin:
            headers["Origin"] = origin
        request = make_mocked_request("POST", "/api/network/forget-all", headers=headers)
        request.json = AsyncMock(return_value=body)
        match = await self.app.router.resolve(request)
        return await match.handler(request)

    async def test_forget_all_requires_confirmation_and_same_origin(self):
        with patch.object(scanner, "network_helper_request", return_value={"ok": True}) as helper:
            self.assertEqual((await self.request({})).status, 400)
            helper.assert_not_called()
            with self.assertRaises(self.web.HTTPForbidden):
                await self.request({"confirm": True}, "http://other.example")
            helper.assert_not_called()
            self.assertEqual((await self.request({"confirm": True}, "http://scanner:8080")).status, 202)
            helper.assert_called_once_with("unused", {"command": "forget_all"})

    async def test_forget_all_reports_helper_failure(self):
        with patch.object(scanner, "network_helper_request", side_effect=RuntimeError("unavailable")):
            self.assertEqual((await self.request({"confirm": True})).status, 503)

    async def test_access_log_excludes_body_and_query(self):
        logger = Mock()
        request = SimpleNamespace(method="POST", path="/api/network/connect", remote="192.168.1.2", query_string="password=secret-query", body=b"secret-body")
        self.access_logger_class(logger, "").log(request, SimpleNamespace(status=202), 0.1)
        logged = str(logger.info.call_args)
        self.assertNotIn("secret-query", logged)
        self.assertNotIn("secret-body", logged)
        self.assertIn("/api/network/connect", logged)


if __name__ == "__main__":
    unittest.main()
