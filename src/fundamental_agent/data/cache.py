"""轻量缓存与限流（M1 基础版，进程内）。

两个目的：
1. TTLCache  —— 避免短时间内重复打外部接口（如二次生成同一公司），
                既省钱也规避 AkShare 上游的频次风控。
2. throttle  —— 全局最小请求间隔，串行化外部请求，规避被封 IP。

二期可替换为 Redis 缓存 + 令牌桶，接口形态不变。
"""
from __future__ import annotations

import threading
import time
from typing import Callable


_cache: dict[str, tuple[float, object]] = {}
_lock = threading.Lock()

_MIN_INTERVAL = 0.25  # 外部请求最小间隔（秒）
_last_call = 0.0
_rl_lock = threading.Lock()


def ttl_cache(ttl: float):
    """方法装饰器：按 `函数名:code` 缓存返回值，ttl 秒内命中直接返回。"""
    def deco(fn: Callable) -> Callable:
        def wrapper(self, code: str = "", *args, **kwargs):
            key = f"{fn.__qualname__}:{code}"
            now = time.time()
            with _lock:
                hit = _cache.get(key)
                if hit and (now - hit[0]) < ttl:
                    return hit[1]
            val = fn(self, code, *args, **kwargs)
            with _lock:
                _cache[key] = (time.time(), val)
            return val
        return wrapper
    return deco


def throttle(interval: float = _MIN_INTERVAL):
    """装饰器：保证两次外部请求之间至少间隔 interval 秒（全局串行）。"""
    def deco(fn: Callable) -> Callable:
        def wrapper(*args, **kwargs):
            global _last_call
            with _rl_lock:
                wait = interval - (time.time() - _last_call)
                if wait > 0:
                    time.sleep(wait)
                _last_call = time.time()
            return fn(*args, **kwargs)
        return wrapper
    return deco
