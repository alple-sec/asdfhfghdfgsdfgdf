#!/usr/bin/env python3
"""push_tag_costs — publish APEX per-tag AI spend into Jira issue fields.

Designed to run as an APEX **script agent** (`runner_type=script`), scheduled
hourly/daily via Scheduled Prompts. No model, no prompt, no AI.

What it does
------------
1. Asks APEX which run tags were active in the last ``LOOKBACK_HOURS``.
2. Keeps the ones that look like Jira issue keys (``TICKET_TAG_PATTERN``) —
   these are the *dynamic* tags a run tag rule applied from the matched text
   (pattern ``SAAS-\\d+`` on a run mentioning SAAS-9310 → tag ``SAAS-9310``).
   Carrying such a tag is the whole condition for being pushed: there is no
   separate marker tag, so ``TICKET_TAG_PATTERN`` is the only gate and is worth
   keeping tight (narrow it to your real project keys — ``^(SAAS|APEX)-[0-9]+$``
   — if the tenant has tags that merely look like issue keys).
3. For each of those tickets, asks APEX for the tag's **all-time** total cost —
   every run ever tagged with it, not just the window. The window only decides
   *which* tickets are worth re-pushing; the number pushed is the running total.
4. Writes that total into a single Jira field (``JIRA_FIELD_ID``) on that issue.

Why the window and the total are two different queries: a ticket touched today
may have cost accumulated over three weeks, and the field is meant to read
"what has this ticket cost us so far", so it must be recomputed from all runs
every time rather than incremented.

Environment
-----------
APEX_API_URL, APEX_API_TOKEN   Injected by APEX into every script-agent pod.
                               The token is short-lived and read-scoped to this
                               agent's own tenant, so no tenant_id is passed.
JIRA_BASE_URL                  e.g. https://beyondnow.atlassian.net
JIRA_EMAIL                     Atlassian account email (Basic auth username).
JIRA_API_TOKEN                 Atlassian API token (Basic auth password).
                               Supply these three via an APEX Access Token
                               attached to the agent — its keys arrive as env
                               vars verbatim.
DRY_RUN                        "1"/"true" (or --dry-run) to log without writing.

Optional INPUT_PAYLOAD (JSON) — for a manual backfill from the API trigger's
Test button or a one-off scheduled run:
    {"lookback_hours": 720}          widen the window for this run only
    {"tickets": ["SAAS-9310"]}       push exactly these, ignore the window

Exit code is the run result: 0 = every intended update succeeded.
Everything on stdout/stderr becomes the APEX run output verbatim.
"""

import base64
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

# ── Configuration ─────────────────────────────────────────────────────────────

# How far back to look for *activity*. Note APEX filters on whole days
# (finished_at >= start_date), so this is rounded down to a date: 24 h means
# "yesterday and today". Widen freely — it only affects which tickets get
# re-pushed, never the value pushed.
LOOKBACK_HOURS = 24

# Shape of the dynamic tag carrying the issue key, and the only thing that
# decides whether a tag is pushed. Anchored, so a tag like "JIRA" or "dev" is
# never mistaken for an issue.
TICKET_TAG_PATTERN = r"^[A-Z][A-Z0-9]*-[0-9]+$"

# The Jira field the total is written into. A custom field id, or a system
# field name. Find it at: <JIRA_BASE_URL>/rest/api/3/field
JIRA_FIELD_ID = "customfield_10101"

# True  → send a JSON number (Jira "Number" field type).
# False → send a formatted string (Jira "Short text" field type).
JIRA_FIELD_IS_NUMERIC = True
JIRA_FIELD_TEXT_TEMPLATE = "${value:.2f} USD"

# Decimals to round to before pushing. Two matches how APEX shows dollars.
COST_DECIMALS = 2

# Skip tickets whose all-time cost is 0 (or unpriced) rather than writing 0.00 —
# avoids stamping a field with a number that only means "not priced yet".
SKIP_ZERO_COST = True

HTTP_TIMEOUT_S = 30

# APEX read endpoint. Per-tag aggregation over run_records.tags; the only
# endpoint a script agent's run token is authorised to reach.
APEX_TAGS_PATH = "/api/usage-analytics/tags"

_TICKET_RE = re.compile(TICKET_TAG_PATTERN)


# ── Tiny stdlib HTTP helper ───────────────────────────────────────────────────
# urllib only: the runner image ships plain python3 with no third-party packages,
# and a reporting script should not depend on a pip install at run time.

def _request(method, url, headers=None, payload=None):
    """Return (status, parsed_body_or_text). Never raises for HTTP errors."""
    body = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=body, method=method)
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    if body is not None:
        req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:
            raw = resp.read().decode("utf-8", "replace")
            status = resp.status
    except urllib.error.HTTPError as exc:  # 4xx/5xx — the body holds the reason
        raw = exc.read().decode("utf-8", "replace")
        status = exc.code
    except urllib.error.URLError as exc:
        return 0, f"connection failed: {exc.reason}"
    if not raw:
        return status, None
    try:
        return status, json.loads(raw)
    except ValueError:
        return status, raw


def _env(name, required=True):
    value = (os.environ.get(name) or "").strip()
    if required and not value:
        print(f"ERROR: {name} is not set", file=sys.stderr)
        sys.exit(2)
    return value


# ── APEX side ─────────────────────────────────────────────────────────────────

def fetch_tag_rows(api_url, token, start_date=None):
    """GET /api/usage-analytics/tags — one row per tag, aggregated over runs.

    The query. ``start_date`` omitted = all time, which is exactly the total we
    want to publish. APEX already restricts this to finished runs
    (status success|failed), excludes script runs, and scopes it to the token's
    own tenant — so the numbers here are the same ones the Usage Analytics page
    shows for the Tags breakdown, with no filtering left to do here.

    Each run contributes its full cost to every tag it carries, so rows overlap
    and do not sum to the tenant total. That overlap is the point: it is what
    makes "cost of SAAS-9310" answerable.
    """
    query = {}
    if start_date is not None:
        query["start_date"] = start_date.isoformat()
    url = f"{api_url.rstrip('/')}{APEX_TAGS_PATH}"
    if query:
        url = f"{url}?{urllib.parse.urlencode(query)}"

    status, body = _request("GET", url, {"Authorization": f"Bearer {token}"})
    if status != 200 or not isinstance(body, dict):
        print(f"ERROR: APEX {url} → HTTP {status}: {body}", file=sys.stderr)
        sys.exit(3)
    return body.get("rows") or []


def window_tickets(api_url, token, lookback_hours):
    """Issue-key-shaped tags active in the window — every one of them gets pushed."""
    start_date = (datetime.now(timezone.utc) - timedelta(hours=lookback_hours)).date()
    rows = fetch_tag_rows(api_url, token, start_date=start_date)
    tags = {row["tag"] for row in rows}

    print(f"INFO: window starts {start_date.isoformat()} "
          f"({lookback_hours}h lookback, day-granular) — {len(tags)} tag(s) active")

    return sorted(tag for tag in tags if _TICKET_RE.match(tag))


def all_time_totals(api_url, token):
    """tag → row, over every run ever. This is what gets pushed."""
    return {row["tag"]: row for row in fetch_tag_rows(api_url, token)}


# ── Jira side ─────────────────────────────────────────────────────────────────

def jira_auth_header(email, api_token):
    raw = f"{email}:{api_token}".encode()
    return "Basic " + base64.b64encode(raw).decode()


def push_cost(base_url, auth, issue_key, cost):
    """PUT the cost into one field. Returns (ok, message).

    notifyUsers=false: this runs on a schedule and must not email every watcher
    on every tick.
    """
    value = (round(cost, COST_DECIMALS) if JIRA_FIELD_IS_NUMERIC
             else JIRA_FIELD_TEXT_TEMPLATE.format(value=cost))
    url = (f"{base_url.rstrip('/')}/rest/api/3/issue/"
           f"{urllib.parse.quote(issue_key)}?notifyUsers=false")

    status, body = _request("PUT", url, {"Authorization": auth},
                           {"fields": {JIRA_FIELD_ID: value}})
    if status == 204:
        return True, f"{JIRA_FIELD_ID} = {value}"
    if status == 404:
        # Deleted, moved, or a tag that merely looks like an issue key. Not the
        # script's problem to fix and not worth failing the whole run over.
        return None, "issue not found (or not visible to this account)"
    if status == 400:
        # Overwhelmingly: the field is not on the issue's Edit screen, or the
        # value type does not match the field type.
        return False, (f"HTTP 400 — check JIRA_FIELD_ID={JIRA_FIELD_ID} exists on this "
                       f"issue type's screen and matches JIRA_FIELD_IS_NUMERIC"
                       f"={JIRA_FIELD_IS_NUMERIC}: {body}")
    if status in (401, 403):
        print(f"ERROR: Jira rejected the credentials (HTTP {status}): {body}", file=sys.stderr)
        sys.exit(4)  # every later issue would fail identically — stop now
    return False, f"HTTP {status}: {body}"


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    dry_run = ("--dry-run" in sys.argv
               or (os.environ.get("DRY_RUN") or "").lower() in ("1", "true", "yes"))

    api_url = _env("APEX_API_URL")
    api_token = _env("APEX_API_TOKEN")
    jira_url = _env("JIRA_BASE_URL")
    auth = jira_auth_header(_env("JIRA_EMAIL"), _env("JIRA_API_TOKEN"))

    lookback = LOOKBACK_HOURS
    forced = None
    payload = (os.environ.get("INPUT_PAYLOAD") or "").strip()
    if payload:
        try:
            parsed = json.loads(payload)
            lookback = int(parsed.get("lookback_hours", lookback))
            forced = parsed.get("tickets") or None
        except (ValueError, TypeError, AttributeError) as exc:
            print(f"WARN: ignoring unparseable INPUT_PAYLOAD ({exc})")

    if forced:
        tickets = sorted(set(forced))
        print(f"INFO: INPUT_PAYLOAD override — pushing {len(tickets)} ticket(s), window ignored")
    else:
        tickets = window_tickets(api_url, api_token, lookback)
        if not tickets:
            print(f"INFO: no tags matching {TICKET_TAG_PATTERN} in the window — nothing to push")
            return 0
        print(f"INFO: {len(tickets)} ticket tag(s) active: {', '.join(tickets)}")

    totals = all_time_totals(api_url, api_token)
    updated = skipped = failed = 0

    for ticket in tickets:
        row = totals.get(ticket)
        if row is None:
            print(f"SKIP  {ticket}: no all-time rows (tag no longer on any finished run)")
            skipped += 1
            continue

        cost = row.get("cost_usd") or 0.0
        runs = row.get("total_runs", 0)
        unpriced = row.get("unpriced_run_count", 0)
        detail = f"${cost:.{COST_DECIMALS}f} over {runs} run(s)"
        if unpriced:
            detail += f" — WARN {unpriced} run(s) unpriced, total understates actual spend"

        if SKIP_ZERO_COST and cost <= 0:
            print(f"SKIP  {ticket}: {detail}")
            skipped += 1
            continue

        if dry_run:
            print(f"DRY   {ticket}: would set {JIRA_FIELD_ID} — {detail}")
            updated += 1
            continue

        ok, message = push_cost(jira_url, auth, ticket, cost)
        if ok is True:
            print(f"OK    {ticket}: {detail} → {message}")
            updated += 1
        elif ok is None:
            print(f"SKIP  {ticket}: {message}")
            skipped += 1
        else:
            print(f"FAIL  {ticket}: {message}", file=sys.stderr)
            failed += 1

    print(f"---\n{'DRY RUN — ' if dry_run else ''}"
          f"{updated} updated, {skipped} skipped, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
