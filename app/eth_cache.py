"""
Caché ligero para el bot ETH.

Usa Redis si está habilitado y disponible; si no, degrada silenciosamente a un
caché en memoria del proceso. Nunca lanza excepciones al caller: ante cualquier
fallo devuelve None (miss) para que el runner descargue datos frescos.
"""

import json
import logging
import time

from app import config

logger = logging.getLogger(__name__)

_redis_client = None
_redis_tried = False
_mem_cache: dict[str, tuple[float, str]] = {}   # key -> (expires_at, value)


def _get_redis():
    global _redis_client, _redis_tried
    if not config.REDIS_ENABLED:
        return None
    if _redis_tried:
        return _redis_client
    _redis_tried = True
    try:
        import redis  # type: ignore
        _redis_client = redis.Redis.from_url(config.REDIS_URL, decode_responses=True)
        _redis_client.ping()
        logger.info("[CACHE] Redis conectado en %s", config.REDIS_URL)
    except Exception as exc:
        logger.warning("[CACHE] Redis no disponible (%s) — usando caché en memoria", exc)
        _redis_client = None
    return _redis_client


def cache_set(key: str, value, ttl_seconds: int) -> None:
    """Guarda `value` (serializable a JSON) bajo `key` con expiración `ttl_seconds`."""
    try:
        payload = json.dumps(value, default=str)
    except Exception:
        return
    client = _get_redis()
    if client is not None:
        try:
            client.setex(key, ttl_seconds, payload)
            return
        except Exception as exc:
            logger.warning("[CACHE] set falló en Redis: %s", exc)
    _mem_cache[key] = (time.time() + ttl_seconds, payload)


def cache_get(key: str):
    """Devuelve el valor cacheado o None si no existe / expiró."""
    client = _get_redis()
    if client is not None:
        try:
            raw = client.get(key)
            return json.loads(raw) if raw else None
        except Exception as exc:
            logger.warning("[CACHE] get falló en Redis: %s", exc)
    entry = _mem_cache.get(key)
    if not entry:
        return None
    expires_at, payload = entry
    if time.time() > expires_at:
        _mem_cache.pop(key, None)
        return None
    try:
        return json.loads(payload)
    except Exception:
        return None
