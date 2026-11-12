#!/usr/bin/env python3
"""Claude Code usage collector for the kov.claude-usage panel.

Each ~/.claude* config dir is a profile ("claude", "work", ...). Reads:
  * token usage from every assistant record in <dir>/projects/**/*.jsonl
  * plan limits from Claude Code's own cache (<config>.cachedUsageUtilization),
    refreshed from the OAuth usage endpoint when stale (read-only: the stored
    access token is used as-is and never refreshed or rewritten)

  * prompts and sessions per day from <dir>/history.jsonl, which outlives the
    transcripts and fills in activity (not tokens) for older days

Daily totals are kept in ~/.local/state/kov-claude-usage/history.json so the
heatmap outlives Claude Code's ~30 day transcript cleanup.

Usage: collect.py [--no-network] [--limits-only] [--max-age SECS]
"""

import datetime
import glob
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
DEFAULT_MAX_AGE = 300      # refresh limits older than this when online
RETRY_AFTER_FAIL = 300     # back off this long after a failed fetch
HISTORY_VERSION = 1

# $/MTok: input, output, cache read. Cache writes: 5m = 1.25x input,
# 1h = 2x input. Fast mode doubles everything. Longest prefix wins.
PRICES = {
    "claude-fable-5-2": (8.0, 40.0, 0.20),
    "claude-mythos-5-2": (8.0, 40.0, 0.20),
    "claude-fable-5-1": (10.0, 50.0, 0.25),
    "claude-mythos-5-1": (10.0, 50.0, 0.25),
    "claude-fable-5": (10.0, 50.0, 1.00),
    "claude-mythos-5": (10.0, 50.0, 1.00),
    "claude-opus-5-5": (4.0, 20.0, 0.20),
    "claude-opus-5": (5.0, 25.0, 0.50),
    "claude-opus-4-8": (5.0, 25.0, 0.50),
    "claude-opus-4-7": (5.0, 25.0, 0.50),
    "claude-opus-4-6": (5.0, 25.0, 0.50),
    "claude-opus-4-5": (5.0, 25.0, 0.50),
    "claude-opus-4": (15.0, 75.0, 1.50),
    "claude-sonnet-5-5": (2.0, 10.0, 0.10),
    "claude-sonnet-5": (2.0, 10.0, 0.20),
    "claude-sonnet-4": (3.0, 15.0, 0.30),
    "claude-haiku-5-5": (0.10, 0.50, 0.01),
    "claude-haiku-4-5": (1.0, 5.0, 0.10),
    "claude-3-5-haiku": (0.80, 4.0, 0.08),
}
HAIKU_55_LONG = (0.50, 2.50, 0.05)  # prompts over 100K tokens
WEB_SEARCH_USD = 0.01


def normalize_model(name):
    name = re.sub(r"\[.*?\]$", "", str(name or ""))
    return re.sub(r"-\d{8}$", "", name)


def price_for(model):
    best = None
    for prefix in PRICES:
        if model == prefix or model.startswith(prefix + "-"):
            if best is None or len(prefix) > len(best):
                best = prefix
    return PRICES[best] if best else None


def cost_of(model, u):
    """API-equivalent USD for one usage block, or None if the model is unpriced."""
    p = price_for(model)
    if p is None:
        return None
    inp = u.get("input_tokens") or 0
    out = u.get("output_tokens") or 0
    cr = u.get("cache_read_input_tokens") or 0
    cw5, cw1h = cache_writes(u)
    if model.startswith("claude-haiku-5-5") and inp + cr + cw5 + cw1h > 100000:
        p = HAIKU_55_LONG
    pin, pout, pcr = p
    usd = (inp * pin + out * pout + cr * pcr + cw5 * pin * 1.25 + cw1h * pin * 2.0) / 1e6
    if u.get("speed") == "fast":
        usd *= 2
    web = ((u.get("server_tool_use") or {}).get("web_search_requests") or 0)
    return usd + web * WEB_SEARCH_USD


def cache_writes(u):
    total = u.get("cache_creation_input_tokens") or 0
    split = u.get("cache_creation") or {}
    h1 = split.get("ephemeral_1h_input_tokens") or 0
    m5 = split.get("ephemeral_5m_input_tokens") or 0
    if h1 + m5 == 0:
        m5 = total  # older records: no split, priced as 5-minute writes
    return m5, h1


# ---------------------------------------------------------------- profiles --

def profile_name(dirname):
    base = os.path.basename(dirname.rstrip("/"))
    if base == ".claude":
        return "claude"
    return re.sub(r"^\.claude[-_.]?", "", base) or base


def discover_profiles(home):
    out = []
    for d in sorted(glob.glob(os.path.join(home, ".claude*"))):
        if os.path.isdir(os.path.join(d, "projects")):
            out.append({"id": profile_name(d), "dir": d})
    return out


def load_json(path, default=None):
    try:
        with open(path, "r") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = "%s.%d.tmp" % (path, os.getpid())
    with open(tmp, "w") as f:
        json.dump(data, f, separators=(",", ":"))
    os.replace(tmp, path)


def config_file(home, profile_dir):
    inside = os.path.join(profile_dir, ".claude.json")
    if os.path.exists(inside):
        return inside
    if os.path.basename(profile_dir.rstrip("/")) == ".claude":
        return os.path.join(home, ".claude.json")  # default CLAUDE_CONFIG_DIR
    return inside


PLAN_NAMES = {"pro": "Pro", "max": "Max", "team": "Team", "enterprise": "Enterprise"}


def plan_label(sub, tier):
    m = re.search(r"max_(\d+)x", tier or "")
    parts = []
    if sub:
        parts.append(PLAN_NAMES.get(sub, sub.title()))
    if m and sub != "max":
        parts.append("Max %sx" % m.group(1))
    elif m:
        parts[-1] = "Max %sx" % m.group(1)
    return " · ".join(parts)


def read_profile_meta(home, prof):
    cfg = load_json(config_file(home, prof["dir"]), {}) or {}
    creds = (load_json(os.path.join(prof["dir"], ".credentials.json"), {}) or {}).get("claudeAiOauth") or {}
    acct = cfg.get("oauthAccount") or {}
    prof["accountKey"] = "%s:%s" % (acct.get("accountUuid", ""), acct.get("organizationUuid", "")) if acct else ""
    prof["email"] = acct.get("emailAddress", "")
    prof["org"] = acct.get("organizationName", "")
    prof["plan"] = plan_label(creds.get("subscriptionType"), creds.get("rateLimitTier") or acct.get("userRateLimitTier"))
    prof["_cached"] = cfg.get("cachedUsageUtilization")
    prof["_token"] = creds.get("accessToken")
    prof["_expires"] = (creds.get("expiresAt") or 0) / 1000.0
    return prof


# ------------------------------------------------------------------- usage --

def local_day(ts):
    t = datetime.datetime.fromisoformat(ts.replace("Z", "+00:00"))
    return t.astimezone().strftime("%Y-%m-%d"), t.timestamp()


def scan_usage(profiles):
    """Rows [day, profile, model, in, out, cw5, cw1h, cr, web, cost, msgs],
    sessions {day: {profile: n}}, last activity per profile, unpriced models."""
    seen = {}
    sessions = {}
    last = {}
    for prof in profiles:
        files = glob.glob(os.path.join(prof["dir"], "projects", "**", "*.jsonl"), recursive=True)
        for path in files:
            try:
                fh = open(path, "r", errors="replace")
            except OSError:
                continue
            with fh:
                for line in fh:
                    if '"usage"' not in line or '"assistant"' not in line:
                        continue
                    try:
                        o = json.loads(line)
                    except ValueError:
                        continue
                    if o.get("type") != "assistant":
                        continue
                    m = o.get("message") or {}
                    u = m.get("usage")
                    model = normalize_model(m.get("model"))
                    if not isinstance(u, dict) or not model or model.startswith("<"):
                        continue
                    try:
                        day, ts = local_day(o.get("timestamp", ""))
                    except (ValueError, TypeError):
                        continue
                    key = (m.get("id"), o.get("requestId"))
                    if key == (None, None):
                        key = ("uuid", o.get("uuid"))
                    prev = seen.get(key)
                    # Copies of a reply carry the same usage; keep the fullest.
                    if prev and (prev[3].get("output_tokens") or 0) >= (u.get("output_tokens") or 0):
                        continue
                    seen[key] = (day, prof["id"], model, u)
                    sid = o.get("sessionId")
                    if sid and not o.get("isSidechain"):
                        sessions.setdefault(day, {}).setdefault(prof["id"], set()).add(sid)
                    if ts > last.get(prof["id"], 0):
                        last[prof["id"]] = ts

    groups = {}
    unpriced = set()
    for day, pid, model, u in seen.values():
        r = groups.get((day, pid, model))
        if r is None:
            r = groups[(day, pid, model)] = [day, pid, model, 0, 0, 0, 0, 0, 0, 0.0, 0]
        cw5, cw1h = cache_writes(u)
        r[3] += u.get("input_tokens") or 0
        r[4] += u.get("output_tokens") or 0
        r[5] += cw5
        r[6] += cw1h
        r[7] += u.get("cache_read_input_tokens") or 0
        r[8] += (u.get("server_tool_use") or {}).get("web_search_requests") or 0
        c = cost_of(model, u)
        if c is None:
            unpriced.add(model)
        else:
            r[9] += c
        r[10] += 1
    rows = sorted(groups.values())
    for r in rows:
        r[9] = round(r[9], 4)
    sess = {d: {p: len(s) for p, s in v.items()} for d, v in sessions.items()}
    return rows, sess, last, sorted(unpriced)


def scan_prompts(profiles):
    """{day: {profile: [prompts, sessions]}} from each dir's history.jsonl, the
    log of typed prompts. Claude Code keeps it long after the transcripts are
    gone, so it fills in activity (but not tokens) for older days."""
    out = {}
    for prof in profiles:
        try:
            fh = open(os.path.join(prof["dir"], "history.jsonl"), "r", errors="replace")
        except OSError:
            continue
        days = {}
        with fh:
            for line in fh:
                try:
                    o = json.loads(line)
                    day = datetime.datetime.fromtimestamp(o["timestamp"] / 1000.0).strftime("%Y-%m-%d")
                except (ValueError, TypeError, KeyError, OverflowError, OSError):
                    continue
                d = days.setdefault(day, [0, set()])
                d[0] += 1
                if o.get("sessionId"):
                    d[1].add(o["sessionId"])
        for day, (n, sids) in days.items():
            out.setdefault(day, {})[prof["id"]] = [n, len(sids)]
    return out


def merge_prompts(stored, fresh):
    """Per (day, profile): the larger prompt count wins, so trimming the log
    never loses days already seen."""
    out = {day: dict(v) for day, v in (stored or {}).items() if isinstance(v, dict)}
    for day, byp in fresh.items():
        for pid, cur in byp.items():
            old = out.get(day, {}).get(pid)
            if not old or cur[0] >= old[0]:
                out.setdefault(day, {})[pid] = cur
    return out


def row_tokens(r):
    return r[3] + r[4] + r[5] + r[6] + r[7]


def merge_history(stored, rows, sessions, prompts=None):
    """Per (day, profile): keep the fresh scan unless the stored copy holds
    more tokens (its transcripts have since been cleaned up). Prompt counts
    merge separately (see merge_prompts)."""
    def group(rs):
        g = {}
        for r in rs:
            g.setdefault((r[0], r[1]), []).append(r)
        return g

    old = group(stored.get("rows") or [])
    new = group(rows)
    old_sess = stored.get("sessions") or {}
    out_rows, out_sess = [], {}
    for key in set(old) | set(new):
        day, pid = key
        o, n = old.get(key, []), new.get(key, [])
        use_new = bool(n) and sum(map(row_tokens, n)) >= sum(map(row_tokens, o))
        out_rows.extend(n if use_new else o)
        s = (sessions.get(day, {}).get(pid) if use_new else old_sess.get(day, {}).get(pid))
        if s:
            out_sess.setdefault(day, {})[pid] = s
    out_rows.sort()
    return {"version": HISTORY_VERSION, "rows": out_rows, "sessions": out_sess,
            "prompts": merge_prompts(stored.get("prompts"), prompts or {})}


# ------------------------------------------------------------------ limits --

def iso_epoch(s):
    if not s:
        return None
    try:
        return datetime.datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


LIMIT_LABELS = {"session": "5-hour", "weekly_all": "Weekly"}
LEGACY_LIMITS = [("five_hour", "5-hour"), ("seven_day", "Weekly"),
                 ("seven_day_opus", "Weekly · Opus"), ("seven_day_sonnet", "Weekly · Sonnet")]


def normalize_limits(util):
    out = []
    if not isinstance(util, dict):
        return out
    if isinstance(util.get("limits"), list) and util["limits"]:
        for l in util["limits"]:
            if not isinstance(l, dict) or l.get("percent") is None:
                continue
            kind = l.get("kind", "")
            label = LIMIT_LABELS.get(kind)
            scope = ((l.get("scope") or {}).get("model") or {}).get("display_name")
            if not label:
                label = "Weekly" if l.get("group") == "weekly" else kind.replace("_", " ").title()
                if scope:
                    label += " · " + scope
            out.append({"kind": kind, "label": label, "percent": l.get("percent"),
                        "resetsAt": iso_epoch(l.get("resets_at")), "severity": l.get("severity", ""),
                        "active": bool(l.get("is_active"))})
        return out
    for key, label in LEGACY_LIMITS:
        v = util.get(key)
        if isinstance(v, dict) and v.get("utilization") is not None:
            out.append({"kind": key, "label": label, "percent": v.get("utilization"),
                        "resetsAt": iso_epoch(v.get("resets_at")), "severity": "", "active": False})
    return out


def extra_usage(util):
    spend = (util or {}).get("spend")
    if not isinstance(spend, dict) or not spend.get("enabled"):
        return None
    used = spend.get("used") or {}
    lim = spend.get("limit") or {}
    def amount(a):
        if not isinstance(a, dict) or a.get("amount_minor") is None:
            return None
        return a["amount_minor"] / (10 ** (a.get("exponent") or 0))
    return {"used": amount(used), "limit": amount(lim), "currency": used.get("currency", ""),
            "percent": spend.get("percent")}


def fetch_usage(token):
    req = urllib.request.Request(USAGE_URL, headers={
        "Authorization": "Bearer " + token,
        "anthropic-beta": "oauth-2025-04-20",
        "Content-Type": "application/json",
        "User-Agent": "kov-claude-usage/1.0",
    })
    with urllib.request.urlopen(req, timeout=8) as r:
        return json.load(r)


def safe_name(key):
    return re.sub(r"[^A-Za-z0-9_-]", "_", key)[:80] or "unknown"


def account_limits(key, profs, cache_dir, now, online, max_age, fetch=fetch_usage):
    """Freshest known utilisation for one account, refreshed when stale."""
    best_at, best = 0, None
    for p in profs:
        c = p.get("_cached") or {}
        at = (c.get("fetchedAtMs") or 0) / 1000.0
        if c.get("utilization") and at > best_at:
            best_at, best = at, c["utilization"]
    own_path = os.path.join(cache_dir, "limits-%s.json" % safe_name(key))
    own = load_json(own_path, {}) or {}
    if own.get("utilization") and (own.get("fetchedAt") or 0) > best_at:
        best_at, best = own["fetchedAt"], own["utilization"]

    source, error = ("cache" if best else "none"), ""
    if online and now - best_at > max_age:
        attempt_path = os.path.join(cache_dir, "attempt-%s.json" % safe_name(key))
        last_try = (load_json(attempt_path, {}) or {}).get("at", 0)
        token = next((p["_token"] for p in profs if p.get("_token") and p.get("_expires", 0) > now + 60), None)
        if not token:
            error = "token-expired"
        elif now - last_try < RETRY_AFTER_FAIL:
            error = "backoff"
        else:
            try:
                util = fetch(token)
                if isinstance(util, dict):
                    best_at, best, source = now, util, "live"
                    write_json(own_path, {"fetchedAt": now, "utilization": util})
            except (urllib.error.URLError, OSError, ValueError) as e:
                code = getattr(e, "code", None)
                error = "http-%s" % code if code else "offline"
                write_json(attempt_path, {"at": now, "error": error})
    return {"limits": normalize_limits(best), "extra": extra_usage(best),
            "fetchedAt": best_at or None, "source": source, "error": error}


# -------------------------------------------------------------------- main --

def parse_args(argv):
    opts = {"online": True, "limits_only": False, "max_age": DEFAULT_MAX_AGE}
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--no-network":
            opts["online"] = False
        elif a == "--limits-only":
            opts["limits_only"] = True
        elif a == "--max-age" and i + 1 < len(argv):
            i += 1
            try:
                opts["max_age"] = max(0, int(argv[i]))
            except ValueError:
                pass
        i += 1
    return opts


def collect(home, state_dir, cache_dir, now, online=True, limits_only=False,
            max_age=DEFAULT_MAX_AGE, fetch=fetch_usage):
    profiles = [read_profile_meta(home, p) for p in discover_profiles(home)]
    out = {"now": now, "profiles": [], "accounts": [], "rows": [], "sessions": {},
           "prompts": {}, "unpriced": [], "limitsOnly": limits_only}

    if not limits_only:
        rows, sessions, last, unpriced = scan_usage(profiles)
        hist_path = os.path.join(state_dir, "history.json")
        stored = load_json(hist_path, {}) or {}
        merged = merge_history(stored if stored.get("version") == HISTORY_VERSION else {},
                               rows, sessions, scan_prompts(profiles))
        if merged != {k: stored.get(k) for k in merged}:
            write_json(hist_path, merged)
        out["rows"], out["sessions"], out["unpriced"] = merged["rows"], merged["sessions"], unpriced
        out["prompts"] = merged["prompts"]
        for p in profiles:
            p["lastActive"] = last.get(p["id"])

    by_acct = {}
    for p in profiles:
        by_acct.setdefault(p["accountKey"] or ("none:" + p["id"]), []).append(p)
    for key, profs in by_acct.items():
        a = {"key": key, "profiles": [p["id"] for p in profs], "email": profs[0]["email"],
             "org": profs[0]["org"], "plan": profs[0]["plan"]}
        if profs[0]["accountKey"]:
            a.update(account_limits(key, profs, cache_dir, now, online, max_age, fetch))
        else:
            a.update({"limits": [], "extra": None, "fetchedAt": None, "source": "none", "error": "signed-out"})
        out["accounts"].append(a)

    known = {p["id"] for p in profiles}
    for p in profiles:
        out["profiles"].append({k: v for k, v in p.items() if not k.startswith("_")})
    # Profiles only present in history (their dir was removed) still show up.
    for r in out["rows"]:
        if r[1] not in known:
            known.add(r[1])
            out["profiles"].append({"id": r[1], "dir": "", "accountKey": "", "missing": True})
    return out


def main():
    opts = parse_args(sys.argv[1:])
    home = os.path.expanduser("~")
    state = os.path.join(os.environ.get("XDG_STATE_HOME") or os.path.join(home, ".local", "state"), "kov-claude-usage")
    cache = os.path.join(os.environ.get("XDG_CACHE_HOME") or os.path.join(home, ".cache"), "kov-claude-usage")
    out = collect(home, state, cache, time.time(), opts["online"], opts["limits_only"], opts["max_age"])
    json.dump(out, sys.stdout, separators=(",", ":"))


if __name__ == "__main__":
    main()
