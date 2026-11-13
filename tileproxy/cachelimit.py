#!/usr/bin/env python3
"""Keep a tile directory under a byte cap.

Deletes the least recently used files when the directory grows past the cap,
and also when the filesystem itself runs low on free space. A young ``*.tmp``
file is left alone so an in-flight write can finish.
"""

import os
import shutil
import stat
import sys
import threading
import time
from pathlib import Path

_UNITS = (
    ("tib", 1024**4),
    ("tb", 1000**4),
    ("gib", 1024**3),
    ("gb", 1000**3),
    ("mib", 1024**2),
    ("mb", 1000**2),
    ("kib", 1024),
    ("kb", 1000),
    ("ti", 1024**4),
    ("gi", 1024**3),
    ("mi", 1024**2),
    ("ki", 1024),
    ("t", 1000**4),
    ("g", 1000**3),
    ("m", 1000**2),
    ("k", 1000),
    ("b", 1),
)


def parse_bytes(value):
    text = str(value).strip().lower().replace(" ", "")
    if not text:
        raise ValueError("empty size")
    for unit, mult in _UNITS:
        if text.endswith(unit) and text[: -len(unit)]:
            number = text[: -len(unit)]
            return int(float(number) * mult)
    return int(text)


def require_bytes(env, default):
    raw = os.environ.get(env, default)
    try:
        size = parse_bytes(raw)
    except ValueError as exc:
        raise SystemExit(f"invalid {env}: {raw}") from exc
    if size < 0:
        raise SystemExit(f"invalid {env}: {raw}")
    return size


def format_bytes(size):
    value = float(max(0, size))
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            if unit == "B":
                return f"{int(value)}B"
            return f"{value:.1f}{unit}"
        value /= 1024


def rank_by_mtime(path, st):
    return (st.st_mtime,)


def rank_tileproxy(native_ttl, upscaled_ttl, upscaled_penalty=24 * 3600):
    """Prefer dropping expired tiles, then upscaled tiles a day ahead of native ones.

    Upscaled tiles are rebuilt from the cached parent, so they are treated as
    a day older than their last use. A tile someone is looking at still wins
    over a native tile that has sat unused for longer than that.
    """

    def rank(path, st):
        if path.name.endswith(".tmp"):
            return (0, st.st_mtime)
        age = time.time() - st.st_mtime
        upscaled = "upscaled" in path.parts
        if upscaled and age > upscaled_ttl:
            return (1, st.st_atime)
        if not upscaled and age > native_ttl:
            return (2, st.st_atime)
        penalty = upscaled_penalty if upscaled else 0
        return (3, st.st_atime - penalty)

    return rank


def reclaimable(path, st, now, tmp_min_age=60):
    if path.name.endswith(".tmp"):
        return now - st.st_mtime >= tmp_min_age
    return True


def unlink_if_same(path, st):
    try:
        current = path.lstat()
    except FileNotFoundError:
        return 0
    if not stat.S_ISREG(current.st_mode):
        return 0
    if (
        current.st_mtime_ns != st.st_mtime_ns
        or current.st_atime_ns != st.st_atime_ns
        or current.st_size != st.st_size
    ):
        return 0
    try:
        path.unlink()
    except FileNotFoundError:
        return 0
    return current.st_size


class Limiter:
    def __init__(
        self,
        root,
        max_bytes,
        rank=rank_by_mtime,
        label="cache",
        min_free=1024**3,
        target_ratio=0.9,
        interval=30,
        scan_interval=300,
        skip_parts=(),
    ):
        self.root = Path(root)
        self.max_bytes = max_bytes
        self.target = int(max_bytes * target_ratio) if max_bytes > 0 else 0
        self.rank = rank
        self.label = label
        self.min_free = min_free
        self.interval = interval
        self.scan_interval = scan_interval
        self.skip_parts = frozenset(skip_parts)
        self.baseline = 0
        self.writes = 0
        self.writes_mark = 0
        self.usage = 0
        self.ready = False
        self.lock = threading.Lock()
        self.pruning = threading.Lock()
        self.wake = threading.Event()

    def manages(self):
        return self.max_bytes > 0 or self.min_free > 0

    def start(self):
        if not self.manages():
            print(f"{self.label}: cache limit disabled", flush=True)
            return
        cap = "none" if self.max_bytes <= 0 else format_bytes(self.max_bytes)
        print(
            f"{self.label}: cap {cap}, keep {format_bytes(self.min_free)} free on disk",
            flush=True,
        )
        threading.Thread(
            target=self.loop, name=f"cache-limit-{self.label}", daemon=True
        ).start()

    def stored(self, delta):
        if not delta:
            return
        with self.lock:
            self.writes += delta
            self.usage = max(0, self.baseline + self.writes)
            over = self.ready and self.max_bytes > 0 and self.usage > self.max_bytes
        if over:
            self.wake.set()

    def loop(self):
        next_scan = 0.0
        while True:
            self.wake.clear()
            try:
                now = time.monotonic()
                if not self.ready or now >= next_scan:
                    self.refresh()
                    next_scan = now + self.scan_interval
                if self.should_prune():
                    self.prune(block=False)
            except Exception as exc:
                print(f"{self.label}: cache limit failed: {exc}", flush=True)
            self.wake.wait(self.interval)

    def should_prune(self):
        with self.lock:
            over = self.ready and self.max_bytes > 0 and self.usage > self.max_bytes
        if over:
            return True
        free = self.free_bytes()
        return free is not None and self.min_free > 0 and free < self.min_free

    def refresh(self):
        if not self.pruning.acquire(blocking=False):
            return
        try:
            self._begin_scan()
            total = 0
            for path, st in self._files():
                total += st.st_size
            self._note_scan(total, 0)
        finally:
            self.pruning.release()

    def prune(self, force=False, block=True):
        if not self.pruning.acquire(blocking=block):
            return 0
        try:
            return self._prune(force)
        finally:
            self.pruning.release()

    def free_bytes(self):
        try:
            return shutil.disk_usage(self.root).free
        except OSError:
            return None

    def _files(self):
        root = self.root
        if not root.exists():
            return
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [name for name in dirnames if name not in self.skip_parts]
            for name in filenames:
                path = Path(dirpath) / name
                try:
                    st = path.lstat()
                except OSError:
                    continue
                if stat.S_ISREG(st.st_mode):
                    yield path, st

    def _begin_scan(self):
        with self.lock:
            self.writes_mark = self.writes

    def _note_scan(self, scanned, removed):
        with self.lock:
            during = self.writes - self.writes_mark
            self.baseline = max(0, scanned - removed)
            self.writes = during
            self.usage = max(0, self.baseline + self.writes)
            self.ready = True

    def _goal(self, total, free, force):
        goal = total
        reasons = []
        if self.max_bytes > 0 and total > self.max_bytes:
            goal = min(goal, self.target)
            reasons.append("over cap")
        if free is not None and self.min_free > 0 and free < self.min_free:
            goal = min(goal, total - (self.min_free - free))
            reasons.append("low disk space")
        if force and goal >= total:
            cushion = self.max_bytes // 10 if self.max_bytes > 0 else 64 * 1024 * 1024
            goal = total - max(cushion, 64 * 1024 * 1024)
            reasons.append("disk full")
        return max(0, goal), ", ".join(reasons)

    def _prune(self, force):
        self._begin_scan()
        entries = []
        total = 0
        for path, st in self._files():
            entries.append((path, st))
            total += st.st_size
        free = self.free_bytes()
        goal, reason = self._goal(total, free, force)
        removed = 0
        files = 0
        if goal < total:
            now = time.time()
            entries.sort(key=lambda item: self.rank(item[0], item[1]))
            for path, st in entries:
                if total - removed <= goal:
                    break
                if not reclaimable(path, st, now):
                    continue
                size = unlink_if_same(path, st)
                if size:
                    removed += size
                    files += 1
            if removed:
                self._remove_empty_dirs()
        self._note_scan(total, removed)
        if removed:
            noun = "file" if files == 1 else "files"
            print(
                f"{self.label}: {format_bytes(total)} -> {format_bytes(total - removed)}, "
                f"removed {files} {noun} ({reason})",
                flush=True,
            )
        return removed

    def _remove_empty_dirs(self):
        root_abs = os.path.abspath(self.root)
        for dirpath, dirnames, filenames in os.walk(root_abs, topdown=False):
            if os.path.abspath(dirpath) == root_abs:
                continue
            if self.skip_parts.intersection(Path(dirpath).parts):
                continue
            try:
                os.rmdir(dirpath)
            except OSError:
                pass


def main():
    root = Path(os.environ.get("CACHE_ROOT", "/cache"))
    max_bytes = require_bytes("CACHE_MAX_BYTES", "8GiB")
    min_free = require_bytes("CACHE_MIN_FREE", "1GiB")
    interval = int(os.environ.get("CACHE_PRUNE_INTERVAL", "30"))
    root.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(root, 0o777)
    except OSError:
        pass
    limiter = Limiter(
        root,
        max_bytes,
        label=str(root),
        min_free=min_free,
        interval=interval,
        skip_parts=frozenset({"tile_locks", "locks"}),
    )
    if not limiter.manages():
        raise SystemExit("cache limit disabled")
    cap = "none" if max_bytes <= 0 else format_bytes(max_bytes)
    print(
        f"{root}: cap {cap}, keep {format_bytes(min_free)} free on disk",
        flush=True,
    )
    limiter.loop()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
