"""
In-Memory Fast Cache Service for Low-Latency AI Receptionist.

Caches safe, frequently accessed organization data:
- Organization Profile (name, address, phone, business hours)
- Verified Staff & Doctor List
- Department List & Services Catalog
- Active KB Metadata

Provides thread-safe TTL storage and immediate invalidation methods.
"""

import time
import logging
import threading
from typing import Any, Dict, Optional, List

logger = logging.getLogger(__name__)

DEFAULT_TTL_SECONDS = 300  # 5 minutes default TTL


class CacheService:
    def __init__(self, default_ttl: float = DEFAULT_TTL_SECONDS):
        self.default_ttl = default_ttl
        self._store: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.Lock()

    def get(self, key: str) -> Optional[Any]:
        """Retrieve cached item if present and not expired."""
        with self._lock:
            entry = self._store.get(key)
            if not entry:
                return None
            if time.time() > entry["expires_at"]:
                del self._store[key]
                return None
            return entry["data"]

    def set(self, key: str, value: Any, ttl: Optional[float] = None) -> None:
        """Store item with explicit or default TTL."""
        ttl_val = ttl if ttl is not None else self.default_ttl
        expires_at = time.time() + ttl_val
        with self._lock:
            self._store[key] = {
                "data": value,
                "expires_at": expires_at
            }

    def delete(self, key: str) -> None:
        """Explicitly remove an entry from cache."""
        with self._lock:
            self._store.pop(key, None)

    def invalidate_organization(self, organization_id: int = 1) -> None:
        """Invalidate all cached entries for a given organization."""
        prefix1 = f"org_{organization_id}_"
        prefix2 = f"fast_resp_{organization_id}_"
        with self._lock:
            keys_to_delete = [k for k in self._store if k.startswith(prefix1) or k.startswith(prefix2)]
            for k in keys_to_delete:
                del self._store[k]
        logger.info(f"[CACHE INVALIDATION] Cleared {len(keys_to_delete)} keys for org_{organization_id}")

    def clear_all(self) -> None:
        """Flush the entire cache."""
        with self._lock:
            self._store.clear()
        logger.info("[CACHE CLEAR] In-memory cache cleared.")

    def prewarm_cache(self, organization_id: int = 1) -> None:
        """Dynamically prewarm organization profile and services from active RAG context."""
        try:
            from src.services.rag_service import rag_service
            org_name = rag_service.get_organization_name(organization_id) or ""
            master_ctx = rag_service.get_organization_master_context(organization_id)
            
            org_profile = {
                "name": org_name,
                "context_preview": master_ctx[:200] if master_ctx else ""
            }
            self.set(f"org_{organization_id}_profile", org_profile, ttl=1800)
        except Exception as e:
            logger.debug(f"Cache prewarm note: {e}")

    def get_organization_profile(self, organization_id: int = 1) -> Dict[str, Any]:
        val = self.get(f"org_{organization_id}_profile")
        if not val:
            self.prewarm_cache(organization_id)
            val = self.get(f"org_{organization_id}_profile")
        return val or {}

    def get_service_catalog(self, organization_id: int = 1) -> List[str]:
        val = self.get(f"org_{organization_id}_services")
        if not val:
            try:
                from src.services.rag_service import rag_service
                master_ctx = rag_service.get_organization_master_context(organization_id)
                import re
                services = []
                if master_ctx:
                    service_lines = re.findall(r'(?m)^[-*•]\s+([^\n\r]+)', master_ctx)
                    services = [s.strip() for s in service_lines if len(s.strip()) < 50][:15]
                if services:
                    self.set(f"org_{organization_id}_services", services, ttl=1800)
                    return services
            except Exception:
                pass
        return val or []


cache_service = CacheService()

