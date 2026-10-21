"""Loopback-only compatibility proxy; account authentication belongs to FastAPI."""
from __future__ import annotations

import argparse
import http.client
import ipaddress
import socket
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit


class Gateway(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    backend = "http://127.0.0.1:8000"

    def log_message(self, fmt: str, *args: object) -> None:
        # Never log request cookies or other authentication headers.
        return

    def do_GET(self) -> None:
        self._proxy()

    def do_HEAD(self) -> None:
        self._proxy(head_only=True)

    def do_POST(self) -> None:
        self._proxy()

    def do_PUT(self) -> None:
        self._proxy()

    def do_PATCH(self) -> None:
        self._proxy()

    def do_DELETE(self) -> None:
        self._proxy()

    def do_OPTIONS(self) -> None:
        self._proxy()

    def _error(self, status: int, message: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(message)))
        self.send_header("Connection", "close")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(message)
        self.close_connection = True

    def _proxy(self, *, head_only: bool = False) -> None:
        target = urlsplit(self.backend)
        if target.scheme != "http" or not target.hostname or not target.port:
            self._error(502, b'{"detail":"invalid backend configuration"}')
            return
        try:
            address = ipaddress.ip_address(socket.gethostbyname(target.hostname))
            if not address.is_loopback:
                self._error(403, b'{"detail":"backend must be local"}')
                return
        except (OSError, ValueError):
            self._error(502, b'{"detail":"backend unavailable"}')
            return

        try:
            length = int(self.headers.get("Content-Length", "0") or "0")
        except ValueError:
            self._error(400, b'{"detail":"invalid content length"}')
            return
        if length < 0 or length > 32 * 1024 * 1024:
            self._error(413, b'{"detail":"request body too large"}')
            return
        body = self.rfile.read(length) if length else None
        headers = {
            name: self.headers[name]
            for name in (
                "Accept", "Content-Type", "Cookie", "Origin", "Range",
                "If-None-Match", "If-Modified-Since", "X-Session-ID",
                "X-Model-Profile",
            )
            if self.headers.get(name)
        }
        incoming_host = self.headers.get("Host")
        if incoming_host:
            headers["X-Forwarded-Host"] = incoming_host
        headers["X-Forwarded-Proto"] = self.headers.get("X-Forwarded-Proto", "http")
        path = self.path if self.path.startswith("/") else "/"
        connection = http.client.HTTPConnection(target.hostname, target.port, timeout=600)
        headers_sent = False
        try:
            connection.request(self.command, path, body=body, headers=headers)
            response = connection.getresponse()
            self.send_response(response.status, response.reason)
            for name in (
                "Content-Type", "Cache-Control", "Content-Disposition", "Set-Cookie",
                "Last-Modified", "ETag", "Accept-Ranges", "Content-Range",
            ):
                value = response.getheader(name)
                if value:
                    self.send_header(name, value)
            content_length = response.getheader("Content-Length")
            chunked = content_length is None and not head_only and response.status not in {204, 304}
            if content_length is not None:
                self.send_header("Content-Length", content_length)
            elif chunked:
                self.send_header("Transfer-Encoding", "chunked")
            self.send_header("Connection", "close")
            self.end_headers()
            headers_sent = True
            if not head_only and response.status not in {204, 304}:
                # read(amt) waits for amt bytes on chunked responses. For the
                # small SSE frames from /api/ask/stream that can mean buffering
                # every progress event until the answer finishes. read1() reads
                # at most one underlying chunk so each event can be forwarded.
                while block := response.read1(64 * 1024):
                    if chunked:
                        self.wfile.write(f"{len(block):X}\r\n".encode("ascii"))
                        self.wfile.write(block + b"\r\n")
                    else:
                        self.wfile.write(block)
                    self.wfile.flush()
                if chunked:
                    self.wfile.write(b"0\r\n\r\n")
                    self.wfile.flush()
        except (OSError, http.client.HTTPException):
            if not headers_sent:
                try:
                    self._error(502, b'{"detail":"backend unavailable"}')
                except OSError:
                    pass
            else:
                self.close_connection = True
        finally:
            connection.close()
            self.close_connection = True


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a loopback proxy; FastAPI handles user login.")
    parser.add_argument("--port", type=int, default=8011)
    parser.add_argument("--backend", default="http://127.0.0.1:8000")
    args = parser.parse_args()
    Gateway.backend = args.backend
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Gateway)
    print(f"application proxy on 127.0.0.1:{args.port} -> {args.backend}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
