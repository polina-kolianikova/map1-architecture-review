"""Serve scoped dashboard snapshots while one background reader refreshes them."""
from __future__ import annotations

import hashlib
import json
import math
import logging
import secrets
import time
import threading
from contextvars import ContextVar
from functools import lru_cache

from fastapi import HTTPException
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.core.config import get_settings
from app.db.session import SessionLocal

FRESH_SECONDS = 120
MAX_STALE_SECONDS = 3600
REFRESH_LOCK_KEY = "map1:dashboard-summary:v3:refresh-lock"
REFRESH_LOCK_SECONDS = 90
_logger = logging.getLogger(__name__)
_REFRESHING = ContextVar("dashboard_summary_refreshing", default=False)


def refreshing_summary() -> bool:
    return _REFRESHING.get()


@lru_cache(maxsize=1)
def _client():
    from redis import Redis

    return Redis.from_url(
        get_settings().stage4_queue_url,
        socket_connect_timeout=2,
        socket_timeout=3,
        decode_responses=True,
    )


def _scope(region_code, grid_version):
    settings = get_settings()
    region_code = (region_code or settings.active_region_code).strip()
    grid_version = (grid_version or settings.active_grid_version).strip()
    with SessionLocal() as db:
        region = db.execute(text(
            "SELECT id, metadata FROM regions WHERE code=:region"
        ), {"region": region_code}).mappings().one_or_none()
        if region is None:
            raise HTTPException(status_code=404, detail="Region scope not found")
        grid = db.execute(text("""
            SELECT id, inserted_cells, finished_at
            FROM stage3_grid_generation_runs
            WHERE region_id=:region AND grid_version=:grid AND status='completed'
              AND inserted_cells IS NOT NULL
            ORDER BY finished_at DESC NULLS LAST, id DESC LIMIT 1
        """), {"region": region["id"], "grid": grid_version}).mappings().one_or_none()
    # Small, manually seeded scopes keep the existing synchronous API path.
    if grid is None or int(grid["inserted_cells"] or 0) < 10000:
        return None
    identity = {
        "version": 3,
        "region_code": region_code,
        "grid_version": grid_version,
        "region_metadata": region["metadata"],
        "generation": dict(grid),
        "partitioned_evidence": settings.stage3_partitioned_evidence_reads,
        "partitioned_grid": settings.stage3_v10_partitioned_grid_reads,
    }
    digest = hashlib.sha256(json.dumps(identity, sort_keys=True, default=str).encode()).hexdigest()
    return {"key": "map1:dashboard-summary:v3:" + digest,
            "region_code": region_code, "grid_version": grid_version,
            "grid_cells": int(grid["inserted_cells"])}


def _refresh(scope, loader, token):
    client = _client()
    stopped = threading.Event()

    def renew_lease():
        while not stopped.wait(20):
            try:
                renewed = client.eval(
                    "if redis.call('GET',KEYS[1])==ARGV[1] then return redis.call('EXPIRE',KEYS[1],ARGV[2]) end return 0",
                    1, REFRESH_LOCK_KEY, token, REFRESH_LOCK_SECONDS,
                )
                if not renewed:
                    return
            except Exception:
                return

    lease = threading.Thread(target=renew_lease, daemon=True)
    lease.start()
    marker = _REFRESHING.set(True)
    started = time.time()
    try:
        value = loader()
        if (value.get("region_code") != scope["region_code"]
                or value.get("grid_version") != scope["grid_version"]
                or value.get("grid_cells") != scope["grid_cells"]):
            raise ValueError("Dashboard summary scope changed during refresh")
        current = _scope(scope["region_code"], scope["grid_version"])
        if (current is None or current["key"] != scope["key"]
                or client.get(REFRESH_LOCK_KEY) != token):
            return
        client.set(scope["key"] + ":retry", "", ex=1)
        client.set(scope["key"], json.dumps({
            "value": jsonable_encoder(value), "updated_at": started, "refreshed_at": time.time(),
        }, allow_nan=False), ex=86400)
    except Exception as error:
        _logger.warning("Dashboard summary refresh failed (%s)", type(getattr(error, "orig", error)).__name__)
        try:
            if client.get(REFRESH_LOCK_KEY) == token:
                client.set(scope["key"] + ":retry", json.dumps({"type": type(getattr(error, "orig", error)).__name__, "at": time.time()}), ex=60)
        except Exception:
            pass
    finally:
        stopped.set()
        lease.join(timeout=4)
        _REFRESHING.reset(marker)
        try:
            client.eval(
                "if redis.call('GET',KEYS[1])==ARGV[1] then return redis.call('DEL',KEYS[1]) end return 0",
                1, REFRESH_LOCK_KEY, token,
            )
        except Exception:
            pass


def cached_summary(region_code, grid_version, background_tasks, loader):
    scope = _scope(region_code, grid_version)
    if scope is None:
        return loader()
    client = _client()
    try:
        raw = client.get(scope["key"])
        try:
            cached = json.loads(raw) if raw else None
            if cached is not None and (not isinstance(cached, dict) or not isinstance(cached.get("value"), dict)
                           or not isinstance(cached.get("updated_at"), (int, float))
                           or not math.isfinite(cached["updated_at"])
                           or not isinstance(cached.get("refreshed_at", cached["updated_at"]), (int, float))
                           or not math.isfinite(cached.get("refreshed_at", cached["updated_at"]))
                           or cached["updated_at"] > time.time() + 5):
                cached = None
        except (ValueError, TypeError):
            cached = None
        age = max(0, time.time() - cached["updated_at"]) if cached else None
        refresh_age = max(0, time.time() - cached.get("refreshed_at", cached["updated_at"])) if cached else None
        if cached and (cached["value"].get("region_code") != scope["region_code"]
                       or cached["value"].get("grid_version") != scope["grid_version"]
                       or cached["value"].get("grid_cells") != scope["grid_cells"]):
            cached, age, refresh_age = None, None, None
        failed = bool(client.get(scope["key"] + ":retry"))
        if (refresh_age is None or refresh_age >= FRESH_SECONDS) and not failed:
            token = secrets.token_hex(16)
            if client.set(REFRESH_LOCK_KEY, token, nx=True, ex=REFRESH_LOCK_SECONDS):
                background_tasks.add_task(_refresh, scope, loader, token)
    except Exception as error:
        raise HTTPException(status_code=503, detail="Dashboard summary is temporarily unavailable") from error
    if cached is None or age > MAX_STALE_SECONDS:
        if failed:
            raise HTTPException(status_code=503, detail="Dashboard summary could not be refreshed. Retry shortly.")
        return JSONResponse(status_code=202, content={
            "status": "preparing", "region_code": scope["region_code"],
            "grid_version": scope["grid_version"], "retry_after_seconds": 10,
        })
    return {**cached["value"], "summary_updated_at": cached["updated_at"],
            "summary_age_seconds": round(age), "summary_refreshing": refresh_age >= FRESH_SECONDS,
            "summary_refresh_failed": failed}
