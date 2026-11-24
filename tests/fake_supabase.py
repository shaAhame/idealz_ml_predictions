"""In-memory emulation of the slice of Supabase (PostgREST + Storage) this project
uses, exposed as an httpx.MockTransport. Lets the full pipeline
(train -> registry -> batch forecast -> API) run offline in tests."""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import parse_qsl

import httpx
import pandas as pd


class FakeSupabase:
    def __init__(self, daily: pd.DataFrame | None = None, page_cap: int = 1000) -> None:
        self.daily: list[dict[str, Any]] = []
        if daily is not None:
            d = daily.copy()
            d["sale_date"] = d["sale_date"].dt.date.astype(str)
            self.daily = d.to_dict("records")
        self.registry: dict[str, dict[str, Any]] = {}
        self.forecasts: dict[tuple[str, ...], dict[str, Any]] = {}
        self.storage: dict[str, bytes] = {}
        self.requests: list[httpx.Request] = []
        self.page_cap = page_cap  # emulates PostgREST max-rows
        self.fail_next: list[int] = []

    # ------------------------------------------------------------------
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.fail_next:
            return httpx.Response(self.fail_next.pop(0), text='{"message":"injected failure"}')
        if "apikey" not in request.headers:
            return httpx.Response(401, json={"message": "no apikey"})
        path = request.url.path
        params = parse_qsl(request.url.query.decode(), keep_blank_values=True)
        if path.startswith("/storage/v1/object/"):
            key = path.removeprefix("/storage/v1/object/")
            if request.method == "POST":
                if key in self.storage and request.headers.get("x-upsert") != "true":
                    return httpx.Response(409, json={"message": "exists"})
                self.storage[key] = request.content
                return httpx.Response(200, json={"Key": key})
            if key not in self.storage:
                return httpx.Response(404, json={"message": "not found"})
            return httpx.Response(200, content=self.storage[key])
        if path == "/rest/v1/v_daily_sales" and request.method == "GET":
            return self._select(self.daily, params)
        if path == "/rest/v1/model_registry":
            if request.method == "POST":
                row = json.loads(request.content)
                if row["model_version"] in self.registry:
                    return httpx.Response(409, json={"message": "duplicate key"})
                self.registry[row["model_version"]] = {**row, "promoted_at": None}
                return httpx.Response(201)
            return self._select(list(self.registry.values()), params)
        if path == "/rest/v1/rpc/promote_model":
            v = json.loads(request.content)["p_model_version"]
            target = self.registry[v]["target_metric"]
            for r in self.registry.values():
                if r["target_metric"] == target and r["status"] == "production":
                    r["status"] = "archived"
            self.registry[v].update(status="production", promoted_at=pd.Timestamp.now(tz="UTC").isoformat())
            return httpx.Response(204)
        if path == "/rest/v1/sales_forecasts" and request.method == "POST":
            assert "merge-duplicates" in request.headers["prefer"]
            conflict = dict(params)["on_conflict"].split(",")
            for row in json.loads(request.content):
                self.forecasts[tuple(str(row[c]) for c in conflict)] = row
            return httpx.Response(201)
        return httpx.Response(404, json={"message": f"no route {request.method} {path}"})

    # ------------------------------------------------------------------
    def _select(self, rows: list[dict[str, Any]], params: list[tuple[str, str]]) -> httpx.Response:
        out = rows
        limit, offset, order = None, 0, None
        cols = None
        for k, v in params:
            if k == "select":
                cols = None if v == "*" else v.split(",")
            elif k == "limit":
                limit = int(v)
            elif k == "offset":
                offset = int(v)
            elif k == "order":
                order = v
            else:
                op, val = v.split(".", 1)
                fn = {"eq": lambda a, b: str(a) == b, "gte": lambda a, b: str(a) >= b,
                      "lte": lambda a, b: str(a) <= b}[op]
                out = [r for r in out if fn(r.get(k), val)]
        if order:
            for part in reversed(order.split(",")):
                col, direction = [*part.split("."), "asc"][:2]
                out = sorted(out, key=lambda r: (r.get(col) is None, str(r.get(col))), reverse=direction == "desc")
        limit = min(limit or self.page_cap, self.page_cap)
        page = out[offset : offset + limit]
        if cols:
            page = [{c: r.get(c) for c in cols} for r in page]
        return httpx.Response(200, json=page)
