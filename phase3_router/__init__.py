"""Phase 3: multi-provider router with health checks and failover."""
from phase3_router.router import Router, RouterResult
from phase3_router.providers.http_provider import HTTPProvider

__all__ = ["Router", "RouterResult", "HTTPProvider"]
