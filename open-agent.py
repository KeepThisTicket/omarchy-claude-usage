#!/usr/bin/env python3
"""Bring an agent from the Agents view to the front.

  open-agent.py --pid PID
      An interactive session: focus the terminal window it runs in.
  open-agent.py --attach ID --config-dir DIR [--cwd DIR] [--name NAME]
      A background job: focus a terminal already showing it -- running
      `claude attach ID`, or titled NAME in Claude Code's agent view (the most
      recently used if several) -- or open one running `claude attach ID`.
  open-agent.py --remote HOST [--port PORT] [--session SID] [--cwd DIR]
      A session on another host: focus the local terminal holding its SSH
      connection (found by the connection's local port, as claude-waybar-picker
      does), or open a new SSH session resuming it.

Right-click actions:
  open-agent.py --logs ID --config-dir DIR [--cwd DIR]   `claude logs` in a terminal
  open-agent.py --stop ID --config-dir DIR               `claude stop` (conversation kept)
  open-agent.py --rm ID --config-dir DIR                 `claude rm` (job and its worktree);
      never passes --discard-unpushed: if rm refuses, its message is printed
  open-agent.py --end PID                                SIGTERM an interactive session,
      only if PID really is a Claude Code process

Exit status: 0 done, 3 nothing to focus and nothing to open, 2 bad arguments,
1 the action failed (reason on stdout).
"""

import json
import os
import re
import shlex
import signal
import subprocess
import sys


def parent_of(pid, proc="/proc"):
    try:
        with open(os.path.join(proc, str(pid), "status"), "r") as f:
            for line in f:
                if line.startswith("PPid:"):
                    return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        pass
    return 0


def ancestors(pid, parent=parent_of, limit=40):
    out = []
    while pid and pid > 1 and len(out) < limit:
        out.append(pid)
        pid = parent(pid)
    return out


def clients():
    try:
        r = subprocess.run(["hyprctl", "clients", "-j"], capture_output=True, text=True, timeout=5)
        data = json.loads(r.stdout or "[]")
        return data if isinstance(data, list) else []
    except (OSError, ValueError, subprocess.SubprocessError):
        return []


def window_for(pid, wins, parent=parent_of):
    """Address of the window owned by pid or its nearest ancestor."""
    by_pid = {}
    for w in wins:
        if isinstance(w, dict) and w.get("pid") and w.get("address"):
            by_pid.setdefault(w["pid"], w["address"])
    for p in ancestors(pid, parent):
        if p in by_pid:
            return by_pid[p]
    return None


def attach_pids(job_id, proc="/proc"):
    """Processes already running `claude attach <job_id>`."""
    out = []
    for name in os.listdir(proc):
        if not name.isdigit():
            continue
        try:
            with open(os.path.join(proc, name, "cmdline"), "rb") as f:
                args = f.read().split(b"\0")
        except OSError:
            continue
        words = [a.decode("utf-8", "replace") for a in args if a]
        if "attach" in words and job_id in words[words.index("attach") + 1:]:
            out.append(int(name))
    return out


def claude_pids(proc="/proc"):
    """Every Claude Code process (comm "claude" or a bare version string)."""
    out = []
    for name in os.listdir(proc):
        if not name.isdigit():
            continue
        try:
            with open(os.path.join(proc, name, "comm")) as f:
                comm = f.read().strip()
        except OSError:
            continue
        if comm in ("claude", "claude.exe") or re.fullmatch(r"\d+\.\d+\.\d+", comm):
            out.append(int(name))
    return out


def title_shows(title, name):
    """Claude Code titles its terminal "<status glyph> <session name>"."""
    if not name:
        return False
    return re.sub(r"^[^\w\s]+\s+", "", str(title or "")).strip() == name.strip()


def windows_showing(name, wins, pids, parent=parent_of):
    """Addresses of windows that host a Claude process and are titled after
    `name` -- a terminal viewing that session through the agent view, where
    nothing in its command line names the job."""
    titled = {w["address"]: w for w in wins
              if isinstance(w, dict) and w.get("address") and title_shows(w.get("title"), name)}
    out = []
    for pid in pids:
        addr = window_for(pid, wins, parent)
        if addr in titled and addr not in out:
            out.append(addr)
    return out


def job_name(config_dir, job_id):
    """A background job's session name, from its state file."""
    try:
        with open(os.path.join(config_dir, "jobs", job_id, "state.json")) as f:
            name = json.load(f).get("name")
        return name if isinstance(name, str) else ""
    except (OSError, ValueError, AttributeError):
        return ""


def session_name(pid, home=None):
    """An interactive session's name, from <config dir>/sessions/<pid>.json."""
    import glob
    for path in glob.glob(os.path.join(home or os.path.expanduser("~"), ".claude*", "sessions", "%d.json" % pid)):
        try:
            with open(path) as f:
                name = json.load(f).get("name")
            if isinstance(name, str) and name:
                return name
        except (OSError, ValueError, AttributeError):
            continue
    return ""


def most_recent(addresses, wins):
    """The candidate window that was focused last."""
    order = {w.get("address"): w.get("focusHistoryID", 1 << 30) for w in wins if isinstance(w, dict)}
    best = sorted(addresses, key=lambda a: order.get(a, 1 << 30))
    return best[0] if best else None


# ssh options that take an argument (from ssh(1)).
SSH_ARG_OPTS = set("BbcDEeFIiJLlmOoPpQRSWw")


def ssh_destination(argv):
    """-> (destination, remote command words, single-letter flags, -o options)
    for an ssh client command line, or None if it isn't one."""
    if not argv or os.path.basename(argv[0]) != "ssh":
        return None
    flags, opts, i = set(), [], 1
    while i < len(argv):
        a = argv[i]
        if a == "--":
            i += 1
            break
        if not a.startswith("-") or a == "-":
            break
        letters = a[1:]
        for j, ch in enumerate(letters):
            if ch in SSH_ARG_OPTS:
                value = letters[j + 1:] or (argv[i + 1] if i + 1 < len(argv) else "")
                if not letters[j + 1:]:
                    i += 1
                if ch == "o":
                    opts.append(value)
                flags.add(ch)
                break
            flags.add(ch)
        i += 1
    if i >= len(argv):
        return None
    return argv[i], argv[i + 1:], flags, opts


def ssh_session_pids(host, proc="/proc"):
    """Local ssh clients with a terminal session on `host`: not shared-connection
    masters (-N), control commands (-O) or batch links like claude-waybar-sync.
    -> [(pid, runs claude)], ones running claude first."""
    with_claude, others = [], []
    for name in os.listdir(proc):
        if not name.isdigit():
            continue
        try:
            with open(os.path.join(proc, name, "cmdline"), "rb") as f:
                argv = [a.decode("utf-8", "replace") for a in f.read().split(b"\0") if a]
        except OSError:
            continue
        parsed = ssh_destination(argv)
        if not parsed:
            continue
        dest, command, flags, opts = parsed
        if dest.split("@")[-1] != host or "N" in flags or "O" in flags:
            continue
        if any(o.replace(" ", "").lower() == "batchmode=yes" for o in opts):
            continue
        runs_claude = "claude" in " ".join(command)
        (with_claude if runs_claude else others).append((int(name), runs_claude))
    return with_claude + others


def ssh_pid_for_port(port):
    try:
        r = subprocess.run(["ss", "-tnpH"], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    for line in r.stdout.splitlines():
        cols = line.split()
        if len(cols) >= 4 and cols[3].endswith(":%d" % port):
            m = re.search(r"pid=(\d+)", line)
            if m:
                return int(m.group(1))
    return None


def dispatch(arg, run=subprocess.run):
    try:
        r = run(["hyprctl", "dispatch", arg], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return False
    return r.returncode == 0 and not (r.stdout or "").lstrip().startswith("error")


def focus(address, run=subprocess.run):
    """Focus a window, switching to its workspace. Hyprland's Lua config takes
    hl.dsp.focus(...); older configs the plain dispatcher. Same order as
    omarchy-hyprland-focus-app."""
    if not re.fullmatch(r"0x[0-9a-fA-F]+", address or ""):
        return False
    return (dispatch('hl.dsp.focus({ window = "address:%s" })' % address, run)
            or dispatch("focuswindow address:" + address, run))


def launch(argv, cwd=None):
    """A new terminal window running argv, detached from us."""
    term = ["xdg-terminal-exec"]
    if cwd and os.path.isdir(cwd):
        term.append("--dir=" + cwd)
    subprocess.Popen(["setsid", "uwsm-app", "--"] + term + ["--"] + argv,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True)


def attach_command(job_id, config_dir):
    # ~/.claude is the default dir: CLAUDE_CONFIG_DIR must be unset for it, not
    # pointed at it, or Claude Code looks for its config in the wrong place.
    if os.path.basename(config_dir.rstrip("/")) == ".claude":
        return ["env", "-u", "CLAUDE_CONFIG_DIR", "claude", "attach", job_id]
    return ["env", "CLAUDE_CONFIG_DIR=" + config_dir, "claude", "attach", job_id]


def claude_env(config_dir, base=None):
    env = {k: v for k, v in (base if base is not None else os.environ).items() if k != "CLAUDE_CONFIG_DIR"}
    if os.path.basename(config_dir.rstrip("/")) != ".claude":
        env["CLAUDE_CONFIG_DIR"] = config_dir
    return env


def run_claude(sub, job_id, config_dir, timeout=60):
    """-> (ok, message) for `claude stop|rm <id>`."""
    try:
        r = subprocess.run(["claude", sub, job_id], env=claude_env(config_dir), capture_output=True,
                           text=True, timeout=timeout, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError) as e:
        return False, str(e)
    text = " ".join((r.stdout + "\n" + r.stderr).split())
    return r.returncode == 0, text[:400]


def logs_command(job_id, config_dir):
    # Through a pager so the window stays up; q closes it.
    env_prefix = attach_command(job_id, config_dir)[:-3]  # drop "claude attach <id>"
    return env_prefix + ["sh", "-c", 'claude logs "$1" | less -R +G', "sh", job_id]


def is_claude_session(pid, proc="/proc"):
    """A live Claude Code process, and not one of the daemon's idle spares."""
    base = os.path.join(proc, str(pid))
    try:
        with open(os.path.join(base, "comm")) as f:
            comm = f.read().strip()
        with open(os.path.join(base, "cmdline"), "rb") as f:
            args = f.read().replace(b"\0", b" ").decode("utf-8", "replace")
        exe = os.path.realpath(os.path.join(base, "exe"))
    except OSError:
        return False
    if not (comm in ("claude", "claude.exe") or re.fullmatch(r"\d+\.\d+\.\d+", comm)):
        return False
    if "--bg-spare" in args:
        return False
    return "/claude/versions/" in exe or "claude-code" in exe or os.path.basename(exe) in ("claude", "claude.exe")


def remote_command(host, session=None, cwd=None):
    """Resume a session on `host`, the way a typical remote terminal starts
    claude: over the shared connection (~/.ssh/cm-%C, so no passphrase while
    it is up), in an interactive login shell (claude is on PATH only after
    .bashrc), dropping to a shell if claude fails. If ssh itself fails, the
    window waits instead of vanishing with the error."""
    claude = "claude --resume %s" % shlex.quote(session) if session else "claude --continue"
    cd = "cd %s 2>/dev/null; " % shlex.quote(cwd) if cwd else ""
    remote = cd + '%s; s=$?; if [ $s -ne 0 ]; then echo "claude failed (status $s)"; exec bash -l; fi' % claude
    ssh = ["ssh", "-o", "ControlMaster=auto", "-o", "ControlPath=~/.ssh/cm-%C", "-o", "ControlPersist=8h",
           "-t", host, "bash -lic " + shlex.quote(remote)]
    hold = ('"$@"; s=$?; if [ $s -ne 0 ]; then echo; echo "ssh to %s failed (status $s)"; '
            'read -r -p "Press Enter to close " _; fi' % host)
    return ["sh", "-c", hold, "sh"] + ssh


def focus_failed(addr):
    print("Hyprland would not focus window %s" % addr)
    return 1


LOG = os.path.join(os.path.expanduser("~"), ".cache", "kov-claude-usage", "open-agent.log")


def log(line):
    """Last 200 decisions, so "it opened a new terminal" can be traced."""
    try:
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
        try:
            with open(LOG) as f:
                lines = f.read().splitlines()[-199:]
        except OSError:
            lines = []
        import time
        lines.append(time.strftime("%Y-%m-%d %H:%M:%S ") + line)
        with open(LOG, "w") as f:
            f.write("\n".join(lines) + "\n")
    except OSError:
        pass


def parse(argv):
    opts, i = {}, 0
    while i < len(argv):
        key = argv[i]
        if not key.startswith("--") or i + 1 >= len(argv):
            return None
        opts[key[2:]] = argv[i + 1]
        i += 2
    return opts


def main(argv):
    code = run(argv)
    log("%s -> %d" % (" ".join(shlex.quote(a) for a in argv), code))
    return code


def run(argv):
    opts = parse(argv)
    if not opts:
        print(__doc__, file=sys.stderr)
        return 2

    for action in ("stop", "rm", "logs"):
        if action in opts:
            job = opts[action]
            if not re.fullmatch(r"[A-Za-z0-9_-]+", job) or "config-dir" not in opts:
                return 2
            if action == "logs":
                launch(logs_command(job, opts["config-dir"]), opts.get("cwd"))
                return 0
            ok, message = run_claude(action, job, opts["config-dir"])
            if not ok:
                print(message or "claude %s failed" % action)
                return 1
            return 0

    if "end" in opts:
        pid = int(opts["end"]) if opts["end"].isdigit() else 0
        if pid <= 1 or not is_claude_session(pid):
            print("process %s is not a Claude Code session" % opts["end"])
            return 1
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError as e:
            print(str(e))
            return 1
        return 0

    wins = clients()

    if "pid" in opts:
        addr = window_for(int(opts["pid"]), wins) if opts["pid"].isdigit() else None
        if not addr and opts["pid"].isdigit():
            name = opts.get("name") or session_name(int(opts["pid"]))
            addr = most_recent(windows_showing(name, wins, claude_pids()), wins)
        if not addr:
            return 3
        return 0 if focus(addr) else focus_failed(addr)

    if "attach" in opts and "config-dir" in opts:
        job = opts["attach"]
        if not re.fullmatch(r"[A-Za-z0-9_-]+", job):
            return 2
        # A terminal running `claude attach <id>`, or one showing the job in
        # Claude Code's agent view; the one used last wins.
        found = [a for a in (window_for(pid, wins) for pid in attach_pids(job)) if a]
        name = opts.get("name") or job_name(opts["config-dir"], job)
        found += [a for a in windows_showing(name, wins, claude_pids()) if a not in found]
        addr = most_recent(found, wins)
        if addr:
            return 0 if focus(addr) else focus_failed(addr)
        log("attach %s: no window showing it (name %r), launching a terminal" % (job, name))
        launch(attach_command(job, opts["config-dir"]), opts.get("cwd"))
        return 0

    if "remote" in opts:
        host = opts["remote"]
        if not re.fullmatch(r"[A-Za-z0-9_.@-]+", host):
            return 2
        # A terminal already in a session on that host. The mirrored port is
        # no help when ssh shares one connection (ControlMaster): it belongs
        # to the background master, not to any terminal.
        claude_wins, other_wins = [], []
        port = opts.get("port", "")
        pid = ssh_pid_for_port(int(port)) if port.isdigit() and int(port) > 0 else None
        if pid and window_for(pid, wins):
            claude_wins.append(window_for(pid, wins))
        for p, runs_claude in ssh_session_pids(host):
            addr = window_for(p, wins)
            if addr and addr not in claude_wins + other_wins:
                (claude_wins if runs_claude else other_wins).append(addr)
        addr = most_recent(claude_wins, wins) or most_recent(other_wins, wins)
        if addr:
            return 0 if focus(addr) else focus_failed(addr)
        log("remote %s: no local terminal in a session there, opening ssh" % host)
        launch(remote_command(host, opts.get("session"), opts.get("cwd")))
        return 0

    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
