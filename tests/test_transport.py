"""LAN reply regression tests: loopback only, no lamps or third-party packages."""

import importlib.machinery
import importlib.util
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
                def send(_payload):
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
                          side_effect=socket.gaierror("no such host")):
            self.assertIsNone(self.device.status(timeout=0.05))
        self.assertEqual(self.requests, [])

    def test_discovery_does_not_admit_status_only_sender(self):
        self.respond([(self.server, reply("devStatus", STATUS))])
        self.assertEqual(lamp.discover({}, timeout=0.05), {})


if __name__ == "__main__":
    unittest.main()
