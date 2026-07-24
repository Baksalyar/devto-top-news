"""Best-effort GitHub Actions status reporter for BeanDashboard."""

from __future__ import annotations

import json
import os
import sys
import urllib.request


def main() -> int:
    ingest_url = os.environ.get("BEAN_EVENT_INGEST_URL", "").strip()
    token = os.environ.get("BEAN_EVENT_TOKEN", "").strip()
    if not ingest_url or not token:
        print("BeanDashboard reporting is not configured; skipping.")
        return 0

    refresh_status = os.environ.get("BEAN_REPORT_REFRESH_STATUS", "workflow_failed")
    commit_status = os.environ.get("BEAN_REPORT_COMMIT_STATUS", "not_run")
    successful = refresh_status == "success" and commit_status not in {"failure", "cancelled"}
    status = "run_success" if successful else "run_failure"

    if successful:
        level = "info"
        message = "DEV.to feed refresh completed successfully."
        reason = "success"
    elif refresh_status == "rate_limited":
        level = "error"
        message = "DEV.to feed refresh was rate-limited; the previous feed was kept."
        reason = "upstream_rate_limit"
    else:
        level = "error"
        message = "DEV.to feed refresh failed; inspect the GitHub Actions run."
        reason = "workflow_failure"

    run_url = os.environ.get("GITHUB_RUN_URL", "")
    metadata = {
        "status": status,
        "reason": reason,
        "refresh_status": refresh_status,
        "commit_status": commit_status,
    }
    if run_url:
        metadata["github_run_url"] = run_url

    payload = {
        "level": level,
        "kind": "heartbeat",
        "code": status,
        "message": message,
        "source": "devto-top-news",
        "host": "github-actions",
        "env": "prod",
        "run_id": os.environ.get("GITHUB_RUN_ID", "") or None,
        "metadata": metadata,
    }
    payload = {key: value for key, value in payload.items() if value is not None}

    request = urllib.request.Request(
        ingest_url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            response_body = json.loads(response.read().decode("utf-8"))
        if not response_body.get("accepted"):
            raise RuntimeError(f"BeanDashboard rejected the event: {response_body.get('error')}")
        print(f"Reported {status} to BeanDashboard.")
    except (OSError, RuntimeError, ValueError) as exc:
        # Monitoring must not turn an already classified scraper result into a
        # second, unrelated GitHub failure.
        print(f"Warning: BeanDashboard report failed: {exc}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
