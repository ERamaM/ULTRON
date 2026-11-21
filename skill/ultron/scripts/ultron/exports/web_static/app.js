(function () {
  "use strict";

  const BANDS = [
    { key: "strong", label: "Strong" },
    { key: "good", label: "Good" },
    { key: "borderline", label: "Borderline" },
    { key: "ineligible", label: "Ineligible" },
    { key: "unscored", label: "Unscored" },
  ];

  const STATES = [
    { key: "unreviewed", label: "Unreviewed" },
    { key: "shortlisted", label: "Shortlisted" },
    { key: "applied", label: "Applied" },
    { key: "rejected", label: "Rejected" },
    { key: "archived", label: "Archived" },
  ];

  const state = {
    snapshot: null,
    activeTab: "jobs",
    tableMode: false,
    filters: {
      query: "",
      profile: "",
      run: "",
      band: "",
      reviewState: "",
      feedback: "",
      newOnly: false,
      sort: "last_seen",
    },
  };

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) {
      node.className = className;
    }
    if (text !== undefined && text !== null) {
      node.textContent = String(text);
    }
    return node;
  }

  function clearNode(node) {
    while (node.firstChild) {
      node.removeChild(node.firstChild);
    }
  }

  function safeHttpUrl(raw) {
    if (typeof raw !== "string" || !raw) {
      return null;
    }
    try {
      const parsed = new URL(raw);
      if (parsed.protocol === "http:" || parsed.protocol === "https:") {
        return parsed.href;
      }
    } catch {
      return null;
    }
    return null;
  }

  function formatDate(raw) {
    if (!raw) {
      return "—";
    }
    const text = String(raw);
    return text.length >= 16 ? text.slice(0, 16).replace("T", " ") : text;
  }

  function createExternalLink(url, label, className) {
    const safe = safeHttpUrl(url);
    if (!safe) {
      return el("span", "panel-hint", "No safe URL");
    }
    const anchor = el("a", className, label);
    anchor.href = safe;
    anchor.target = "_blank";
    anchor.rel = "noopener noreferrer";
    return anchor;
  }

  function createScoreRing(score) {
    if (typeof score !== "number" || !Number.isFinite(score)) {
      return el("span", "panel-hint", "No score");
    }
    const clamped = Math.max(0, Math.min(100, Math.round(score)));
    const radius = 16;
    const circ = 2 * Math.PI * radius;
    const offset = circ * (1 - clamped / 100);
    const ns = "http://www.w3.org/2000/svg";
    const svg = document.createElementNS(ns, "svg");
    svg.setAttribute("class", "score-svg");
    svg.setAttribute("viewBox", "0 0 40 40");
    svg.setAttribute("role", "img");
    svg.setAttribute("aria-label", `Match score ${clamped} out of 100`);

    const track = document.createElementNS(ns, "circle");
    track.setAttribute("class", "score-track");
    track.setAttribute("cx", "20");
    track.setAttribute("cy", "20");
    track.setAttribute("r", String(radius));

    const arc = document.createElementNS(ns, "circle");
    arc.setAttribute("class", "score-arc");
    arc.setAttribute("cx", "20");
    arc.setAttribute("cy", "20");
    arc.setAttribute("r", String(radius));
    arc.setAttribute("stroke-dasharray", circ.toFixed(2));
    arc.setAttribute("stroke-dashoffset", offset.toFixed(2));

    const txt = document.createElementNS(ns, "text");
    txt.setAttribute("class", "score-text");
    txt.setAttribute("x", "20");
    txt.setAttribute("y", "20");
    txt.textContent = String(clamped);

    svg.appendChild(track);
    svg.appendChild(arc);
    svg.appendChild(txt);
    return svg;
  }

  function applyTheme(theme) {
    document.documentElement.dataset.theme = theme;
    const btn = document.getElementById("theme-btn");
    if (btn) {
      btn.textContent = theme === "dark" ? "Light mode" : "Dark mode";
    }
    try {
      localStorage.setItem("ultron-ledger-theme", theme);
    } catch {
      // Storage may be unavailable in restricted contexts.
    }
  }

  function initTheme() {
    let saved = "";
    try {
      saved = localStorage.getItem("ultron-ledger-theme") || "";
    } catch {
      saved = "";
    }
    if (saved === "light" || saved === "dark") {
      applyTheme(saved);
      return;
    }
    const prefersDark = Boolean(globalThis.matchMedia?.("(prefers-color-scheme: dark)")?.matches);
    applyTheme(prefersDark ? "dark" : "light");
  }

  function readHashFilters() {
    const raw = (globalThis.location.hash || "").replace(/^#/, "");
    if (!raw) {
      return;
    }
    const params = new URLSearchParams(raw);
    const run = params.get("run");
    const profile = params.get("profile");
    if (run) {
      state.filters.run = run;
    }
    if (profile) {
      state.filters.profile = profile;
    }
  }

  function updateStatusPill(snapshot) {
    const pill = document.getElementById("db-status");
    const meta = snapshot.meta;
    if (!meta.db_exists) {
      pill.textContent = `${meta.db_name} not created yet • zero-write mode`;
      return;
    }
    pill.textContent = `${meta.db_name} (v${meta.schema_version}) • refreshed ${formatDate(meta.generated_at)}`;
  }

  function addKpiCard(container, label, value, sub) {
    const card = el("article", "kpi-card");
    card.appendChild(el("p", "kpi-label", label));
    card.appendChild(el("p", "kpi-value", value));
    card.appendChild(el("p", "kpi-sub", sub));
    container.appendChild(card);
  }

  function renderKpis(summary) {
    const grid = document.getElementById("kpi-grid");
    clearNode(grid);
    const strongGood = (summary.band_counts.strong || 0) + (summary.band_counts.good || 0);
    const active = (summary.state_counts.shortlisted || 0) + (summary.state_counts.applied || 0);
    const avgLabel = summary.avg_score === null ? "No scored jobs" : `Avg score ${summary.avg_score}`;
    addKpiCard(grid, "Tracked Jobs", summary.total_jobs, `${summary.new_jobs} seen once (new)`);
    addKpiCard(grid, "Strong + Good", strongGood, avgLabel);
    addKpiCard(grid, "Active Pipeline", active, `${summary.state_counts.shortlisted || 0} shortlisted · ${summary.state_counts.applied || 0} applied`);
    addKpiCard(grid, "Runs & Profiles", summary.total_runs, `${summary.profile_count} profile(s)`);
    addKpiCard(grid, "Companies", summary.company_count, `${summary.seed_count} seed entries`);
  }

  function renderBandPanel(summary) {
    const bar = document.getElementById("band-bar");
    const legend = document.getElementById("band-legend");
    clearNode(bar);
    clearNode(legend);

    const total = Math.max(1, summary.total_jobs);
    for (const item of BANDS) {
      const count = summary.band_counts[item.key] || 0;
      if (count > 0) {
        const seg = el("span", `seg-${item.key}`);
        seg.style.width = `${(count / total) * 100}%`;
        seg.title = `${item.label}: ${count}`;
        bar.appendChild(seg);
      }
      const li = document.createElement("li");
      const btn = el("button", `filter-pill band-${item.key}`, `${item.label}: ${count}`);
      btn.type = "button";
      if (state.filters.band === item.key) {
        btn.classList.add("is-selected");
      }
      btn.addEventListener("click", () => {
        state.filters.band = state.filters.band === item.key ? "" : item.key;
        syncFilterControls();
        renderAll();
      });
      li.appendChild(btn);
      legend.appendChild(li);
    }
  }

  function renderStatePanel(summary) {
    const list = document.getElementById("state-pills");
    clearNode(list);
    for (const item of STATES) {
      const count = summary.state_counts[item.key] || 0;
      const li = document.createElement("li");
      const btn = el("button", "filter-pill", `${item.label}: ${count}`);
      btn.type = "button";
      if (state.filters.reviewState === item.key) {
        btn.classList.add("is-selected");
      }
      btn.addEventListener("click", () => {
        state.filters.reviewState = state.filters.reviewState === item.key ? "" : item.key;
        syncFilterControls();
        renderAll();
      });
      li.appendChild(btn);
      list.appendChild(li);
    }
  }

  function renderTopCompanies(summary) {
    const list = document.getElementById("top-companies");
    clearNode(list);
    const items = summary.top_companies || [];
    if (items.length === 0) {
      list.appendChild(el("li", "panel-hint", "No companies recorded yet."));
      return;
    }
    const maxCount = Math.max(1, items[0].count);
    for (const entry of items.slice(0, 6)) {
      const li = document.createElement("li");
      const btn = el("button", "bar-row-btn");
      btn.type = "button";
      btn.appendChild(el("span", "bar-label", entry.name));
      const track = el("span", "bar-track");
      const fill = el("span", "bar-fill");
      fill.style.width = `${(entry.count / maxCount) * 100}%`;
      track.appendChild(fill);
      btn.appendChild(track);
      btn.appendChild(el("span", "bar-num", entry.count));
      btn.addEventListener("click", () => {
        state.filters.query = state.filters.query === entry.name ? "" : entry.name;
        syncFilterControls();
        renderJobs();
      });
      li.appendChild(btn);
      list.appendChild(li);
    }
  }

  function populateDropdowns(snapshot) {
    const profSel = document.getElementById("filter-profile");
    const runSel = document.getElementById("filter-run");
    const currentProf = state.filters.profile;
    const currentRun = state.filters.run;

    clearNode(profSel);
    profSel.appendChild(el("option", "", "All profiles")).value = "";
    for (const slug of snapshot.summary.profiles || []) {
      const opt = el("option", "", slug);
      opt.value = slug;
      profSel.appendChild(opt);
    }
    profSel.value = currentProf;

    clearNode(runSel);
    runSel.appendChild(el("option", "", "All runs")).value = "";
    for (const run of snapshot.runs || []) {
      const label = run.profile_slug ? `${run.run_id} (${run.profile_slug})` : run.run_id;
      const opt = el("option", "", label);
      opt.value = run.run_id;
      runSel.appendChild(opt);
    }
    runSel.value = currentRun;
  }

  function syncFilterControls() {
    document.getElementById("filter-query").value = state.filters.query;
    document.getElementById("filter-profile").value = state.filters.profile;
    document.getElementById("filter-run").value = state.filters.run;
    document.getElementById("filter-band").value = state.filters.band;
    document.getElementById("filter-state").value = state.filters.reviewState;
    document.getElementById("filter-feedback").value = state.filters.feedback;
    document.getElementById("filter-sort").value = state.filters.sort;
    document.getElementById("filter-new").checked = state.filters.newOnly;
  }

  function isNewForFilter(job) {
    if (!state.filters.run) {
      return job.times_seen === 1;
    }
    const match = (job.occurrences || []).find((occ) => occ.run_id === state.filters.run);
    return match ? Boolean(match.is_new) : false;
  }

  function matchesJob(job) {
    const f = state.filters;
    if (f.profile && job.profile_slug !== f.profile) {
      return false;
    }
    if (f.run && !(job.run_ids || []).includes(f.run)) {
      return false;
    }
    if (f.band && job.match_band !== f.band) {
      return false;
    }
    if (f.reviewState && job.review_state !== f.reviewState) {
      return false;
    }
    if (f.feedback && job.feedback_relevance !== f.feedback) {
      return false;
    }
    if (f.newOnly && !isNewForFilter(job)) {
      return false;
    }
    if (f.query) {
      const q = f.query.trim().toLowerCase();
      const hay = [job.title, job.company, job.location, job.source_type, job.source_key, job.notes, job.identity_key]
        .join(" ")
        .toLowerCase();
      if (!hay.includes(q)) {
        return false;
      }
    }
    return true;
  }

  function compareJobs(left, right) {
    const sortBy = state.filters.sort;
    if (sortBy === "score") {
      const ls = left.match_score ?? -1;
      const rs = right.match_score ?? -1;
      return rs - ls || String(right.last_seen).localeCompare(String(left.last_seen));
    }
    if (sortBy === "times_seen") {
      return right.times_seen - left.times_seen || String(right.last_seen).localeCompare(String(left.last_seen));
    }
    if (sortBy === "company") {
      return String(left.company).localeCompare(String(right.company)) || String(left.title).localeCompare(String(right.title));
    }
    return String(right.last_seen).localeCompare(String(left.last_seen));
  }

  function createJobPills(job) {
    const row = el("ul", "pill-list");
    const addPill = (text, cls) => {
      const li = document.createElement("li");
      li.appendChild(el("span", cls ? `pill ${cls}` : "pill", text));
      row.appendChild(li);
    };
    addPill(job.match_band, `band-${job.match_band}`);
    addPill(job.review_state, "");
    if (isNewForFilter(job)) {
      addPill("New", "pill-new");
    }
    if (job.profile_slug) {
      addPill(job.profile_slug, "");
    }
    if (job.source_type) {
      addPill(job.source_type, "");
    }
    if (job.feedback_relevance) {
      addPill(`feedback: ${job.feedback_relevance}`, "");
    }
    return row;
  }

  function renderJobCards(container, rows) {
    const ul = el("ul", "jobs-list");
    for (const job of rows) {
      const isNew = isNewForFilter(job);
      const li = el("li", isNew ? "job-card is-new" : "job-card");

      const mainCol = document.createElement("div");
      const titleBtn = el("button", "job-title-btn", job.title);
      titleBtn.type = "button";
      titleBtn.addEventListener("click", () => openDrawer(job));
      mainCol.appendChild(titleBtn);

      const org = el("p", "job-org");
      org.appendChild(el("strong", "", job.company || "Unknown company"));
      if (job.location) {
        org.appendChild(document.createTextNode(` · ${job.location}`));
      }
      org.appendChild(document.createTextNode(` · seen ${job.times_seen}x · last ${formatDate(job.last_seen)}`));
      mainCol.appendChild(org);
      mainCol.appendChild(createJobPills(job));
      li.appendChild(mainCol);

      const side = el("div", "job-side");
      side.appendChild(createScoreRing(job.match_score));
      const link = createExternalLink(job.canonical_url, "Open ↗", "btn btn-primary");
      side.appendChild(link);
      li.appendChild(side);

      ul.appendChild(li);
    }
    container.appendChild(ul);
  }

  function renderJobTable(container, rows) {
    const wrap = el("div", "table-wrap");
    const table = el("table", "data-table");
    const thead = document.createElement("thead");
    const headRow = document.createElement("tr");
    for (const heading of ["Score", "Title", "Company", "State", "Seen", "Last Seen", "Link"]) {
      headRow.appendChild(el("th", "", heading));
    }
    thead.appendChild(headRow);
    table.appendChild(thead);

    const tbody = document.createElement("tbody");
    for (const job of rows) {
      const tr = document.createElement("tr");
      tr.appendChild(el("td", "", job.match_score === null ? "—" : `${job.match_score} (${job.match_band})`));

      const titleTd = document.createElement("td");
      const titleBtn = el("button", "job-title-btn", job.title);
      titleBtn.type = "button";
      titleBtn.addEventListener("click", () => openDrawer(job));
      titleTd.appendChild(titleBtn);
      tr.appendChild(titleTd);

      tr.appendChild(el("td", "", job.company || "Unknown"));
      tr.appendChild(el("td", "", job.review_state));
      tr.appendChild(el("td", "", `${job.times_seen}x`));
      tr.appendChild(el("td", "", formatDate(job.last_seen)));

      const linkTd = document.createElement("td");
      linkTd.appendChild(createExternalLink(job.canonical_url, "Open ↗", "btn btn-secondary"));
      tr.appendChild(linkTd);
      tbody.appendChild(tr);
    }
    table.appendChild(tbody);
    wrap.appendChild(table);
    container.appendChild(wrap);
  }

  function renderJobs() {
    const snapshot = state.snapshot;
    if (!snapshot) {
      return;
    }
    const container = document.getElementById("jobs-container");
    const metaEl = document.getElementById("jobs-meta");
    clearNode(container);

    if (!snapshot.meta.db_exists) {
      metaEl.textContent = "0 jobs shown";
      const empty = el("div", "empty-box");
      empty.appendChild(el("p", "", "No SQLite ledger exists yet at $ULTRON_HOME/ultron.sqlite3."));
      const hint = el("p", "");
      hint.appendChild(document.createTextNode("Run a search first with "));
      hint.appendChild(el("code", "", "ultron run --unattended --profile <slug>"));
      hint.appendChild(document.createTextNode(" and click Refresh above."));
      empty.appendChild(hint);
      container.appendChild(empty);
      return;
    }

    const filtered = (snapshot.jobs || []).filter(matchesJob).sort(compareJobs);
    metaEl.textContent = `Showing ${filtered.length} of ${(snapshot.jobs || []).length} jobs`;

    if (filtered.length === 0) {
      const box = el("div", "empty-box");
      box.appendChild(el("p", "", "No jobs match the current filters."));
      container.appendChild(box);
      return;
    }

    if (state.tableMode) {
      renderJobTable(container, filtered);
    } else {
      renderJobCards(container, filtered);
    }
  }

  function renderRuns() {
    const container = document.getElementById("runs-container");
    clearNode(container);
    const items = state.snapshot?.runs || [];
    if (items.length === 0) {
      const box = el("div", "empty-box");
      box.appendChild(el("p", "", "No finalized runs recorded in the ledger yet."));
      container.appendChild(box);
      return;
    }

    const stack = el("div", "cards-stack");
    for (const run of items) {
      const card = el("article", "panel");
      const head = el("div", "panel-head");
      head.appendChild(el("h2", "mono", run.run_id));
      const jumpBtn = el("button", "btn btn-secondary", "Inspect run jobs");
      jumpBtn.type = "button";
      jumpBtn.addEventListener("click", () => {
        state.filters.run = run.run_id;
        syncFilterControls();
        switchTab("jobs");
        renderJobs();
      });
      head.appendChild(jumpBtn);
      card.appendChild(head);

      card.appendChild(
        el(
          "p",
          "job-org",
          `Profile: ${run.profile_slug || "—"} · Preset: ${run.preset || "—"} · Kept: ${run.roles_kept} (${run.new_roles_count} new) · Finished: ${formatDate(run.finished_at || run.created_at)}`
        )
      );

      const discards = Object.entries(run.discard_counts || {});
      if (discards.length > 0) {
        const ul = el("ul", "pill-list");
        for (const [reason, count] of discards) {
          const li = document.createElement("li");
          li.appendChild(el("span", "pill", `${reason}: ${count}`));
          ul.appendChild(li);
        }
        card.appendChild(ul);
      }
      stack.appendChild(card);
    }
    container.appendChild(stack);
  }

  function renderSeeds() {
    const container = document.getElementById("seeds-container");
    clearNode(container);
    const items = state.snapshot?.seed_yields || [];
    if (items.length === 0) {
      const box = el("div", "empty-box");
      box.appendChild(el("p", "", "No seed yield telemetry recorded yet."));
      container.appendChild(box);
      return;
    }

    const wrap = el("div", "table-wrap");
    const table = el("table", "data-table");
    const thead = document.createElement("thead");
    const headRow = document.createElement("tr");
    for (const col of ["Profile", "Seed", "Attempts", "Runs w/ Keeps", "Total Kept", "Yield %"]) {
      headRow.appendChild(el("th", "", col));
    }
    thead.appendChild(headRow);
    table.appendChild(thead);

    const tbody = document.createElement("tbody");
    for (const item of items) {
      const tr = document.createElement("tr");
      tr.appendChild(el("td", "", item.profile_slug || "—"));
      tr.appendChild(el("td", "mono", item.seed_key));
      tr.appendChild(el("td", "", item.attempts));
      tr.appendChild(el("td", "", item.runs_with_keeps));
      tr.appendChild(el("td", "", item.total_kept));
      tr.appendChild(el("td", "", `${item.yield_pct}%`));
      tbody.appendChild(tr);
    }
    table.appendChild(tbody);
    wrap.appendChild(table);
    container.appendChild(wrap);
  }

  function renderOps() {
    const container = document.getElementById("ops-container");
    clearNode(container);
    const stack = el("div", "cards-stack");

    const saved = state.snapshot?.saved_searches || [];
    const savedPanel = el("article", "panel");
    savedPanel.appendChild(el("h2", "", `Saved Searches (${saved.length})`));
    if (saved.length === 0) {
      savedPanel.appendChild(el("p", "panel-hint", "No saved searches configured."));
    } else {
      const ul = el("ul", "jobs-list");
      for (const s of saved) {
        const li = el("li", "drawer-section");
        li.appendChild(el("strong", "", `${s.name} · ${s.profile_slug} (${s.preset})`));
        if (s.prompt) {
          li.appendChild(el("p", "notes-pre", s.prompt));
        }
        ul.appendChild(li);
      }
      savedPanel.appendChild(ul);
    }
    stack.appendChild(savedPanel);

    const diags = state.snapshot?.diagnostics || [];
    const diagPanel = el("article", "panel");
    diagPanel.appendChild(el("h2", "", `Diagnostics (${diags.length})`));
    if (diags.length === 0) {
      diagPanel.appendChild(el("p", "panel-hint", "No URL diagnosis records stored."));
    } else {
      const ul = el("ul", "jobs-list");
      for (const d of diags) {
        const li = el("li", "drawer-section");
        li.appendChild(el("strong", "", `${d.first_failed_stage || "unknown"} · ${d.reason_code || "unspecified"}`));
        li.appendChild(el("p", "mono", d.identity_key || "—"));
        li.appendChild(createExternalLink(d.submitted_url, d.submitted_url || "No safe URL", ""));
        ul.appendChild(li);
      }
      diagPanel.appendChild(ul);
    }
    stack.appendChild(diagPanel);
    container.appendChild(stack);
  }

  function copyWithButton(button, text) {
    const orig = button.textContent;
    if (navigator.clipboard?.writeText) {
      navigator.clipboard.writeText(text).then(() => {
        button.textContent = "Copied";
        setTimeout(() => {
          button.textContent = orig;
        }, 1200);
      }).catch(() => {
        button.textContent = "Failed";
      });
    }
  }

  function openDrawer(job) {
    const drawer = document.getElementById("detail-drawer");
    const backdrop = document.getElementById("drawer-backdrop");
    const body = document.getElementById("drawer-body");
    document.getElementById("drawer-company").textContent = `${job.company || "Unknown company"}${job.location ? ` · ${job.location}` : ""}`;
    document.getElementById("drawer-title").textContent = job.title;
    clearNode(body);

    const metaSec = el("section", "drawer-section");
    metaSec.appendChild(el("h3", "", "Overview"));
    metaSec.appendChild(createJobPills(job));
    metaSec.appendChild(
      el(
        "p",
        "job-org",
        `Score: ${job.match_score ?? "—"} · First seen: ${formatDate(job.first_seen)} · Last seen: ${formatDate(job.last_seen)} · Seen ${job.times_seen}x`
      )
    );
    metaSec.appendChild(createExternalLink(job.canonical_url, "Open job posting ↗", "btn btn-primary"));
    body.appendChild(metaSec);

    if (job.notes || job.feedback_note || job.feedback_relevance) {
      const noteSec = el("section", "drawer-section");
      noteSec.appendChild(el("h3", "", "Notes & Feedback"));
      if (job.feedback_relevance) {
        noteSec.appendChild(el("p", "", `Feedback: ${job.feedback_relevance}${job.feedback_reason ? ` (${job.feedback_reason})` : ""}`));
      }
      if (job.feedback_note) {
        noteSec.appendChild(el("p", "notes-pre", job.feedback_note));
      }
      if (job.notes) {
        noteSec.appendChild(el("pre", "notes-pre", job.notes));
      }
      body.appendChild(noteSec);
    }

    const occSec = el("section", "drawer-section");
    occSec.appendChild(el("h3", "", `Run History (${(job.occurrences || []).length})`));
    if ((job.occurrences || []).length === 0) {
      occSec.appendChild(el("p", "panel-hint", "No individual run occurrences recorded."));
    } else {
      const ul = el("ul", "pill-list");
      for (const occ of job.occurrences) {
        const li = document.createElement("li");
        const suffix = occ.is_new ? " (new)" : "";
        li.appendChild(el("span", "pill mono", `${occ.run_id}${suffix}`));
        ul.appendChild(li);
      }
      occSec.appendChild(ul);
    }
    body.appendChild(occSec);

    const cliSec = el("section", "drawer-section");
    cliSec.appendChild(el("h3", "", "CLI Commands"));
    cliSec.appendChild(el("p", "mono", `Key: ${job.identity_key}`));
    const cmds = [
      `ultron jobs mark "${job.identity_key}" shortlisted`,
      `ultron jobs note "${job.identity_key}" "Reviewed in web dashboard"`,
    ];
    for (const cmd of cmds) {
      const row = el("div", "cmd-row");
      row.appendChild(el("code", "", cmd));
      const copyBtn = el("button", "btn btn-secondary", "Copy");
      copyBtn.type = "button";
      copyBtn.addEventListener("click", () => copyWithButton(copyBtn, cmd));
      row.appendChild(copyBtn);
      cliSec.appendChild(row);
    }
    body.appendChild(cliSec);

    drawer.hidden = false;
    backdrop.hidden = false;
  }

  function closeDrawer() {
    document.getElementById("detail-drawer").hidden = true;
    document.getElementById("drawer-backdrop").hidden = true;
  }

  function switchTab(tabName) {
    state.activeTab = tabName;
    for (const btn of document.querySelectorAll(".tab")) {
      const active = btn.dataset.tab === tabName;
      btn.classList.toggle("is-active", active);
      btn.setAttribute("aria-selected", active ? "true" : "false");
    }
    for (const name of ["jobs", "runs", "seeds", "ops"]) {
      document.getElementById(`view-${name}`).hidden = name !== tabName;
    }
  }

  function renderAll() {
    const snap = state.snapshot;
    if (!snap) {
      return;
    }
    updateStatusPill(snap);
    renderKpis(snap.summary);
    renderBandPanel(snap.summary);
    renderStatePanel(snap.summary);
    renderTopCompanies(snap.summary);
    populateDropdowns(snap);
    document.getElementById("tab-count-jobs").textContent = String((snap.jobs || []).length);
    document.getElementById("tab-count-runs").textContent = String((snap.runs || []).length);
    document.getElementById("tab-count-seeds").textContent = String((snap.seed_yields || []).length);
    document.getElementById("tab-count-ops").textContent = String((snap.saved_searches || []).length + (snap.diagnostics || []).length);
    renderJobs();
    renderRuns();
    renderSeeds();
    renderOps();
  }

  async function loadSnapshot() {
    const status = document.getElementById("db-status");
    try {
      const resp = await fetch("/api/ledger", { cache: "no-store" });
      if (!resp.ok) {
        throw new Error(`HTTP ${resp.status}`);
      }
      state.snapshot = await resp.json();
      syncFilterControls();
      renderAll();
    } catch (err) {
      status.textContent = `Error loading ledger (${err instanceof Error ? err.message : "unknown"})`;
    }
  }

  function bindEvents() {
    document.getElementById("refresh-btn").addEventListener("click", () => {
      void loadSnapshot();
    });
    document.getElementById("theme-btn").addEventListener("click", () => {
      const current = document.documentElement.dataset.theme === "dark" ? "dark" : "light";
      applyTheme(current === "dark" ? "light" : "dark");
    });
    for (const btn of document.querySelectorAll(".tab")) {
      btn.addEventListener("click", () => switchTab(btn.dataset.tab));
    }
    document.getElementById("filter-query").addEventListener("input", (e) => {
      state.filters.query = e.target.value;
      renderJobs();
    });
    document.getElementById("filter-profile").addEventListener("change", (e) => {
      state.filters.profile = e.target.value;
      renderJobs();
    });
    document.getElementById("filter-run").addEventListener("change", (e) => {
      state.filters.run = e.target.value;
      renderJobs();
    });
    document.getElementById("filter-band").addEventListener("change", (e) => {
      state.filters.band = e.target.value;
      renderAll();
    });
    document.getElementById("filter-state").addEventListener("change", (e) => {
      state.filters.reviewState = e.target.value;
      renderAll();
    });
    document.getElementById("filter-feedback").addEventListener("change", (e) => {
      state.filters.feedback = e.target.value;
      renderJobs();
    });
    document.getElementById("filter-sort").addEventListener("change", (e) => {
      state.filters.sort = e.target.value;
      renderJobs();
    });
    document.getElementById("filter-new").addEventListener("change", (e) => {
      state.filters.newOnly = Boolean(e.target.checked);
      renderJobs();
    });
    document.getElementById("view-mode-btn").addEventListener("click", (e) => {
      state.tableMode = !state.tableMode;
      e.target.textContent = state.tableMode ? "Card view" : "Table view";
      renderJobs();
    });
    document.getElementById("clear-filters-btn").addEventListener("click", () => {
      state.filters = {
        query: "",
        profile: "",
        run: "",
        band: "",
        reviewState: "",
        feedback: "",
        newOnly: false,
        sort: "last_seen",
      };
      syncFilterControls();
      renderAll();
    });
    document.getElementById("drawer-close").addEventListener("click", closeDrawer);
    document.getElementById("drawer-backdrop").addEventListener("click", closeDrawer);
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape") {
        closeDrawer();
      } else if (e.key === "/" && document.activeElement?.tagName !== "INPUT") {
        e.preventDefault();
        switchTab("jobs");
        document.getElementById("filter-query").focus();
      }
    });
  }

  initTheme();
  readHashFilters();
  bindEvents();
  void loadSnapshot();
})();
