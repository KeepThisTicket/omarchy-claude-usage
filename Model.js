// Pure helpers for the Claude usage panel. No QML imports, so they can be
// exercised with plain node (tests/model.test.js).
//
// Collector rows: [day, profile, model, input, output, cacheWrite5m,
//                  cacheWrite1h, cacheRead, webSearches, costUsd, messages]

var RANGES = [
  { value: "today", days: 1 },
  { value: "7d", days: 7 },
  { value: "30d", days: 30 },
  { value: "all", days: 0 }
]

var METRICS = ["tokens", "cost", "messages"]

function rangeIndex(value) {
  for (var i = 0; i < RANGES.length; i++)
    if (RANGES[i].value === value) return i
  return -1
}

function shiftRange(value, delta) {
  var i = Math.max(0, Math.min(RANGES.length - 1, Math.max(0, rangeIndex(value)) + delta))
  return RANGES[i].value
}

// ---------- Days ----------

function pad2(n) { return n < 10 ? "0" + n : String(n) }

function dayKey(date) {
  return date.getFullYear() + "-" + pad2(date.getMonth() + 1) + "-" + pad2(date.getDate())
}

function parseDay(key) {
  var p = String(key).split("-")
  return new Date(Number(p[0]), Number(p[1]) - 1, Number(p[2]), 12)  // noon dodges DST edges
}

function addDays(key, n) {
  var d = parseDay(key)
  d.setDate(d.getDate() + n)
  return dayKey(d)
}

// First day of a range ending today, "" for all time.
function rangeStart(range, today) {
  var i = rangeIndex(range)
  var days = i >= 0 ? RANGES[i].days : 7
  return days > 0 ? addDays(today, 1 - days) : ""
}

// ---------- Aggregation ----------

function rowTokens(r) { return r[3] + r[4] + r[5] + r[6] + r[7] }

function matches(r, profile, from, to) {
  return (profile === "all" || r[1] === profile) && (!from || r[0] >= from) && (!to || r[0] <= to)
}

function sessionsIn(sessions, profile, from, to) {
  var n = 0
  for (var day in sessions) {
    if ((from && day < from) || (to && day > to)) continue
    var byProfile = sessions[day]
    for (var p in byProfile)
      if (profile === "all" || p === profile) n += byProfile[p]
  }
  return n
}

function emptyTotals() {
  return { input: 0, output: 0, cacheWrite: 0, cacheRead: 0, tokens: 0, cost: 0, messages: 0, webSearches: 0 }
}

function addRow(t, r) {
  t.input += r[3]
  t.output += r[4]
  t.cacheWrite += r[5] + r[6]
  t.cacheRead += r[7]
  t.webSearches += r[8]
  t.cost += r[9]
  t.messages += r[10]
  t.tokens += rowTokens(r)
}

function sortedGroups(map) {
  var out = []
  for (var k in map) out.push(map[k])
  out.sort(function(a, b) { return b.cost - a.cost || b.tokens - a.tokens })
  return out
}

// Totals for one profile ("all" for every one) between two days inclusive.
function summarize(rows, sessions, profile, from, to) {
  var t = emptyTotals()
  var models = {}, profiles = {}, days = {}
  for (var i = 0; i < (rows || []).length; i++) {
    var r = rows[i]
    if (!matches(r, profile, from, to)) continue
    addRow(t, r)
    var m = models[r[2]] || (models[r[2]] = Object.assign(emptyTotals(), { model: r[2], label: modelLabel(r[2]) }))
    addRow(m, r)
    var p = profiles[r[1]] || (profiles[r[1]] = Object.assign(emptyTotals(), { profile: r[1], sessions: 0 }))
    addRow(p, r)
    days[r[0]] = (days[r[0]] || 0) + rowTokens(r)
  }
  var busiest = null, active = 0
  for (var d in days) {
    if (days[d] <= 0) continue
    active++
    if (!busiest || days[d] > busiest.tokens) busiest = { day: d, tokens: days[d] }
  }
  var byProfile = sortedGroups(profiles)
  for (var j = 0; j < byProfile.length; j++)
    byProfile[j].sessions = sessionsIn(sessions || {}, byProfile[j].profile, from, to)
  var prompt = t.input + t.cacheWrite + t.cacheRead
  return Object.assign(t, {
    sessions: sessionsIn(sessions || {}, profile, from, to),
    activeDays: active,
    busiest: busiest,
    cacheHitRate: prompt > 0 ? t.cacheRead / prompt : null,
    byModel: sortedGroups(models),
    byProfile: byProfile
  })
}

// day -> { tokens, cost, messages, sessions, prompts, promptsOnly } for one
// profile or all. prompts is the collector's {day: {profile: [prompts,
// sessions]}} from Claude Code's prompt log; a day it covers with no token
// rows (its transcripts were cleaned up) is marked promptsOnly and takes its
// session count from the log.
function daily(rows, sessions, profile, prompts) {
  var out = {}
  for (var i = 0; i < (rows || []).length; i++) {
    var r = rows[i]
    if (!matches(r, profile, "", "")) continue
    var d = out[r[0]] || (out[r[0]] = { tokens: 0, cost: 0, messages: 0, sessions: 0, prompts: 0, promptsOnly: false })
    d.tokens += rowTokens(r)
    d.cost += r[9]
    d.messages += r[10]
  }
  for (var day in out) out[day].sessions = sessionsIn(sessions || {}, profile, day, day)
  for (var pday in (prompts || {})) {
    var n = 0, s = 0, byProfile = prompts[pday]
    for (var p in byProfile) {
      if (profile !== "all" && p !== profile) continue
      n += byProfile[p][0]
      s += byProfile[p][1]
    }
    if (n <= 0) continue
    if (out[pday]) out[pday].prompts = n
    else out[pday] = { tokens: 0, cost: 0, messages: 0, sessions: s, prompts: n, promptsOnly: true }
  }
  return out
}

// ---------- Heatmap ----------

// Thresholds splitting the non-zero values into four even buckets, like
// GitHub's contribution graph.
function levelThresholds(values) {
  var nz = values.filter(function(v) { return v > 0 }).sort(function(a, b) { return a - b })
  if (nz.length === 0) return [0, 0, 0]
  function q(f) { return nz[Math.floor(f * (nz.length - 1))] }  // the top value always lands in bucket 4
  return [q(0.25), q(0.5), q(0.75)]
}

function levelFor(v, th) {
  if (!(v > 0)) return 0
  if (v <= th[0]) return 1
  if (v <= th[1]) return 2
  if (v <= th[2]) return 3
  return 4
}

// Weeks run Monday..Sunday top to bottom; the last column holds today.
// Prompts-only days (no token data) are levelled on their own scale by prompt
// count, so they show activity without pretending to be token/cost data.
// Returns { cells: [{day, col, row, value, level, future, promptsOnly}],
//           months: [{col, month}], thresholds, promptsOnlyDays }.
function heatmap(dailyMap, today, weeks, metric) {
  var t = parseDay(today)
  var dow = (t.getDay() + 6) % 7  // Monday = 0
  var start = addDays(today, -dow - 7 * (weeks - 1))
  var cells = [], values = [], promptValues = []
  var months = [], lastMonth = -1
  for (var c = 0; c < weeks; c++) {
    for (var r = 0; r < 7; r++) {
      var day = addDays(start, c * 7 + r)
      var future = day > today
      var e = dailyMap[day]
      var only = !future && !!e && !!e.promptsOnly
      var v = future || !e || only ? 0 : Number(e[metric] || 0)
      if (only) promptValues.push(e.prompts)
      else if (!future) values.push(v)
      cells.push({ day: day, col: c, row: r, value: v, level: 0, future: future, promptsOnly: only,
                   prompts: only ? e.prompts : 0 })
      if (r === 0) {
        var month = parseDay(day).getMonth()
        // Label a month at the first week that starts in it.
        if (month !== lastMonth) {
          if (lastMonth !== -1 || parseDay(day).getDate() <= 7) months.push({ col: c, month: month })
          lastMonth = month
        }
      }
    }
  }
  var th = levelThresholds(values), pth = levelThresholds(promptValues)
  for (var i = 0; i < cells.length; i++)
    cells[i].level = cells[i].promptsOnly ? levelFor(cells[i].prompts, pth) : levelFor(cells[i].value, th)
  return { cells: cells, months: months, thresholds: th, promptsOnlyDays: promptValues.length }
}

// Consecutive active days ending today (or yesterday, if today is still
// empty), and the longest run on record.
function streaks(dailyMap, today) {
  function active(d) { var e = dailyMap[d]; return !!e && (e.messages > 0 || e.tokens > 0 || e.prompts > 0) }
  var current = 0
  var d = active(today) ? today : addDays(today, -1)
  while (active(d)) { current++; d = addDays(d, -1) }
  var days = Object.keys(dailyMap).filter(active).sort()
  var longest = 0, run = 0, prev = ""
  for (var i = 0; i < days.length; i++) {
    run = prev && addDays(prev, 1) === days[i] ? run + 1 : 1
    if (run > longest) longest = run
    prev = days[i]
  }
  return { current: current, longest: longest, activeDays: days.length }
}

// ---------- Limits ----------

function accountsFor(accounts, profile) {
  return (accounts || []).filter(function(a) {
    return profile === "all" || (a.profiles || []).indexOf(profile) >= 0
  })
}

// The limit shown on the bar: the highest 5-hour use among the accounts in
// view, else the highest of anything.
function headlineLimit(accounts) {
  var best = null, bestSession = null
  for (var i = 0; i < (accounts || []).length; i++) {
    var ls = accounts[i].limits || []
    for (var j = 0; j < ls.length; j++) {
      var l = ls[j]
      if (l.percent === null || l.percent === undefined) continue
      if ((l.kind === "session" || l.kind === "five_hour") && (!bestSession || l.percent > bestSession.percent)) bestSession = l
      if (!best || l.percent > best.percent) best = l
    }
  }
  return bestSession || best
}

// A limit whose reset has passed shows 0 until the next refresh.
function livePercent(limit, now) {
  if (!limit) return null
  if (limit.resetsAt && now >= limit.resetsAt) return 0
  return limit.percent
}

// ---------- Formatting ----------

function trimZero(s) { return s.replace(/\.0$/, "") }

// Thousands separators without toLocaleString, which QML redefines.
function groupDigits(n) {
  return String(Math.round(n)).replace(/\B(?=(\d{3})+(?!\d))/g, ",")
}

function formatTokens(n) {
  n = Number(n) || 0
  var a = Math.abs(n)
  if (a < 1000) return String(Math.round(n))
  if (a < 1e6) return trimZero((n / 1e3).toFixed(a < 1e4 ? 1 : 0)) + "K"
  if (a < 1e9) return trimZero((n / 1e6).toFixed(a < 1e8 ? 1 : 0)) + "M"
  return trimZero((n / 1e9).toFixed(2).replace(/0$/, "")) + "B"
}

function formatCost(usd) {
  var v = Number(usd) || 0
  if (v >= 10000) return "$" + trimZero((v / 1000).toFixed(1)) + "K"
  if (v >= 100) return "$" + groupDigits(v)
  return "$" + v.toFixed(2)
}

function formatCount(n) {
  return groupDigits(Number(n) || 0)
}

function formatMetric(metric, v) {
  if (metric === "cost") return formatCost(v)
  if (metric === "messages") return formatCount(v)
  return formatTokens(v)
}

// "2h 41m", "1d 19h", "12m", "<1m"
function formatCountdown(seconds) {
  var s = Math.max(0, Math.round(Number(seconds) || 0))
  if (s < 60) return "<1m"
  var m = Math.floor(s / 60), h = Math.floor(m / 60), d = Math.floor(h / 24)
  if (d > 0) return h % 24 ? d + "d " + (h % 24) + "h" : d + "d"
  if (h > 0) return m % 60 ? h + "h " + (m % 60) + "m" : h + "h"
  return m + "m"
}

function formatPercent(p) {
  return p === null || p === undefined || isNaN(p) ? "—" : Math.round(p) + "%"
}

// "claude-opus-5-5" -> "Opus 5.5", "claude-3-5-haiku" -> "Haiku 3.5"
function modelLabel(model) {
  var s = String(model || "").replace(/^claude-/, "")
  var m = s.match(/^([a-z]+)-(\d+)(?:-(\d+))?$/)
  if (m) return m[1].charAt(0).toUpperCase() + m[1].slice(1) + " " + m[2] + (m[3] ? "." + m[3] : "")
  m = s.match(/^(\d+)-(\d+)-([a-z]+)$/)
  if (m) return m[3].charAt(0).toUpperCase() + m[3].slice(1) + " " + m[1] + "." + m[2]
  return s || "?"
}


// ---------- Agents ----------
// Groups for the Agents view, most urgent first.
var AGENT_STATUSES = ["needs", "failed", "working", "idle", "done"]
var DONE_WINDOW = 86400  // finished background jobs older than this are folded away

// agents.py output -> what the view draws. `profile` is "all" or a profile id;
// remote hosts ("@host") only show under "all".
function agentView(agents, profile, now) {
  var counts = { needs: 0, failed: 0, working: 0, idle: 0, done: 0 }
  var buckets = { needs: [], failed: [], working: [], idle: [], done: [] }
  var hiddenDone = 0
  var list = agents || []
  for (var i = 0; i < list.length; i++) {
    var a = list[i]
    if (profile && profile !== "all" && a.profile !== profile) continue
    var st = AGENT_STATUSES.indexOf(a.status) >= 0 ? a.status : "idle"
    counts[st]++
    if (st === "done" && now - (a.updatedAt || a.startedAt || 0) > DONE_WINDOW) { hiddenDone++; continue }
    buckets[st].push(a)
  }
  function newest(x, y) {
    var tx = x.updatedAt || x.startedAt || 0, ty = y.updatedAt || y.startedAt || 0
    if (tx !== ty) return ty - tx
    return String(x.name).localeCompare(String(y.name))
  }
  var groups = [], next = 0
  for (var g = 0; g < AGENT_STATUSES.length; g++) {
    var key = AGENT_STATUSES[g]
    if (!buckets[key].length) continue
    // Copies, each with the key that opens it, in the order they're drawn.
    var items = buckets[key].sort(newest).map(function(a) {
      var c = {}
      for (var k in a) c[k] = a[k]
      c.hotkey = next < HOTKEYS.length ? HOTKEYS.charAt(next) : ""
      next++
      return c
    })
    groups.push({ status: key, items: items })
  }
  return { groups: groups, counts: counts, hiddenDone: hiddenDone }
}

// Keys that open agents, in screen order. h j k l (moving) and x (delete)
// belong to the panel's own key handling.
var HOTKEYS = "1234567890abcdefgimnopqrstuvwyz"

// The agent a key press opens, or null.
function agentForKey(view, key) {
  var k = String(key || "").toLowerCase()
  if (k.length !== 1 || HOTKEYS.indexOf(k) < 0) return null
  var groups = (view && view.groups) || []
  for (var g = 0; g < groups.length; g++)
    for (var i = 0; i < groups[g].items.length; i++)
      if (groups[g].items[i].hotkey === k) return groups[g].items[i]
  return null
}

// Agents still going: everything but finished jobs.
function activeAgentCount(counts) {
  return counts ? counts.needs + counts.failed + counts.working + counts.idle : 0
}

// Statuses with at least one agent on screen, in panel order: one dot each on
// the bar. Finished jobs folded away (older than a day) don't count.
function barStatuses(view) {
  if (!view || !view.groups) return []
  return view.groups.map(function(g) { return g.status })
}

// "now", "4m", "3h", "2d"
function formatAge(seconds) {
  var s = Math.max(0, Math.round(Number(seconds) || 0))
  if (s < 60) return "now"
  var m = Math.floor(s / 60), h = Math.floor(m / 60), d = Math.floor(h / 24)
  if (d > 0) return d + "d"
  if (h > 0) return h + "h"
  return m + "m"
}

if (typeof module !== "undefined") {
  module.exports = {
    RANGES: RANGES,
    METRICS: METRICS,
    rangeIndex: rangeIndex,
    shiftRange: shiftRange,
    dayKey: dayKey,
    parseDay: parseDay,
    addDays: addDays,
    rangeStart: rangeStart,
    summarize: summarize,
    daily: daily,
    levelThresholds: levelThresholds,
    heatmap: heatmap,
    streaks: streaks,
    accountsFor: accountsFor,
    headlineLimit: headlineLimit,
    livePercent: livePercent,
    formatTokens: formatTokens,
    formatCost: formatCost,
    formatCount: formatCount,
    formatMetric: formatMetric,
    formatCountdown: formatCountdown,
    formatPercent: formatPercent,
    modelLabel: modelLabel,
    AGENT_STATUSES: AGENT_STATUSES,
    agentView: agentView,
    HOTKEYS: HOTKEYS,
    agentForKey: agentForKey,
    activeAgentCount: activeAgentCount,
    barStatuses: barStatuses,
    formatAge: formatAge
  }
}
