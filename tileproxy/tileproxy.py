#!/usr/bin/env python3
"""Serve Google tiles, filling gaps from the next lower zoom.

Google's satellite endpoint returns 404 where it has no native tile:
open water, and many places once the zoom is high enough. MapProxy
turns that 404 into an HTTP 500, so a layer that looks fine over
covered land fails as soon as the view includes a gap. This proxy
answers the gap with the parent tile cropped to the missing cell.
"""

import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from PIL import Image

HOSTS = ("mt0", "mt1", "mt2", "mt3")
LAYERS = {"s", "y", "m"}
CACHE = Path(os.environ.get("TILE_CACHE", "/cache"))
PORT = int(os.environ.get("PORT", "8090"))
UPSTREAM_TIMEOUT = 8
MAX_ZOOM = 22
NATIVE_TTL = 30 * 24 * 3600
UPSCALED_TTL = 12 * 3600
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)

upstream_slots = threading.BoundedSemaphore(4)
locks_guard = threading.Lock()
tile_locks = {}


class Missing(Exception):
    pass


class Upstream(Exception):
    pass


def get_lock(key):
    with locks_guard:
        lock = tile_locks.get(key)
        if lock is None:
            if len(tile_locks) > 20000:
                tile_locks.clear()
            lock = threading.Lock()
            tile_locks[key] = lock
        return lock


def cache_file(kind, lyrs, z, x, y):
    return CACHE / kind / lyrs / str(z) / str(x) / f"{y}.img"


def read_fresh(path, ttl):
    try:
        age = time.time() - path.stat().st_mtime
    except FileNotFoundError:
        return None
    if age > ttl:
        return None
    data = path.read_bytes()
    return data or None


def write_atomic(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def looks_like_image(data, content_type):
    if data.startswith(b"\xff\xd8\xff") or data.startswith(b"\x89PNG\r\n\x1a\n") or data.startswith(b"GIF8"):
        return True
    return content_type.startswith("image/") and len(data) > 32


def fetch_network(lyrs, z, x, y):
    start = (x + y + z) % len(HOSTS)
    order = HOSTS[start:] + HOSTS[:start]
    saw_missing = False
    last_error = None
    for host in order:
        url = f"https://{host}.google.com/vt/lyrs={lyrs}&x={x}&y={y}&z={z}"
        req = Request(url, headers={"User-Agent": UA})
        try:
            with upstream_slots:
                with urlopen(req, timeout=UPSTREAM_TIMEOUT) as resp:
                    data = resp.read()
                    content_type = resp.headers.get("Content-Type", "")
        except HTTPError as exc:
            if exc.code == 404:
                saw_missing = True
                continue
            last_error = exc
            continue
        except (URLError, TimeoutError, OSError) as exc:
            last_error = exc
            continue
        if looks_like_image(data, content_type):
            return data
        last_error = Upstream(f"{host} returned {content_type or 'empty'}")
    if saw_missing:
        raise Missing()
    raise Upstream(str(last_error or "no response"))


def upscale_quadrant(parent, x, y):
    image = Image.open(BytesIO(parent))
    keep_alpha = image.mode in ("RGBA", "LA") or (
        image.mode == "P" and "transparency" in image.info
    )
    image = image.convert("RGBA" if keep_alpha else "RGB")
    width, height = image.size
    sub_x = x & 1
    sub_y = y & 1
    tile_w = max(1, width // 2)
    tile_h = max(1, height // 2)
    crop = image.crop(
        (
            sub_x * tile_w,
            sub_y * tile_h,
            sub_x * tile_w + tile_w,
            sub_y * tile_h + tile_h,
        )
    )
    crop = crop.resize((256, 256), Image.Resampling.BILINEAR)
    buf = BytesIO()
    if keep_alpha:
        crop.save(buf, format="PNG")
    else:
        crop.save(buf, format="JPEG", quality=82)
    return buf.getvalue()


def resolve_tile(lyrs, z, x, y, depth=0):
    key = (lyrs, z, x, y)
    with get_lock(key):
        native = read_fresh(cache_file("native", lyrs, z, x, y), NATIVE_TTL)
        if native:
            return native
        upscaled = read_fresh(cache_file("upscaled", lyrs, z, x, y), UPSCALED_TTL)
        if upscaled:
            return upscaled
        try:
            data = fetch_network(lyrs, z, x, y)
        except Missing:
            if z == 0 or depth >= MAX_ZOOM:
                raise
            parent = resolve_tile(lyrs, z - 1, x // 2, y // 2, depth + 1)
            data = upscale_quadrant(parent, x, y)
            write_atomic(cache_file("upscaled", lyrs, z, x, y), data)
            print(f"filled {lyrs}/{z}/{x}/{y} from z{z - 1}", flush=True)
            return data
        write_atomic(cache_file("native", lyrs, z, x, y), data)
        return data


def content_type(data):
    if data.startswith(b"\x89PNG"):
        return "image/png"
    if data.startswith(b"GIF8"):
        return "image/gif"
    return "image/jpeg"


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        if args and str(args[1]).startswith(("4", "5")):
            print(f"{self.address_string()} {fmt % args}", flush=True)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in ("/health", "/health/"):
            self.respond(200, b"ok", "text/plain")
            return
        parts = path.strip("/").split("/")
        if len(parts) != 4 or parts[0] not in LAYERS:
            self.respond(404, b"not found", "text/plain")
            return
        lyrs, z_text, x_text, y_text = parts
        y_text = y_text.split(".", 1)[0]
        try:
            z, x, y = int(z_text), int(x_text), int(y_text)
        except ValueError:
            self.respond(400, b"bad coordinates", "text/plain")
            return
        if not 0 <= z <= MAX_ZOOM:
            self.respond(400, b"bad zoom", "text/plain")
            return
        limit = 1 << z
        if not (0 <= x < limit and 0 <= y < limit):
            self.respond(400, b"out of range", "text/plain")
            return
        try:
            data = resolve_tile(lyrs, z, x, y)
        except Missing:
            self.respond(404, b"missing", "text/plain")
            return
        except Upstream as exc:
            print(f"upstream {lyrs}/{z}/{x}/{y}: {exc}", flush=True)
            self.respond(502, b"upstream", "text/plain")
            return
        except Exception as exc:
            print(f"error {lyrs}/{z}/{x}/{y}: {exc}", flush=True)
            self.respond(500, b"error", "text/plain")
            return
        self.respond(200, data, content_type(data))

    def respond(self, status, body, mime):
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main():
    CACHE.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"tileproxy listening on {PORT}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
