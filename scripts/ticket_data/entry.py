#!/usr/bin/env python3
"""Report the current APEX spend of every ticket tag touched in the last day.

Written to run as an APEX script agent (runner_type="script"): it reads APEX's own
figures back out through APEX_API_URL / APEX_API_TOKEN, which the runner injects
into the pod for the life of the run. Nothing else is needed — no service
principal, no database access, stdlib only (the runner image ships python3 with no
guaranteed site-packages).

Output is one line per recently-active tag:

    Issue tag SAAS-9310 is now at 12.4183 USD. Uploading to jira...

"Is now at" is the tag's **lifetime** total, not the last day's — the last day only
decides *which* tags are worth reporting. That is the figure you would want on a
ticket: what this ticket has cost so far.

Environment:
  APEX_API_URL    — APEX API base URL (injected for script agents)
  APEX_API_TOKEN  — run-scoped read token, valid for this run only (injected)
  LOOKBACK_HOURS  — optional, default 24
  JIRA_UPLOAD     — optional, "1" to actually upload (unimplemented — see
                    upload_to_jira below); anything else just reports

INPUT_PAYLOAD, when a trigger supplies one, may be a JSON object overriding
the above, e.g. {"lookback_hours": 72}.

Run locally against a dev API with a hand-minted token:
    APEX_API_URL=http://localhost:8000 APEX_API_TOKEN=... python3 main.py
"""
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone

HTTP_TIMEOUT_S = 30


# ─── APEX API ─────────────────────────────────────────────────────────────────

def api_get(base_url: str, token: str, path: str, params: dict) -> dict:
    """GET a JSON endpoint with the run token, or exit with a readable reason."""
    query = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
    url = f"{base_url.rstrip('/')}{path}" + (f"?{query}" if query else "")
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")[:500]
        if exc.code in (401, 403):
            # The token lives only as long as the run; a 401 here almost always
            # means the run outlived it, not that it was wrong.
            die(f"APEX rejected the run token ({exc.code}). It is scoped to this "
                f"run and this tenant, and expires with the run. Body: {body}")
        die(f"GET {path} failed: HTTP {exc.code}. Body: {body}")
    except urllib.error.URLError as exc:
        die(f"GET {path} failed: cannot reach {base_url} ({exc.reason}). "
            f"Check APEX_API_URL is reachable from inside the pod.")
    except json.JSONDecodeError as exc:
        die(f"GET {path} returned invalid JSON: {exc}")


def fetch_tag_usage(base_url: str, token: str,
                    start_date: date = None, end_date: date = None) -> dict:
    """Per-tag usage for a date range, or for all time when both are None.

    /api/usage-analytics/tags is the only endpoint a run token may read, and it is
    also exactly the right one: it aggregates cost per tag server-side, so this
    script never walks individual runs.
    """
    return api_get(base_url, token, "/api/usage-analytics/tags", {
        "start_date": start_date.isoformat() if start_date else None,
        "end_date": end_date.isoformat() if end_date else None,
    })


# ─── Jira ─────────────────────────────────────────────────────────────────────

def upload_to_jira(tag: str, cost_usd, lifetime: dict) -> None:
    """Seam for pushing the figure onto the ticket. Deliberately not implemented.

    Wire this to Jira by giving the agent a `jira` Permission — its keys arrive as
    env vars in this pod (JIRA_URL / JIRA_USER / JIRA_API_TOKEN or whatever the
    Permission defines) — and PUT the cost onto a custom field, or POST a comment:

        PUT {JIRA_URL}/rest/api/3/issue/{tag}
            {"fields": {"customfield_XXXXX": cost_usd}}

    Left as a no-op so a dry run reports without touching any ticket.
    """
    return None


# ─── Reporting ────────────────────────────────────────────────────────────────

def fmt_usd(cost_usd) -> str:
    """4 decimals: per-ticket AI spend is routinely under a cent, and rounding to
    2 would print "0.00 USD" for a real, non-zero cost."""
    return f"{cost_usd:.4f}" if cost_usd is not None else "unknown"


def die(message: str) -> "NoReturn":
    print(f"ERROR: {message}", file=sys.stderr, flush=True)
    sys.exit(1)


def read_config() -> tuple:
    base_url = os.environ.get("APEX_API_URL", "").strip()
    token = os.environ.get("APEX_API_TOKEN", "").strip()
    if not base_url or not token:
        die("APEX_API_URL and APEX_API_TOKEN are not both set. APEX injects them "
            "for script agents when PROGRESS_CALLBACK_BASE_URL is configured and "
            "Redis is reachable — check both if this is an APEX run.")

    lookback_hours = 10
    raw = os.environ.get("LOOKBACK_HOURS", "").strip()
    payload = os.environ.get("INPUT_PAYLOAD", "").strip()
    if payload:
        try:
            parsed = json.loads(payload)
            if isinstance(parsed, dict) and "lookback_hours" in parsed:
                raw = str(parsed["lookback_hours"])
        except json.JSONDecodeError:
            # A payload need not be JSON (a Teams message is not), so this is not
            # an error — the default window just stands.
            print("INFO: INPUT_PAYLOAD is not JSON — using the default window.",
                  flush=True)
    if raw:
        try:
            lookback_hours = int(float(raw))
        except ValueError:
            die(f"lookback_hours must be a number, got {raw!r}")
        if lookback_hours < 1:
            die(f"lookback_hours must be at least 1, got {lookback_hours}")

    return base_url, token, lookback_hours


def main() -> int:
    # Both streams land in the same container log, and a pipe makes stdout
    # block-buffered — without this, an error on stderr is printed before output
    # that logically preceded it, which is thoroughly misleading in a run's log.
    sys.stdout.reconfigure(line_buffering=True)

    base_url, token, lookback_hours = read_config()

    now = datetime.now(tz=timezone.utc)
    since = now - timedelta(hours=lookback_hours)

    # The endpoint's date filters are calendar-day granular (they bound
    # run_records.finished_at by whole UTC days), so the queried window starts at
    # midnight of the day `since` falls in. That over-covers rather than
    # under-covers: every tag touched in the last `lookback_hours` is present, plus
    # possibly some touched slightly earlier the same day. Reporting a lifetime
    # total for one extra tag is harmless; missing a ticket that just ran is not.
    start_date, end_date = since.date(), now.date()

    print(f"Window: runs finishing {start_date} .. {end_date} UTC "
          f"(covers the last {lookback_hours}h; day-granular, so the window may "
          f"start up to 24h earlier)")

    recent = fetch_tag_usage(base_url, token, start_date, end_date)
    lifetime = fetch_tag_usage(base_url, token)

    recent_rows = {row["tag"]: row for row in recent.get("rows", [])}
    lifetime_rows = {row["tag"]: row for row in lifetime.get("rows", [])}

    print(f"Runs in window: {recent.get('total_run_count', 0)} "
          f"({recent.get('tagged_run_count', 0)} tagged, "
          f"{recent.get('untagged_run_count', 0)} untagged)")
    print(f"Tags active in window: {len(recent_rows)} "
          f"of {len(lifetime_rows)} known to APEX")
    print("---", flush=True)

    if not recent_rows:
        # Not a failure: a quiet day, or no tag rule matched. Say which, since
        # "nothing to report" and "tagging is misconfigured" look identical here.
        if recent.get("total_run_count", 0) == 0:
            print("No runs finished in the window — nothing to report.")
        else:
            print(f"{recent['total_run_count']} run(s) finished in the window but "
                  f"none carried a tag. Check the tenant's Run Tag Rules.")
        return 0

    # Highest lifetime spend first: if this is skimmed, the expensive tickets are
    # the ones worth seeing.
    def sort_key(item):
        cost = (lifetime_rows.get(item[0]) or {}).get("cost_usd")
        return (cost is None, -(cost or 0.0), item[0])

    uploaded = 0
    unpriced_tags = []
    for tag, recent_row in sorted(recent_rows.items(), key=sort_key):
        # Fall back to the window row: a tag can only be missing from the lifetime
        # result if a run finished between the two calls above.
        life_row = lifetime_rows.get(tag, recent_row)
        cost_usd = life_row.get("cost_usd")

        print(f"Issue tag {tag} is now at {fmt_usd(cost_usd)} USD. "
              f"Uploading to jira...")

        if life_row.get("unpriced_run_count"):
            # The total is real but incomplete — say so rather than letting a
            # partial figure land on a ticket unremarked.
            unpriced_tags.append(tag)
            print(f"  NOTE: {life_row['unpriced_run_count']} of "
                  f"{life_row.get('total_runs', 0)} run(s) on {tag} have no cost "
                  f"recorded — the figure above excludes them.")

        if os.environ.get("JIRA_UPLOAD") == "1":
            upload_to_jira(tag, cost_usd, life_row)
        uploaded += 1

    print("---")
    print(f"Reported {uploaded} tag(s). "
          f"Window spend across all tags: {fmt_usd(recent.get('tagged_cost_usd'))} USD; "
          f"lifetime tagged spend: {fmt_usd(lifetime.get('tagged_cost_usd'))} USD.")
    if unpriced_tags:
        print(f"{len(unpriced_tags)} tag(s) had unpriced runs: "
              f"{', '.join(unpriced_tags)}")
    if os.environ.get("JIRA_UPLOAD") != "1":
        print("Jira upload is a no-op in this build (set JIRA_UPLOAD=1 once "
              "upload_to_jira is implemented).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
