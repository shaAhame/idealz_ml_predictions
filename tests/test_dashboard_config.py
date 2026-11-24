"""The public dashboard must never be deployed with a secret key."""

from __future__ import annotations

import base64
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / ".github/scripts/write_dashboard_config.py"
spec = importlib.util.spec_from_file_location("wdc", SCRIPT)
assert spec and spec.loader
wdc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wdc)


def _jwt(role: str) -> str:
    seg = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")  # noqa: E731
    return f"{seg({'alg': 'HS256'})}.{seg({'role': role})}.sig"


def test_key_rules() -> None:
    assert wdc.check_key("sb_publishable_abc") is None
    assert wdc.check_key(_jwt("anon")) is None
    assert "SECRET" in wdc.check_key("sb_secret_abc")
    assert "service_role" in wdc.check_key(_jwt("service_role"))
    assert wdc.check_key("") and wdc.check_key("random")


def test_url_rules() -> None:
    assert wdc.check_url("https://djwtetqlbmraavaampng.supabase.co") is None
    assert wdc.check_url("https://x.supabase.co/rest/v1") and wdc.check_url("http://x.supabase.co")


def test_cli_writes_config_and_refuses_secret(tmp_path: Path) -> None:
    out = tmp_path / "config.js"
    env = {"SUPABASE_URL": "https://abc.supabase.co/", "PUBLISHABLE_KEY": "sb_publishable_xyz", "PATH": ""}
    assert subprocess.run([sys.executable, str(SCRIPT), str(out)], check=False, env=env).returncode == 0
    assert out.read_text() == ('window.DASHBOARD_CONFIG = {"supabaseUrl": "https://abc.supabase.co", '
                               '"publishableKey": "sb_publishable_xyz"};\n')
    bad = tmp_path / "bad.js"
    env["PUBLISHABLE_KEY"] = "sb_secret_oops"
    r = subprocess.run([sys.executable, str(SCRIPT), str(bad)], check=False, env=env, capture_output=True, text=True)
    assert r.returncode == 1 and "SECRET" in r.stdout and not bad.exists()


def test_page_has_no_embedded_keys() -> None:
    html = (ROOT / "dashboard/index.html").read_text()
    assert "sb_secret_" not in html and "sb_publishable_" not in html and "eyJ" not in html
