"""Write dashboard/config.js for GitHub Pages.

The page is public, so it may only ever contain the PUBLISHABLE key (made for
browsers, protected by RLS + the dashboard_viewers allowlist). This script
refuses secret / service_role keys so a mix-up can never publish one.

    SUPABASE_URL=... PUBLISHABLE_KEY=... python .github/scripts/write_dashboard_config.py dashboard/config.js
"""

from __future__ import annotations

import base64
import json
import os
import re
import sys


def check_key(key: str) -> str | None:
    """Return an error message if `key` must not be published, else None."""
    if not key:
        return ("SUPABASE_PUBLISHABLE_KEY is empty. Add it as a repository SECRET under Settings > "
                "Secrets and variables > Actions > Secrets.")
    if key.startswith("sb_secret_"):
        return "SUPABASE_PUBLISHABLE_KEY holds a SECRET key (sb_secret_...). Use the publishable key."
    if key.startswith("sb_publishable_"):
        return None
    if key.startswith("eyJ"):  # legacy JWT: only the anon role may be public
        try:
            payload = key.split(".")[1]
            claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        except (IndexError, ValueError):
            return "SUPABASE_PUBLISHABLE_KEY is not a valid key."
        if claims.get("role") != "anon":
            role = claims.get("role")
            return f"SUPABASE_PUBLISHABLE_KEY has role '{role}'. Only the anon/publishable key may be public."
        return None
    return "SUPABASE_PUBLISHABLE_KEY does not look like a Supabase publishable key (sb_publishable_...)."


def check_url(url: str) -> str | None:
    if not url:
        return ("SUPABASE_URL is empty. Add it as a repository SECRET under Settings > "
                "Secrets and variables > Actions > Secrets.")
    if not re.fullmatch(r"https://[a-z0-9-]+\.supabase\.co", url or ""):
        return "SUPABASE_URL must look like https://<project-ref>.supabase.co (no trailing path)."
    return None


def _set_output(ready: bool) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a") as f:
            f.write(f"ready={'true' if ready else 'false'}\n")


def main(out: str) -> int:
    url = os.environ.get("SUPABASE_URL", "").strip().rstrip("/")
    key = os.environ.get("PUBLISHABLE_KEY", "").strip()
    if not key and os.environ.get("GITHUB_EVENT_NAME") == "push":
        print("::notice::Dashboard not configured yet (no SUPABASE_PUBLISHABLE_KEY secret); skipping deploy.")
        _set_output(False)
        return 0
    for err in (check_url(url), check_key(key)):
        if err:
            print(f"::error::{err}")
            _set_output(False)
            return 1
    with open(out, "w") as f:
        f.write("window.DASHBOARD_CONFIG = " + json.dumps({"supabaseUrl": url, "publishableKey": key}) + ";\n")
    print(f"wrote {out}")
    _set_output(True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
