#!/usr/bin/env python3
"""Every Claude Code agent on this machine, for the Agents view of kov.claude-usage.

One JSON document on stdout:
  {now, agents: [...], errors: [...]}

Sources, per config dir (~/.claude, ~/.claude-work, ...):
  * `claude agents --json --all` -- the list itself: interactive sessions and
    background jobs, with liveness. ~/.claude is the default dir, so it is run
    with CLAUDE_CONFIG_DIR unset: set to ~/.claude, Claude Code looks for its
    config inside the dir, finds none, and writes a fresh one there.
  * <dir>/jobs/<id>/state.json -- a background job's state, status line, what
    it is blocked on, its result and token count.
  * <dir>/sessions/<pid>.json -- when an interactive session's status changed.
  * <dir>/projects/*/<session>/subagents/*.meta.json -- subagents still running.
Plus the claude-waybar hook's records (~/.local/state/claude-waybar): the last
prompt of each session, "needs input" from permission prompts, and sessions on
remote hosts mirrored by claude-waybar-sync.

Usage: agents.py
"""

import glob
import json
import os
import re
import shutil
import subprocess
import sys
import time

REMOTE_STALE_AFTER = 60      # claude-waybar-sync rewrites a live host's file at least this often
SUBAGENT_STALE_AFTER = 600   # an unfinished subagent with no transcript write in this long is gone
CLI_TIMEOUT = 15

# Raw states -> the five the view groups by. Anything unknown falls back on
# whether the process is busy.
STATE_MAP = {
    "working": "working", "running": "working", "busy": "working", "active": "working",
    "blocked": "needs", "needs_input": "needs", "needs-input": "needs", "waiting": "needs",
    "done": "done", "completed": "done", "finished": "done", "stopped": "done", "cancelled": "done",
    "failed": "failed", "error": "failed", "errored": "failed", "crashed": "failed",
    "idle": "idle",
}


def load_json(path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def profile_name(dirname):
    """Same naming as collect.py: ~/.claude -> claude, ~/.claude-work -> work."""
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


def cli_env(profile_dir, base_env):
    env = {k: v for k, v in base_env.items() if k != "CLAUDE_CONFIG_DIR"}
    if os.path.basename(profile_dir.rstrip("/")) != ".claude":
        env["CLAUDE_CONFIG_DIR"] = profile_dir
    return env


def run_cli(profile_dir):
    """`claude agents --json --all` for one config dir -> list of entries."""
    exe = shutil.which("claude")
    if not exe:
        raise RuntimeError("claude not found on PATH")
    r = subprocess.run([exe, "agents", "--json", "--all"], env=cli_env(profile_dir, os.environ),
                       capture_output=True, text=True, timeout=CLI_TIMEOUT)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout or "exit %d" % r.returncode).strip().splitlines()[0][:160])
    data = json.loads(r.stdout or "[]")
    if not isinstance(data, list):
        raise RuntimeError("unexpected output")
    return data


def iso_epoch(s):
    if not isinstance(s, str) or not s:
        return None
    try:
        from datetime import datetime
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def ms(v):
    return v / 1000.0 if isinstance(v, (int, float)) and v > 0 else None


def one_line(s, limit=240):
    s = re.sub(r"\s+", " ", str(s or "")).strip()
    return s if len(s) <= limit else s[:limit - 1] + "…"


def waybar_records(state_dir):
    """claude-waybar hook records by session id."""
    out = {}
    for path in glob.glob(os.path.join(state_dir, "sessions", "*.json")):
        rec = load_json(path)
        if isinstance(rec, dict) and rec.get("session_id"):
            out[rec["session_id"]] = rec
    return out


def remote_agents(state_dir, now):
    out = []
    for path in sorted(glob.glob(os.path.join(state_dir, "remote", "*.state"))):
        host = os.path.basename(path)[: -len(".state")]
        try:
            if now - os.stat(path).st_mtime > REMOTE_STALE_AFTER:
                continue  # the sync link is down; what it last saw may be long gone
            with open(path, "r", encoding="utf-8") as f:
                lines = f.read().splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if not isinstance(rec, dict):
                continue
            state = str(rec.get("state") or "idle")
            cwd = str(rec.get("cwd") or "")
            out.append({
                "key": "remote:%s:%s" % (host, rec.get("session_id") or rec.get("pid") or cwd),
                "profile": "@" + host, "kind": "remote", "host": host,
                "id": None, "sessionId": rec.get("session_id"), "pid": None,
                "port": rec.get("port") or None,
                "name": os.path.basename(cwd.rstrip("/")) or host,
                "status": STATE_MAP.get(state, "idle"), "rawState": state,
                "detail": one_line(rec.get("detail")), "prompt": one_line(rec.get("prompt")),
                "cwd": cwd, "branch": None, "startedAt": None, "updatedAt": None,
                "tokens": None, "alive": True, "subagents": [], "links": [],
                "runningSince": None,  # mirrored records carry no start time
            })
    return out


def running_subagents(profile_dir, session_id, now):
    out = []
    pattern = os.path.join(profile_dir, "projects", "*", session_id, "subagents", "*.meta.json")
    for meta_path in glob.glob(pattern):
        meta = load_json(meta_path, {})
        if not isinstance(meta, dict) or meta.get("finished"):
            continue
        transcript = meta_path[: -len(".meta.json")] + ".jsonl"
        try:
            started = os.stat(meta_path).st_mtime
            last = os.stat(transcript).st_mtime if os.path.exists(transcript) else started
        except OSError:
            continue
        if now - last > SUBAGENT_STALE_AFTER:
            continue
        out.append({
            "id": os.path.basename(transcript)[: -len(".jsonl")],
            "description": one_line(meta.get("description"), 120),
            "type": meta.get("agentType") or "",
            "since": started, "lastActive": last,
        })
    out.sort(key=lambda s: s["since"])
    return out


def turn_ended(entry, job):
    """A background job whose last turn is over: its process is idle, its own
    tempo is not active and nothing it started is still in flight."""
    if entry.get("status") != "idle" or not isinstance(job, dict):
        return False
    if job.get("tempo") == "active":
        return False
    flight = job.get("inFlight")
    if isinstance(flight, dict) and ((flight.get("tasks") or 0) > 0 or (flight.get("queued") or 0) > 0):
        return False
    return True


def last_assistant_text(profile_dir, session_id, tail_bytes=262144):
    """The last thing the agent said, from the end of its transcript."""
    paths = glob.glob(os.path.join(profile_dir, "projects", "*", session_id + ".jsonl"))
    if not paths:
        return ""
    try:
        with open(paths[0], "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - tail_bytes))
            lines = f.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return ""
    for line in reversed(lines):
        try:
            rec = json.loads(line)
        except ValueError:
            continue  # the first line of the tail is usually cut
        if not isinstance(rec, dict) or rec.get("type") != "assistant":
            continue
        content = (rec.get("message") or {}).get("content")
        texts = [c.get("text") for c in content or [] if isinstance(c, dict) and c.get("type") == "text" and c.get("text")]
        if texts:
            return texts[-1]
    return ""


def classify(entry, job, session_file, waybar):
    """-> (status, rawState) for one `claude agents` entry."""
    status = str(entry.get("status") or "")
    if entry.get("kind") == "background":
        # The CLI's state is the live one; the job's state file only moves on
        # with the agent's next message (it says "blocked" for a while after
        # the question has been answered).
        raw = str(entry.get("state") or (job or {}).get("state") or status or "")
        mapped = STATE_MAP.get(raw)
        # A busy process is neither waiting on you nor finished: it has been
        # answered or prompted again and its state hasn't caught up yet.
        if mapped in ("done", "needs") and status == "busy":
            return "working", raw
        # The job state comes from the agent's messages, and only a closing
        # "result:" line moves it on; a job that wraps up without one stays
        # "working" for good. Its process and tempo know better.
        if mapped == "working" and turn_ended(entry, job):
            return "done", raw
        if mapped:
            return mapped, raw
        return ("working" if status == "busy" else "idle"), raw
    # Interactive: the hook knows about permission prompts, the CLI does not.
    if (waybar or {}).get("state") == "needs_input":
        return "needs", "needs_input"
    raw = status or str((session_file or {}).get("status") or "idle")
    return STATE_MAP.get(raw, "idle"), raw


def build_agent(prof, entry, job, session_file, waybar, now):
    kind = entry.get("kind") or "interactive"
    status, raw = classify(entry, job, session_file, waybar)
    last_said = ""
    if kind == "background" and status == "done" and raw == "working":
        # Ended its turn without a result line: its last message says what it
        # did. One that has said nothing yet was never given a prompt.
        last_said = last_assistant_text(prof["dir"], entry.get("sessionId") or "")
        if not last_said:
            status = "idle"
    job = job or {}
    waybar = waybar or {}
    session_file = session_file or {}
    pid = entry.get("pid")
    if kind == "background":
        if status == "needs":
            detail = job.get("needs") or job.get("detail")
        elif status in ("done", "failed"):
            detail = ((job.get("output") or {}).get("result") if isinstance(job.get("output"), dict) else None) \
                or last_said or job.get("detail")
        else:
            detail = job.get("detail")
        prompt = waybar.get("prompt") or job.get("intent")
        updated = iso_epoch(job.get("updatedAt"))
        tokens = job.get("tokens")
        try:
            tokens = int(tokens) if tokens is not None else None
        except (TypeError, ValueError):
            tokens = None
    else:
        detail = waybar.get("detail") if status == "needs" else ""
        prompt = waybar.get("prompt")
        updated = max([t for t in (ms(session_file.get("statusUpdatedAt")), waybar.get("ts")) if isinstance(t, (int, float))]
                      or [None]) if (session_file or waybar) else None
        tokens = None
    links = []
    for c in job.get("children") or []:
        if isinstance(c, dict) and c.get("href"):
            links.append({"kind": c.get("kind") or "", "id": str(c.get("id") or ""), "href": c["href"]})
    sid = entry.get("sessionId") or ""
    return {
        "key": "%s:%s:%s" % (prof["id"], kind, entry.get("id") or sid or pid),
        "profile": prof["id"], "configDir": prof["dir"], "kind": kind,
        "id": entry.get("id"), "sessionId": sid, "pid": pid if isinstance(pid, int) else None,
        "host": None, "port": None,
        "name": one_line(entry.get("name") or job.get("name") or os.path.basename(str(entry.get("cwd") or "")), 80),
        "status": status, "rawState": raw,
        "detail": one_line(detail), "prompt": one_line(prompt),
        "cwd": entry.get("cwd") or job.get("cwd") or "",
        "branch": job.get("worktreeBranch") or None,
        "startedAt": ms(entry.get("startedAt")), "updatedAt": updated,
        "tokens": tokens, "alive": isinstance(pid, int),
        # When the current run began: a busy session's status last changed when
        # it started working (it stays put through tool calls).
        "runningSince": ms(session_file.get("statusUpdatedAt"))
            if status == "working" and "busy" in (entry.get("status"), session_file.get("status")) else None,
        "subagents": running_subagents(prof["dir"], sid, now) if sid and isinstance(pid, int) else [],
        "links": links,
    }


def collect(home, state_dir, now, cli=run_cli):
    agents, errors = [], []
    waybar = waybar_records(state_dir)
    for prof in discover_profiles(home):
        try:
            entries = cli(prof["dir"])
        except Exception as e:  # one broken profile must not hide the others
            errors.append("%s: %s" % (prof["id"], one_line(e, 160)))
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            job = load_json(os.path.join(prof["dir"], "jobs", str(entry.get("id")), "state.json")) \
                if entry.get("id") else None
            sfile = load_json(os.path.join(prof["dir"], "sessions", "%s.json" % entry.get("pid"))) \
                if entry.get("pid") else None
            agents.append(build_agent(prof, entry, job, sfile, waybar.get(entry.get("sessionId")), now))
    agents.extend(remote_agents(state_dir, now))
    return {"now": now, "agents": agents, "errors": errors}


def main():
    home = os.path.expanduser("~")
    state_dir = os.path.join(os.environ.get("XDG_STATE_HOME") or os.path.join(home, ".local", "state"), "claude-waybar")
    json.dump(collect(home, state_dir, time.time()), sys.stdout, separators=(",", ":"))


if __name__ == "__main__":
    main()
