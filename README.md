# Claude Code usage and agents for Omarchy

A bar widget for the [Omarchy](https://omarchy.org) shell for people who use
[Claude Code](https://docs.claude.com/en/docs/claude-code). The bar shows your
current 5-hour limit. The popout has two views:

**Usage**
- Plan limits (5-hour, weekly and per-model) with reset countdowns.
- A year heatmap of tokens, cost or messages, with streaks and a hover readout.
- Totals for today, 7 days, 30 days or all time: input/output/cache tokens, an
  API-price cost estimate, messages, sessions, cache hit rate, busiest day,
  and breakdowns by model and by profile.

**Agents**: every Claude Code session and background job on the machine, grouped
as *Needs you · Failed · Working · Idle · Finished*.
- Each row shows what the agent is doing (its status line, what it is blocked on,
  its result or your last prompt), where it runs, its branch, tokens and PRs, and
  any subagents still running.
- **Click** to jump to it: focuses the terminal it runs in (also a terminal
  showing it through Claude Code's agent view), or opens `claude attach` for a
  background job.
- **Right-click** for more: show logs, stop, delete (`claude rm`, which never
  discards unpushed commits), end a session, or copy a resume command.

Profiles are config dirs: `~/.claude` is `claude`, and any other `~/.claude-*`
dir (for example one used through `CLAUDE_CONFIG_DIR=~/.claude-work`) is
another profile. The profile chips filter both views.

## Install

```sh
omarchy plugin add https://github.com/KeepThisTicket/omarchy-claude-usage.git --enable
```

Update later with `omarchy plugin update kov.claude-usage`.

## Requirements

- Omarchy with its Quickshell-based shell and plugin support, on Hyprland.
- Claude Code (`claude` on `PATH`), signed in.
- `python3`, `wl-copy`, `xdg-terminal-exec` and `uwsm-app` (all standard on Omarchy).

## Privacy and network use

- Usage numbers come from your local transcripts (`<config dir>/projects/**/*.jsonl`).
- Plan limits come from Claude Code's own cached reading. While the popout is
  open, and at most every 5 minutes, the plugin refreshes them from
  `https://api.anthropic.com/api/oauth/usage` using the access token Claude Code
  already stored. The token is only read: never refreshed, rewritten or logged.
- The agent list comes from `claude agents --json --all`, plus Claude Code's
  job and session files.
- Daily totals are kept in `~/.local/state/kov-claude-usage/history.json`, so the
  heatmap outlives Claude Code's ~30-day transcript cleanup. Click decisions are
  logged to `~/.cache/kov-claude-usage/open-agent.log` for troubleshooting.

Cost is an estimate of what the usage would cost at API prices (`PRICES` in
`collect.py`); subscription plans don't bill per token.

## Optional: richer agent status and remote hosts

If a Claude Code hook writes small JSON records to
`~/.local/state/claude-waybar/sessions/<session id>.json`
(`{"session_id", "state": "running"|"idle"|"needs_input", "prompt", "detail", "ts"}`),
the Agents view uses them for each session's last prompt and to flag sessions
waiting on a permission prompt. Sessions on other machines show up if one-line
records of the same shape (plus `"cwd"` and, optionally, `"port"`) are mirrored to
`~/.local/state/claude-waybar/remote/<host>.state`. Without these files the view
still works; it just has less detail.

Clicking a remote session focuses a local terminal already connected to that
host over ssh. Otherwise it opens one, reusing an ssh connection shared through
`ControlPath=~/.ssh/cm-%C` if there is one.

## Development

```sh
python3 -m unittest discover -s tests
node tests/model.test.js
```

`collect.py` and `agents.py` print JSON and can be run on their own.
`Model.js` holds the pure helpers and `Panel.qml` only draws.

## License

MIT, see [LICENSE](LICENSE).
