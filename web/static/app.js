(function () {
  "use strict";

  const REFRESH_MS = 60_000;
  const THEME_KEY = "container-agent-theme";
  const ACTIVITY_PAGE = 50;

  let state = {
    status: null,
    pending: [],
    incidents: [],
    snoozes: [],
    approvals: [],
  };

  let activityState = {
    items: [],
    offset: 0,
    hasMore: true,
    loading: false,
    error: null,
  };
  let activityBootstrapped = false;

  function normalizeActivityResponse(data) {
    if (Array.isArray(data)) {
      return { items: data, has_more: false };
    }
    if (data && typeof data === "object") {
      return {
        items: Array.isArray(data.items) ? data.items : [],
        has_more: Boolean(data.has_more),
      };
    }
    return { items: [], has_more: false };
  }

  function applyActivityData(data, { append = false, resetScroll = false } = {}) {
    const panel = $("activity-panel");
    const normalized = normalizeActivityResponse(data);
    if (append) {
      activityState.items = activityState.items.concat(normalized.items);
      activityState.offset += normalized.items.length;
    } else {
      activityState.items = normalized.items;
      activityState.offset = normalized.items.length;
      if (resetScroll && panel) panel.scrollTop = 0;
    }
    activityState.hasMore = normalized.has_more;
    activityState.error = null;
  }

  function $(id) {
    return document.getElementById(id);
  }

  function esc(s) {
    if (s == null) return "";
    const d = document.createElement("div");
    d.textContent = String(s);
    return d.innerHTML;
  }

  function parseTs(ts) {
    if (!ts) return null;
    const d = new Date(ts.replace("Z", "+00:00"));
    return Number.isNaN(d.getTime()) ? null : d;
  }

  function fmtUtc(ts) {
    const d = parseTs(ts);
    if (!d) return esc(ts || "");
    return d.toISOString().slice(0, 16).replace("T", " ") + " UTC";
  }

  function fmtRelative(ts) {
    const d = parseTs(ts);
    if (!d) return "";
    const sec = Math.floor((Date.now() - d.getTime()) / 1000);
    if (sec < 60) return "just now";
    if (sec < 3600) return Math.floor(sec / 60) + "m ago";
    if (sec < 86400) return Math.floor(sec / 3600) + "h ago";
    return Math.floor(sec / 86400) + "d ago";
  }

  function tsHtml(ts) {
    const rel = fmtRelative(ts);
    return `<span data-ts="${esc(ts)}">${fmtUtc(ts)}</span>${rel ? `<span class="rel">· ${rel}</span>` : ""}`;
  }

  function applyTheme(theme) {
    document.documentElement.dataset.theme = theme;
    localStorage.setItem(THEME_KEY, theme);
    const btn = $("theme-toggle");
    if (btn) btn.textContent = theme === "light" ? "☀" : "◐";
  }

  function initTheme() {
    const saved = localStorage.getItem(THEME_KEY);
    const theme = saved || "dark";
    applyTheme(theme);
    const btn = $("theme-toggle");
    if (btn) {
      btn.addEventListener("click", () => {
        const next = document.documentElement.dataset.theme === "light" ? "dark" : "light";
        applyTheme(next);
      });
    }
  }

  async function fetchJson(url, options) {
    const r = await fetch(url, { credentials: "same-origin", ...options });
    if (r.status === 401) {
      window.location.href = "/login";
      throw new Error("unauthenticated");
    }
    if (!r.ok) throw new Error(url + " " + r.status);
    return r.json();
  }

  async function logout() {
    await fetch("/api/logout", { method: "POST", credentials: "same-origin" });
    window.location.href = "/login";
  }

  async function loadActivity(append) {
    if (activityState.loading) return;
    if (append && !activityState.hasMore) return;

    const panel = $("activity-panel");
    const prevHeight = panel ? panel.scrollHeight : 0;
    const prevTop = panel ? panel.scrollTop : 0;

    activityState.loading = true;
    renderActivityStatus();

    const offset = append ? activityState.offset : 0;
    try {
      const data = await fetchJson(`/api/activity?limit=${ACTIVITY_PAGE}&offset=${offset}`);
      applyActivityData(data, { append });
    } catch (err) {
      activityState.error = err && err.message ? err.message : "Failed to load activity";
    } finally {
      activityState.loading = false;
      renderActivity();
      renderActivityStatus();
      if (append && panel) {
        panel.scrollTop = prevTop + (panel.scrollHeight - prevHeight);
      }
    }
  }

  async function loadDashboard() {
    const panel = $("activity-panel");
    const atTop = !activityBootstrapped || !panel || panel.scrollTop < 40;
    const requests = [
      fetchJson("/api/status"),
      fetchJson("/api/pending"),
      fetchJson("/api/incidents"),
      fetchJson("/api/snoozes"),
      fetchJson("/api/approvals/history"),
    ];
    if (atTop) {
      requests.push(fetchJson(`/api/activity?limit=${ACTIVITY_PAGE}&offset=0`));
    }
    const results = await Promise.all(requests);
    state = {
      status: results[0],
      pending: results[1],
      incidents: results[2],
      snoozes: results[3],
      approvals: results[4],
    };
    if (atTop) {
      applyActivityData(results[5], { resetScroll: !activityBootstrapped });
      activityBootstrapped = true;
    }
    render();
    const el = $("last-refresh");
    if (el) el.textContent = "Updated " + new Date().toLocaleTimeString();
    const ind = $("refresh-indicator");
    if (ind) {
      ind.textContent = "Live";
      ind.classList.remove("stale");
    }
  }

  function renderBanner() {
    const el = $("banner");
    if (!el || !state.status) return;
    const s = state.status;
    const hb = s.heartbeat || {};
    el.classList.add("hidden");
    el.classList.remove("warn", "error");

    if (!s.data_dir_readable) {
      el.textContent = "UI cannot read /data — check Podman volume mount and SELinux (:z label). Run: container-agent sync-ui";
      el.classList.remove("hidden");
      el.classList.add("error");
      return;
    }
    if (s.activity_count === 0 && !hb.ts) {
      el.textContent = "No scan data in the UI volume yet. On the host run: container-agent run — then container-agent sync-ui and recreate the UI container.";
      el.classList.remove("hidden");
      el.classList.add("warn");
      return;
    }
    if (!s.activity_readable && s.activity_count === 0 && hb.ts) {
      el.textContent = "Scan ran on the host but the UI cannot read activity data (SELinux/volume). Run: container-agent sync-ui && podman compose -f compose/docker-compose.yml up -d --force-recreate";
      el.classList.remove("hidden");
      el.classList.add("error");
    }
  }

  function renderStats() {
    const hb = state.status?.heartbeat || {};
    const el = $("stats");
    if (!el) return;
    const last = hb.ts ? fmtUtc(hb.ts) : "never";
    el.innerHTML = `
      <div class="stat pending"><div class="value">${esc(hb.pending ?? state.status?.pending_count ?? 0)}</div><div class="label">Pending</div></div>
      <div class="stat unresolved"><div class="value">${esc(hb.unresolved ?? 0)}</div><div class="label">Unresolved</div></div>
      <div class="stat resolved"><div class="value">${esc(hb.resolved ?? 0)}</div><div class="label">Resolved</div></div>
      <div class="stat scan"><div class="value">${esc(last)}</div><div class="label">Last scan · ${esc(hb.projects ?? "—")} projects</div></div>`;
    const hint = $("health-hint");
    if (hint && hb.ts) hint.textContent = fmtRelative(hb.ts) ? "Last scan " + fmtRelative(hb.ts) : "Last scan";
  }

  function renderHealth() {
    const el = $("health-strip");
    if (!el) return;
    const services = state.status?.services || [];
    if (!services.length) {
      el.innerHTML = `<span class="empty" style="padding:0.6rem 1rem;border:none;background:transparent">No service data yet — run a scan.</span>`;
      return;
    }
    el.innerHTML = services
      .map((s) => {
        const label = esc(s.display_name || s.service || s.project);
        const snoozed = s.snoozed ? " snoozed" : "";
        const title = (s.issues || []).join("; ") || s.status;
        const detail = s.display_name ? `${esc(s.project)}/${esc(s.service)}` : "";
        return `<div class="health-chip${snoozed}" title="${esc(title)}"
            data-project="${esc(s.project)}" data-service="${esc(s.service)}">
          <span class="health-dot ${esc(s.status)}"></span>
          <span>${label}</span>
          ${detail ? `<span class="pill">${detail}</span>` : ""}
          ${s.snoozed ? '<span class="pill">snoozed</span>' : ""}
        </div>`;
      })
      .join("");
    el.querySelectorAll(".health-chip").forEach((chip) => {
      chip.addEventListener("click", () => {
        openServiceModal(chip.dataset.project, chip.dataset.service);
      });
    });
  }

  function renderSnoozes() {
    const section = $("snoozes-section");
    const el = $("snoozes");
    if (!el) return;
    const rows = state.snoozes || [];
    if (!rows.length) {
      if (section) section.classList.add("hidden");
      return;
    }
    if (section) section.classList.remove("hidden");
    el.innerHTML = rows
      .map((s) => {
        const rem = s.remaining_seconds || 0;
        const hrs = Math.max(1, Math.round(rem / 3600));
        return `<li class="card">
          <div class="card-head"><span class="title">${esc(s.project)}/${esc(s.service)}</span></div>
          <div class="detail">${esc(s.reason || "Snoozed")} · ${hrs}h remaining</div>
          <button class="btn-secondary btn-sm unsnooze-btn" data-project="${esc(s.project)}" data-service="${esc(s.service)}">Unsnooze</button>
        </li>`;
      })
      .join("");
    el.querySelectorAll(".unsnooze-btn").forEach((btn) => {
      btn.addEventListener("click", async () => {
        const p = btn.dataset.project;
        const s = btn.dataset.service;
        await fetch("/api/snooze/" + encodeURIComponent(p) + "/" + encodeURIComponent(s), { method: "DELETE" });
        await loadDashboard();
      });
    });
  }

  function displayName(project, service) {
    const services = state.status?.services || [];
    const match = services.find((s) => s.project === project && s.service === service);
    return match?.display_name || `${project}/${service}`;
  }

  function renderTopIssues() {
    const el = $("top-issues");
    if (!el) return;
    const rows = state.status?.top_issues || [];
    if (!rows.length) {
      el.innerHTML = `<li class="empty">No recurring issues in recent incidents.</li>`;
      return;
    }
    el.innerHTML = rows
      .map(
        (row, idx) => `<li class="card">
        <div class="card-head">
          <span class="top-issue-rank">#${idx + 1}</span>
          <span class="title">${esc(row.display_name || displayName(row.project, row.service))}</span>
          <span class="badge badge-${esc(row.latest_outcome || "open")}">${esc(row.count)}×</span>
        </div>
        <div class="issue">${esc((row.latest_issue || "").slice(0, 180))}</div>
        <div class="detail">${esc(row.project)}/${esc(row.service)} · last ${fmtRelative(row.latest_ts)}</div>
      </li>`
      )
      .join("");
  }

  function renderPending() {
    const el = $("pending");
    if (!el) return;
    const rows = state.pending || [];
    if (!rows.length) {
      el.innerHTML = `<li class="empty">No pending approvals</li>`;
      return;
    }
    el.innerHTML = rows
      .map(
        (p) => `<li class="card pending-card">
        <div class="meta">${tsHtml(p.ts)}</div>
        <div class="card-head"><span class="title">${esc(p.project)}/${esc(p.service)}</span>
          <span class="badge badge-pending_approval">approval required</span></div>
        <div class="issue">${esc(p.issue)}</div>
        <div class="detail">Action · ${esc(p.action)}</div>
        <div class="detail">Paths · ${esc((p.paths || []).join(", "))}</div>
        <div class="btn-row">
          <a class="btn" href="/approve/${esc(p.incident_id)}">Approve fix</a>
          <button class="btn-secondary btn-sm cancel-pending-btn" data-id="${esc(p.incident_id)}">Cancel</button>
        </div>
      </li>`
      )
      .join("");
    el.querySelectorAll(".cancel-pending-btn").forEach((btn) => {
      btn.addEventListener("click", () => cancelPending(btn.dataset.id));
    });
  }

  function filterText(items, query, fields) {
    const q = (query || "").trim().toLowerCase();
    if (!q) return items;
    return items.filter((item) =>
      fields.some((f) => String(item[f] || "").toLowerCase().includes(q))
    );
  }

  function activityServiceLabel(a) {
    if (!a.project) return "";
    return a.service ? `${a.project}/${a.service}` : a.project;
  }

  function renderActivityStatus() {
    const el = $("activity-status");
    if (!el) return;
    el.classList.remove("loading", "hidden");
    if (activityState.loading) {
      el.textContent = "Loading…";
      el.classList.add("loading");
      return;
    }
    if (activityState.error) {
      el.textContent = activityState.error;
      return;
    }
    const q = ($("activity-filter") || {}).value || "";
    if (!activityState.items.length) {
      const count = state.status?.activity_count || 0;
      if (count > 0 && !q.trim()) {
        el.textContent = `${count} records on disk — reload or rebuild the UI container`;
      } else {
        el.textContent = q.trim() ? "No matching activity" : "No activity yet";
      }
      return;
    }
    if (activityState.hasMore) {
      el.textContent = "Scroll down for older entries";
      return;
    }
    el.textContent = "End of activity log";
  }

  function renderActivity() {
    const el = $("activity");
    const input = $("activity-filter");
    if (!el) return;
    const q = input ? input.value : "";
    const rows = filterText(activityState.items, q, ["message", "project", "service", "category"]);
    if (!rows.length) {
      const count = state.status?.activity_count || 0;
      if (count > 0 && !q.trim()) {
        el.innerHTML = `<div class="empty">${count} activity records exist but could not be displayed. Hard-refresh the page or run: podman compose -f compose/docker-compose.yml up -d --build --force-recreate</div>`;
      } else {
        el.innerHTML = `<div class="empty">No matching activity</div>`;
      }
      renderActivityStatus();
      return;
    }
    el.innerHTML = rows
      .map((a) => {
        const svc = activityServiceLabel(a);
        return `<div class="activity-line ${esc(a.level || "info")}">
          <span class="activity-ts">${fmtUtc(a.ts)}</span>
          <span class="activity-cat">${esc(a.category || "agent")}</span>
          <span class="activity-msg">${esc(a.message)}</span>
          ${svc ? `<span class="activity-svc">${esc(svc)}</span>` : ""}
        </div>`;
      })
      .join("");
    renderActivityStatus();
  }

  function renderIncidents() {
    const el = $("incidents");
    const input = $("incident-filter");
    if (!el) return;
    const q = input ? input.value : "";
    const rows = filterText(state.incidents || [], q, [
      "project",
      "service",
      "outcome",
      "issue",
      "root_cause",
    ]);
    if (!rows.length) {
      el.innerHTML = `<li class="empty">No matching incidents</li>`;
      return;
    }
    el.innerHTML = rows
      .map(
        (i) => `<li class="card clickable incident-card" data-id="${esc(i.id)}">
        <div class="meta">${tsHtml(i.ts)} · <code>${esc((i.id || "").slice(0, 8))}</code></div>
        <div class="card-head"><span class="title">${esc(displayName(i.project, i.service))}</span>
          <span class="badge badge-${esc(i.outcome)}">${esc(i.outcome)}</span></div>
        <div class="detail">${esc(i.project)}/${esc(i.service)}</div>
        <div class="issue">${esc(i.issue)}</div>
        <div class="detail">${esc((i.root_cause || "").slice(0, 200))}</div>
      </li>`
      )
      .join("");
    el.querySelectorAll(".incident-card").forEach((card) => {
      card.addEventListener("click", () => openIncidentModal(card.dataset.id));
    });
  }

  function renderApprovals() {
    const el = $("approvals");
    if (!el) return;
    const rows = state.approvals || [];
    if (!rows.length) {
      el.innerHTML = `<li class="empty">No approvals yet</li>`;
      return;
    }
    el.innerHTML = rows
      .map(
        (a) => `<li class="card">
        <div class="meta">${tsHtml(a.approved_at)}</div>
        <div class="card-head"><span class="title">${esc(a.project)}/${esc(a.service)}</span></div>
        <div class="detail">${esc(a.action || "fix")} · by ${esc(a.approved_by)} · <code>${esc((a.incident_id || "").slice(0, 8))}</code></div>
      </li>`
      )
      .join("");
  }

  function renderDiagnostics() {
    const el = $("diagnostics");
    if (!el || !state.status) return;
    const s = state.status;
    const hb = s.heartbeat || {};
    el.innerHTML = `
      <div>UI data dir · <code>${esc(s.data_dir)}</code> (${s.data_dir_readable ? "readable" : "not readable"})</div>
      <div>Agent data dir · <code>${esc(s.agent_data_dir || hb.data_dir || "—")}</code></div>
      <div>Scan data visible · ${s.has_scan_data ? "yes" : "no"}</div>
      <div>Records · incidents=${s.incidents_count}, activity=${s.activity_count}, pending=${s.pending_count}, snoozes=${s.snooze_count}</div>
      <div>Activity file readable · ${s.activity_readable ? "yes" : "no"}</div>`;
  }

  function render() {
    renderBanner();
    renderStats();
    renderHealth();
    renderSnoozes();
    renderPending();
    renderActivity();
    renderTopIssues();
    renderIncidents();
    renderApprovals();
    renderDiagnostics();
  }

  // -------------------------------------------------------------------------
  // Toast notifications
  // -------------------------------------------------------------------------
  function showToast(msg, type = "info") {
    const container = $("toast-container");
    if (!container) return;
    const el = document.createElement("div");
    el.className = `toast toast-${type}`;
    el.textContent = msg;
    container.appendChild(el);
    setTimeout(() => el.remove(), 3500);
  }

  // -------------------------------------------------------------------------
  // Scan now
  // -------------------------------------------------------------------------
  async function scanNow() {
    const btn = $("scan-now-btn");
    if (btn) { btn.disabled = true; btn.textContent = "Requesting…"; }
    try {
      await fetchJson("/api/scan", { method: "POST" });
      showToast("Scan requested — agent runs within 5 minutes", "success");
    } catch (e) {
      showToast("Failed to request scan", "error");
    } finally {
      if (btn) { btn.disabled = false; btn.textContent = "Scan now"; }
    }
  }

  // -------------------------------------------------------------------------
  // Container actions (queued via action queue)
  // -------------------------------------------------------------------------
  async function serviceAction(type, project, service) {
    try {
      await fetchJson(`/api/${encodeURIComponent(type)}/${encodeURIComponent(project)}/${encodeURIComponent(service)}`, {
        method: "POST",
      });
      showToast(`${type.charAt(0).toUpperCase() + type.slice(1)} queued for ${project}/${service} — runs on next scan`, "success");
    } catch (e) {
      showToast(`Failed to queue ${type}`, "error");
    }
  }

  async function clearRestartCounts(project, service) {
    try {
      await fetchJson(`/api/restart-counts/${encodeURIComponent(project)}/${encodeURIComponent(service)}`, {
        method: "DELETE",
      });
      showToast(`Restart rate limit cleared for ${project}/${service}`, "success");
    } catch (e) {
      showToast("Failed to clear rate limit", "error");
    }
  }

  // -------------------------------------------------------------------------
  // Service action modal
  // -------------------------------------------------------------------------
  async function openServiceModal(project, service) {
    const modal = $("svc-modal");
    const content = $("svc-modal-content");
    if (!modal || !content) return;

    const svcInfo = (state.status?.services || []).find(
      (s) => s.project === project && s.service === service
    ) || { project, service, status: "unknown", container_status: "unknown", issues: [] };

    const statusColor = svcInfo.status === "healthy" ? "success"
      : svcInfo.status === "critical" ? "danger" : "warning";
    const isMissing = svcInfo.container_status === "missing" || !svcInfo.container_status || svcInfo.container_status === "unknown";
    const isRunning = svcInfo.container_status === "running";

    const issuesHtml = (svcInfo.issues || []).length
      ? `<ul class="svc-issues">${(svcInfo.issues).map((i) => `<li>${esc(i)}</li>`).join("")}</ul>`
      : "";

    content.innerHTML = `
      <div class="svc-modal-header">
        <h2 id="svc-modal-title">${esc(project)}/${esc(service)}</h2>
        <div class="svc-status-row">
          <span class="health-dot ${esc(svcInfo.status)}" style="width:0.7rem;height:0.7rem"></span>
          <span style="font-size:0.84rem;color:var(--${statusColor})">${esc(svcInfo.container_status || svcInfo.status)}</span>
          ${svcInfo.app_version ? `<span class="pill">${esc(svcInfo.app_version)}</span>` : ""}
        </div>
        ${issuesHtml}
      </div>
      <div class="svc-actions">
        ${isMissing ? `<button class="btn-success svc-action-btn" data-action="start">Start</button>` : ""}
        ${isRunning ? `<button class="btn-secondary svc-action-btn" data-action="restart">Restart</button>` : ""}
        ${!isMissing ? `<button class="btn-danger svc-action-btn" data-action="stop">Stop</button>` : ""}
        <button class="btn-secondary btn-sm svc-action-btn" data-action="clear-rate-limit" title="Reset hourly restart counter">Clear rate limit</button>
      </div>
      <div class="log-section">
        <h3>Recent logs</h3>
        <div id="svc-log-box" class="log-box">Loading…</div>
      </div>`;

    modal.classList.remove("hidden");

    // Wire action buttons
    content.querySelectorAll(".svc-action-btn").forEach((btn) => {
      btn.addEventListener("click", async () => {
        btn.disabled = true;
        const action = btn.dataset.action;
        if (action === "clear-rate-limit") {
          await clearRestartCounts(project, service);
        } else {
          await serviceAction(action, project, service);
        }
        btn.disabled = false;
        closeServiceModal();
      });
    });

    // Load logs async
    const logBox = $("svc-log-box");
    try {
      const data = await fetchJson(
        `/api/logs/${encodeURIComponent(project)}/${encodeURIComponent(service)}?lines=100`
      );
      if (logBox) {
        const lines = (data.lines || []);
        logBox.textContent = lines.length
          ? lines.join("\n")
          : "(no log output cached yet — runs after next scan)";
        const cached = data.cached_at ? `  ·  cached ${fmtRelative(data.cached_at)}` : "";
        const note = document.createElement("div");
        note.className = "muted";
        note.style.cssText = "font-size:0.72rem;margin-top:0.35rem;font-family:inherit";
        note.textContent = `${data.total_cached_lines ?? lines.length} lines${cached}`;
        logBox.insertAdjacentElement("afterend", note);
      }
    } catch (_) {
      if (logBox) logBox.textContent = "(no cached logs yet — logs are written during each scan)";
    }
  }

  function closeServiceModal() {
    const modal = $("svc-modal");
    if (modal) modal.classList.add("hidden");
  }

  function initServiceModal() {
    document.querySelectorAll("[data-close-svc]").forEach((el) => {
      el.addEventListener("click", closeServiceModal);
    });
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape") closeServiceModal();
    });
  }

  // -------------------------------------------------------------------------
  // Cancel pending / dismiss incident
  // -------------------------------------------------------------------------
  async function cancelPending(incidentId) {
    if (!confirm("Cancel this pending action? The fix will not be applied.")) return;
    try {
      await fetchJson(`/api/pending/${encodeURIComponent(incidentId)}`, { method: "DELETE" });
      showToast("Pending action cancelled", "success");
      await loadDashboard();
    } catch (e) {
      showToast("Failed to cancel pending action", "error");
    }
  }

  async function dismissIncident(incidentId) {
    try {
      await fetchJson(`/api/dismiss/${encodeURIComponent(incidentId)}`, { method: "POST" });
      showToast("Incident dismissed", "success");
      closeModal();
      await loadDashboard();
    } catch (e) {
      showToast("Failed to dismiss incident", "error");
    }
  }

  async function snoozeService(project, service, hours) {
    await fetch("/api/snooze", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ project, service, hours, reason: "Muted from UI" }),
    });
    await loadDashboard();
  }

  function incidentDetailHtml(inc) {
    const actions = (inc.actions || [])
      .map((a) => `<li><code>${esc(a.command)}</code><br><span class="detail">${esc(a.result)}</span></li>`)
      .join("");
    const cmds = (inc.recommended_commands || [])
      .map((c) => `<li><code>${esc(c)}</code></li>`)
      .join("");
    return `
      <div class="meta">${tsHtml(inc.ts)} · <code>${esc((inc.id || "").slice(0, 8))}</code></div>
      <h2 id="modal-title" style="margin:0.5rem 0">${esc(inc.project)}/${esc(inc.service)}</h2>
      <span class="badge badge-${esc(inc.outcome)}">${esc(inc.outcome)}</span>
      <p class="issue">${esc(inc.issue)}</p>
      <h3 style="font-size:0.88rem;color:var(--muted)">Root cause</h3>
      <p class="detail">${esc(inc.root_cause || "—")}</p>
      <h3 style="font-size:0.88rem;color:var(--muted)">Actions taken</h3>
      <ul class="detail-list">${actions || "<li class='muted'>—</li>"}</ul>
      <h3 style="font-size:0.88rem;color:var(--muted)">Recommended commands</h3>
      <ul class="detail-list">${cmds || "<li class='muted'>—</li>"}</ul>
      <div class="btn-row">
        <button class="btn-secondary snooze-btn" data-project="${esc(inc.project)}" data-service="${esc(inc.service)}">Snooze 4h</button>
        <button class="btn-secondary dismiss-btn" data-id="${esc(inc.id)}">Dismiss</button>
        <a class="btn" href="/incident/${esc(inc.id)}">Open full page</a>
      </div>`;
  }

  async function openIncidentModal(id) {
    const modal = $("modal");
    const content = $("modal-content");
    if (!modal || !content) return;
    content.innerHTML = "<p class='muted'>Loading…</p>";
    modal.classList.remove("hidden");
    try {
      const inc = await fetchJson("/api/incidents/" + encodeURIComponent(id));
      content.innerHTML = incidentDetailHtml(inc);
      const snoozeBtn = content.querySelector(".snooze-btn");
      if (snoozeBtn) {
        snoozeBtn.addEventListener("click", async () => {
          await snoozeService(snoozeBtn.dataset.project, snoozeBtn.dataset.service, 4);
          closeModal();
        });
      }
      const dismissBtn = content.querySelector(".dismiss-btn");
      if (dismissBtn) {
        dismissBtn.addEventListener("click", () => dismissIncident(dismissBtn.dataset.id));
      }
    } catch (e) {
      content.innerHTML = `<p class="detail">Failed to load incident.</p>`;
    }
  }

  function closeModal() {
    const modal = $("modal");
    if (modal) modal.classList.add("hidden");
  }

  function initModal() {
    document.querySelectorAll("[data-close-modal]").forEach((el) => {
      el.addEventListener("click", closeModal);
    });
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape") closeModal();
    });
  }

  function initActivityScroll() {
    const panel = $("activity-panel");
    if (!panel) return;
    panel.addEventListener("scroll", () => {
      if (activityState.loading || !activityState.hasMore) return;
      if (panel.scrollTop + panel.clientHeight >= panel.scrollHeight - 64) {
        loadActivity(true);
      }
    });
  }

  function initFilters() {
    const af = $("activity-filter");
    const inf = $("incident-filter");
    if (af) af.addEventListener("input", renderActivity);
    if (inf) inf.addEventListener("input", renderIncidents);
  }

  function initIncidentPage() {
    document.querySelectorAll(".snooze-btn").forEach((btn) => {
      btn.addEventListener("click", async () => {
        await snoozeService(btn.dataset.project, btn.dataset.service, 4);
        btn.textContent = "Snoozed ✓";
      });
    });
    document.querySelectorAll("[data-ts]").forEach((el) => {
      const ts = el.dataset.ts;
      const rel = fmtRelative(ts);
      if (rel && !el.parentElement.querySelector(".rel")) {
        const span = document.createElement("span");
        span.className = "rel";
        span.textContent = " · " + rel;
        el.parentElement.appendChild(span);
      }
    });
  }

  async function loadUser() {
    try {
      const me = await fetchJson("/api/me");
      const label = $("user-label");
      if (label && me.user) label.textContent = me.user;
    } catch (_) {
      /* redirect handled in fetchJson */
    }
  }

  function initLogout() {
    ["logout-btn", "logout-footer"].forEach((id) => {
      const btn = $(id);
      if (btn) btn.addEventListener("click", logout);
    });
  }

  function initLogin() {
    const form = $("login-form");
    if (!form) return;
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const err = $("login-error");
      const username = $("username").value.trim();
      const password = $("password").value;
      try {
        const r = await fetch("/api/login", {
          method: "POST",
          credentials: "same-origin",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ username, password }),
        });
        if (!r.ok) {
          if (err) {
            err.textContent = "Invalid username or password";
            err.classList.remove("hidden");
          }
          return;
        }
        window.location.href = "/";
      } catch (_) {
        if (err) {
          err.textContent = "Login failed";
          err.classList.remove("hidden");
        }
      }
    });
  }

  function initDashboard() {
    initTheme();
    initModal();
    initServiceModal();
    initFilters();
    initActivityScroll();
    initLogout();
    const scanBtn = $("scan-now-btn");
    if (scanBtn) scanBtn.addEventListener("click", scanNow);
    loadUser();
    loadDashboard().catch(() => {
      const ind = $("refresh-indicator");
      if (ind) {
        ind.textContent = "Offline";
        ind.classList.add("stale");
      }
    });
    setInterval(() => {
      loadDashboard().catch(() => {
        const ind = $("refresh-indicator");
        if (ind) {
          ind.textContent = "Stale";
          ind.classList.add("stale");
        }
      });
    }, REFRESH_MS);
  }

  document.addEventListener("DOMContentLoaded", () => {
    const page = document.body.dataset.page || "dashboard";
    initTheme();
    if (page === "login") {
      initLogin();
    } else if (page === "dashboard") {
      initDashboard();
    } else {
      initIncidentPage();
    }
  });
})();
