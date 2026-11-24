"""Thin, typed client for Supabase PostgREST + Storage built on httpx.

Why not supabase-py? We need explicit control over pagination, retries, upsert
headers and key handling, and fewer transitive dependencies in the API image.
"""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Iterator, Mapping, Sequence
from typing import Any

import httpx

log = logging.getLogger(__name__)

RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}


class SupabaseError(RuntimeError):
    def __init__(self, status: int, method: str, path: str, body: str) -> None:
        super().__init__(f"Supabase {method} {path} -> HTTP {status}: {body[:500]}")
        self.status = status


def auth_headers(key: str) -> dict[str, str]:
    """New `sb_secret_...` keys go in `apikey` only (they are not JWTs and are
    rejected as Bearer tokens). Legacy service_role JWTs go in both headers."""
    headers = {"apikey": key}
    if key.startswith("eyJ"):
        headers["Authorization"] = f"Bearer {key}"
    return headers


class SupabaseClient:
    def __init__(
        self,
        url: str,
        key: str,
        *,
        timeout_s: float = 30.0,
        max_attempts: int = 5,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._max_attempts = max_attempts
        self._http = httpx.Client(
            base_url=url.rstrip("/"),
            headers={**auth_headers(key), "Accept": "application/json"},
            timeout=timeout_s,
            transport=transport,
        )

    # -- lifecycle -----------------------------------------------------------
    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> SupabaseClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- core request with retries --------------------------------------------
    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, str] | Sequence[tuple[str, str]] | None = None,
        json: Any = None,
        content: bytes | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> httpx.Response:
        last_exc: Exception | None = None
        for attempt in range(1, self._max_attempts + 1):
            try:
                resp = self._http.request(
                    method, path, params=params, json=json, content=content, headers=headers
                )
                if resp.status_code < 300:
                    return resp
                err = SupabaseError(resp.status_code, method, path, resp.text)
                if resp.status_code not in RETRYABLE_STATUS:
                    raise err
                last_exc = err
            except (httpx.TransportError, httpx.TimeoutException) as exc:
                last_exc = exc
            if attempt < self._max_attempts:
                delay = min(30.0, 2 ** (attempt - 1)) + random.uniform(0, 0.5)
                log.warning("supabase retry", extra={"path": path, "attempt": attempt, "delay_s": round(delay, 2),
                                                     "error": str(last_exc)[:200]})
                time.sleep(delay)
        assert last_exc is not None
        raise last_exc

    # -- PostgREST -------------------------------------------------------------
    def select_pages(
        self,
        table: str,
        *,
        select: str = "*",
        filters: Sequence[tuple[str, str]] = (),
        order: str,
        page_size: int = 1000,
    ) -> Iterator[list[dict[str, Any]]]:
        """Yield pages using limit/offset over a TOTAL ordering (`order` must make
        rows unique, otherwise rows can be skipped/duplicated between pages)."""
        offset = 0
        while True:
            params = [("select", select), *filters, ("order", order),
                      ("limit", str(page_size)), ("offset", str(offset))]
            rows: list[dict[str, Any]] = self._request("GET", f"/rest/v1/{table}", params=params).json()
            if rows:
                yield rows
            if len(rows) < page_size:
                return
            offset += page_size

    def select_all(self, table: str, **kwargs: Any) -> list[dict[str, Any]]:
        return [row for page in self.select_pages(table, **kwargs) for row in page]

    def upsert(
        self,
        table: str,
        rows: Sequence[Mapping[str, Any]],
        *,
        on_conflict: str,
        chunk_size: int = 500,
    ) -> int:
        """Idempotent bulk upsert (INSERT ... ON CONFLICT DO UPDATE)."""
        headers = {"Prefer": "resolution=merge-duplicates,return=minimal"}
        for i in range(0, len(rows), chunk_size):
            self._request("POST", f"/rest/v1/{table}", params={"on_conflict": on_conflict},
                          json=list(rows[i : i + chunk_size]), headers=headers)
        return len(rows)

    def insert(self, table: str, row: Mapping[str, Any]) -> None:
        self._request("POST", f"/rest/v1/{table}", json=row, headers={"Prefer": "return=minimal"})

    def rpc(self, fn: str, params: Mapping[str, Any] | None = None) -> Any:
        resp = self._request("POST", f"/rest/v1/rpc/{fn}", json=dict(params or {}))
        return resp.json() if resp.content else None

    # -- Storage ---------------------------------------------------------------
    def storage_upload(self, bucket: str, path: str, data: bytes, *,
                       content_type: str = "application/octet-stream", upsert: bool = False) -> None:
        headers = {"Content-Type": content_type, "x-upsert": "true" if upsert else "false"}
        self._request("POST", f"/storage/v1/object/{bucket}/{path}", content=data, headers=headers)

    def storage_download(self, bucket: str, path: str) -> bytes:
        return self._request("GET", f"/storage/v1/object/{bucket}/{path}").content


def client_from_settings(settings: Any) -> SupabaseClient:
    url, key = settings.require_supabase()
    return SupabaseClient(url, key, timeout_s=settings.supabase_timeout_s)
