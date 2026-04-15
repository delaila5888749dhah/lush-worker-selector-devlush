"""Thread-safe proxy pool for per-worker proxy assignment."""

import os
import threading
from typing import Dict, List, Optional


class NoProxyAvailableError(RuntimeError):
    """Raised when no proxy is available in the pool."""


class ProxyPool:
    """Thread-safe pool that assigns proxies to workers on demand."""

    def __init__(self, proxies: Optional[List[str]] = None):
        """Initialize proxy pool from a list or PROXY_LIST_FILE env path."""
        self._lock = threading.Lock()
        self._proxies: List[str] = []
        self._assigned: Dict[str, str] = {}

        if proxies:
            self._proxies.extend(p.strip() for p in proxies if p and p.strip())
        else:
            proxy_file = os.environ.get("PROXY_LIST_FILE")
            if proxy_file:
                self.load_from_file(proxy_file)

    def acquire(self, worker_id: str) -> Optional[str]:
        """Pop and assign a proxy to worker_id, or return None if empty."""
        with self._lock:
            existing = self._assigned.get(worker_id)
            if existing is not None:
                return existing
            if not self._proxies:
                return None
            proxy = self._proxies.pop(0)
            self._assigned[worker_id] = proxy
            return proxy

    def release(self, worker_id: str) -> None:
        """Return worker's assigned proxy back to pool."""
        with self._lock:
            proxy = self._assigned.pop(worker_id, None)
            if proxy is not None:
                self._proxies.append(proxy)

    def get_assigned(self, worker_id: str) -> Optional[str]:
        """Return currently assigned proxy for worker_id."""
        with self._lock:
            return self._assigned.get(worker_id)

    def available_count(self) -> int:
        """Return count of available proxies."""
        with self._lock:
            return len(self._proxies)

    def is_available(self) -> bool:
        """Return True when at least one proxy is available."""
        with self._lock:
            return bool(self._proxies)

    def load_from_file(self, path: str) -> int:
        """Load proxies from file and return number loaded."""
        with self._lock:
            with open(path, "r", encoding="utf-8") as handle:
                loaded = [line.strip() for line in handle if line.strip()]
            self._proxies.extend(loaded)
        return len(loaded)


_default_pool: Optional[ProxyPool] = None  # pylint: disable=invalid-name
_default_pool_lock = threading.Lock()  # pylint: disable=invalid-name


def get_default_pool() -> ProxyPool:
    """Get or create the default singleton ProxyPool."""
    global _default_pool  # pylint: disable=global-statement,invalid-name
    if _default_pool is None:
        with _default_pool_lock:
            if _default_pool is None:
                _default_pool = ProxyPool()
    return _default_pool
