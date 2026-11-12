"""Fixture tests for collect.py. Run: python3 -m unittest discover -s tests"""

import json
import os
import sys
import tempfile
import time
import unittest
import urllib.error

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import collect  # noqa: E402

ACCT = {"accountUuid": "a1", "organizationUuid": "o1", "emailAddress": "me@example.com",
        "organizationName": "Org", "userRateLimitTier": "default_claude_max_5x"}


def usage(inp=0, out=0, cw=0, cr=0, h1=0, speed="standard"):
    return {"input_tokens": inp, "output_tokens": out, "cache_creation_input_tokens": cw + h1,
            "cache_read_input_tokens": cr, "speed": speed,
            "cache_creation": {"ephemeral_5m_input_tokens": cw, "ephemeral_1h_input_tokens": h1}}


def rec(mid, req, ts, model="claude-opus-5-5", sid="s1", side=False, **u):
    return json.dumps({"type": "assistant", "requestId": req, "timestamp": ts, "sessionId": sid,
                       "isSidechain": side, "message": {"id": mid, "model": model, "usage": usage(**u)}})


def local_ms(y, mo, d, h):
    return int(time.mktime((y, mo, d, h, 0, 0, 0, 0, -1)) * 1000)


class Fixture:
    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = self.tmp.name
        self.state = os.path.join(self.home, "state")
        self.cache = os.path.join(self.home, "cache")

    def profile(self, name, acct=ACCT, cached=None, token="tok", expires_in=3600):
        d = os.path.join(self.home, name)
        os.makedirs(os.path.join(d, "projects", "p"), exist_ok=True)
        cfg = {"oauthAccount": acct}
        if cached is not None:
            cfg["cachedUsageUtilization"] = cached
        cfg_path = os.path.join(self.home, ".claude.json") if name == ".claude" else os.path.join(d, ".claude.json")
        with open(cfg_path, "w") as f:
            json.dump(cfg, f)
        with open(os.path.join(d, ".credentials.json"), "w") as f:
            json.dump({"claudeAiOauth": {"accessToken": token, "subscriptionType": "team",
                                         "rateLimitTier": "default_claude_max_5x",
                                         "expiresAt": (time.time() + expires_in) * 1000}}, f)
        return d

    def transcript(self, profile_dir, name, lines):
        path = os.path.join(profile_dir, "projects", "p", name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write("\n".join(lines) + "\n")
        return path

    def prompt_log(self, profile_dir, entries):
        """entries: (epoch_ms, session_id) pairs written to <dir>/history.jsonl."""
        with open(os.path.join(profile_dir, "history.jsonl"), "w") as f:
            for ms, sid in entries:
                f.write(json.dumps({"display": "hi", "pastedContents": {}, "timestamp": ms,
                                    "project": "/p", "sessionId": sid}) + "\n")

    def run(self, **kw):
        kw.setdefault("online", False)
        return collect.collect(self.home, self.state, self.cache, time.time(), **kw)


class CostTests(unittest.TestCase):
    def test_opus_55_prices_each_token_kind(self):
        u = usage(inp=1_000_000, out=1_000_000, cw=1_000_000, cr=1_000_000, h1=1_000_000)
        # 4 + 20 + 5 (1.25x) + 0.20 + 8 (2x)
        self.assertAlmostEqual(collect.cost_of("claude-opus-5-5", u), 37.2)

    def test_fast_mode_doubles(self):
        self.assertAlmostEqual(collect.cost_of("claude-opus-5-5", usage(out=1_000_000, speed="fast")), 40.0)

    def test_longest_prefix_wins(self):
        self.assertEqual(collect.price_for("claude-opus-5-5"), collect.PRICES["claude-opus-5-5"])
        self.assertEqual(collect.price_for("claude-opus-5"), collect.PRICES["claude-opus-5"])
        self.assertEqual(collect.price_for("claude-sonnet-4-6"), collect.PRICES["claude-sonnet-4"])

    def test_unknown_model_is_unpriced(self):
        self.assertIsNone(collect.cost_of("gpt-9", usage(out=10)))

    def test_model_names_lose_suffixes(self):
        self.assertEqual(collect.normalize_model("claude-opus-4-5-20251101"), "claude-opus-4-5")
        self.assertEqual(collect.normalize_model("claude-opus-5-5[1m]"), "claude-opus-5-5")


class ScanTests(unittest.TestCase):
    def setUp(self):
        self.f = Fixture()

    def tearDown(self):
        self.f.tmp.cleanup()

    def test_streamed_and_resumed_copies_count_once(self):
        d = self.f.profile(".claude")
        line = rec("m1", "r1", "2026-11-10T12:00:00Z", out=100, cr=1000)
        self.f.transcript(d, "a.jsonl", [line, line, rec("m2", "r2", "2026-11-10T12:01:00Z", out=50)])
        self.f.transcript(d, "b.jsonl", [line])  # resumed session copies history
        out = self.f.run()
        rows = [r for r in out["rows"] if r[1] == "claude"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][4], 150)   # output
        self.assertEqual(rows[0][7], 1000)  # cache read
        self.assertEqual(rows[0][10], 2)    # messages

    def test_two_profiles_split_and_share_one_account(self):
        a = self.f.profile(".claude")
        b = self.f.profile(".claude-work")
        self.f.transcript(a, "a.jsonl", [rec("m1", "r1", "2026-11-10T12:00:00Z", out=10)])
        self.f.transcript(b, "b.jsonl", [rec("m2", "r2", "2026-11-10T12:00:00Z", out=20, sid="s2"),
                                         rec("m3", "r3", "2026-11-10T13:00:00Z", out=5, sid="s3", side=True)])
        out = self.f.run()
        self.assertEqual(sorted(p["id"] for p in out["profiles"]), ["claude", "work"])
        by = {r[1]: r for r in out["rows"]}
        self.assertEqual(by["claude"][4], 10)
        self.assertEqual(by["work"][4], 25)
        day = by["work"][0]
        self.assertEqual(out["sessions"][day]["work"], 1)  # sidechain is not a session
        self.assertEqual(len(out["accounts"]), 1)
        self.assertEqual(out["accounts"][0]["profiles"], ["claude", "work"])
        self.assertEqual(out["accounts"][0]["plan"], "Team · Max 5x")

    def test_bad_lines_are_skipped(self):
        d = self.f.profile(".claude")
        self.f.transcript(d, "a.jsonl", ['{"type":"assistant","usage" broken', "not json",
                                         rec("m1", "r1", "2026-11-10T12:00:00Z", out=7),
                                         rec("m2", "r2", "2026-11-10T12:00:00Z", model="<synthetic>", out=9)])
        out = self.f.run()
        self.assertEqual(sum(r[4] for r in out["rows"]), 7)

    def test_history_survives_transcript_cleanup(self):
        d = self.f.profile(".claude")
        path = self.f.transcript(d, "a.jsonl", [rec("m1", "r1", "2026-09-01T12:00:00Z", out=500),
                                                rec("m2", "r2", "2026-11-10T12:00:00Z", out=10)])
        first = self.f.run()
        os.remove(path)
        self.f.transcript(d, "b.jsonl", [rec("m3", "r3", "2026-11-10T13:00:00Z", out=1)])
        second = self.f.run()
        old_day = [r for r in first["rows"] if r[4] == 500][0][0]
        self.assertIn(old_day, [r[0] for r in second["rows"]])
        # A partially cleaned day keeps the larger stored total.
        nov = [r for r in second["rows"] if r[0] != old_day]
        self.assertEqual(sum(r[4] for r in nov), 10)

    def test_prompt_log_counts_prompts_and_sessions_per_day(self):
        claude = self.f.profile(".claude")
        work = self.f.profile(".claude-work")
        may = local_ms(2026, 5, 9, 12)
        jun = local_ms(2026, 6, 1, 9)
        self.f.prompt_log(claude, [(may, "a"), (may + 1000, "a"), (may + 2000, "b"), (jun, "c")])
        with open(os.path.join(claude, "history.jsonl"), "a") as f:
            f.write("not json\n" + json.dumps({"display": "no timestamp"}) + "\n")
        self.f.prompt_log(work, [(may, "z")])
        out = self.f.run()
        self.assertEqual(out["prompts"], {"2026-05-09": {"claude": [3, 2], "work": [1, 1]},
                                          "2026-06-01": {"claude": [1, 1]}})

    def test_prompt_counts_survive_a_trimmed_log(self):
        d = self.f.profile(".claude")
        may = local_ms(2026, 5, 9, 12)
        jun = local_ms(2026, 6, 1, 9)
        self.f.prompt_log(d, [(may, "a"), (may + 1, "a"), (jun, "b")])
        self.f.run()
        self.f.prompt_log(d, [(may + 1, "a"), (jun, "b"), (jun + 1, "b")])
        out = self.f.run()
        self.assertEqual(out["prompts"]["2026-05-09"]["claude"], [2, 1])
        self.assertEqual(out["prompts"]["2026-06-01"]["claude"], [2, 1])

    def test_limits_only_skips_scan(self):
        self.f.profile(".claude")
        out = self.f.run(limits_only=True)
        self.assertEqual(out["rows"], [])
        self.assertTrue(out["limitsOnly"])


UTIL = {"limits": [
    {"kind": "session", "group": "session", "percent": 6, "resets_at": "2026-11-11T18:59:59+00:00", "is_active": False},
    {"kind": "weekly_all", "group": "weekly", "percent": 20, "resets_at": "2026-11-13T11:59:59+00:00", "is_active": True},
    {"kind": "weekly_scoped", "group": "weekly", "percent": 10, "resets_at": "2026-11-13T11:59:59+00:00",
     "scope": {"model": {"display_name": "Fable"}}}]}


class LimitTests(unittest.TestCase):
    def setUp(self):
        self.f = Fixture()

    def tearDown(self):
        self.f.tmp.cleanup()

    def test_normalizes_limit_list(self):
        ls = collect.normalize_limits(UTIL)
        self.assertEqual([l["label"] for l in ls], ["5-hour", "Weekly", "Weekly · Fable"])
        self.assertEqual(ls[0]["percent"], 6)
        self.assertAlmostEqual(ls[0]["resetsAt"], 1794423599, delta=1)

    def test_legacy_shape(self):
        ls = collect.normalize_limits({"five_hour": {"utilization": 4, "resets_at": None},
                                       "seven_day": {"utilization": 9, "resets_at": None}})
        self.assertEqual([(l["label"], l["percent"]) for l in ls], [("5-hour", 4), ("Weekly", 9)])

    def test_fresh_cache_is_not_refetched(self):
        self.f.profile(".claude", cached={"fetchedAtMs": time.time() * 1000, "utilization": UTIL})
        calls = []
        out = self.f.run(online=True, fetch=lambda t: calls.append(t) or UTIL)
        self.assertEqual(calls, [])
        self.assertEqual(out["accounts"][0]["source"], "cache")

    def test_stale_cache_fetches_with_token_and_caches(self):
        self.f.profile(".claude", cached={"fetchedAtMs": 1000, "utilization": UTIL})
        calls = []
        out = self.f.run(online=True, fetch=lambda t: calls.append(t) or UTIL)
        self.assertEqual(calls, ["tok"])
        self.assertEqual(out["accounts"][0]["source"], "live")
        again = self.f.run(online=True, fetch=lambda t: calls.append(t) or UTIL)
        self.assertEqual(len(calls), 1)  # own cache is fresh now
        self.assertEqual(again["accounts"][0]["source"], "cache")

    def test_expired_token_falls_back_to_cache(self):
        self.f.profile(".claude", cached={"fetchedAtMs": 1000, "utilization": UTIL}, expires_in=-10)
        out = self.f.run(online=True, fetch=lambda t: self.fail("should not fetch"))
        a = out["accounts"][0]
        self.assertEqual((a["source"], a["error"]), ("cache", "token-expired"))
        self.assertEqual(len(a["limits"]), 3)

    def test_failure_backs_off(self):
        self.f.profile(".claude", cached={"fetchedAtMs": 1000, "utilization": UTIL})
        calls = []

        def boom(t):
            calls.append(t)
            raise urllib.error.HTTPError("u", 429, "rate limited", {}, None)
        first = self.f.run(online=True, fetch=boom)
        second = self.f.run(online=True, fetch=boom)
        self.assertEqual(len(calls), 1)
        self.assertEqual(first["accounts"][0]["error"], "http-429")
        self.assertEqual(second["accounts"][0]["error"], "backoff")

    def test_offline_never_fetches(self):
        self.f.profile(".claude", cached={"fetchedAtMs": 1000, "utilization": UTIL})
        out = self.f.run(online=False, fetch=lambda t: self.fail("should not fetch"))
        self.assertEqual(out["accounts"][0]["source"], "cache")

    def test_token_never_leaks_into_output(self):
        self.f.profile(".claude", token="SECRET-TOKEN")
        self.assertNotIn("SECRET-TOKEN", json.dumps(self.f.run()))


if __name__ == "__main__":
    unittest.main()
