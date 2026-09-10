"""
=============================================================================
 BRAND TRACKER  —  managed by the "Brand Tracker" plugin
=============================================================================

 This is the tracking worker the plugin provisions as a PyRun Script. You do
 NOT edit or run it by hand — configure everything on the plugin page
 (/plugins/brand_tracker/) and it provisions this script, its secrets, a data
 store, and a weekly schedule for you.

 WHAT IT DOES (every run, once per week)
   1. For each tracked keyword, searches the enabled sources (exact phrase):
        - Serper "web"   (Google organic, past week)      [costs Serper credits]
        - Serper "news"  (Google News, past week)          [costs Serper credits]
        - Hacker News    (Algolia, past ~week)             [free]
        - Reddit         (OAuth search, newest)            [free]
   2. Canonicalizes + dedupes URLs across sources (the same article via web and
      news collapses to one), drops excluded domains.
   3. Stores NEW mentions in the `brand_tracker:state` data store; the dashboard
      reads them as a live feed.
   4. Prunes everything older than the retention window (default 90 days).
   5. Tracks Serper credit usage per month and stops Serper calls if a monthly
      cap is set and reached (free sources keep running).
   6. Optionally emails a report of the new mentions (Resend) — suppressed on the
      very first run so a fresh install never floods you.

 ALERTS for operational failures are handled by PyRun itself: the managed
 Script has notify_on set, so PyRun emails when a run fails. The optional
 Resend email here is the *content* report (the mentions themselves).

 SECRETS (injected as clean env vars, selected-mode):
   SERPER_API_KEY        (required)
   REDDIT_CLIENT_ID      (optional — only if Reddit is enabled)
   REDDIT_CLIENT_SECRET  (optional)
   RESEND_API_KEY        (optional — only if the email report is enabled)

 NON-SECRET CONFIG (keywords, excluded_domains, source toggles, num_results,
 retention_days, monthly_credit_cap, email settings) is read from the
 `brand_tracker:state` data store (entry "config"), so this script body is
 identical for every install and the plugin page can show/edit it in plain text.

 ENVIRONMENT must provide: requests
=============================================================================
"""

import os
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import requests

# Emoji in log lines crash on a non-UTF-8 console (e.g. a Windows cp1252 shell).
# PyRun captures stdout as UTF-8, but make standalone runs robust too.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


# =============================================================================
# CONFIGURATION
# =============================================================================

# Credentials — injected as clean env vars by selected-mode grants. Read soft
# (``.get``) so this module is importable for unit tests; main() validates the
# required key at runtime and fails fast with a friendly message.
SERPER_API_KEY = os.environ.get("SERPER_API_KEY", "")
REDDIT_CLIENT_ID = os.environ.get("REDDIT_CLIENT_ID", "")
REDDIT_CLIENT_SECRET = os.environ.get("REDDIT_CLIENT_SECRET", "")
RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")

# The plugin's owner slug + owned data store. Derive the slug from the env var
# PyRun injects for owned-script runs (PYRUNNER_OWNER_PLUGIN) so the store
# name is never hardcoded; fall back to the literal slug for local runs.
OWNER = os.environ.get("PYRUNNER_OWNER_PLUGIN") or "brand_tracker"
STATE_STORE = f"{OWNER}:state"
RUN_ID = os.environ.get("PYRUNNER_RUN_ID", "")  # ties live progress to this run

REQUEST_TIMEOUT = 30
USER_AGENT = "pyrunner-brand-tracker/1.0"
HISTORY_LIMIT = 50      # most recent N runs kept for the dashboard
MAX_MENTIONS = 2000     # hard cap on the stored feed (newest kept)

SERPER_WEB_URL = "https://google.serper.dev/search"
SERPER_NEWS_URL = "https://google.serper.dev/news"
HN_SEARCH_URL = "https://hn.algolia.com/api/v1/search_by_date"
REDDIT_TOKEN_URL = "https://www.reddit.com/api/v1/access_token"
REDDIT_SEARCH_URL = "https://oauth.reddit.com/search"


def load_plugin_config():
    """Read non-secret config from the owned data store (entry "config").

    Falls back to empty defaults when not running under PyRun (local testing)
    so this module imports cleanly without a data store.
    """
    defaults = {
        "keywords": [],
        "excluded_domains": [],
        "news_enabled": True,
        "hackernews_enabled": False,
        "reddit_enabled": False,
        "num_results": 10,
        "retention_days": 90,
        "monthly_credit_cap": 0,
        "email_enabled": False,
        "email_to": "",
        "email_from": "",
        "enrich_provider": "off",   # off | claude | openrouter
        "enrich_model": "",
    }
    try:
        from pyrunner_datastore import DataStore
        cfg = DataStore(STATE_STORE).get("config", {}) or {}
    except Exception:
        return defaults  # not under PyRun / no store yet — import stays safe
    merged = {**defaults, **cfg}
    merged["keywords"] = list(merged.get("keywords") or [])
    merged["excluded_domains"] = list(merged.get("excluded_domains") or [])
    merged["num_results"] = int(merged.get("num_results") or 10)
    merged["retention_days"] = int(merged.get("retention_days") or 90)
    merged["monthly_credit_cap"] = int(merged.get("monthly_credit_cap") or 0)
    return merged


_cfg = load_plugin_config()
KEYWORDS = _cfg["keywords"]
EXCLUDED_DOMAINS = _cfg["excluded_domains"]
NEWS_ENABLED = _cfg["news_enabled"]
HACKERNEWS_ENABLED = _cfg["hackernews_enabled"]
REDDIT_ENABLED = _cfg["reddit_enabled"]
NUM_RESULTS = _cfg["num_results"]
RETENTION_DAYS = _cfg["retention_days"]
MONTHLY_CREDIT_CAP = _cfg["monthly_credit_cap"]
EMAIL_ENABLED = _cfg["email_enabled"]
EMAIL_TO = _cfg["email_to"]
EMAIL_FROM = _cfg["email_from"]
ENRICH_PROVIDER = _cfg["enrich_provider"]
ENRICH_MODEL = _cfg["enrich_model"]

# AI enrichment tuning. Only NEW mentions are enriched; batches keep the prompt
# bounded, and the per-run ceiling caps cost/latency on a spike.
ENRICH_BATCH = 25
ENRICH_MAX = 100
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_DEFAULT_MODEL = "openai/gpt-4o-mini"
_SOURCE_TYPES = {"news", "blog", "forum", "social", "docs", "other"}
_SENTIMENTS = {"positive", "neutral", "negative"}


def log(message):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {message}")


# =============================================================================
# PURE HELPERS (no I/O — unit-tested)
# =============================================================================

# Query parameters that are pure tracking noise: drop them so the same article
# arriving via web + news + a shared link collapses to one canonical URL.
_TRACKING_KEYS = {
    "fbclid", "gclid", "gbraid", "wbraid", "msclkid", "dclid", "yclid",
    "mc_cid", "mc_eid", "igshid", "ref", "ref_src", "ref_url", "source",
    "spm", "_hsenc", "_hsmi", "vero_id", "oly_anon_id", "oly_enc_id",
}


def canonical_url(url):
    """Normalize a URL for cross-source dedup.

    Forces https, lowercases the host, drops ``www.``/``m.`` and an AMP suffix,
    strips tracking query params + fragment + trailing slash. Two URLs that point
    at the same content (http vs https, amp, utm tags, mobile host) collapse to
    one key — so we never report the same article twice.
    """
    raw = (url or "").strip()
    try:
        p = urlparse(raw)
    except Exception:
        return raw.lower()
    if not p.scheme and not p.netloc:
        return raw.lower()

    host = (p.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if host.startswith("m."):
        host = host[2:]
    if host.startswith("amp."):
        host = host[4:]

    path = p.path or "/"
    # strip a trailing /amp or /amp/ segment (a common AMP variant)
    low = path.lower()
    if low.endswith("/amp"):
        path = path[:-4] or "/"
    elif low.endswith("/amp/"):
        path = path[:-5] or "/"
    if len(path) > 1:
        path = path.rstrip("/") or "/"

    kept = [
        (k, v) for k, v in parse_qsl(p.query, keep_blank_values=False)
        if not k.lower().startswith("utm_") and k.lower() not in _TRACKING_KEYS
    ]
    query = urlencode(sorted(kept))
    return urlunparse(("https", host, path, "", query, ""))


def domain_of(url):
    host = (urlparse(url or "").hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    return host


def is_excluded_domain(url, excluded):
    """True if ``url``'s host equals or is a subdomain of any excluded domain.

    Proper domain matching (not substring): excluding ``example.com`` blocks
    ``example.com`` and ``blog.example.com`` but NOT ``notexample.com``.
    """
    host = domain_of(url)
    if not host:
        return False
    for raw in excluded or []:
        d = (raw or "").strip().lower().lstrip(".")
        if d and (host == d or host.endswith("." + d)):
            return True
    return False


def matches_keyword(keyword, *texts):
    """Loose relevance check for fuzzy sources (HN/Reddit) whose search is not
    exact-phrase: keep a hit only if the keyword actually appears in its text."""
    needle = (keyword or "").lower()
    return any(needle in (t or "").lower() for t in texts)


def prune_window(items, cutoff_iso, *, key="found_at"):
    """Keep only items whose ISO ``key`` timestamp is >= cutoff (lexicographic
    compare is valid for same-format naive ISO strings)."""
    return [it for it in items if (it.get(key) or "") >= cutoff_iso]


def _strip_html(text):
    out, depth = [], 0
    for ch in text or "":
        if ch == "<":
            depth += 1
        elif ch == ">":
            depth = max(0, depth - 1)
        elif depth == 0:
            out.append(ch)
    return "".join(out).strip()


def _iso_from_epoch(epoch):
    try:
        return datetime.fromtimestamp(int(epoch)).isoformat()
    except (TypeError, ValueError, OSError):
        return ""


def _mention(keyword, title, url, snippet, source, published=""):
    return {
        "keyword": keyword,
        "title": (title or "(no title)").strip(),
        "url": url,
        "canonical": canonical_url(url),
        "snippet": (snippet or "").strip()[:400],
        "source": source,         # web | news | hackernews | reddit
        "published": published,   # source's own date string (best effort)
        "found_at": datetime.now().isoformat(),
        "source_type": "",        # filled by AI enrichment (Stage 4)
        "sentiment": "",          # filled by AI enrichment (Stage 4)
    }


# =============================================================================
# SOURCES (network) — each returns a list of normalized mention dicts
# =============================================================================

def _serper_headers():
    return {"X-API-KEY": SERPER_API_KEY, "Content-Type": "application/json"}


def search_serper_web(keyword, num):
    resp = requests.post(
        SERPER_WEB_URL, headers=_serper_headers(),
        json={"q": f'"{keyword}"', "num": num, "tbs": "qdr:w"},
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    out = []
    for r in resp.json().get("organic", []):
        link = r.get("link") or ""
        if link:
            out.append(_mention(keyword, r.get("title"), link, r.get("snippet"), "web", r.get("date", "")))
    return out


def search_serper_news(keyword, num):
    resp = requests.post(
        SERPER_NEWS_URL, headers=_serper_headers(),
        json={"q": f'"{keyword}"', "num": num, "tbs": "qdr:w"},
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    out = []
    for r in resp.json().get("news", []):
        link = r.get("link") or ""
        if link:
            out.append(_mention(keyword, r.get("title"), link, r.get("snippet"), "news", r.get("date", "")))
    return out


def search_hackernews(keyword, since_ts):
    resp = requests.get(
        HN_SEARCH_URL,
        params={
            "query": keyword,
            "tags": "(story,comment)",
            "numericFilters": f"created_at_i>{since_ts}",
            "hitsPerPage": 50,
        },
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    out = []
    for h in resp.json().get("hits", []):
        title = h.get("title") or h.get("story_title") or ""
        body = _strip_html(h.get("story_text") or h.get("comment_text") or "")
        url = h.get("url") or h.get("story_url") or f"https://news.ycombinator.com/item?id={h.get('objectID')}"
        # Algolia is full-text + typo-tolerant; keep only true keyword hits.
        if not matches_keyword(keyword, title, body):
            continue
        out.append(_mention(keyword, title or "(Hacker News item)", url, body, "hackernews",
                            _iso_from_epoch(h.get("created_at_i"))))
    return out


def reddit_token():
    resp = requests.post(
        REDDIT_TOKEN_URL,
        auth=(REDDIT_CLIENT_ID, REDDIT_CLIENT_SECRET),
        data={"grant_type": "client_credentials"},
        headers={"User-Agent": USER_AGENT},
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def search_reddit(keyword, num, token):
    resp = requests.get(
        REDDIT_SEARCH_URL,
        params={"q": f'"{keyword}"', "sort": "new", "limit": num, "type": "link"},
        headers={"Authorization": f"bearer {token}", "User-Agent": USER_AGENT},
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    out = []
    for child in resp.json().get("data", {}).get("children", []):
        d = child.get("data", {})
        title = d.get("title") or ""
        body = (d.get("selftext") or "")[:400]
        if not matches_keyword(keyword, title, body):
            continue
        # The mention URL is the reddit thread itself (not any external link it
        # points to), so it dedupes as a distinct community mention.
        permalink = "https://www.reddit.com" + (d.get("permalink") or "")
        out.append(_mention(keyword, title, permalink, body, "reddit",
                            _iso_from_epoch(d.get("created_utc"))))
    return out


# =============================================================================
# DATA STORE: progress heartbeat + run history (best effort)
# =============================================================================

def write_progress(state, *, index=0, total=0, keyword="", phase=""):
    """Live-progress heartbeat the plugin page polls while a run is in flight.
    Tagged with this run's id so a previous run's bar never lingers. A failure
    here never affects tracking."""
    try:
        from pyrunner_datastore import DataStore

        DataStore(STATE_STORE)["progress"] = {
            "run_id": RUN_ID,
            "state": state,        # "running" | "done"
            "index": index,
            "total": total,
            "keyword": keyword,
            "phase": phase,
            "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
    except Exception:
        pass


def record_run(store, *, status, new_mentions, total_time, credits, cap_reached,
               source_errors, seeded_run):
    """Append a compact record of this run to the data store for the dashboard."""
    by_source = Counter(m["source"] for m in new_mentions)
    by_keyword = Counter(m["keyword"] for m in new_mentions)
    record = {
        "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "date": datetime.now().strftime("%Y-%m-%d"),
        "status": status,                       # success | partial | failed
        "duration_s": round(total_time, 1),
        "new_count": len(new_mentions),
        "by_source": dict(by_source),
        "by_keyword": dict(by_keyword),
        "credits_used_this_month": credits.get("estimated_credits", 0),
        "cap_reached": cap_reached,
        "seeded_run": seeded_run,               # first run = feed populated, email suppressed
        "errors": source_errors,
    }
    try:
        runs = store.get("runs", [])
        if not isinstance(runs, list):
            runs = []
        runs.append(record)
        store["runs"] = runs[-HISTORY_LIMIT:]
    except Exception as exc:
        log(f"⚠️  Dashboard: failed to record run ({exc}).")


# =============================================================================
# AI ENRICHMENT (optional)  —  off | claude (platform) | openrouter (BYO key)
# =============================================================================
#
# Tags each NEW mention with a source_type + sentiment. Cost-bounded: only new
# mentions, batched ENRICH_BATCH per call, at most ENRICH_MAX per run. Degrades
# silently (mentions stored unenriched) if the provider is unavailable or a batch
# fails — enrichment never breaks a tracking run.

_ENRICH_SYSTEM = (
    "You classify brand/keyword mentions. Respond with a SINGLE valid JSON object "
    "and nothing else — no prose, no markdown, no code fences."
)


def _claude_available():
    """True only if a Claude credential is injected AND the SDK is installed."""
    if not (os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") or os.environ.get("ANTHROPIC_API_KEY")):
        return False
    try:
        import claude_agent_sdk  # noqa: F401
        return True
    except Exception:
        return False


def _provider_available():
    if ENRICH_PROVIDER == "claude":
        return _claude_available()
    if ENRICH_PROVIDER == "openrouter":
        return bool(OPENROUTER_API_KEY)
    return False


def _build_classify_prompt(batch):
    lines = []
    for i, m in enumerate(batch):
        lines.append(
            f'{i}. keyword="{m["keyword"]}" title="{m["title"]}" '
            f'snippet="{m["snippet"][:200]}" url={m["url"]}'
        )
    return (
        "For each numbered item, classify:\n"
        "- source_type: one of news, blog, forum, social, docs, other\n"
        "- sentiment toward the keyword/brand: one of positive, neutral, negative\n\n"
        'Return ONLY {"results": [{"i": <index>, "source_type": "...", "sentiment": "..."}, ...]} '
        f"with one object per item ({len(batch)} total), same order.\n\n"
        + "\n".join(lines)
    )


def _classify_openrouter(prompt):
    model = ENRICH_MODEL or OPENROUTER_DEFAULT_MODEL
    resp = requests.post(
        OPENROUTER_URL,
        headers={"Authorization": f"Bearer {OPENROUTER_API_KEY}", "Content-Type": "application/json"},
        json={
            "model": model,
            "messages": [
                {"role": "system", "content": _ENRICH_SYSTEM},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0,
            "response_format": {"type": "json_object"},
        },
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]


def _classify(prompt):
    """Dispatch one batch prompt to the configured provider; return raw text."""
    if ENRICH_PROVIDER == "claude":
        from pyrunner_ai import ask_claude

        return ask_claude(
            prompt, tools=[], system_prompt=_ENRICH_SYSTEM,
            model=(ENRICH_MODEL or None), lean=True,
        )
    return _classify_openrouter(prompt)


def _parse_classifications(text):
    """Parse a model reply into {index: {source_type, sentiment}} (validated).

    Tolerates a bare array, an object with ``results``, and ```json fences``.
    Unknown categories fall back to other/neutral; junk yields ``{}``."""
    import json

    s = (text or "").strip()
    if s.startswith("```"):
        s = s.split("```", 2)[1] if "```" in s[3:] else s[3:]
        if s.lstrip().lower().startswith("json"):
            s = s.lstrip()[4:]
        s = s.strip().rstrip("`").strip()
    try:
        data = json.loads(s)
    except Exception:
        return {}
    rows = data.get("results", []) if isinstance(data, dict) else data
    if not isinstance(rows, list):
        return {}
    out = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            idx = int(row.get("i"))
        except (TypeError, ValueError):
            continue
        st = str(row.get("source_type", "")).lower().strip()
        se = str(row.get("sentiment", "")).lower().strip()
        out[idx] = {
            "source_type": st if st in _SOURCE_TYPES else "other",
            "sentiment": se if se in _SENTIMENTS else "neutral",
        }
    return out


def _apply_classifications(batch, parsed):
    for i, m in enumerate(batch):
        tags = parsed.get(i)
        if tags:
            m["source_type"] = tags["source_type"]
            m["sentiment"] = tags["sentiment"]


def enrich_mentions(new_mentions):
    """Tag new mentions with source_type + sentiment via the configured provider.

    No-op when enrichment is off or the provider is unavailable; the list is
    always returned (enriched in place) so the main flow is unaffected."""
    if not new_mentions or ENRICH_PROVIDER not in ("claude", "openrouter"):
        return new_mentions
    if not _provider_available():
        log(f"🤖 Enrichment '{ENRICH_PROVIDER}' unavailable — storing mentions unenriched.")
        return new_mentions

    to_enrich = new_mentions[:ENRICH_MAX]
    enriched = 0
    for start in range(0, len(to_enrich), ENRICH_BATCH):
        batch = to_enrich[start:start + ENRICH_BATCH]
        try:
            parsed = _parse_classifications(_classify(_build_classify_prompt(batch)))
            _apply_classifications(batch, parsed)
            enriched += len(parsed)
        except Exception as exc:
            log(f"🤖 Enrichment batch failed ({exc.__class__.__name__}) — left unenriched.")

    log(f"🤖 Enriched {enriched}/{len(to_enrich)} mention(s) via {ENRICH_PROVIDER}.")
    if len(new_mentions) > ENRICH_MAX:
        log(f"🤖 {len(new_mentions) - ENRICH_MAX} mention(s) over the per-run ceiling "
            f"({ENRICH_MAX}) left unenriched.")
    return new_mentions


# =============================================================================
# EMAIL REPORT (optional, Resend) — the *content* report of new mentions
# =============================================================================

_SOURCE_LABEL = {"web": "Web", "news": "News", "hackernews": "Hacker News", "reddit": "Reddit"}


def build_email_html(new_by_keyword):
    today = datetime.now().strftime("%B %d, %Y")
    total = sum(len(v) for v in new_by_keyword.values())
    rows = []
    for keyword, mentions in new_by_keyword.items():
        if not mentions:
            continue
        rows.append(
            f'<tr><td style="padding:24px 32px 8px 32px;border-bottom:2px solid #e5e7eb;">'
            f'<span style="font-size:16px;font-weight:600;color:#111827;">"{keyword}"</span>'
            f'<span style="background:#dbeafe;color:#1d4ed8;font-size:12px;font-weight:600;'
            f'padding:2px 9px;border-radius:10px;margin-left:8px;">{len(mentions)} new</span></td></tr>'
        )
        for m in mentions:
            label = _SOURCE_LABEL.get(m["source"], m["source"])
            rows.append(
                f'<tr><td style="padding:12px 32px 0 32px;">'
                f'<a href="{m["url"]}" style="color:#1d4ed8;font-size:15px;font-weight:600;'
                f'text-decoration:none;">{m["title"]}</a>'
                f'<span style="background:#f1f5f9;color:#475569;font-size:11px;font-weight:600;'
                f'padding:1px 7px;border-radius:8px;margin-left:8px;">{label}</span>'
                f'<p style="color:#6b7280;font-size:12px;margin:4px 0 0 0;word-break:break-all;">{m["url"]}</p>'
                f'<p style="color:#4b5563;font-size:14px;margin:6px 0 10px 0;line-height:1.5;">{m["snippet"]}</p>'
                f'</td></tr>'
            )
    return (
        '<html><body style="margin:0;background:#f4f5f7;font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;">'
        '<table width="100%" cellpadding="0" cellspacing="0" style="background:#f4f5f7;padding:32px 0;"><tr><td align="center">'
        '<table width="620" cellpadding="0" cellspacing="0" style="background:#fff;border-radius:12px;overflow:hidden;">'
        '<tr><td style="background:linear-gradient(135deg,#1a1a2e,#0f3460);padding:28px 32px;">'
        '<h1 style="color:#fff;margin:0;font-size:20px;">🔍 Brand Tracker Report</h1>'
        f'<p style="color:#94a3b8;margin:4px 0 0 0;font-size:13px;">{today} — {total} new mention(s)</p></td></tr>'
        + "".join(rows)
        + '<tr><td style="padding:24px 32px;text-align:center;color:#9ca3af;font-size:11px;">'
        'Powered by PyRun Brand Tracker</td></tr></table></td></tr></table></body></html>'
    )


def send_email_report(new_by_keyword):
    total = sum(len(v) for v in new_by_keyword.values())
    subject = f"🔍 Brand Tracker: {total} new mention{'s' if total != 1 else ''}"
    try:
        resp = requests.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {RESEND_API_KEY}", "Content-Type": "application/json"},
            json={"from": EMAIL_FROM, "to": [EMAIL_TO], "subject": subject,
                  "html": build_email_html(new_by_keyword)},
            timeout=REQUEST_TIMEOUT,
        )
        if resp.status_code in (200, 201):
            log("📧 Email report sent.")
        else:
            log(f"⚠️  Email report failed: HTTP {resp.status_code} — {resp.text[:200]}")
    except requests.exceptions.RequestException as exc:
        log(f"⚠️  Email report error: {exc}")


# =============================================================================
# MAIN
# =============================================================================

def main():
    log("=" * 60)
    log("🔍 BRAND TRACKER — Starting (weekly run)")
    log("=" * 60)

    if not SERPER_API_KEY:
        log("❌ SERPER_API_KEY is not set. Configure it on the plugin page.")
        sys.exit(1)
    if not KEYWORDS:
        log("⚠️  No keywords configured — nothing to track. Add keywords on the plugin page.")
        return

    try:
        from pyrunner_datastore import DataStore
    except Exception:
        log("❌ pyrunner_datastore unavailable — must run under PyRun.")
        sys.exit(1)

    store = DataStore(STATE_STORE)
    seen = store.get("seen", {}) or {}
    mentions = store.get("mentions", []) or []
    stats = store.get("stats", {}) or {}
    credits = store.get("credits", {}) or {}
    is_first_run = not stats.get("seeded")

    # ---- Monthly Serper credit counter (resets when the month rolls over) ----
    month = datetime.now().strftime("%Y-%m")
    if credits.get("month") != month:
        credits = {"month": month, "serper_requests": 0, "estimated_credits": 0}
    per_request_cost = 1 if NUM_RESULTS <= 10 else 2

    def serper_allowed():
        if MONTHLY_CREDIT_CAP <= 0:
            return True
        return credits["estimated_credits"] < MONTHLY_CREDIT_CAP

    def charge_serper():
        credits["serper_requests"] += 1
        credits["estimated_credits"] += per_request_cost

    log(f"🎯 {len(KEYWORDS)} keyword(s) · sources: web"
        f"{' news' if NEWS_ENABLED else ''}"
        f"{' hackernews' if HACKERNEWS_ENABLED else ''}"
        f"{' reddit' if REDDIT_ENABLED else ''}"
        f" · {'FIRST RUN (email suppressed)' if is_first_run else 'incremental'}")

    # ---- Reddit OAuth token (once, if enabled + credentials present) ----
    reddit_tok = None
    source_errors = []
    if REDDIT_ENABLED and REDDIT_CLIENT_ID and REDDIT_CLIENT_SECRET:
        try:
            reddit_tok = reddit_token()
        except Exception as exc:
            source_errors.append(f"reddit-auth: {exc.__class__.__name__}")
            log(f"⚠️  Reddit auth failed ({exc}); skipping Reddit this run.")

    hn_since = int((datetime.now() - timedelta(days=8)).timestamp())
    cap_reached = False
    new_mentions = []

    # ---- Gather across sources, dedupe, collect new ----
    total = len(KEYWORDS)
    for i, keyword in enumerate(KEYWORDS, 1):
        write_progress("running", index=i, total=total, keyword=keyword, phase="searching")
        log(f"\n  [{i}/{total}] \"{keyword}\"")
        results = []

        # Serper web (always on) — credit-gated
        if serper_allowed():
            try:
                results += search_serper_web(keyword, NUM_RESULTS)
                charge_serper()
            except Exception as exc:
                source_errors.append(f"web:{keyword}: {exc.__class__.__name__}")
                log(f"    ⚠️  web error: {exc}")
        else:
            cap_reached = True

        # Serper news — credit-gated
        if NEWS_ENABLED:
            if serper_allowed():
                try:
                    results += search_serper_news(keyword, NUM_RESULTS)
                    charge_serper()
                except Exception as exc:
                    source_errors.append(f"news:{keyword}: {exc.__class__.__name__}")
                    log(f"    ⚠️  news error: {exc}")
            else:
                cap_reached = True

        # Hacker News (free)
        if HACKERNEWS_ENABLED:
            try:
                results += search_hackernews(keyword, hn_since)
            except Exception as exc:
                source_errors.append(f"hn:{keyword}: {exc.__class__.__name__}")
                log(f"    ⚠️  hackernews error: {exc}")

        # Reddit (free)
        if reddit_tok:
            try:
                results += search_reddit(keyword, NUM_RESULTS, reddit_tok)
            except Exception as exc:
                source_errors.append(f"reddit:{keyword}: {exc.__class__.__name__}")
                log(f"    ⚠️  reddit error: {exc}")

        # Dedupe (canonical) + drop excluded + collect new
        kept = 0
        for m in results:
            canon = m["canonical"]
            if not canon or is_excluded_domain(m["url"], EXCLUDED_DOMAINS):
                continue
            if canon in seen:
                continue
            seen[canon] = {"keyword": keyword, "source": m["source"], "found_at": m["found_at"]}
            mentions.append(m)
            new_mentions.append(m)
            kept += 1
        log(f"    → {len(results)} result(s), {kept} new")

    if cap_reached:
        log(f"\n🛑 Monthly Serper credit cap ({MONTHLY_CREDIT_CAP}) reached — some Serper "
            f"searches were skipped. Free sources still ran.")

    # ---- Enrichment (Stage 4 no-op for now) ----
    new_mentions = enrich_mentions(new_mentions)

    # ---- Retention prune + stats recompute ----
    cutoff_iso = (datetime.now() - timedelta(days=RETENTION_DAYS)).isoformat()
    before = len(mentions)
    mentions = prune_window(mentions, cutoff_iso)[-MAX_MENTIONS:]
    seen = {k: v for k, v in seen.items() if (v.get("found_at") or "") >= cutoff_iso}
    pruned = before - len(mentions)

    stats = {
        "seeded": True,
        "last_run": datetime.now().isoformat(),
        "window_total": len(mentions),
        "total_all_time": int(stats.get("total_all_time", 0)) + len(new_mentions),
        "by_keyword": dict(Counter(m["keyword"] for m in mentions)),
        "by_source": dict(Counter(m["source"] for m in mentions)),
    }

    store["seen"] = seen
    store["mentions"] = mentions
    store["stats"] = stats
    store["credits"] = credits

    # ---- Run record + email ----
    status = "partial" if source_errors else "success"
    record_run(store, status=status, new_mentions=new_mentions, total_time=0.0,
               credits=credits, cap_reached=cap_reached, source_errors=source_errors,
               seeded_run=is_first_run)
    write_progress("done", index=total, total=total)

    log("\n" + "=" * 60)
    log(f"🏁 DONE — {len(new_mentions)} new mention(s), {len(mentions)} in feed "
        f"(pruned {pruned}), ~{credits['estimated_credits']} Serper credits this month")
    log("=" * 60)

    if new_mentions and not is_first_run and EMAIL_ENABLED and RESEND_API_KEY and EMAIL_TO and EMAIL_FROM:
        new_by_keyword = defaultdict(list)
        for m in new_mentions:
            new_by_keyword[m["keyword"]].append(m)
        send_email_report(new_by_keyword)
    elif is_first_run and new_mentions:
        log("📧 First run — email suppressed (feed populated). Future runs will email new mentions.")

    # A run with source errors is worth an operational alert, but only if nothing
    # at all came back is it a hard failure.
    if source_errors and not new_mentions:
        sys.exit(1)


if __name__ == "__main__":
    main()
