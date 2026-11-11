"""Tests for open-agent.py's pure parts. Run: python3 -m unittest discover -s tests"""

import importlib.util
import os
import shlex
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("open_agent", os.path.join(HERE, "..", "open-agent.py"))
oa = importlib.util.module_from_spec(spec)
spec.loader.exec_module(oa)


class OpenAgentTest(unittest.TestCase):
    def setUp(self):
        # Keep test runs out of the real decision log.
        self._log_dir = tempfile.TemporaryDirectory()
        self._log = oa.LOG
        oa.LOG = os.path.join(self._log_dir.name, "open-agent.log")

    def tearDown(self):
        oa.LOG = self._log
        self._log_dir.cleanup()

    def test_window_for_walks_up_to_the_terminal(self):
        tree = {500: 400, 400: 300, 300: 1}  # claude -> shell -> alacritty
        wins = [{"pid": 300, "address": "0xterm"}, {"pid": 999, "address": "0xother"}]
        self.assertEqual(oa.window_for(500, wins, parent=lambda p: tree.get(p, 0)), "0xterm")
        self.assertIsNone(oa.window_for(777, wins, parent=lambda p: 0))

    def test_attach_pids_matches_only_that_job(self):
        with tempfile.TemporaryDirectory() as proc:
            def cmd(pid, *args):
                os.makedirs(os.path.join(proc, str(pid)))
                with open(os.path.join(proc, str(pid), "cmdline"), "wb") as f:
                    f.write(b"\0".join(a.encode() for a in args) + b"\0")
            cmd(10, "claude", "attach", "abc123")
            cmd(11, "claude", "attach", "zzz999")
            cmd(12, "vim", "abc123")
            os.makedirs(os.path.join(proc, "self"))
            self.assertEqual(oa.attach_pids("abc123", proc=proc), [10])

    def test_attach_command_handles_default_dir(self):
        self.assertEqual(oa.attach_command("abc", "/home/u/.claude"),
                         ["env", "-u", "CLAUDE_CONFIG_DIR", "claude", "attach", "abc"])
        self.assertEqual(oa.attach_command("abc", "/home/u/.claude-work"),
                         ["env", "CLAUDE_CONFIG_DIR=/home/u/.claude-work", "claude", "attach", "abc"])

    def test_remote_command_reuses_connection_and_holds_on_failure(self):
        cmd = oa.remote_command("devbox", "sid-1", "/home/u/My Dir")
        self.assertEqual(cmd[:2], ["sh", "-c"])
        ssh = cmd[4:]
        self.assertEqual(ssh[:7], ["ssh", "-o", "ControlMaster=auto", "-o", "ControlPath=~/.ssh/cm-%C",
                                   "-o", "ControlPersist=8h"])
        self.assertEqual(ssh[7:9], ["-t", "devbox"])
        # The remote shell unquotes the command once; bash -lic gets the script.
        words = shlex.split(ssh[9])
        self.assertEqual(words[:2], ["bash", "-lic"])
        self.assertIn("cd '/home/u/My Dir'", words[2])
        self.assertIn("claude --resume sid-1", words[2])
        self.assertIn("Press Enter to close", cmd[2])
        self.assertNotIn("cd ", oa.remote_command("devbox", "sid-1", "")[-1])

    def test_ssh_destination(self):
        argv = ["ssh", "-o", "ControlMaster=auto", "-o", "ControlPath=/x/cm-%C", "-t", "devbox",
                "bash", "-lic", "cd ~/project && claude"]
        dest, command, flags, opts = oa.ssh_destination(argv)
        self.assertEqual((dest, flags), ("devbox", {"o", "t"}))
        self.assertEqual(command[-1], "cd ~/project && claude")
        self.assertEqual(oa.ssh_destination(["ssh", "-fN", "devbox"])[2], {"f", "N"})
        self.assertEqual(oa.ssh_destination(["ssh", "-i/k", "-p22", "u@h"])[0], "u@h")
        self.assertIsNone(oa.ssh_destination(["scp", "a", "b"]))
        self.assertIsNone(oa.ssh_destination(["ssh", "-o", "X=1"]))

    def test_ssh_session_pids_skips_masters_and_batch_links(self):
        with tempfile.TemporaryDirectory() as proc:
            def cmd(pid, *args):
                os.makedirs(os.path.join(proc, str(pid)))
                with open(os.path.join(proc, str(pid), "cmdline"), "wb") as f:
                    f.write(b"\0".join(a.encode() for a in args) + b"\0")
            cmd(10, "ssh", "-o", "ControlPersist=8h", "-fN", "devbox")                       # master
            cmd(11, "ssh", "-o", "BatchMode=yes", "-i", "/k", "devbox", "true")             # sync link
            cmd(12, "ssh", "-o", "ControlMaster=auto", "devbox")                             # plain shell
            cmd(13, "ssh", "-t", "devbox", "bash", "-lic", "cd ~/project && claude")           # claude
            cmd(14, "ssh", "-t", "prodbox", "bash", "-lic", "claude")                        # other host
            cmd(15, "ssh", "-O", "check", "devbox")                                          # control cmd
            got = oa.ssh_session_pids("devbox", proc=proc)
            self.assertEqual(got, [(13, True), (12, False)])

    def test_logs_command(self):
        self.assertEqual(oa.logs_command("abc", "/home/u/.claude-work"),
                         ["env", "CLAUDE_CONFIG_DIR=/home/u/.claude-work",
                          "sh", "-c", 'claude logs "$1" | less -R +G', "sh", "abc"])
        self.assertEqual(oa.logs_command("abc", "/home/u/.claude")[:3], ["env", "-u", "CLAUDE_CONFIG_DIR"])

    def test_claude_env(self):
        base = {"PATH": "/bin", "CLAUDE_CONFIG_DIR": "/x"}
        self.assertNotIn("CLAUDE_CONFIG_DIR", oa.claude_env("/home/u/.claude", base))
        self.assertEqual(oa.claude_env("/home/u/.claude-work", base)["CLAUDE_CONFIG_DIR"], "/home/u/.claude-work")

    def test_is_claude_session(self):
        with tempfile.TemporaryDirectory() as proc:
            def fake(pid, comm, args, exe):
                d = os.path.join(proc, str(pid))
                os.makedirs(d)
                with open(os.path.join(d, "comm"), "w") as f:
                    f.write(comm + "\n")
                with open(os.path.join(d, "cmdline"), "wb") as f:
                    f.write(b"\0".join(a.encode() for a in args))
                target = os.path.join(proc, "bin", exe)
                os.makedirs(os.path.dirname(target), exist_ok=True)
                open(target, "w").close()
                os.symlink(target, os.path.join(d, "exe"))
            fake(1, "claude", ["claude"], "claude/versions/2.1.322")
            fake(2, "2.1.322", ["claude", "bg-spare", "--bg-spare", "/tmp/x"], "claude/versions/2.1.322")
            fake(3, "bash", ["bash"], "bash")
            fake(4, "claude", ["claude"], "evil")
            self.assertTrue(oa.is_claude_session(1, proc=proc))
            self.assertFalse(oa.is_claude_session(2, proc=proc))
            self.assertFalse(oa.is_claude_session(3, proc=proc))
            self.assertFalse(oa.is_claude_session(4, proc=proc))
            self.assertFalse(oa.is_claude_session(99, proc=proc))

    def test_end_refuses_non_claude_and_bad_ids(self):
        self.assertEqual(oa.main(["--end", str(os.getpid())]), 1)  # this python process
        self.assertEqual(oa.main(["--end", "1"]), 1)
        self.assertEqual(oa.main(["--rm", "bad id!", "--config-dir", "/x"]), 2)
        self.assertEqual(oa.main(["--stop", "abc"]), 2)  # no config dir

    def test_focus_lua_first_then_legacy(self):
        calls = []

        class R:
            def __init__(self, code, out):
                self.returncode, self.stdout = code, out

        def lua_ok(argv, **kw):
            calls.append(argv[2])
            return R(0, "ok")
        self.assertTrue(oa.focus("0xabc", run=lua_ok))
        self.assertEqual(calls, ['hl.dsp.focus({ window = "address:0xabc" })'])

        calls.clear()
        def legacy_only(argv, **kw):
            calls.append(argv[2])
            return R(7, "error: ...") if argv[2].startswith("hl.") else R(0, "ok")
        self.assertTrue(oa.focus("0xabc", run=legacy_only))
        self.assertEqual(calls[-1], "focuswindow address:0xabc")

        # Hyprland prints "error: ..." with status 0 on some versions.
        self.assertFalse(oa.focus("0xabc", run=lambda argv, **kw: R(0, "error: nope")))
        # Never pass anything but a hex address into the Lua expression.
        self.assertFalse(oa.focus('0x1" }) os.exit() --', run=lua_ok))

    def test_title_shows(self):
        self.assertTrue(oa.title_shows("◐ battery-history-graph-zoom", "battery-history-graph-zoom"))
        self.assertTrue(oa.title_shows("✳ PMIC substitution candidates", "PMIC substitution candidates"))
        self.assertFalse(oa.title_shows("✳ PMIC substitution candidates v2", "PMIC substitution candidates"))
        self.assertFalse(oa.title_shows("user@laptop:~", ""))

    def test_windows_showing_needs_a_claude_process(self):
        # 300 = terminal running claude 500 (agent view on the job);
        # 600 = a browser whose tab happens to have the same title.
        tree = {500: 400, 400: 300}
        wins = [
            {"pid": 300, "address": "0xterm", "title": "◐ zoom job", "focusHistoryID": 4},
            {"pid": 600, "address": "0xweb", "title": "◐ zoom job", "focusHistoryID": 0},
            {"pid": 700, "address": "0xother", "title": "✳ something else", "focusHistoryID": 1},
        ]
        found = oa.windows_showing("zoom job", wins, [500], parent=lambda p: tree.get(p, 0))
        self.assertEqual(found, ["0xterm"])

    def test_most_recent(self):
        wins = [{"address": "0xa", "focusHistoryID": 5}, {"address": "0xb", "focusHistoryID": 2}]
        self.assertEqual(oa.most_recent(["0xa", "0xb"], wins), "0xb")
        self.assertIsNone(oa.most_recent([], wins))

    def test_names_from_state_files(self):
        with tempfile.TemporaryDirectory() as home:
            cfg = os.path.join(home, ".claude-work")
            os.makedirs(os.path.join(cfg, "jobs", "abc"))
            os.makedirs(os.path.join(cfg, "sessions"))
            with open(os.path.join(cfg, "jobs", "abc", "state.json"), "w") as f:
                f.write('{"name": "zoom job", "state": "working"}')
            with open(os.path.join(cfg, "sessions", "42.json"), "w") as f:
                f.write('{"pid": 42, "name": "laptop-85"}')
            self.assertEqual(oa.job_name(cfg, "abc"), "zoom job")
            self.assertEqual(oa.job_name(cfg, "missing"), "")
            self.assertEqual(oa.session_name(42, home=home), "laptop-85")
            self.assertEqual(oa.session_name(43, home=home), "")

    def test_parse(self):
        self.assertEqual(oa.parse(["--pid", "5"]), {"pid": "5"})
        self.assertIsNone(oa.parse(["--pid"]))
        self.assertIsNone(oa.parse(["pid", "5"]))
        self.assertEqual(oa.main(["--attach", "bad id!", "--config-dir", "/x"]), 2)


if __name__ == "__main__":
    unittest.main()
