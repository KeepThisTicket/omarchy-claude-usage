"""Fixture tests for agents.py. Run: python3 -m unittest discover -s tests"""

import json
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import agents  # noqa: E402

NOW = 1794415000.0


def write(path, data, mtime=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(data if isinstance(data, str) else json.dumps(data))
    if mtime is not None:
        os.utime(path, (mtime, mtime))


class Fixture:
    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = self.tmp.name
        self.state = os.path.join(self.home, ".local", "state", "claude-waybar")
        for d in (".claude", ".claude-work"):
            os.makedirs(os.path.join(self.home, d, "projects"))
        self.cli_out = {}
        self.cli_calls = []

    def dir(self, name):
        return os.path.join(self.home, name)

    def cli(self, profile_dir):
        self.cli_calls.append(profile_dir)
        out = self.cli_out.get(os.path.basename(profile_dir))
        if isinstance(out, Exception):
            raise out
        return out or []

    def collect(self):
        return agents.collect(self.home, self.state, NOW, cli=self.cli)

    def close(self):
        self.tmp.cleanup()


def by_key(result):
    return {a["key"]: a for a in result["agents"]}


class CollectTest(unittest.TestCase):
    def setUp(self):
        self.f = Fixture()

    def tearDown(self):
        self.f.close()

    def test_background_job_states(self):
        m = self.f.dir(".claude-work")
        self.f.cli_out[".claude-work"] = [
            {"id": "aaaa1111", "kind": "background", "sessionId": "s-a", "cwd": "/w/a", "name": "zoom",
             "pid": 101, "status": "busy", "state": "working", "startedAt": 1794414000000},
            {"id": "bbbb2222", "kind": "background", "sessionId": "s-b", "cwd": "/w/b", "name": "blocked one",
             "state": "blocked", "startedAt": 1794000000000},
            {"id": "cccc3333", "kind": "background", "sessionId": "s-c", "cwd": "/w/c", "name": "finished",
             "pid": 103, "status": "idle", "state": "done", "startedAt": 1794000000000},
            {"id": "dddd4444", "kind": "background", "sessionId": "s-d", "cwd": "/w/d", "name": "reprompted",
             "pid": 104, "status": "busy", "state": "done", "startedAt": 1794000000000},
        ]
        write(os.path.join(m, "jobs", "aaaa1111", "state.json"),
              {"state": "working", "detail": "Reading files", "tokens": "62987", "intent": "first ask",
               "updatedAt": "2026-11-11T16:37:35.119Z", "worktreeBranch": "wt-x",
               "children": [{"id": "2", "href": "https://example.com/pr/2", "kind": "pr"}]})
        write(os.path.join(m, "jobs", "bbbb2222", "state.json"),
              {"state": "blocked", "detail": "API Error", "needs": "invalid API request"})
        write(os.path.join(m, "jobs", "cccc3333", "state.json"),
              {"state": "done", "detail": "status", "output": {"result": "Built the thing"}})
        write(os.path.join(self.f.state, "sessions", "s-a.json"),
              {"session_id": "s-a", "state": "running", "prompt": "latest ask", "ts": 1794414999})

        a = by_key(self.f.collect())
        w = a["work:background:aaaa1111"]
        self.assertEqual((w["status"], w["detail"], w["prompt"], w["tokens"]), ("working", "Reading files", "latest ask", 62987))
        self.assertEqual(w["branch"], "wt-x")
        self.assertEqual(w["links"], [{"kind": "pr", "id": "2", "href": "https://example.com/pr/2"}])
        self.assertAlmostEqual(w["updatedAt"], 1794415055.119, places=2)
        self.assertEqual(w["startedAt"], 1794414000.0)
        self.assertTrue(w["alive"])
        b = a["work:background:bbbb2222"]
        self.assertEqual((b["status"], b["detail"], b["alive"]), ("needs", "invalid API request", False))
        self.assertEqual(b["prompt"], "")
        c = a["work:background:cccc3333"]
        self.assertEqual((c["status"], c["detail"]), ("done", "Built the thing"))
        self.assertEqual(a["work:background:dddd4444"]["status"], "working")

    def test_interactive_sessions_and_needs_input(self):
        c = self.f.dir(".claude")
        self.f.cli_out[".claude"] = [
            {"pid": 201, "kind": "interactive", "sessionId": "s-i1", "cwd": "/w/x", "name": "x-1", "status": "idle",
             "startedAt": 1794300000000},
            {"pid": 202, "kind": "interactive", "sessionId": "s-i2", "cwd": "/w/y", "name": "y-2", "status": "busy",
             "startedAt": 1794300000000},
            {"pid": 203, "kind": "interactive", "sessionId": "s-i3", "cwd": "/w/z", "name": "z-3", "status": "busy",
             "startedAt": 1794300000000},
        ]
        write(os.path.join(c, "sessions", "202.json"), {"status": "busy", "statusUpdatedAt": 1794414900000})
        write(os.path.join(self.f.state, "sessions", "s-i3.json"),
              {"session_id": "s-i3", "state": "needs_input", "detail": "Claude needs your permission to use Bash",
               "prompt": "fix it", "ts": 1794414950})
        a = by_key(self.f.collect())
        self.assertEqual(a["claude:interactive:s-i1"]["status"], "idle")
        i2 = a["claude:interactive:s-i2"]
        self.assertEqual((i2["status"], i2["updatedAt"], i2["pid"]), ("working", 1794414900.0, 202))
        i3 = a["claude:interactive:s-i3"]
        self.assertEqual((i3["status"], i3["detail"], i3["prompt"]),
                         ("needs", "Claude needs your permission to use Bash", "fix it"))

    def test_running_subagents_only(self):
        m = self.f.dir(".claude-work")
        self.f.cli_out[".claude-work"] = [
            {"pid": 301, "kind": "interactive", "sessionId": "s-p", "cwd": "/w", "name": "parent", "status": "busy"},
            {"kind": "background", "id": "eeee5555", "sessionId": "s-dead", "cwd": "/w", "name": "dead", "state": "done"},
        ]
        sub = os.path.join(m, "projects", "-w", "s-p", "subagents")
        write(os.path.join(sub, "agent-a1.meta.json"), {"description": "Audit docs", "agentType": "Explore"}, NOW - 120)
        write(os.path.join(sub, "agent-a1.jsonl"), "{}\n", NOW - 5)
        write(os.path.join(sub, "agent-a2.meta.json"), {"description": "Done one", "finished": True}, NOW - 60)
        write(os.path.join(sub, "agent-a2.jsonl"), "{}\n", NOW - 5)
        write(os.path.join(sub, "agent-a3.meta.json"), {"description": "Killed long ago"}, NOW - 9000)
        write(os.path.join(sub, "agent-a3.jsonl"), "{}\n", NOW - 8000)
        dead = os.path.join(m, "projects", "-w", "s-dead", "subagents")
        write(os.path.join(dead, "agent-b1.meta.json"), {"description": "orphan"}, NOW - 10)
        a = by_key(self.f.collect())
        subs = a["work:interactive:s-p"]["subagents"]
        self.assertEqual([(s["id"], s["description"], s["type"]) for s in subs], [("agent-a1", "Audit docs", "Explore")])
        self.assertEqual(subs[0]["since"], NOW - 120)
        self.assertEqual(a["work:background:eeee5555"]["subagents"], [])

    def test_remote_hosts_fresh_only(self):
        write(os.path.join(self.f.state, "remote", "devbox.state"),
              '{"state":"running","cwd":"/home/u/Project","port":35150,"prompt":"merge","session_id":"r1"}\n'
              '{"state":"needs_input","cwd":"/home/u/Other","session_id":"r2"}\n', NOW - 10)
        write(os.path.join(self.f.state, "remote", "gone.state"),
              '{"state":"running","cwd":"/x","session_id":"r3"}\n', NOW - 600)
        res = self.f.collect()
        a = by_key(res)
        r1 = a["remote:devbox:r1"]
        self.assertEqual((r1["profile"], r1["kind"], r1["status"], r1["name"], r1["port"], r1["prompt"]),
                         ("@devbox", "remote", "working", "Project", 35150, "merge"))
        self.assertEqual(a["remote:devbox:r2"]["status"], "needs")
        self.assertNotIn("remote:gone:r3", a)

    def test_failing_profile_is_reported_not_fatal(self):
        self.f.cli_out[".claude"] = RuntimeError("boom")
        self.f.cli_out[".claude-work"] = [{"pid": 1, "kind": "interactive", "sessionId": "s", "cwd": "/w",
                                            "name": "ok", "status": "idle"}]
        res = self.f.collect()
        self.assertEqual(res["errors"], ["claude: boom"])
        self.assertEqual([a["name"] for a in res["agents"]], ["ok"])

    def test_cli_env_default_dir_unsets_config_dir(self):
        base = {"PATH": "/bin", "CLAUDE_CONFIG_DIR": "/elsewhere"}
        self.assertNotIn("CLAUDE_CONFIG_DIR", agents.cli_env("/home/u/.claude", base))
        self.assertEqual(agents.cli_env("/home/u/.claude-work/", base)["CLAUDE_CONFIG_DIR"], "/home/u/.claude-work/")
        self.assertEqual(base["CLAUDE_CONFIG_DIR"], "/elsewhere")  # caller's env untouched

    def test_working_job_whose_turn_ended_is_finished(self):
        # A job that ends its turn without a result line keeps state "working";
        # the process, tempo and in-flight count say otherwise.
        m = self.f.dir(".claude-work")
        self.f.cli_out[".claude-work"] = [
            {"id": "g1", "kind": "background", "sessionId": "s-g1", "pid": 7, "status": "idle", "state": "working"},
            {"id": "g2", "kind": "background", "sessionId": "s-g2", "pid": 8, "status": "idle", "state": "working"},
            {"id": "g3", "kind": "background", "sessionId": "s-g3", "pid": 9, "status": "idle", "state": "working"},
            {"id": "g4", "kind": "background", "sessionId": "s-g4", "pid": 10, "status": "busy", "state": "working"},
            {"id": "g5", "kind": "background", "sessionId": "s-g5", "pid": 11, "status": "idle", "state": "working"},
        ]
        write(os.path.join(m, "jobs", "g5", "state.json"),
              {"state": "working", "tempo": "idle", "detail": "(idle — send a prompt to start)"})
        write(os.path.join(m, "jobs", "g1", "state.json"),
              {"state": "working", "tempo": "idle", "detail": "stale progress note",
               "inFlight": {"tasks": 0, "queued": 0}})
        write(os.path.join(m, "jobs", "g2", "state.json"),
              {"state": "working", "tempo": "idle", "detail": "waiting on a subagent", "inFlight": {"tasks": 1}})
        write(os.path.join(m, "jobs", "g3", "state.json"),
              {"state": "working", "tempo": "active", "detail": "between steps"})
        write(os.path.join(m, "jobs", "g4", "state.json"), {"state": "working", "tempo": "idle", "detail": "busy"})
        transcript = os.path.join(m, "projects", "-w", "s-g1.jsonl")
        write(transcript, "\n".join([
            json.dumps({"type": "user", "message": {"role": "user", "content": "do it"}}),
            json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "Working on it."}]}}),
            json.dumps({"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Bash"}]}}),
            json.dumps({"type": "assistant", "message": {"content": [
                {"type": "text", "text": "To keep one copy,\n\nI deleted the PDF."}]}}),
            json.dumps({"type": "system", "subtype": "turn_duration"}),
        ]) + "\n")
        a = by_key(self.f.collect())
        g1 = a["work:background:g1"]
        self.assertEqual((g1["status"], g1["rawState"], g1["detail"]), ("done", "working", "To keep one copy, I deleted the PDF."))
        self.assertEqual(a["work:background:g2"]["status"], "working")
        self.assertEqual(a["work:background:g5"]["status"], "idle")  # never prompted: nothing finished
        self.assertEqual(a["work:background:g3"]["status"], "working")
        self.assertEqual(a["work:background:g4"]["status"], "working")

    def test_unknown_state_falls_back_on_busy(self):
        self.f.cli_out[".claude-work"] = [
            {"id": "f1", "kind": "background", "sessionId": "s1", "pid": 5, "status": "busy", "state": "mystery"},
            {"id": "f2", "kind": "background", "sessionId": "s2", "pid": 6, "status": "idle", "state": "mystery"},
        ]
        a = by_key(self.f.collect())
        self.assertEqual((a["work:background:f1"]["status"], a["work:background:f1"]["rawState"]), ("working", "mystery"))
        self.assertEqual(a["work:background:f2"]["status"], "idle")


if __name__ == "__main__":
    unittest.main()
