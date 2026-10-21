"""Loopback bridge for Cloudflare Tunnel on networks requiring HTTP CONNECT."""
from __future__ import annotations

import argparse
import asyncio
import socket
import struct

EDGE_HOST = "region1.v2.argotunnel.com"
DEFAULT_EDGE_PORT = 7844


async def pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while data := await reader.read(64 * 1024):
            writer.write(data)
            await writer.drain()
    finally:
        writer.close()
        await writer.wait_closed()


async def tunnel_client(
    client_reader: asyncio.StreamReader,
    client_writer: asyncio.StreamWriter,
    proxy_host: str,
    proxy_port: int,
    edge_port: int,
) -> None:
    try:
        proxy_reader, proxy_writer = await asyncio.wait_for(
            asyncio.open_connection(proxy_host, proxy_port), timeout=8
        )
        proxy_writer.write(
            f"CONNECT {EDGE_HOST}:{edge_port} HTTP/1.1\r\n"
            f"Host: {EDGE_HOST}:{edge_port}\r\n"
            "Proxy-Connection: Keep-Alive\r\n\r\n".encode()
        )
        await proxy_writer.drain()
        header = await asyncio.wait_for(proxy_reader.readuntil(b"\r\n\r\n"), timeout=8)
        if not header.startswith(b"HTTP/1.1 200") and not header.startswith(b"HTTP/1.0 200"):
            raise ConnectionError(f"HTTP proxy rejected CONNECT: {header[:120]!r}")
        await asyncio.gather(pipe(client_reader, proxy_writer), pipe(proxy_reader, client_writer))
    except (OSError, asyncio.TimeoutError, asyncio.IncompleteReadError, ConnectionError):
        client_writer.close()
        await client_writer.wait_closed()


def dns_response(query: bytes) -> bytes | None:
    if len(query) < 12:
        return None
    if struct.unpack("!H", query[4:6])[0] != 1:
        return None
    cursor = 12
    labels: list[str] = []
    while cursor < len(query):
        length = query[cursor]
        cursor += 1
        if length == 0:
            break
        if length & 0xC0 or cursor + length > len(query):
            return None
        labels.append(query[cursor : cursor + length].decode("ascii", "ignore"))
        cursor += length
    if cursor + 4 > len(query):
        return None
    question_end = cursor + 4
    name = ".".join(labels).lower()
    question = query[12:question_end]
    if name not in {"region1.v2.argotunnel.com", "region2.v2.argotunnel.com"}:
        return query[:2] + struct.pack("!H", 0x8183) + query[4:6] + b"\0\0\0\0\0\0" + question
    answer = b"\xc0\x0c" + struct.pack("!HHIH4s", 1, 1, 30, 4, socket.inet_aton("127.0.0.1"))
    return query[:2] + struct.pack("!H", 0x8180) + struct.pack("!HHHH", 1, 1, 0, 0) + question + answer


class DnsProtocol(asyncio.DatagramProtocol):
    transport: asyncio.DatagramTransport | None = None

    def datagram_received(self, data: bytes, address: tuple[str, int]) -> None:
        response = dns_response(data)
        if response is not None and self.transport is not None:
            self.transport.sendto(response, address)

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = transport  # type: ignore[assignment]


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--proxy-host", default="127.0.0.1")
    parser.add_argument("--proxy-port", type=int, default=7897)
    parser.add_argument("--tcp-port", type=int, default=7844)
    parser.add_argument("--edge-port", type=int, default=DEFAULT_EDGE_PORT)
    parser.add_argument("--dns-port", type=int, default=5353)
    args = parser.parse_args()
    loop = asyncio.get_running_loop()
    server = await asyncio.start_server(
        lambda reader, writer: tunnel_client(reader, writer, args.proxy_host, args.proxy_port, args.edge_port),
        "127.0.0.1",
        args.tcp_port,
    )
    await loop.create_datagram_endpoint(DnsProtocol, local_addr=("127.0.0.1", args.dns_port))
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
