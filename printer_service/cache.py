import threading
import time

# Image cache: (url, width, height) -> (monotonic_time, (label_img_L, tsc_bitmap_bytes))
CACHE_TTL = 300  # seconds
_cache: dict[tuple, tuple[float, tuple]] = {}
_cache_lock = threading.Lock()


def cache_get(key: tuple) -> tuple | None:
    with _cache_lock:
        entry = _cache.get(key)
    if entry and time.monotonic() - entry[0] < CACHE_TTL:
        return entry[1]
    return None


def cache_set(key: tuple, value: tuple) -> None:
    with _cache_lock:
        _cache[key] = (time.monotonic(), value)
