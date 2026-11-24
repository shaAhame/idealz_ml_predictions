"""Model bundle (de)serialisation, Supabase Storage upload/download and the
`model_registry` table.

A bundle is a single .joblib file containing the fitted sklearn Pipeline AND
everything needed to use it correctly (feature config, feature column order,
target, version, training window). A human-readable metadata.json sits next to it.

Security: joblib/pickle executes code on load. Bundles are only ever loaded from
paths we wrote or from the private bucket (service-role write only), and their
SHA-256 is verified against `model_registry` before loading.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import joblib
from sklearn.pipeline import Pipeline

from ml.config import Settings
from ml.features import FeatureConfig
from ml.supabase_client import SupabaseClient, client_from_settings

log = logging.getLogger(__name__)
BUNDLE_SCHEMA_VERSION = 1
REGISTRY_TABLE = "model_registry"


class ModelLoadError(RuntimeError):
    pass


@dataclass
class ModelBundle:
    pipeline: Pipeline
    feature_config: dict[str, Any]
    feature_columns: list[str]
    target_metric: str
    model_version: str
    trained_at: str
    data_start: str
    data_end: str
    known_series: list[list[str]]
    metrics: dict[str, Any] = field(default_factory=dict)
    schema_version: int = BUNDLE_SCHEMA_VERSION

    @property
    def fcfg(self) -> FeatureConfig:
        return FeatureConfig.from_dict(self.feature_config)

    def metadata(self) -> dict[str, Any]:
        d = asdict(self)
        d.pop("pipeline")
        d["known_series_count"] = len(self.known_series)
        return d


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def save_bundle(bundle: ModelBundle, out_dir: Path, extra_metadata: dict[str, Any] | None = None) -> tuple[Path, str]:
    out_dir.mkdir(parents=True, exist_ok=True)
    model_path = out_dir / "model.joblib"
    tmp = model_path.with_suffix(".joblib.tmp")
    joblib.dump(bundle, tmp, compress=3)
    os.replace(tmp, model_path)  # atomic: a crash never leaves a half-written model
    digest = sha256_file(model_path)
    meta = {**bundle.metadata(), **(extra_metadata or {}), "artifact_sha256": digest}
    (out_dir / "metadata.json").write_text(json.dumps(meta, indent=2, default=str))
    return model_path, digest


def load_bundle(path: Path, expected_sha256: str | None = None) -> ModelBundle:
    if not path.exists():
        raise ModelLoadError(f"Model artifact not found at {path}")
    if expected_sha256:
        actual = sha256_file(path)
        if actual != expected_sha256:
            raise ModelLoadError(f"SHA-256 mismatch for {path}: expected {expected_sha256}, got {actual}")
    obj = joblib.load(path)
    if not isinstance(obj, ModelBundle):
        raise ModelLoadError(f"{path} does not contain a ModelBundle (got {type(obj).__name__})")
    if obj.schema_version != BUNDLE_SCHEMA_VERSION:
        raise ModelLoadError(f"Unsupported bundle schema {obj.schema_version} (expected {BUNDLE_SCHEMA_VERSION})")
    return obj


# ----------------------------------------------------------------------------
# Supabase registry
# ----------------------------------------------------------------------------
class ModelRegistry:
    def __init__(self, client: SupabaseClient, bucket: str) -> None:
        self.client = client
        self.bucket = bucket

    @staticmethod
    def artifact_path(target: str, version: str, name: str = "model.joblib") -> str:
        return f"models/{target}/{version}/{name}"

    def upload(self, local_dir: Path, target: str, version: str) -> str:
        path = self.artifact_path(target, version)
        self.client.storage_upload(self.bucket, path, (local_dir / "model.joblib").read_bytes())
        self.client.storage_upload(self.bucket, self.artifact_path(target, version, "metadata.json"),
                                   (local_dir / "metadata.json").read_bytes(), content_type="application/json")
        return path

    def register(self, row: dict[str, Any]) -> None:
        self.client.insert(REGISTRY_TABLE, row)

    def promote(self, version: str) -> None:
        self.client.rpc("promote_model", {"p_model_version": version})

    def production(self, target: str) -> dict[str, Any] | None:
        rows = self.client.select_all(
            REGISTRY_TABLE, select="*",
            filters=[("target_metric", f"eq.{target}"), ("status", "eq.production")],
            order="promoted_at.desc", page_size=1)
        return rows[0] if rows else None

    def download(self, entry: dict[str, Any], cache_dir: Path) -> Path:
        dest = cache_dir / entry["model_version"] / "model.joblib"
        if dest.exists() and sha256_file(dest) == entry["artifact_sha256"]:
            return dest
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(".tmp")
        tmp.write_bytes(self.client.storage_download(self.bucket, entry["artifact_path"]))
        os.replace(tmp, dest)
        return dest


def resolve_model_path(settings: Settings, client: SupabaseClient | None = None) -> tuple[Path, str | None]:
    """Return (local path, expected sha256) of the model to serve.

    MODEL_SOURCE=local    -> MODEL_PATH as-is (baked into the image or mounted).
    MODEL_SOURCE=supabase -> the current production model for TARGET_METRIC,
                             downloaded once into MODEL_CACHE_DIR.
    """
    if settings.model_source == "local":
        return settings.model_path, None
    own = client is None
    client = client or client_from_settings(settings)
    try:
        reg = ModelRegistry(client, settings.model_bucket)
        entry = reg.production(settings.target_metric)
        if entry is None:
            raise ModelLoadError(f"No production model registered for target '{settings.target_metric}'. "
                                 "Run the training workflow first.")
        path = reg.download(entry, settings.model_cache_dir)
        log.info("production model resolved", extra={"model_version": entry["model_version"], "path": str(path)})
        return path, entry["artifact_sha256"]
    finally:
        if own:
            client.close()


def load_serving_bundle(settings: Settings) -> ModelBundle:
    path, sha = resolve_model_path(settings)
    bundle = load_bundle(path, sha)
    if bundle.target_metric != settings.target_metric:
        raise ModelLoadError(f"Model target '{bundle.target_metric}' != TARGET_METRIC '{settings.target_metric}'")
    return bundle


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)
