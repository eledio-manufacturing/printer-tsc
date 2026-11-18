import threading
import time

# Image cache: (url, width, height) -> (monotonic_time, (label_img_L, tsc_bitmap_bytes))
CACHE_TTL = 5  # seconds
_cache: dict[tuple, tuple[float, tuple]] = {}
_cache_lock = threading.Lock()


def cache_get(key: tuple) -> tuple | None:
    with _cache_lock:
        entry = _cache.get(key)
    if entry and time.monotonic() - entry[0] < CACHE_TTL:
        return entry[1]
    return None


def cache_set(key: tuple, value: tuple) -> None:
    now = time.monotonic()
    with _cache_lock:
        # cache_get only ignores expired entries, it never removes them -- without this
        # sweep every unique URL kept its image + bitmap (~hundreds of KB) in memory for
        # the life of the process. With a 5 s TTL only a handful of entries survive.
        for k in [k for k, (t, _) in _cache.items() if now - t >= CACHE_TTL]:
            del _cache[k]
        _cache[key] = (now, value)
