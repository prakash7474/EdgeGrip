"""Reproducible router overhead benchmark (loopback-only)."""
from __future__ import annotations

import socket
import threading
import time
import uvicorn

from phase1_pipeline.capture import FrameGrabber
from phase3_router import Router, HTTPProvider
from phase3_router.providers import mock_provider_a, mock_provider_b


def free_port():
    s = socket.socket()
    s.bind(('127.0.0.1', 0))
    p = s.getsockname()[1]
    s.close()
    return p


def run_benchmark(runs: int = 20):
    """Run overhead benchmark. loopback-only."""
    pa, pb = free_port(), free_port()
    cfg_a = uvicorn.Config(mock_provider_a.app, host='127.0.0.1', port=pa, log_level='warning')
    cfg_b = uvicorn.Config(mock_provider_b.app, host='127.0.0.1', port=pb, log_level='warning')
    sa = uvicorn.Server(cfg_a)
    sb = uvicorn.Server(cfg_b)
    threading.Thread(target=sa.run, daemon=True).start()
    threading.Thread(target=sb.run, daemon=True).start()
    time.sleep(3)
    p1 = HTTPProvider('mock_a', f'http://127.0.0.1:{pa}/predict')
    p2 = HTTPProvider('mock_b', f'http://127.0.0.1:{pb}/predict')
    router = Router([p1, p2])
    g = FrameGrabber('images', camera_enabled=False)
    router_times = []
    prov_times = []
    for i in range(runs):
        f = g.next_frame()
        t0 = time.perf_counter()
        res = router.predict(f.jpeg_bytes)
        t1 = time.perf_counter()
        router_times.append((t1-t0) * 1000.0)
        t0 = time.perf_counter()
        pr = p1.predict(f.jpeg_bytes)
        t1 = time.perf_counter()
        prov_times.append((t1-t0) * 1000.0)
    sa.should_exit = True
    sb.should_exit = True
    return {
        'runs': runs,
        'loopback_only': True,
        'router_avg_ms': sum(router_times) / len(router_times),
        'provider_avg_ms': sum(prov_times) / len(prov_times),
        'overhead_ms': (sum(router_times) / len(router_times)) - (sum(prov_times) / len(prov_times)),
    }


if __name__ == '__main__':
    print(run_benchmark(20))
