import QtQuick
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Commons as Commons
import qs.Ui
import "Model.js" as Model

// Claude Code usage across config dirs (~/.claude, ~/.claude-work, ...):
// plan limits with reset countdowns, a year heatmap, and token / cost
// totals. Built like kov.battery-history: collect.py does the reading,
// Model.js the arithmetic, this file only draws.
Panel {
  id: root
  moduleName: "kov.claude-usage"
  ipcTarget: "kov.claude-usage"

  Commons.Translations { id: strings; directory: Qt.resolvedUrl("i18n") }

  // ---------- Settings ----------
  readonly property string range: {
    var v = String(setting("range", "7d"))
    return Model.rangeIndex(v) >= 0 ? v : "7d"
  }
  readonly property string metric: {
    var v = String(setting("metric", "tokens"))
    return Model.METRICS.indexOf(v) >= 0 ? v : "tokens"
  }
  readonly property string profile: {
    var v = String(setting("profile", "all"))
    return v === "all" || profileIds.indexOf(v) >= 0 ? v : "all"
  }

  // ---------- Data ----------
  property var usage: null        // last full scan
  property var accounts: []       // freshest limits, from either kind of run
  property var knownProfiles: []
  property bool loading: false
  property real nowSecs: Date.now() / 1000
  property string hoverDay: ""

  // Agents view: every Claude Code session and background job (agents.py).
  readonly property string view: String(setting("view", "usage")) === "agents" ? "agents" : "usage"
  property var agentsData: null
  property string agentNote: ""
  readonly property var agentList: agentsData ? agentsData.agents : []
  readonly property var agentView: Model.agentView(agentList, profile, nowSecs)
  readonly property int activeAgents: Model.activeAgentCount(agentView.counts)

  readonly property var profileIds: knownProfiles.map(function(p) { return p.id })
  readonly property string today: Model.dayKey(new Date(nowSecs * 1000))
  readonly property var rows: usage ? usage.rows : []
  readonly property var sessions: usage ? usage.sessions : ({})
  readonly property var shownAccounts: Model.accountsFor(accounts, profile)
  readonly property var headline: Model.headlineLimit(accounts)
  readonly property var headlinePercent: Model.livePercent(headline, nowSecs)
  readonly property var dailyMap: Model.daily(rows, sessions, profile)
  readonly property var heat: Model.heatmap(dailyMap, today, 53, metric)
  readonly property var summary: Model.summarize(rows, sessions, profile, Model.rangeStart(range, today), today)
  readonly property var streak: Model.streaks(dailyMap, today)
  readonly property var yearTotal: Model.summarize(rows, sessions, profile, Model.addDays(today, -364), today)

  // ---------- Look ----------
  readonly property color fg: root.bar ? root.bar.foreground : Commons.Color.foreground
  readonly property string fontFamily: root.bar ? root.bar.fontFamily : Style.font.family
  readonly property color faint: Qt.darker(fg, 1.4)
  readonly property color claudeColor: "#D97757"
  readonly property color warnColor: "#E5C736"
  readonly property color urgentColor: Commons.Color.urgent
  // The claude-waybar indicator's palette, so the states read the same as before.
  readonly property var agentColors: ({
    needs: "#ffa066", failed: String(Commons.Color.urgent), working: "#98bb6c", idle: "#7e9cd8", done: String(faint)
  })
  readonly property string glyph: ""

  function tr(key) { return strings.t(key) }
  function locale() { return Qt.locale(Commons.I18n.formatLocale) }
  function fmt(t, f) { return new Date(t * 1000).toLocaleString(root.locale(), f) }
  function fmtDay(key, f) { return Model.parseDay(key).toLocaleDateString(root.locale(), f) }
  function levelColor(level) {
    if (level <= 0) return Util.alpha(root.fg, 0.08)
    return Util.alpha(root.claudeColor, [0, 0.3, 0.5, 0.75, 1][level])
  }
  function limitColor(p) {
    if (p >= 90) return root.urgentColor
    if (p >= 75) return root.warnColor
    return root.claudeColor
  }

  function setSetting(key, value) {
    if (String(setting(key, "")) === value) return
    var next = Object.assign({}, root.settings)
    next[key] = value
    root.settings = next
    if (root.bar && root.bar.shell) root.bar.shell.updateEntryInline(root.moduleName, root.settings)
  }

  // ---------- Collector ----------
  readonly property string collectorPath: decodeURIComponent(Qt.resolvedUrl("collect.py").toString().replace(/^file:\/\//, ""))

  function refresh() {
    if (collector.running) { collector.pending = true; return }
    root.loading = root.usage === null
    collector.command = ["python3", "-I", root.collectorPath, "--max-age", "300"]
    collector.running = true
  }

  function refreshLimits() {
    if (collector.running || limitsReader.running) return
    limitsReader.command = ["python3", "-I", root.collectorPath, "--limits-only", "--no-network"]
    limitsReader.running = true
  }

  readonly property string agentsPath: decodeURIComponent(Qt.resolvedUrl("agents.py").toString().replace(/^file:\/\//, ""))
  readonly property string openerPath: decodeURIComponent(Qt.resolvedUrl("open-agent.py").toString().replace(/^file:\/\//, ""))

  function refreshAgents() {
    if (agentsReader.running) return
    agentsReader.command = ["python3", "-I", root.agentsPath]
    agentsReader.running = true
  }

  function ingestAgents(text) {
    var parsed = null
    try { parsed = JSON.parse(text) } catch (e) { console.warn("kov.claude-usage: bad agents output") }
    if (!parsed || !Array.isArray(parsed.agents)) return
    root.agentsData = parsed
    root.nowSecs = Date.now() / 1000
  }

  // Jump to an agent: its terminal window, `claude attach` for a background
  // job, or the SSH terminal for a remote one.
  function openAgent(a) {
    if (opener.running || !a) return
    var args
    if (a.kind === "remote") {
      args = ["--remote", a.host, "--port", String(a.port || 0), "--session", a.sessionId || "", "--cwd", a.cwd || ""]
    } else if (a.kind === "background" && a.id) {
      args = ["--attach", a.id, "--config-dir", a.configDir, "--cwd", a.cwd || "", "--name", a.name || ""]
    } else if (a.pid) {
      args = ["--pid", String(a.pid), "--name", a.name || ""]
    } else {
      return
    }
    runOpener(args, a.name)
  }

  function runOpener(args, name) {
    if (opener.running) return
    root.agentNote = ""
    opener.agentName = name
    opener.command = ["python3", "-I", root.openerPath].concat(args)
    opener.running = true
  }

  // ---------- Right-click menu ----------
  property var menuAgent: null
  property point menuScenePoint: Qt.point(0, 0)
  property var pendingAction: null   // { kind: "rm" | "stop" | "end", agent } awaiting confirmation

  function openMenu(a, scenePoint) {
    root.pendingAction = null
    root.menuScenePoint = scenePoint
    root.menuAgent = a
  }

  // What the menu offers for one agent, in order.
  function menuItems(a) {
    if (!a) return []
    var items = []
    if (a.kind === "background") {
      items.push({ action: "open", label: root.tr("menu.attach") })
      items.push({ action: "logs", label: root.tr("menu.logs") })
      if (a.alive) items.push({ action: "stop", label: root.tr("menu.stop") })
      items.push({ action: "rm", label: root.tr("menu.delete"), danger: true })
    } else if (a.kind === "remote") {
      items.push({ action: "open", label: root.tr("menu.ssh") })
    } else {
      items.push({ action: "open", label: root.tr("menu.focus") })
      if (a.pid) items.push({ action: "end", label: root.tr("menu.end"), danger: true })
    }
    items.push({ action: "copy", label: root.tr("menu.copyResume") })
    return items
  }

  // A command that reopens the agent from any terminal.
  function resumeCommand(a) {
    if (a.kind === "remote") return "ssh -t " + a.host + " claude --resume " + (a.sessionId || "")
    var dir = String(a.configDir || "")
    var prefix = /\/\.claude\/?$/.test(dir) ? "" : "CLAUDE_CONFIG_DIR=" + dir.replace(/^\/home\/[^\/]+/, "~") + " "
    return prefix + (a.kind === "background" && a.id ? "claude attach " + a.id : "claude --resume " + a.sessionId)
  }

  function menuAction(action, a) {
    root.menuAgent = null
    if (!a) return
    if (action === "open") root.openAgent(a)
    else if (action === "logs") root.runOpener(["--logs", a.id, "--config-dir", a.configDir, "--cwd", a.cwd || ""], a.name)
    else if (action === "copy") { copier.command = ["wl-copy", "--", root.resumeCommand(a)]; copier.running = true }
    else root.pendingAction = { kind: action, agent: a }
  }

  function confirmText(p) {
    if (!p) return ""
    return strings.t("confirm." + p.kind).arg(p.agent.name)
  }

  function runPending() {
    var p = root.pendingAction
    root.pendingAction = null
    if (!p || actionRunner.running) return
    var args = p.kind === "end" ? ["--end", String(p.agent.pid)]
                                : ["--" + p.kind, p.agent.id, "--config-dir", p.agent.configDir]
    root.agentNote = ""
    actionRunner.agentName = p.agent.name
    actionRunner.command = ["python3", "-I", root.openerPath].concat(args)
    actionRunner.running = true
  }

  function ingest(text) {
    root.loading = false
    root.nowSecs = Date.now() / 1000
    var parsed = null
    try { parsed = JSON.parse(text) } catch (e) { console.warn("kov.claude-usage: bad collector output") }
    if (!parsed || !Array.isArray(parsed.accounts)) return
    root.accounts = parsed.accounts
    if (!parsed.limitsOnly) root.usage = parsed
    if (!parsed.limitsOnly || root.knownProfiles.length === 0) root.knownProfiles = parsed.profiles || []
  }

  Process {
    id: collector
    property bool pending: false
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: {
        root.ingest(text)
        if (collector.pending) { collector.pending = false; Qt.callLater(root.refresh) }
      }
    }
  }

  Process {
    id: limitsReader
    stdout: StdioCollector { waitForEnd: true; onStreamFinished: root.ingest(text) }
  }

  Process {
    id: agentsReader
    stdout: StdioCollector { waitForEnd: true; onStreamFinished: root.ingestAgents(text) }
  }

  Process {
    id: opener
    property string agentName: ""
    onExited: function(exitCode, exitStatus) {
      if (exitCode === 0) root.close()
      else if (exitCode === 3) root.agentNote = strings.t("agents.noWindow").arg(agentName)
      else root.agentNote = strings.t("agents.openFailed").arg(agentName)
    }
  }

  Process {
    id: actionRunner
    property string agentName: ""
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: actionRunner.output = text
    }
    property string output: ""
    onExited: function(exitCode, exitStatus) {
      if (exitCode !== 0) {
        var reason = String(actionRunner.output || "").trim()
        root.agentNote = strings.t("agents.actionFailed").arg(actionRunner.agentName) + (reason ? " " + reason : "")
      }
      actionRunner.output = ""
      root.refreshAgents()
      afterAction.restart()  // the daemon can take a moment to drop a stopped job
    }
  }
  Timer { id: afterAction; interval: 1500; onTriggered: root.refreshAgents() }

  Process { id: copier }

  Component.onCompleted: { refreshLimits(); refreshAgents() }
  onOpenedChanged: {
    menuAgent = null
    pendingAction = null
    if (opened) { refresh(); refreshAgents(); agentNote = "" }
  }
  // Agents: every 3 s while the list is on screen, otherwise each minute for
  // the bar's tooltip.
  Timer {
    interval: root.opened && (panel.split || root.view === "agents") ? 3000 : 60000
    running: true; repeat: true
    onTriggered: root.refreshAgents()
  }
  // Open: full scan each minute (the network at most every 5). Closed: the
  // bar's percentage from cached readings only.
  Timer { interval: 60000; running: root.opened; repeat: true; onTriggered: root.refresh() }
  Timer { interval: 300000; running: !root.opened; repeat: true; onTriggered: root.refreshLimits() }
  Timer { interval: 30000; running: true; repeat: true; onTriggered: root.nowSecs = Date.now() / 1000 }

  // ---------- Text ----------
  function resetText(l) {
    if (!l || !l.resetsAt) return ""
    if (root.nowSecs >= l.resetsAt) return root.tr("limits.resetPassed")
    var soon = l.resetsAt - root.nowSecs < 86400
    return strings.t("limits.resetsIn").arg(Model.formatCountdown(l.resetsAt - root.nowSecs))
                                       .arg(root.fmt(l.resetsAt, soon ? "HH:mm" : "ddd HH:mm"))
  }

  function freshnessText(a) {
    if (a.error === "token-expired") return root.tr("limits.tokenExpired")
    if (a.error && a.error !== "backoff" && a.source !== "live") return root.tr("limits.offline")
    if (a.fetchedAt && root.nowSecs - a.fetchedAt > 600)
      return strings.t("limits.asOf").arg(root.fmt(a.fetchedAt, root.nowSecs - a.fetchedAt > 86400 ? "ddd HH:mm" : "HH:mm"))
    return ""
  }

  function dayText(day) {
    var e = root.dailyMap[day]
    var label = root.fmtDay(day, "ddd d MMM")
    if (!e) return strings.t("activity.empty").arg(label)
    return strings.t("activity.day").arg(label).arg(Model.formatTokens(e.tokens)).arg(Model.formatCost(e.cost))
      .arg(Model.formatCount(e.messages)).arg(e.sessions)
  }

  // "2 working · 1 needs you · 3 idle"
  function agentSummary(counts) {
    var parts = []
    var order = ["needs", "failed", "working", "idle"]
    for (var i = 0; i < order.length; i++)
      if (counts[order[i]] > 0) parts.push(strings.t("agents.count." + order[i]).arg(counts[order[i]]))
    return parts.length ? parts.join(" · ") : root.tr("agents.none")
  }

  readonly property string statusText: {
    if (view === "agents" && !panel.split && hoverDay === "") return agentsData ? agentSummary(agentView.counts) : tr("agents.loading")
    if (hoverDay !== "") return dayText(hoverDay)
    if (headline) return strings.t("status.headline").arg(headline.label).arg(Model.formatPercent(headlinePercent))
      .arg(Model.formatCountdown(Math.max(0, (headline.resetsAt || nowSecs) - nowSecs)))
    if (loading) return tr("loading")
    return usage && rows.length === 0 ? tr("noData") : ""
  }

  readonly property string tooltip: {
    var parts = [tr("title")]
    for (var i = 0; i < accounts.length; i++) {
      var ls = accounts[i].limits || []
      for (var j = 0; j < ls.length; j++) {
        if (ls[j].kind === "weekly_scoped") continue
        parts.push(ls[j].label + " " + Model.formatPercent(Model.livePercent(ls[j], nowSecs)))
      }
    }
    var text = parts.join(" · ")
    if (agentsData) text += "\n" + strings.t("tooltip.agents").arg(agentSummary(Model.agentView(agentList, "all", nowSecs).counts))
    return text
  }

  // ---------- Bar ----------
  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  BarIconButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    text: root.glyph
    labelText: root.headline && !vertical ? Model.formatPercent(root.headlinePercent) : ""
    slotSize: Style.bar.iconSlot * (labelText !== "" ? 2 : 1)
    active: root.headlinePercent !== null && root.headlinePercent >= 90
    tooltipText: root.tooltip
    onPressed: function(b) { root.toggle() }
  }

  // ---------- Popout ----------
  KeyboardPanel {
    id: panel
    anchorItem: button
    owner: root
    bar: root.bar
    open: root.opened
    focusTarget: panel.loadedContent
    // Usage and Agents side by side when the screen has room for both columns;
    // otherwise one column with the Usage | Agents switch.
    readonly property real columnWidth: Style.space(520)
    readonly property real columnGap: Style.space(28)
    readonly property real splitWidth: 2 * columnWidth + columnGap
    readonly property bool split: fittedContentWidth(splitWidth) >= splitWidth
    contentWidth: split ? splitWidth : panel.fittedContentWidth(columnWidth)
    contentHeight: panel.fittedContentHeight(panel.loadedContentHeight)

    content: PanelKeyCatcher {
      id: catcher
      anchors.fill: parent
      implicitHeight: column.implicitHeight
      // With the menu or the confirmation up, keys go to them first.
      onMoveRequested: function(dx, dy) {
        if (root.pendingAction) { if (dx !== 0) confirm.selectedIndex = confirm.selectedIndex === 0 ? 1 : 0; return }
        if (dx !== 0 && root.view === "usage") root.setSetting("range", Model.shiftRange(root.range, dx))
      }
      onActivateRequested: {
        if (!root.pendingAction) return
        if (confirm.selectedIndex === 1) root.runPending()
        else root.pendingAction = null
      }
      onCloseRequested: {
        if (root.pendingAction) root.pendingAction = null
        else if (root.menuAgent) root.menuAgent = null
        else root.close()
      }
      onTabRequested: function(direction) { root.switchPanel(direction) }
      // An agent's key (shown on its row) opens it, as a click would.
      onTextKey: function(text, modifiers) {
        if (root.pendingAction || root.menuAgent) return
        if (modifiers & (Qt.ControlModifier | Qt.AltModifier | Qt.MetaModifier)) return
        if (!panel.split && root.view !== "agents") return
        var a = Model.agentForKey(root.agentView, text)
        if (a) root.openAgent(a)
      }

      Column {
        id: column
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.top: parent.top
        spacing: Style.space(14)

        // ---------- Hero ----------
        Item {
          width: parent.width
          implicitHeight: Math.max(heroIcon.implicitHeight, heroLabels.implicitHeight, heroPercent.implicitHeight)

          Text {
            id: heroIcon
            textFormat: Text.PlainText
            text: root.glyph
            color: root.claudeColor
            font.family: root.fontFamily
            font.pixelSize: Style.font.display
            anchors.left: parent.left
            anchors.verticalCenter: parent.verticalCenter
          }

          Column {
            id: heroLabels
            anchors.left: heroIcon.right
            anchors.leftMargin: Style.space(14)
            anchors.right: heroChips.visible ? heroChips.left : heroPercent.left
            anchors.rightMargin: Style.space(10)
            anchors.verticalCenter: parent.verticalCenter
            spacing: Style.space(2)

            Text {
              text: root.tr("title")
              textFormat: Text.PlainText
              color: root.fg
              font.family: root.fontFamily
              font.pixelSize: Style.font.title
              font.bold: true
              elide: Text.ElideRight
              width: parent.width
            }

            Text {
              textFormat: Text.PlainText
              text: root.statusText.toUpperCase()
              color: root.faint
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
              font.bold: true
              font.letterSpacing: 1.2
              elide: Text.ElideRight
              width: parent.width
            }
          }

          // Side by side, the profile chips ride in the title row instead of
          // taking a row of their own.
          ProfileChips {
            id: heroChips
            visible: panel.split && root.knownProfiles.length > 1
            anchors.right: heroPercent.left
            anchors.rightMargin: Style.space(24)
            anchors.verticalCenter: parent.verticalCenter
          }

          Text {
            id: heroPercent
            textFormat: Text.PlainText
            text: Model.formatPercent(root.headlinePercent)
            color: root.headlinePercent !== null && root.headlinePercent >= 75 ? root.limitColor(root.headlinePercent) : root.fg
            font.family: root.fontFamily
            font.pixelSize: Style.font.displayLarge
            font.bold: true
            anchors.right: parent.right
            anchors.verticalCenter: parent.verticalCenter
          }
        }

        // ---------- View (one-column layout only) ----------
        ButtonGroup {
          visible: !panel.split
          width: parent.width
          spacing: Style.space(6)
          focusable: false
          options: [
            { value: "usage", label: root.tr("view.usage") },
            { value: "agents", label: root.activeAgents > 0 ? strings.t("view.agentsCount").arg(root.activeAgents) : root.tr("view.agents") }
          ]
          value: root.view
          foreground: root.fg
          fontFamily: root.fontFamily
          fontSize: Style.font.bodySmall
          onChanged: function(v) { root.setSetting("view", v); if (v === "agents") root.refreshAgents() }
        }

        // ---------- Profiles (own row in the one-column layout) ----------
        ProfileChips {
          visible: !panel.split && root.knownProfiles.length > 1
          width: parent.width
        }

        // ---------- Usage | Agents ----------
        Row {
          id: views
          width: parent.width
          spacing: panel.split ? panel.columnGap : 0
          readonly property real paneWidth: panel.split ? (width - spacing) / 2 : width

        // ---------- Usage view ----------
        Column {
          width: views.paneWidth
          spacing: Style.space(14)
          visible: panel.split || root.view === "usage"

        // ---------- Limits ----------
        Column {
          width: parent.width
          spacing: Style.space(10)

          PanelSectionHeader {
            text: root.tr("limits.header")
            foreground: root.fg
            fontFamily: root.fontFamily
          }

          Text {
            visible: root.shownAccounts.length === 0 && !root.loading
            width: parent.width
            textFormat: Text.PlainText
            text: root.tr("limits.none")
            color: root.faint
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
            wrapMode: Text.WordWrap
          }

          Repeater {
            model: root.shownAccounts

            Column {
              id: acct
              required property var modelData
              width: parent.width
              spacing: Style.space(8)

              Item {
                width: parent.width
                implicitHeight: acctName.implicitHeight

                Text {
                  id: acctName
                  anchors.left: parent.left
                  anchors.right: acctProfiles.left
                  anchors.rightMargin: Style.space(8)
                  textFormat: Text.PlainText
                  text: [acct.modelData.org || acct.modelData.email, acct.modelData.plan].filter(function(s) { return !!s }).join(" · ")
                    || root.tr("limits.signedOut")
                  color: root.fg
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.bodySmall
                  font.bold: true
                  elide: Text.ElideRight
                }
                Text {
                  id: acctProfiles
                  anchors.right: parent.right
                  textFormat: Text.PlainText
                  text: (acct.modelData.profiles || []).join(" · ")
                  color: root.faint
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.caption
                }
              }

              Repeater {
                model: acct.modelData.limits || []
                LimitBar { required property var modelData; limit: modelData }
              }

              Text {
                readonly property var extra: acct.modelData.extra
                visible: !!extra
                textFormat: Text.PlainText
                text: extra ? strings.t("limits.extra").arg((extra.used || 0).toFixed(2) + " " + extra.currency)
                  .arg(extra.limit === null ? "∞" : extra.limit.toFixed(2) + " " + extra.currency) : ""
                color: root.faint
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
              }

              Text {
                readonly property string note: root.freshnessText(acct.modelData)
                visible: note !== ""
                textFormat: Text.PlainText
                text: note
                color: root.faint
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
              }
            }
          }
        }

        // ---------- Activity heatmap ----------
        PanelSeparator { foreground: root.fg }

        Column {
          width: parent.width
          spacing: Style.space(8)

          Item {
            width: parent.width
            implicitHeight: Math.max(activityHeader.implicitHeight, metricGroup.implicitHeight)
            PanelSectionHeader {
              id: activityHeader
              anchors.left: parent.left
              anchors.verticalCenter: parent.verticalCenter
              text: root.tr("activity.header")
              foreground: root.fg
              fontFamily: root.fontFamily
            }
            ButtonGroup {
              id: metricGroup
              anchors.right: parent.right
              anchors.verticalCenter: parent.verticalCenter
              spacing: Style.space(4)
              focusable: false
              options: Model.METRICS.map(function(m) { return { value: m, label: root.tr("metric." + m) } })
              value: root.metric
              foreground: root.fg
              fontFamily: root.fontFamily
              fontSize: Style.font.caption
              onChanged: function(v) { root.setSetting("metric", v) }
            }
          }

          Item {
            id: grid
            width: parent.width
            readonly property int weeks: 53
            readonly property real gutter: Style.space(30)
            readonly property real pitch: (width - gutter) / weeks
            readonly property real gap: Math.max(1, Math.round(pitch * 0.18))
            readonly property real monthsH: Style.font.caption + Style.space(4)
            implicitHeight: monthsH + 7 * pitch

            Repeater {
              model: root.heat.months
              Text {
                required property var modelData
                required property int index
                // Drop a label that would crowd the one before it.
                visible: index === 0 ? root.heat.months.length < 2 || root.heat.months[1].col - modelData.col >= 3 : true
                x: grid.gutter + modelData.col * grid.pitch
                textFormat: Text.PlainText
                text: root.locale().standaloneMonthName(modelData.month, Locale.ShortFormat)
                color: root.faint
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
              }
            }

            Repeater {
              model: [0, 2, 4]
              Text {
                required property var modelData
                y: grid.monthsH + modelData * grid.pitch + (grid.pitch - height) / 2
                width: grid.gutter - Style.space(6)
                horizontalAlignment: Text.AlignRight
                textFormat: Text.PlainText
                text: root.locale().dayName((modelData + 1) % 7, Locale.ShortFormat)
                color: root.faint
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
              }
            }

            Canvas {
              id: cells
              x: grid.gutter
              y: grid.monthsH
              width: grid.width - grid.gutter
              height: 7 * grid.pitch
              antialiasing: true

              onPaint: {
                var ctx = getContext("2d")
                ctx.reset()
                var p = grid.pitch, s = Math.max(1, p - grid.gap), r = Math.min(2, s / 4)
                var list = root.heat.cells
                for (var i = 0; i < list.length; i++) {
                  var c = list[i]
                  if (c.future) continue
                  ctx.fillStyle = root.levelColor(c.level)
                  ctx.beginPath()
                  ctx.roundedRect(c.col * p, c.row * p, s, s, r, r)
                  ctx.fill()
                }
              }

              Connections {
                target: root
                function onHeatChanged() { cells.requestPaint() }
                function onFgChanged() { cells.requestPaint() }
              }
              onWidthChanged: requestPaint()
            }

            Rectangle {
              readonly property var cell: {
                if (root.hoverDay === "") return null
                var list = root.heat.cells
                for (var i = list.length - 1; i >= 0; i--) if (list[i].day === root.hoverDay) return list[i]
                return null
              }
              visible: cell !== null
              x: grid.gutter + (cell ? cell.col * grid.pitch : 0) - 1
              y: grid.monthsH + (cell ? cell.row * grid.pitch : 0) - 1
              width: grid.pitch - grid.gap + 2
              height: width
              radius: 3
              color: "transparent"
              border.width: 1
              border.color: root.fg
            }

            MouseArea {
              x: grid.gutter
              y: grid.monthsH
              width: cells.width
              height: cells.height
              hoverEnabled: true
              function track(mx, my) {
                var col = Math.floor(mx / grid.pitch), row = Math.floor(my / grid.pitch)
                var c = root.heat.cells[col * 7 + row]
                root.hoverDay = c && !c.future ? c.day : ""
              }
              onPositionChanged: function(m) { track(m.x, m.y) }
              onExited: root.hoverDay = ""
            }
          }

          Item {
            width: parent.width
            implicitHeight: Math.max(readout.implicitHeight, legend.implicitHeight)

            Text {
              id: readout
              anchors.left: parent.left
              anchors.right: legend.left
              anchors.rightMargin: Style.space(10)
              anchors.verticalCenter: parent.verticalCenter
              textFormat: Text.PlainText
              text: root.hoverDay !== "" ? root.dayText(root.hoverDay)
                : strings.t("activity.year").arg(Model.formatTokens(root.yearTotal.tokens) + " · " + Model.formatCost(root.yearTotal.cost)
                    + " · " + Model.formatCount(root.yearTotal.messages) + " msgs")
              color: root.hoverDay !== "" ? root.fg : root.faint
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
              elide: Text.ElideRight
            }

            Row {
              id: legend
              anchors.right: parent.right
              anchors.verticalCenter: parent.verticalCenter
              spacing: Style.space(3)
              Text {
                textFormat: Text.PlainText
                text: root.tr("activity.less")
                color: root.faint
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
                anchors.verticalCenter: parent.verticalCenter
              }
              Repeater {
                model: [0, 1, 2, 3, 4]
                Rectangle {
                  required property var modelData
                  width: Math.max(6, grid.pitch - grid.gap)
                  height: width
                  radius: 2
                  color: root.levelColor(modelData)
                  anchors.verticalCenter: parent.verticalCenter
                }
              }
              Text {
                textFormat: Text.PlainText
                text: root.tr("activity.more")
                color: root.faint
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
                anchors.verticalCenter: parent.verticalCenter
              }
            }
          }

          Text {
            textFormat: Text.PlainText
            text: strings.t("activity.streak").arg(strings.t("days").arg(root.streak.current))
              .arg(strings.t("days").arg(root.streak.longest)).arg(root.streak.activeDays)
            color: root.faint
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
          }
        }

        // ---------- Totals ----------
        PanelSeparator { foreground: root.fg }

        Column {
          width: parent.width
          spacing: Style.space(10)

          PanelSectionHeader {
            text: root.tr("stats.header")
            foreground: root.fg
            fontFamily: root.fontFamily
          }

          ButtonGroup {
            width: parent.width
            spacing: Style.space(6)
            focusable: false
            options: Model.RANGES.map(function(r) { return { value: r.value, label: root.tr("range." + r.value) } })
            value: root.range
            foreground: root.fg
            fontFamily: root.fontFamily
            fontSize: Style.font.bodySmall
            onChanged: function(v) { root.setSetting("range", v) }
          }

          Row {
            width: parent.width
            spacing: Style.space(20)

            Column {
              width: (parent.width - parent.spacing) / 2
              spacing: Style.spacing.labelGap
              InfoPair { label: root.tr("stats.tokens"); value: Model.formatTokens(root.summary.tokens); strong: true }
              InfoPair { label: root.tr("stats.input"); value: Model.formatTokens(root.summary.input) }
              InfoPair { label: root.tr("stats.output"); value: Model.formatTokens(root.summary.output) }
              InfoPair { label: root.tr("stats.cacheWrite"); value: Model.formatTokens(root.summary.cacheWrite) }
              InfoPair { label: root.tr("stats.cacheRead"); value: Model.formatTokens(root.summary.cacheRead) }
            }

            Column {
              width: (parent.width - parent.spacing) / 2
              spacing: Style.spacing.labelGap
              InfoPair { label: root.tr("stats.cost"); value: Model.formatCost(root.summary.cost); strong: true }
              InfoPair { label: root.tr("stats.messages"); value: Model.formatCount(root.summary.messages) }
              InfoPair { label: root.tr("stats.sessions"); value: Model.formatCount(root.summary.sessions) }
              InfoPair {
                label: root.tr("stats.cacheHit")
                value: root.summary.cacheHitRate === null ? "—" : Model.formatPercent(root.summary.cacheHitRate * 100)
              }
              InfoPair {
                label: root.tr("stats.busiest")
                value: root.summary.busiest ? root.fmtDay(root.summary.busiest.day, "ddd d MMM") : "—"
              }
            }
          }

          // By model
          Column {
            width: parent.width
            spacing: Style.space(6)
            visible: root.summary.byModel.length > 0
            Text {
              textFormat: Text.PlainText
              text: root.tr("models.header")
              color: root.faint
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
              font.bold: true
            }
            Repeater {
              model: root.summary.byModel
              ShareRow {
                required property var modelData
                label: modelData.label
                tokens: modelData.tokens
                cost: modelData.cost
                share: root.summary.cost > 0 ? modelData.cost / root.summary.cost : 0
              }
            }
          }

          // By profile
          Column {
            width: parent.width
            spacing: Style.space(6)
            visible: root.profile === "all" && root.summary.byProfile.length > 1
            Text {
              textFormat: Text.PlainText
              text: root.tr("profiles.header")
              color: root.faint
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
              font.bold: true
            }
            Repeater {
              model: root.summary.byProfile
              ShareRow {
                required property var modelData
                label: modelData.profile
                tokens: modelData.tokens
                cost: modelData.cost
                share: root.summary.cost > 0 ? modelData.cost / root.summary.cost : 0
              }
            }
          }

          Text {
            width: parent.width
            textFormat: Text.PlainText
            text: root.tr("footer.cost") + (root.usage && root.usage.unpriced.length
              ? "\n" + strings.t("footer.unpriced").arg(root.usage.unpriced.join(", ")) : "")
            color: root.faint
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
            wrapMode: Text.WordWrap
          }
        }
        }

        // ---------- Agents view ----------
        Column {
          width: views.paneWidth
          spacing: Style.space(12)
          visible: panel.split || root.view === "agents"

          // Side by side there is no switch to carry the count, so the column
          // gets its own header.
          Item {
            visible: panel.split
            width: parent.width
            implicitHeight: Math.max(agentsHeader.implicitHeight, agentsSummary.implicitHeight)
            PanelSectionHeader {
              id: agentsHeader
              anchors.left: parent.left
              anchors.verticalCenter: parent.verticalCenter
              text: root.tr("view.agents")
              foreground: root.fg
              fontFamily: root.fontFamily
            }
            Text {
              id: agentsSummary
              anchors.left: agentsHeader.right
              anchors.leftMargin: Style.space(12)
              anchors.right: parent.right
              anchors.verticalCenter: parent.verticalCenter
              horizontalAlignment: Text.AlignRight
              textFormat: Text.PlainText
              text: root.agentsData ? root.agentSummary(root.agentView.counts) : root.tr("agents.loading")
              color: root.faint
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
              elide: Text.ElideRight
            }
          }

          Text {
            visible: !!root.agentsData && root.agentView.groups.length === 0
            width: parent.width
            textFormat: Text.PlainText
            text: root.tr("agents.none")
            color: root.faint
            font.family: root.fontFamily
            font.pixelSize: Style.font.bodySmall
          }

          Repeater {
            model: root.agentView.groups

            Column {
              id: group
              required property var modelData
              width: parent.width
              spacing: Style.space(4)

              Row {
                spacing: Style.space(6)
                Text {
                  textFormat: Text.PlainText
                  text: root.tr("agents.status." + group.modelData.status).toUpperCase()
                  color: root.agentColors[group.modelData.status]
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.caption
                  font.bold: true
                  font.letterSpacing: 1.2
                }
                Text {
                  textFormat: Text.PlainText
                  text: String(group.modelData.items.length)
                  color: root.faint
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.caption
                  font.bold: true
                }
              }

              Repeater {
                model: group.modelData.items
                AgentRow { required property var modelData; agent: modelData }
              }
            }
          }

          Text {
            visible: root.agentView.hiddenDone > 0
            width: parent.width
            textFormat: Text.PlainText
            text: strings.t("agents.hiddenDone").arg(root.agentView.hiddenDone)
            color: root.faint
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
          }

          Text {
            readonly property var errors: root.agentsData ? root.agentsData.errors : []
            visible: root.agentNote !== "" || errors.length > 0
            width: parent.width
            textFormat: Text.PlainText
            text: [root.agentNote].concat(errors.map(function(e) { return strings.t("agents.error").arg(e) }))
              .filter(function(s) { return s !== "" }).join("\n")
            color: root.warnColor
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
            wrapMode: Text.WordWrap
          }

          Text {
            width: parent.width
            textFormat: Text.PlainText
            text: root.tr("agents.hint")
            color: root.faint
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
            wrapMode: Text.WordWrap
          }
        }
        }
      }

      // ---------- Right-click menu ----------
      // Any click outside the menu dismisses it.
      MouseArea {
        anchors.fill: parent
        z: 20
        visible: !!root.menuAgent
        acceptedButtons: Qt.LeftButton | Qt.RightButton
        onClicked: root.menuAgent = null
      }

      BorderSurface {
        id: agentMenu
        readonly property point at: catcher.mapFromItem(null, root.menuScenePoint.x, root.menuScenePoint.y)
        z: 21
        visible: !!root.menuAgent
        width: Math.max(Style.space(190), menuColumn.implicitWidth) + padding * 2
        height: menuColumn.implicitHeight + padding * 2
        x: Math.max(0, Math.min(at.x, catcher.width - width))
        y: Math.max(0, Math.min(at.y, catcher.height - height))
        radius: Style.cornerRadius
        color: Commons.Color.popups.background
        borderSpec: Border.surfaceSpec("popups", "border", Commons.Color.popups.border, Style.normalBorderWidth)
        padding: Style.spacing.sm

        Column {
          id: menuColumn
          x: agentMenu.contentLeftInset
          y: agentMenu.contentTopInset
          width: agentMenu.width - agentMenu.contentLeftInset - agentMenu.contentRightInset

          Repeater {
            model: root.menuItems(root.menuAgent)
            MenuRow {
              required property var modelData
              text: modelData.label
              danger: !!modelData.danger
              onTriggered: root.menuAction(modelData.action, root.menuAgent)
            }
          }
        }
      }

      ConfirmDialog {
        id: confirm
        anchors.fill: parent
        z: 30
        opened: !!root.pendingAction
        message: root.confirmText(root.pendingAction)
        confirmText: root.pendingAction ? root.tr("confirm." + root.pendingAction.kind + ".button") : ""
        background: Commons.Color.popups.background
        foreground: root.fg
        fontFamily: root.fontFamily
        onOpenedChanged: if (opened) selectedIndex = 0  // default to Cancel: these can't be undone
        onCanceled: root.pendingAction = null
        onConfirmed: root.runPending()
      }
    }
  }

  // ---------- Components ----------

  // All / one chip per profile; filters both views.
  component ProfileChips: ButtonGroup {
    spacing: Style.space(6)
    focusable: false
    options: [{ value: "all", label: root.tr("profile.all") }].concat(
      root.knownProfiles.map(function(p) { return { value: p.id, label: p.id } }))
    value: root.profile
    foreground: root.fg
    fontFamily: root.fontFamily
    fontSize: Style.font.bodySmall
    onChanged: function(v) { root.setSetting("profile", v) }
  }

  // One agent: status dot, name, age and profile; what it is doing; where it
  // runs; then any subagents still going. Click to jump to it.
  component AgentRow: Item {
    id: ar
    property var agent: null
    readonly property color tint: root.agentColors[agent ? agent.status : "idle"] || root.fg
    readonly property string line: !agent ? "" : (agent.detail || agent.prompt || "")
    readonly property string meta: {
      if (!agent) return ""
      var parts = [root.tr("agents.kind." + agent.kind)]
      var dir = String(agent.cwd || "").replace(/\/+$/, "")
      if (dir) parts.push(dir.split("/").pop() || dir)
      if (agent.branch) parts.push(agent.branch)
      if (agent.tokens) parts.push(strings.t("agents.tokens").arg(Model.formatTokens(agent.tokens)))
      if (agent.links && agent.links.length)
        parts.push(agent.links.map(function(l) { return (l.kind === "pr" ? "PR #" : "#") + l.id }).join(" "))
      if (agent.status === "working" && agent.rawState && agent.kind !== "interactive" && agent.rawState !== "working")
        parts.push(agent.rawState)
      return parts.join(" · ")
    }

    width: parent ? parent.width : 0
    implicitHeight: arColumn.implicitHeight + Style.space(12)

    Rectangle {
      anchors.fill: parent
      radius: Style.space(6)
      color: Util.alpha(root.fg, arMouse.containsMouse ? 0.08 : 0.03)
    }

    MouseArea {
      id: arMouse
      anchors.fill: parent
      hoverEnabled: true
      cursorShape: Qt.PointingHandCursor
      acceptedButtons: Qt.LeftButton | Qt.RightButton
      onClicked: function(mouse) {
        if (mouse.button === Qt.RightButton) root.openMenu(ar.agent, arMouse.mapToItem(null, mouse.x, mouse.y))
        else root.openAgent(ar.agent)
      }
    }

    Column {
      id: arColumn
      x: Style.space(10)
      y: Style.space(6)
      width: parent.width - 2 * Style.space(10)
      spacing: Style.space(2)

      Item {
        width: parent.width
        implicitHeight: arName.implicitHeight

        // The key that opens this agent while the panel has focus.
        Rectangle {
          id: arKey
          width: Math.max(16, Style.space(18))
          height: width
          radius: Style.space(4)
          color: "transparent"
          border.width: 1
          border.color: Util.alpha(root.fg, 0.25)
          opacity: ar.agent && ar.agent.hotkey ? 1 : 0  // keeps its room so rows stay aligned
          anchors.verticalCenter: parent.verticalCenter
          Text {
            anchors.centerIn: parent
            textFormat: Text.PlainText
            text: ar.agent ? ar.agent.hotkey : ""
            color: root.faint
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
            font.bold: true
          }
        }
        Rectangle {
          id: arDot
          width: Math.max(7, Style.space(8))
          height: width
          radius: width / 2
          color: ar.tint
          anchors.left: arKey.right
          anchors.leftMargin: Style.space(8)
          anchors.verticalCenter: parent.verticalCenter
          SequentialAnimation on opacity {
            running: !!ar.agent && ar.agent.status === "working" && root.opened
            loops: Animation.Infinite
            alwaysRunToEnd: true
            NumberAnimation { from: 1; to: 0.35; duration: 700; easing.type: Easing.InOutSine }
            NumberAnimation { from: 0.35; to: 1; duration: 700; easing.type: Easing.InOutSine }
          }
        }
        Text {
          id: arName
          anchors.left: arDot.right
          anchors.leftMargin: Style.space(8)
          anchors.right: arRight.left
          anchors.rightMargin: Style.space(8)
          textFormat: Text.PlainText
          text: ar.agent ? ar.agent.name : ""
          color: root.fg
          font.family: root.fontFamily
          font.pixelSize: Style.font.bodySmall
          font.bold: true
          elide: Text.ElideRight
        }
        Text {
          id: arRight
          anchors.right: parent.right
          anchors.baseline: arName.baseline
          textFormat: Text.PlainText
          text: {
            if (!ar.agent) return ""
            var t = ar.agent.updatedAt || ar.agent.startedAt
            var parts = []
            if (t) parts.push(Model.formatAge(root.nowSecs - t))
            parts.push(ar.agent.profile)
            return parts.join(" · ")
          }
          color: root.faint
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption
        }
      }

      Text {
        visible: ar.line !== ""
        x: arDot.x + arDot.width + Style.space(8)
        width: parent.width - x
        textFormat: Text.PlainText
        text: ar.line
        color: ar.agent && ar.agent.status === "needs" ? ar.tint : root.fg
        opacity: ar.agent && ar.agent.status === "needs" ? 1 : 0.75
        font.family: root.fontFamily
        font.pixelSize: Style.font.caption
        wrapMode: Text.WordWrap
        maximumLineCount: 2
        elide: Text.ElideRight
      }

      Text {
        x: arDot.x + arDot.width + Style.space(8)
        width: parent.width - x
        textFormat: Text.PlainText
        text: ar.meta
        color: root.faint
        font.family: root.fontFamily
        font.pixelSize: Style.font.caption
        elide: Text.ElideRight
      }

      Repeater {
        model: ar.agent ? ar.agent.subagents : []
        Text {
          required property var modelData
          x: arDot.x + arDot.width + Style.space(8)
          width: arColumn.width - x
          textFormat: Text.PlainText
          text: "↳ " + [modelData.description || root.tr("agents.subagent"), modelData.type,
                        Model.formatAge(root.nowSecs - modelData.since)].filter(function(s) { return !!s }).join(" · ")
          color: root.agentColors.working
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption
          elide: Text.ElideRight
        }
      }
    }
  }

  // Label, percentage and reset on one line; a thin bar underneath.
  component LimitBar: Column {
    id: lb
    property var limit: null
    readonly property var pct: Model.livePercent(limit, root.nowSecs)
    width: parent ? parent.width : 0
    spacing: Style.space(4)

    Item {
      width: parent.width
      implicitHeight: lbLabel.implicitHeight
      Text {
        id: lbLabel
        anchors.left: parent.left
        textFormat: Text.PlainText
        text: lb.limit ? lb.limit.label : ""
        color: root.fg
        opacity: 0.75
        font.family: root.fontFamily
        font.pixelSize: Style.font.bodySmall
      }
      Text {
        anchors.left: lbLabel.right
        anchors.leftMargin: Style.space(8)
        anchors.baseline: lbLabel.baseline
        textFormat: Text.PlainText
        text: Model.formatPercent(lb.pct)
        color: lb.pct !== null && lb.pct >= 75 ? root.limitColor(lb.pct) : root.fg
        font.family: root.fontFamily
        font.pixelSize: Style.font.bodySmall
        font.bold: true
      }
      Text {
        anchors.right: parent.right
        anchors.baseline: lbLabel.baseline
        textFormat: Text.PlainText
        text: root.resetText(lb.limit)
        color: root.faint
        font.family: root.fontFamily
        font.pixelSize: Style.font.caption
      }
    }

    Rectangle {
      width: parent.width
      height: Math.max(4, Style.space(6))
      radius: height / 2
      color: Util.alpha(root.fg, 0.08)
      Rectangle {
        width: Math.max(lb.pct > 0 ? height : 0, parent.width * Math.min(100, lb.pct || 0) / 100)
        height: parent.height
        radius: height / 2
        color: root.limitColor(lb.pct || 0)
      }
    }
  }

  // A row of the right-click menu, like the dock's.
  component MenuRow: Rectangle {
    id: mr
    property string text: ""
    property bool danger: false
    signal triggered()

    width: parent ? parent.width : 0
    height: Style.spacing.popupRowHeight
    radius: Style.cornerRadius
    color: mrMouse.containsMouse ? Style.hoverFill : "transparent"

    Text {
      anchors.verticalCenter: parent.verticalCenter
      anchors.left: parent.left
      anchors.leftMargin: Style.spacing.controlPaddingX
      textFormat: Text.PlainText
      text: mr.text
      color: mr.danger ? root.urgentColor : Commons.Color.popups.text
      font.family: root.fontFamily
      font.pixelSize: Style.font.bodySmall
    }

    MouseArea {
      id: mrMouse
      anchors.fill: parent
      hoverEnabled: true
      cursorShape: Qt.PointingHandCursor
      onClicked: mr.triggered()
    }
  }

  // Name · share bar · tokens · cost
  component ShareRow: Item {
    id: sr
    property string label: ""
    property real tokens: 0
    property real cost: 0
    property real share: 0
    width: parent ? parent.width : 0
    implicitHeight: srLabel.implicitHeight

    Text {
      id: srLabel
      width: Style.space(96)
      textFormat: Text.PlainText
      text: sr.label
      color: root.fg
      opacity: 0.75
      font.family: root.fontFamily
      font.pixelSize: Style.font.bodySmall
      elide: Text.ElideRight
    }
    Rectangle {
      id: srTrack
      x: srLabel.width + Style.space(8)
      anchors.verticalCenter: parent.verticalCenter
      width: parent.width - x - Style.space(150)
      height: Math.max(4, Style.space(5))
      radius: height / 2
      color: Util.alpha(root.fg, 0.08)
      Rectangle {
        width: Math.max(sr.share > 0 ? height : 0, parent.width * sr.share)
        height: parent.height
        radius: height / 2
        color: Util.alpha(root.claudeColor, 0.85)
      }
    }
    Text {
      anchors.right: srCost.left
      anchors.rightMargin: Style.space(12)
      textFormat: Text.PlainText
      text: Model.formatTokens(sr.tokens)
      color: root.faint
      font.family: root.fontFamily
      font.pixelSize: Style.font.bodySmall
    }
    Text {
      id: srCost
      anchors.right: parent.right
      width: Style.space(60)
      horizontalAlignment: Text.AlignRight
      textFormat: Text.PlainText
      text: Model.formatCost(sr.cost)
      color: root.fg
      font.family: root.fontFamily
      font.pixelSize: Style.font.bodySmall
    }
  }

  component InfoPair: Row {
    property string label: ""
    property string value: ""
    property bool strong: false

    width: parent.width
    spacing: Style.space(8)

    InfoLabel { text: label }
    Item { width: Math.max(0, parent.width - parent.children[0].implicitWidth - parent.children[2].implicitWidth - parent.spacing * 2); height: 1 }
    InfoValue { text: value; font.bold: strong }
  }

  component InfoLabel: Text {
    textFormat: Text.PlainText
    color: root.fg
    opacity: 0.6
    font.family: root.fontFamily
    font.pixelSize: Style.font.bodySmall
  }

  component InfoValue: Text {
    textFormat: Text.PlainText
    color: root.fg
    font.family: root.fontFamily
    font.pixelSize: Style.font.bodySmall
  }
}
