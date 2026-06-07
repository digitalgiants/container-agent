(function () {
  "use strict";

  const REFRESH_MS = 60_000;
  const THEME_KEY = "container-agent-theme";

  let state = {
    status: null,
    pending: [],
    incidents: [],
    activity: [],
    snoozes: [],
    approvals: [],
  };

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

  async function fetchJson(url) {
    const r = await fetch(url);
    if (!r.ok) throw new Error(url + " " + r.status);
    return r.json();
  }

  async function loadDashboard() {
    const [status, pending, incidents, activity, snoozes, approvals] = await Promise.all([
      fetchJson("/api/status"),
      fetchJson("/api/pending"),
      fetchJson("/api/incidents"),
      fetchJson("/api/activity"),
      fetchJson("/api/snoozes"),
      fetchJson("/api/approvals/history"),
    ]);
    state = { status, pending, incidents, activity, snoozes, approvals };
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
        const label = esc(s.project) + "/" + esc(s.service);
        const snoozed = s.snoozed ? " snoozed" : "";
        const title = (s.issues || []).join("; ") || s.status;
        return `<div class="health-chip${snoozed}" title="${esc(title)}">
          <span class="health-dot ${esc(s.status)}"></span>
          <span>${label}</span>
          ${s.snoozed ? '<span class="pill">snoozed</span>' : ""}
        </div>`;
      })
      .join("");
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
        <a class="btn" href="/approve/${esc(p.incident_id)}">Approve fix</a>
      </li>`
      )
      .join("");
  }

  function filterText(items, query, fields) {
    const q = (query || "").trim().toLowerCase();
    if (!q) return items;
    return items.filter((item) =>
      fields.some((f) => String(item[f] || "").toLowerCase().includes(q))
    );
  }

  function renderActivity() {
    const el = $("activity");
    const input = $("activity-filter");
    if (!el) return;
    const q = input ? input.value : "";
    const rows = filterText(state.activity || [], q, ["message", "project", "service", "category"]);
    if (!rows.length) {
      el.innerHTML = `<li class="empty">No matching activity</li>`;
      return;
    }
    el.innerHTML = rows
      .map(
        (a) => `<li class="activity-row ${esc(a.level || "info")}">
        <span class="meta">${tsHtml(a.ts)}</span>
        <span><span class="pill">${esc(a.category || "agent")}</span> ${esc(a.message)}</span>
        <span class="muted">${a.project ? esc(a.project) + (a.service ? "/" + esc(a.service) : "") : ""}</span>
      </li>`
      )
      .join("");
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
        <div class="card-head"><span class="title">${esc(i.project)}/${esc(i.service)}</span>
          <span class="badge badge-${esc(i.outcome)}">${esc(i.outcome)}</span></div>
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
    renderIncidents();
    renderApprovals();
    renderDiagnostics();
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
      const btn = content.querySelector(".snooze-btn");
      if (btn) {
        btn.addEventListener("click", async () => {
          await snoozeService(btn.dataset.project, btn.dataset.service, 4);
          closeModal();
        });
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

  function initDashboard() {
    initTheme();
    initModal();
    initFilters();
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
    if (page === "dashboard") {
      initDashboard();
    } else {
      initIncidentPage();
    }
  });
})();
