"""The compatibility gateway must flush SSE chunks before the response ends."""
from __future__ import annotations

import http.client
import importlib.util
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import ModuleType


def load_gateway_module() -> ModuleType:
    gateway_path = Path(__file__).resolve().parents[1] / "infra" / "public-demo" / "login_gateway.py"
    spec = importlib.util.spec_from_file_location("verimind_login_gateway", gateway_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load gateway module: {gateway_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


GATEWAY_MODULE = load_gateway_module()


class ChunkedEventBackend(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: object) -> None:
        return

    def do_GET(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        frames = (
            b'data: {"type":"stage","id":"s1","stage_id":"understand","label":"\xe8\xaf\x86\xe5\x88\xab\xe9\x97\xae\xe9\xa2\x98\xe8\xa6\x81\xe7\xb4\xa0","status":"started"}\n\n',
            b'data: {"type":"complete","response":{"answer":"done"}}\n\n',
        )
        for index, frame in enumerate(frames):
            self.wfile.write(f"{len(frame):X}\r\n".encode("ascii") + frame + b"\r\n")
            self.wfile.flush()
            if index == 0:
                # Keep the connection open after the first event, as real inference does.
                time.sleep(0.8)
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()


class GatewayStreamingTests(unittest.TestCase):
    def test_first_sse_frame_reaches_client_before_backend_completes(self) -> None:
        backend = ThreadingHTTPServer(("127.0.0.1", 0), ChunkedEventBackend)

        class TestGateway(GATEWAY_MODULE.Gateway):
            pass

        TestGateway.backend = f"http://127.0.0.1:{backend.server_port}"

        gateway = ThreadingHTTPServer(("127.0.0.1", 0), TestGateway)
        backend_thread = threading.Thread(target=backend.serve_forever, daemon=True)
        gateway_thread = threading.Thread(target=gateway.serve_forever, daemon=True)
        backend_thread.start()
        gateway_thread.start()
        connection = http.client.HTTPConnection("127.0.0.1", gateway.server_port, timeout=3)
        try:
            connection.request("GET", "/api/ask/stream", headers={"Accept": "text/event-stream"})
            response = connection.getresponse()
            started_at = time.monotonic()
            first_frame = response.read1(64 * 1024)
            first_frame_elapsed = time.monotonic() - started_at

            self.assertEqual(response.status, 200)
            self.assertIn(b'"type":"stage"', first_frame)
            self.assertLess(first_frame_elapsed, 0.6, "gateway buffered the first event until response completion")
            remainder = response.read()
            self.assertIn(b'"type":"complete"', first_frame + remainder)
        finally:
            connection.close()
            gateway.shutdown()
            backend.shutdown()
            gateway.server_close()
            backend.server_close()
            gateway_thread.join(timeout=2)
            backend_thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
