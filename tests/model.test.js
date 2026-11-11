// Run: node tests/model.test.js
const assert = require("assert")
const M = require("../Model.js")

let failed = 0
function test(name, fn) {
  try { fn(); console.log("ok   " + name) }
  catch (e) { failed++; console.log("FAIL " + name + "\n     " + e.message) }
}

// [day, profile, model, in, out, cw5, cw1h, cr, web, cost, msgs]
const rows = [
  ["2026-11-03", "claude", "claude-opus-5-5", 10, 100, 1000, 0, 9000, 0, 1.5, 4],
  ["2026-11-10", "claude", "claude-opus-5-5", 5, 50, 0, 500, 4000, 1, 0.75, 2],
  ["2026-11-10", "work", "claude-sonnet-5-5", 1, 10, 100, 0, 900, 0, 0.1, 1],
  ["2026-11-11", "work", "claude-opus-5-5", 2, 20, 0, 0, 1000, 0, 0.2, 3],
]
const sessions = { "2026-11-10": { claude: 1, work: 2 }, "2026-11-11": { work: 1 } }

test("day arithmetic crosses months", () => {
  assert.strictEqual(M.addDays("2026-10-31", 1), "2026-11-01")
  assert.strictEqual(M.addDays("2026-03-01", -1), "2026-02-28")
})

test("range starts", () => {
  assert.strictEqual(M.rangeStart("today", "2026-11-11"), "2026-11-11")
  assert.strictEqual(M.rangeStart("7d", "2026-11-11"), "2026-11-05")
  assert.strictEqual(M.rangeStart("30d", "2026-11-11"), "2026-10-13")
  assert.strictEqual(M.rangeStart("all", "2026-11-11"), "")
})

test("summarize all profiles over 7 days", () => {
  const s = M.summarize(rows, sessions, "all", "2026-11-05", "2026-11-11")
  assert.strictEqual(s.messages, 6)
  assert.strictEqual(s.output, 80)
  assert.strictEqual(s.cacheWrite, 600)
  assert.strictEqual(s.tokens, 5 + 50 + 500 + 4000 + 1 + 10 + 100 + 900 + 2 + 20 + 1000)
  assert.ok(Math.abs(s.cost - 1.05) < 1e-9)
  assert.strictEqual(s.sessions, 4)
  assert.strictEqual(s.activeDays, 2)
  assert.strictEqual(s.busiest.day, "2026-11-10")
  assert.deepStrictEqual(s.byModel.map(m => m.label), ["Opus 5.5", "Sonnet 5.5"])
  assert.deepStrictEqual(s.byProfile.map(p => [p.profile, p.sessions]), [["claude", 1], ["work", 3]])
  assert.ok(s.cacheHitRate > 0.8 && s.cacheHitRate < 1)
})

test("summarize one profile", () => {
  const s = M.summarize(rows, sessions, "work", "", "")
  assert.strictEqual(s.messages, 4)
  assert.strictEqual(s.sessions, 3)
})

test("empty range", () => {
  const s = M.summarize(rows, sessions, "all", "2027-01-01", "2027-01-02")
  assert.strictEqual(s.tokens, 0)
  assert.strictEqual(s.busiest, null)
  assert.strictEqual(s.cacheHitRate, null)
})

test("daily map", () => {
  const d = M.daily(rows, sessions, "all")
  assert.strictEqual(d["2026-11-10"].messages, 3)
  assert.strictEqual(d["2026-11-10"].sessions, 3)
  assert.strictEqual(d["2026-11-03"].sessions, 0)
})

test("heatmap grid ends on today's week, Monday first", () => {
  // 2026-11-11 is a Wednesday.
  const h = M.heatmap(M.daily(rows, sessions, "all"), "2026-11-11", 53, "tokens")
  assert.strictEqual(h.cells.length, 53 * 7)
  const last = h.cells.filter(c => c.col === 52)
  assert.strictEqual(last[0].day, "2026-11-09")  // Monday
  assert.strictEqual(last[2].day, "2026-11-11")
  assert.ok(last[3].future && last[6].future)
  const tue = h.cells.find(c => c.day === "2026-11-10")
  assert.strictEqual(tue.row, 1)
  assert.ok(tue.level > 0 && tue.level < 4)
  assert.strictEqual(h.cells.find(c => c.day === "2026-11-03").level, 4)  // busiest day is the top bucket
  assert.strictEqual(h.cells.find(c => c.day === "2026-11-01").level, 0)
  assert.ok(h.months.length >= 12 && h.months.length <= 13)
})

test("heatmap levels split non-zero values in quarters", () => {
  const th = M.levelThresholds([0, 1, 2, 3, 4, 5, 6, 7, 8])
  assert.deepStrictEqual(th, [2, 4, 6])
  assert.deepStrictEqual(M.levelThresholds([0, 0]), [0, 0, 0])
})

test("streaks", () => {
  const d = { "2026-11-01": { messages: 1 }, "2026-11-02": { messages: 1 }, "2026-11-03": { messages: 1 },
              "2026-11-09": { messages: 1 }, "2026-11-10": { messages: 2 } }
  // Today empty: the streak through yesterday still counts.
  assert.deepStrictEqual(M.streaks(d, "2026-11-11"), { current: 2, longest: 3, activeDays: 5 })
  assert.strictEqual(M.streaks(d, "2026-11-12").current, 0)
})

test("headline limit prefers the 5-hour window", () => {
  const accts = [{ profiles: ["claude"], limits: [{ kind: "session", percent: 6 }, { kind: "weekly_all", percent: 20 }] },
                 { profiles: ["work"], limits: [{ kind: "session", percent: 40 }] }]
  assert.strictEqual(M.headlineLimit(accts).percent, 40)
  assert.strictEqual(M.headlineLimit(M.accountsFor(accts, "claude")).percent, 6)
  assert.strictEqual(M.headlineLimit([]), null)
})

test("a passed reset reads as 0%", () => {
  assert.strictEqual(M.livePercent({ percent: 70, resetsAt: 100 }, 200), 0)
  assert.strictEqual(M.livePercent({ percent: 70, resetsAt: 300 }, 200), 70)
})

test("formatting", () => {
  assert.strictEqual(M.formatTokens(999), "999")
  assert.strictEqual(M.formatTokens(1234), "1.2K")
  assert.strictEqual(M.formatTokens(56000), "56K")
  assert.strictEqual(M.formatTokens(1500000), "1.5M")
  assert.strictEqual(M.formatTokens(170447218), "170M")
  assert.strictEqual(M.formatTokens(1391638409), "1.39B")
  assert.strictEqual(M.formatTokens(2000000000), "2B")
  assert.strictEqual(M.formatCost(0.4), "$0.40")
  assert.strictEqual(M.formatCost(490.47), "$490")
  assert.strictEqual(M.formatCost(1234.5), "$1,235")
  assert.strictEqual(M.formatCost(23456), "$23.5K")
  assert.strictEqual(M.formatCount(12345), "12,345")
  assert.strictEqual(M.formatCountdown(9660), "2h 41m")
  assert.strictEqual(M.formatCountdown(158400), "1d 20h")
  assert.strictEqual(M.formatCountdown(30), "<1m")
  assert.strictEqual(M.formatPercent(null), "—")
})

test("model labels", () => {
  assert.strictEqual(M.modelLabel("claude-opus-5-5"), "Opus 5.5")
  assert.strictEqual(M.modelLabel("claude-sonnet-5"), "Sonnet 5")
  assert.strictEqual(M.modelLabel("claude-fable-5-2"), "Fable 5.2")
  assert.strictEqual(M.modelLabel("claude-3-5-haiku"), "Haiku 3.5")
})

test("range shifting clamps", () => {
  assert.strictEqual(M.shiftRange("today", -1), "today")
  assert.strictEqual(M.shiftRange("7d", 1), "30d")
  assert.strictEqual(M.shiftRange("all", 1), "all")
})


// ---------- Agents ----------
const NOW = 1794415000
const ag = [
  { key: "a", name: "zoom", profile: "work", status: "working", updatedAt: NOW - 30 },
  { key: "b", name: "older work", profile: "work", status: "working", updatedAt: NOW - 3000 },
  { key: "c", name: "blocked", profile: "work", status: "needs", updatedAt: NOW - 90000 },
  { key: "d", name: "fresh done", profile: "claude", status: "done", updatedAt: NOW - 3600 },
  { key: "e", name: "old done", profile: "claude", status: "done", updatedAt: NOW - 200000 },
  { key: "f", name: "chat", profile: "claude", status: "idle", updatedAt: null, startedAt: NOW - 50 },
  { key: "g", name: "Project", profile: "@devbox", status: "working", updatedAt: null },
  { key: "h", name: "weird", profile: "work", status: "mystery", updatedAt: NOW },
]

test("agent groups: urgent first, newest first, old done folded", () => {
  const v = M.agentView(ag, "all", NOW)
  assert.deepStrictEqual(v.groups.map(g => g.status), ["needs", "working", "idle", "done"])
  assert.deepStrictEqual(v.groups[1].items.map(a => a.key), ["a", "b", "g"])
  assert.deepStrictEqual(v.groups[2].items.map(a => a.key), ["h", "f"])
  assert.deepStrictEqual(v.groups[3].items.map(a => a.key), ["d"])
  assert.strictEqual(v.hiddenDone, 1)
  assert.deepStrictEqual(v.counts, { needs: 1, failed: 0, working: 3, idle: 2, done: 2 })
  assert.strictEqual(M.activeAgentCount(v.counts), 6)
})

test("agent profile filter hides other profiles and remotes", () => {
  const v = M.agentView(ag, "claude", NOW)
  assert.deepStrictEqual(v.groups.map(g => g.items.map(a => a.key)), [["f"], ["d"]])
  assert.strictEqual(v.counts.working, 0)
  assert.strictEqual(M.agentView([], "all", NOW).groups.length, 0)
  assert.strictEqual(M.activeAgentCount(null), 0)
})

test("agent ages", () => {
  assert.strictEqual(M.formatAge(5), "now")
  assert.strictEqual(M.formatAge(240), "4m")
  assert.strictEqual(M.formatAge(3 * 3600 + 59), "3h")
  assert.strictEqual(M.formatAge(2 * 86400 + 5), "2d")
})

test("agent hotkeys follow screen order and skip the panel's own keys", () => {
  const v = M.agentView(ag, "all", NOW)
  const keys = [].concat(...v.groups.map(g => g.items.map(a => a.hotkey)))
  assert.deepStrictEqual(keys, ["1", "2", "3", "4", "5", "6", "7"])
  assert.strictEqual(v.groups[0].items[0].hotkey, "1")  // the one that needs you
  assert.strictEqual(ag[0].hotkey, undefined)           // input objects untouched
  for (const reserved of "hjklx") assert.ok(M.HOTKEYS.indexOf(reserved) < 0, reserved)
  assert.strictEqual(M.HOTKEYS.slice(0, 11), "1234567890a")
  const many = Array.from({ length: 40 }, (_, i) => ({ key: "k" + i, name: "n" + i, profile: "p", status: "idle", updatedAt: NOW - i }))
  const items = M.agentView(many, "all", NOW).groups[0].items
  assert.strictEqual(items[M.HOTKEYS.length - 1].hotkey, M.HOTKEYS[M.HOTKEYS.length - 1])
  assert.strictEqual(items[M.HOTKEYS.length].hotkey, "")  // ran out of keys
  assert.strictEqual(M.agentForKey(M.agentView(ag, "all", NOW), "3").key, "b")
  assert.strictEqual(M.agentForKey(M.agentView(ag, "all", NOW), "Z"), null)
})

if (failed) { console.log(failed + " failed"); process.exit(1) }
console.log("all passed")
