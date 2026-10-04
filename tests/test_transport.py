"""LAN reply regression tests: loopback only, no lamps or third-party packages."""

import contextlib
import importlib.machinery
import importlib.util
import io
import json
from pathlib import Path
import socket
import tempfile
import threading
import unittest
from unittest.mock import patch


loader = importlib.machinery.SourceFileLoader(
    "govee_lamp", str(Path(__file__).resolve().parents[1] / "bin" / "govee-lamp"))
spec = importlib.util.spec_from_loader(loader.name, loader)
lamp = importlib.util.module_from_spec(spec)
loader.exec_module(lamp)


def reply(command, data):
    return json.dumps({"msg": {"cmd": command, "data": data}}).encode()


STATUS = {"onOff": 1, "brightness": 42,
          "color": {"r": 10, "g": 20, "b": 30}, "colorTemInKelvin": 0}
SCAN = {"ip": "127.0.0.1", "device": "test-device", "sku": "H6020"}
MALFORMED = [
    b"not json", b"\xff", b"null", b"[]", b"1", b'"text"', b"{}",
    b'{"msg": null}', b'{"msg": []}', b'{"msg": "text"}',
    b'{"msg": {"cmd": "devStatus"}}',
]


class TransportTests(unittest.TestCase):
    def setUp(self):
        state = tempfile.TemporaryDirectory()
        self.addCleanup(state.cleanup)
        self.patch("STATE_DIR", state.name)

        # A fake lamp receives real requests on an ephemeral loopback port.
        self.server = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.server.bind(("127.0.0.1", 0))
        self.server.settimeout(0.05)
        self.addCleanup(self.server.close)
        port = self.server.getsockname()[1]
        self.patch("SEND_PORT", port)
        self.patch("SCAN_PORT", port)
        self.patch("MULTICAST", "127.0.0.1")

        # Reserve a free reply port without touching the real Govee port 4002.
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.bind(("127.0.0.1", 0))
            self.reply_port = probe.getsockname()[1]
        self.patch("RECV_PORT", self.reply_port)

        self.other = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.other.bind(("127.0.0.2", 0))
        self.addCleanup(self.other.close)
        self.device = lamp.Device({"ip": "127.0.0.1"})
        self.addCleanup(self.device.sock.close)
        self.requests = []
        self.errors = []

    def patch(self, name, value):
        patcher = patch.object(lamp, name, value)
        patcher.start()
        self.addCleanup(patcher.stop)

    def respond(self, packets):
        """Send (socket, bytes) pairs after each request, in the given order."""
        stop = threading.Event()

        def serve():
            while not stop.is_set():
                try:
                    data, _ = self.server.recvfrom(4096)
                except socket.timeout:
                    continue
                try:
                    self.requests.append(json.loads(data)["msg"]["cmd"])
                    for sender, packet in packets:
                        sender.sendto(packet, ("127.0.0.1", self.reply_port))
                except Exception as exc:
                    self.errors.append(exc)
                    return

        worker = threading.Thread(target=serve, daemon=True)
        worker.start()

        def finish():
            stop.set()
            worker.join(timeout=1)
            self.assertFalse(worker.is_alive())
            self.assertEqual(self.errors, [])

        self.addCleanup(finish)

    def test_status_ignores_another_lamps_reply(self):
        self.respond([
            (self.other, reply("devStatus", {**STATUS, "brightness": 99})),
            (self.server, reply("devStatus", STATUS)),
        ])
        self.assertEqual(self.device.status(timeout=0.1), STATUS)
        self.assertEqual(self.requests, ["devStatus"])

    def test_status_ignores_scan_from_same_lamp(self):
        self.respond([
            (self.server, reply("scan", SCAN)),
            (self.server, reply("devStatus", STATUS)),
        ])
        self.assertEqual(self.device.status(timeout=0.1), STATUS)

    def test_status_ignores_malformed_packets(self):
        packets = MALFORMED + [reply("devStatus", value)
                               for value in (None, [], "text", 1)]
        for packet in packets:
            with self.subTest(packet=packet):
                # Inject immediately after the request; reception stays real UDP.
                def send(_payload, ip=None):
                    self.server.sendto(packet, ("127.0.0.1", self.reply_port))
                    self.server.sendto(reply("devStatus", STATUS),
                                       ("127.0.0.1", self.reply_port))
                with patch.object(self.device, "_send", side_effect=send):
                    self.assertEqual(self.device.status(timeout=0.1), STATUS)

    def test_status_times_out_when_only_another_lamp_answers(self):
        self.respond([(self.other, reply("devStatus", STATUS))])
        self.assertIsNone(self.device.status(timeout=0.05))

    def test_status_supports_hostnames(self):
        self.device.ip = "localhost"
        self.respond([(self.server, reply("devStatus", STATUS))])
        self.assertEqual(self.device.status(timeout=0.1), STATUS)

    def test_discovery_does_not_overwrite_scan_with_late_status(self):
        self.respond([
            (self.server, reply("scan", SCAN)),
            (self.server, reply("devStatus", STATUS)),
        ])
        self.assertEqual(lamp.discover({}, timeout=0.05), {"127.0.0.1": SCAN})
        self.assertEqual(self.requests, ["scan"])

    def test_discovery_ignores_malformed_packets(self):
        packets = MALFORMED + [reply("scan", value)
                               for value in (None, [], "text", 1)]
        self.respond([(self.server, packet) for packet in packets] +
                     [(self.server, reply("scan", SCAN))])
        self.assertEqual(lamp.discover({}, timeout=0.05), {"127.0.0.1": SCAN})

    def test_discovery_keeps_multiple_lamps_and_missing_ip_fallback(self):
        other_scan = {"device": "second-device", "sku": "H6056"}
        self.respond([
            (self.server, reply("scan", SCAN)),
            (self.other, reply("scan", other_scan)),
        ])
        self.assertEqual(lamp.discover({}, timeout=0.05), {
            "127.0.0.1": SCAN,
            "127.0.0.2": {**other_scan, "ip": "127.0.0.2"},
        })

    def test_status_unresolvable_host_reads_as_unreachable(self):
        with patch.object(lamp.socket, "gethostbyname",
                          side_effect=socket.gaierror("no such host")) as resolve, \
                patch.object(self.device, "_send") as send:
            self.assertIsNone(self.device.status(timeout=0.05))
        resolve.assert_called_once_with("127.0.0.1")
        send.assert_not_called()
        self.assertEqual(self.requests, [])
        self.assertIn("resolve failed", self.device.last_error)
        self.assertIn("no such host", self.device.last_error)

    def test_status_malformed_hostname_reads_as_unreachable(self):
        self.device.ip = "lamp..lan"
        rejected = UnicodeEncodeError("idna", "lamp..lan", 5, 6, "label empty")
        with patch.object(lamp.socket, "gethostbyname",
                          side_effect=rejected) as resolve, \
                patch.object(self.device, "_send") as send:
            self.assertIsNone(self.device.status(timeout=0.05))
        resolve.assert_called_once_with("lamp..lan")
        send.assert_not_called()
        self.assertEqual(self.requests, [])
        self.assertIn("resolve failed", self.device.last_error)
        self.assertIn("label empty", self.device.last_error)

    def test_status_clears_last_error_after_successful_probe(self):
        self.device.last_error = "resolve failed: stale"
        self.respond([(self.server, reply("devStatus", STATUS))])
        self.assertEqual(self.device.status(timeout=0.1), STATUS)
        self.assertIsNone(self.device.last_error)

    def test_device_json_reports_resolve_error_additively(self):
        self.assertNotIn("error", lamp._device_json("lamp", "h", None))
        self.assertEqual(
            lamp._device_json("lamp", "h", None, "resolve failed: x")["error"],
            "resolve failed: x")

    def test_status_sends_to_the_resolved_address(self):
        self.device.ip = "lamp.test"
        self.respond([(self.server, reply("devStatus", STATUS))])
        with patch.object(lamp.socket, "gethostbyname",
                          return_value="127.0.0.1") as resolve:
            self.assertEqual(self.device.status(timeout=0.1), STATUS)
        resolve.assert_called_once_with("lamp.test")
        self.assertEqual(self.requests[:1], ["devStatus"])

    def test_status_rejects_reply_from_other_than_the_resolved_address(self):
        # The name resolves to 127.0.0.1 (where the fake lamp listens), but the
        # reply comes from 127.0.0.2, so it must not be accepted.
        self.device.ip = "lamp.test"
        self.respond([(self.other, reply("devStatus", STATUS))])
        with patch.object(lamp.socket, "gethostbyname", return_value="127.0.0.1"):
            self.assertIsNone(self.device.status(timeout=0.05))
        self.assertEqual(self.requests[:1], ["devStatus"])

    def _resolve_only_loopback(self, host):
        if host != "127.0.0.1":
            raise socket.gaierror("no such host")
        return host

    def test_status_command_tells_unresolved_from_silent(self):
        cfg = {"devices": [{"name": "typo", "ip": "lamp..lan"},
                           {"name": "desk", "ip": "127.0.0.1"}]}
        self.respond([(self.server, reply("devStatus", STATUS))])
        out = io.StringIO()
        with patch.object(lamp.socket, "gethostbyname",
                          side_effect=self._resolve_only_loopback), \
                contextlib.redirect_stdout(out):
            rc = lamp.cmd_status(cfg, [])
        lines = out.getvalue().splitlines()
        self.assertEqual(rc, 0)
        typo = [l for l in lines if l.split()[:2] == ["typo", "lamp..lan"]]
        desk = [l for l in lines if l.split()[:2] == ["desk", "127.0.0.1"]]
        self.assertEqual(len(typo), 1, lines)
        self.assertEqual(len(desk), 1, lines)
        self.assertIn("UNRESOLVED (resolve failed: no such host)", typo[0])
        self.assertIn("#0a141e  on=1 brightness=42", desk[0])
        self.assertFalse([l for l in lines if "SILENT" in l], lines)
        self.assertEqual(self.requests, ["devStatus"])

    def test_json_command_carries_resolve_error_per_device(self):
        cfg = {"devices": [{"name": "typo", "ip": "lamp..lan"},
                           {"name": "desk", "ip": "127.0.0.1"}]}
        self.respond([(self.server, reply("devStatus", STATUS))])
        out = io.StringIO()
        with patch.object(lamp.socket, "gethostbyname",
                          side_effect=self._resolve_only_loopback), \
                contextlib.redirect_stdout(out):
            self.assertEqual(lamp.cmd_json(cfg, ["--all"]), 0)
        devices = json.loads(out.getvalue())["devices"]
        self.assertEqual([d["name"] for d in devices], ["typo", "desk"])
        self.assertFalse(devices[0]["reachable"])
        self.assertEqual(devices[0]["error"], "resolve failed: no such host")
        self.assertTrue(devices[1]["reachable"])
        self.assertNotIn("error", devices[1])

    def test_discovery_does_not_admit_status_only_sender(self):
        self.respond([(self.server, reply("devStatus", STATUS))])
        self.assertEqual(lamp.discover({}, timeout=0.05), {})


if __name__ == "__main__":
    unittest.main()
