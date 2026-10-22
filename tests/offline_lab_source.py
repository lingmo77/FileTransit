"""Controlled HTTP/tracker fixtures for the isolated Docker acceptance project only."""
import hashlib
import socket
import struct
import time
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


def encode(value):
    if isinstance(value, int):
        return b"i" + str(value).encode() + b"e"
    if isinstance(value, bytes):
        return str(len(value)).encode() + b":" + value
    if isinstance(value, list):
        return b"l" + b"".join(encode(v) for v in value) + b"e"
    return b"d" + b"".join(encode(k) + encode(v) for k, v in sorted(value.items())) + b"e"


root = Path("/fixtures")
root.mkdir(exist_ok=True)
payload = root / "fixture"
payload.mkdir(exist_ok=True)
contents = [("one.bin", bytes(range(256)) * 512), ("two.bin", b"offline-fixture-data" * 4096)]
for name, content in contents:
    (payload / name).write_bytes(content)
joined = b"".join(content for _, content in contents)
info = {b"name": b"fixture", b"piece length": 16384,
        b"pieces": b"".join(hashlib.sha1(joined[i:i+16384]).digest() for i in range(0, len(joined), 16384)),
        b"files": [{b"path": [name.encode()], b"length": len(content)} for name, content in contents]}
torrent = encode({b"announce": b"http://source:8001/announce", b"info": info})
(root / "fixture.torrent").write_bytes(torrent)
(root / "infohash.txt").write_text(hashlib.sha1(encode(info)).hexdigest())


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/announce"):
            try:
                peer = socket.inet_aton(socket.gethostbyname("seed")) + struct.pack("!H", 16883)
            except OSError:
                peer = b""
            body = encode({b"interval": 2, b"complete": 1, b"incomplete": 1, b"peers": peer})
        elif self.path == "/fixture.torrent":
            body = torrent
        elif self.path == "/single.bin":
            body = contents[0][1]
        elif self.path == "/slow.bin":
            body = b"s" * (4 * 1024**2)
        elif self.path == "/redirect-private":
            self.send_response(302)
            self.send_header("Location", "http://source:8002/single.bin")
            self.end_headers()
            return
        else:
            self.send_error(404)
            return
        start, end = 0, len(body)-1
        range_value = self.headers.get("Range", "")
        if range_value.startswith("bytes="):
            first, _, last = range_value[6:].partition("-")
            start, end = int(first or 0), int(last or end)
        self.send_response(206 if range_value else 200)
        self.send_header("Content-Length", str(end-start+1))
        self.send_header("Accept-Ranges", "bytes")
        if range_value:
            self.send_header("Content-Range", f"bytes {start}-{end}/{len(body)}")
        self.end_headers()
        for offset in range(start, end+1, 65536):
            self.wfile.write(body[offset:min(offset+65536, end+1)])
            if self.path == "/slow.bin":
                self.wfile.flush()
                time.sleep(.2)

    def log_message(self, *_args):
        pass


threading.Thread(target=ThreadingHTTPServer(("0.0.0.0", 8002), Handler).serve_forever, daemon=True).start()
ThreadingHTTPServer(("0.0.0.0", 8001), Handler).serve_forever()
