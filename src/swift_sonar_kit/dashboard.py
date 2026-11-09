"""Optional browser presentation, never part of analysis or its exit status.

SPDX-License-Identifier: GPL-3.0-only
"""

import os
import subprocess
import sys
from urllib.parse import urlencode
import webbrowser

from .server import validate_url


def dashboard_url(base: str, project_key: str) -> str:
    """Build a credential-free URL from the validated server and project key."""
    return validate_url(base) + "/dashboard?" + urlencode({"id": project_key})


def open_after_scan(args, output) -> None:
    """Open only saved, completed publications in an interactive human session."""
    result = output.result
    if (getattr(args, "no_browser", False) or args.as_json or args.plan_only
            or args.prepare_only or args.setup_only or output.error is not None
            or output.saved_path is None or result.get("mode") != "analysis-only"
            or result.get("stage") != "complete" or result.get("published") is not True
            or result.get("scanner_exit_code") != 0
            or any(os.environ.get(name) for name in ("CI", "NO_BROWSER", "JCODE_NO_BROWSER"))
            or not sys.stdin.isatty() or not sys.stdout.isatty()):
        return
    try:
        url = dashboard_url(result.get("server_url", ""), result.get("project_key", ""))
        if result.get("dashboard_url") != url:
            return
        if webbrowser.open(url, new=2):
            return
    except (OSError, ValueError, webbrowser.Error, subprocess.SubprocessError):
        pass
    print("swift-sonar: Could not open the browser. Open the Sonar dashboard URL "
          "shown above. The saved scan result and exit status are unchanged.", file=sys.stderr)
