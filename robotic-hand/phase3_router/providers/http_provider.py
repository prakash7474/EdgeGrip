"""HTTP provider with shared session and separate timeouts."""
from __future__ import annotations

import time

import requests
from requests.adapters import HTTPAdapter

from phase1_pipeline.client import CloudClient
from phase3_router.providers.base import ProviderResult


class HTTPProvider:
    def __init__(self, name: str, endpoint: str, timeout: float = 10.0,
                 connect_timeout: float | None = None, read_timeout: float | None = None,
                 api_key: str = "", session: requests.Session | None = None,
                 pool_connections: int = 50, pool_maxsize: int = 50):
        self.name = name
        self.endpoint = endpoint
        self.timeout = timeout
        if connect_timeout is None and read_timeout is None:
            self.connect_timeout = float(timeout)
            self.read_timeout = float(timeout)
        else:
            self.connect_timeout = float(connect_timeout) if connect_timeout is not None else float(timeout)
            self.read_timeout = float(read_timeout) if read_timeout is not None else float(timeout)
        self.api_key = api_key
        self._session = session or requests.Session()
        try:
            adapter = HTTPAdapter(pool_connections=pool_connections, pool_maxsize=pool_maxsize)
            self._session.mount('http://', adapter)
            self._session.mount('https://', adapter)
        except Exception:
            pass
        self.client = CloudClient(endpoint, timeout_seconds=(self.connect_timeout, self.read_timeout), api_key=api_key, session=self._session)

    def health(self) -> bool:
        try:
            base = self.endpoint.rsplit("/predict", 1)[0]
            try:
                r = self._session.get(f"{base}/health", timeout=(self.connect_timeout, min(self.read_timeout, 2.0)))
                if r.status_code == 200:
                    return True
            except Exception:
                pass
            return True
        except Exception:
            return False

    def predict(self, jpeg_bytes: bytes) -> ProviderResult:
        t0 = time.perf_counter()
        res = self.client.predict(jpeg_bytes, image_name="router")
        latency_ms = round((time.perf_counter() - t0) * 1000.0, 3)
        if res.ok and res.label:
            return ProviderResult(ok=True, label=res.label, latency_ms=res.latency_ms, provider_name=self.name, status_code=res.status_code)
        return ProviderResult(ok=False, latency_ms=res.latency_ms, error=res.error_message or res.error_kind, provider_name=self.name, status_code=res.status_code)
