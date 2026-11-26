import {$, el, empty, formatDate, safeHttpUrl} from "./dom.js";
import {createActions} from "./actions.js";
import {renderCharts, renderWatch} from "./charts.js";
import {renderRuns, renderSeeds, renderOps} from "./history.js";
import {renderKanban} from "./kanban.js";
import {renderCards, renderTable, showDetails, closeDetails, downloadCsv, copyUrls} from "./views.js";

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


const VIEWS = ["cards", "table", "kanban"];
const TABS = ["jobs", "runs", "seeds", "ops", "watch"];
const HOTKEY_STATES = {s: "shortlisted", a: "applied", i: "interview", x: "rejected", u: "unreviewed"};
const DEFAULT_FILTERS = {
  query: "", profile: "", run: "", band: "", priority: "", remote: "", reviewState: "", feedback: "",
  tag: "", newOnly: false, sort: "last_seen",
};
const CONTROLS = {
  query: "filter-query", profile: "filter-profile", run: "filter-run", band: "filter-band",
  reviewState: "filter-state", feedback: "filter-feedback", sort: "filter-sort", newOnly: "filter-new",
};
const state = {
  snapshot: null, activeTab: "jobs", view: "cards", selected: "", ranking: "priority",
  filters: {...DEFAULT_FILTERS}, reverse: false,
};
let requestSequence = 0;
const actions = createActions({
  getSnapshot: () => state.snapshot, getJob, redraw: renderJobsAndCharts, refresh: loadSnapshot,
});

function getJob(key) {
  return state.snapshot?.jobs.find(job => job.identity_key === key);
}

function readHashFilters() {
  const params = new URLSearchParams(location.hash.slice(1));
  state.filters.profile = params.get("profile") || "";
  state.filters.run = params.get("run") || "";
}

function writeHashFilters() {
  const params = new URLSearchParams();
  for (const key of ["run", "profile"]) {
    if (state.filters[key]) params.set(key, state.filters[key]);
  }
  const hash = params.toString();
  history.replaceState(null, "", location.pathname + location.search + (hash ? "#" + hash : ""));
}

function fillOptions(select, entries, label, active) {
  select.replaceChildren();
  select.appendChild(el("option", "", label)).value = "";
  for (const [key, name] of entries) select.appendChild(el("option", "", name)).value = key;
  select.value = active;
}

function syncControls() {
  for (const [key, id] of Object.entries(CONTROLS)) {
    if (key === "newOnly") $(id).checked = state.filters[key];
    else $(id).value = state.filters[key];
  }
  $("filter-band").hidden = state.snapshot?.summary.display_mode === "toolkit";
}

function isNew(job) {
  const run = state.filters.run;
  if (!run) return job.times_seen === 1;
  return Boolean(job.occurrences?.find(occ => occ.run_id === run)?.is_new);
}

function matchesJob(job) {
  const f = state.filters;
  const scopedFeedback = f.profile ? (job.profile_feedback?.[f.profile]?.relevance || job.feedback_relevance)
    : job.feedback_relevance;
  if (f.profile && job.profile_slug !== f.profile) return false;
  if (f.run && !job.run_ids.includes(f.run)) return false;
  if (f.band && job.match_band !== f.band) return false;
  if (f.priority && (job.priority ? "p" + job.priority : "unset") !== f.priority) return false;
  if (f.remote && job.remote_policy !== f.remote) return false;
  if (f.reviewState && job.review_state !== f.reviewState) return false;
  if (f.feedback && scopedFeedback !== f.feedback) return false;
  if (f.newOnly && !isNew(job)) return false;
  if (f.tag && !job.fit_signals.includes(f.tag)) return false;
  const query = f.query.trim().toLowerCase();
  return !query || [job.title, job.company, job.location, job.source_key, job.source_type, job.salary,
                    job.notes, job.identity_key, ...job.fit_signals].join(" ").toLowerCase().includes(query);
}

function compareJobs(a, b) {
  const text = (x, y) => (x || "").localeCompare(y || "");
  const order = {
    last_seen: () => text(b.last_seen, a.last_seen),
    score: () => (b.match_score ?? -1) - (a.match_score ?? -1),
    priority: () => (a.priority || 4) - (b.priority || 4),
    times_seen: () => b.times_seen - a.times_seen,
    company: () => text(a.company, b.company), title: () => text(a.title, b.title),
    status: () => text(a.review_state, b.review_state), remote: () => text(a.remote_label, b.remote_label),
    posted: () => text(b.best_date?.iso, a.best_date?.iso),
    salary: () => {
      const difference = Number(b.salary_midpoint !== null) - Number(a.salary_midpoint !== null);
      return difference || text(a.salary_currency, b.salary_currency) || (b.salary_midpoint || 0) - (a.salary_midpoint || 0);
    },
  };
  const result = (order[state.filters.sort] || order.last_seen)();
  return (state.reverse ? -result : result) || text(b.last_seen, a.last_seen) || text(a.identity_key, b.identity_key);
}

function filteredJobs() {
  return (state.snapshot?.jobs || []).filter(matchesJob).sort(compareJobs);
}

function viewContext() {
  return {
    actions, isNew, filter: toggleFilter, tag: state.filters.tag, profile: state.filters.profile,
    selected: state.selected, sort: state.filters.sort, reverse: state.reverse,
    open: openJob, reopen: key => {
      const job = getJob(key);
      if (job && $("detail-drawer").open) showDetails(job, viewContext());
    },
    sortBy: key => {
      state.reverse = state.filters.sort === key ? !state.reverse : false;
      state.filters.sort = key;
      syncControls();
      renderJobs();
    },
  };
}

function openJob(job) {
  if (!job) return;
  state.selected = job.identity_key;
  paintSelection();
  showDetails(job, viewContext());
}

function paintSelection(scroll = false) {
  for (const node of $("jobs-container").querySelectorAll("[data-job-key]")) {
    const active = node.dataset.jobKey === state.selected;
    node.classList.toggle("is-current", active);
    if (active) {
      node.setAttribute("aria-current", "true");
      if (scroll) node.scrollIntoView({block: "nearest"});
    } else node.removeAttribute("aria-current");
  }
}

function renderJobs() {
  const container = $("jobs-container");
  container.replaceChildren();
  if (!state.snapshot) return;
  const jobs = filteredJobs();
  if (!jobs.some(job => job.identity_key === state.selected)) state.selected = "";
  $("jobs-meta").textContent = "Showing " + jobs.length + " of " + state.snapshot.jobs.length + " jobs";
  $("export-csv").disabled = $("copy-urls").disabled = jobs.length === 0;
  if (!jobs.length) {
    container.append(empty(state.snapshot.meta.db_exists ? "No jobs match these filters." : "No ledger yet. Run a search to add jobs."));
    return;
  }
  ({cards: renderCards, table: renderTable, kanban: renderKanban})[state.view](container, jobs, viewContext());
  paintSelection();
}

function renderJobsAndCharts() {
  if (!state.snapshot) return;
  renderCharts(state.snapshot, state, {filter: toggleFilter, rank: selectRanking});
  renderJobs();
}

function toggleFilter(key, value) {
  state.filters[key] = state.filters[key] === value ? "" : value;
  if (key === "band") state.filters.priority = "";
  if (key === "priority") state.filters.band = "";
  if (key === "tag" && $("detail-drawer").open) closeDetails();
  switchTab("jobs");
  syncControls();
  renderJobsAndCharts();
}

function selectRanking(key) {
  state.ranking = key;
  state.filters.priority = state.filters.band = "";
  syncControls();
  renderJobsAndCharts();
}

function showRun(runId) {
  state.filters.run = runId;
  writeHashFilters();
  switchTab("jobs");
  syncControls();
  renderJobs();
}

function switchTab(tabName) {
  if (!TABS.includes(tabName)) return;
  state.activeTab = tabName;
  for (const name of TABS) {
    const tab = $("tab-" + name);
    const active = name === tabName;
    tab.setAttribute("aria-selected", String(active));
    tab.classList.toggle("is-active", active);
    $("view-" + name).hidden = !active;
  }
}

function changeView(view) {
  state.view = view;
  for (const button of document.querySelectorAll("[data-view]")) {
    button.setAttribute("aria-pressed", String(button.dataset.view === view));
  }
  renderJobs();
}

function renderSnapshot() {
  const snap = state.snapshot;
  const meta = snap.meta;
  const mode = actions.editable() ? "Editable" : "Read-only";
  $("db-status").textContent = meta.db_exists ? mode + " \u00b7 " + meta.db_name + " v" + meta.schema_version :
    mode + " \u00b7 no ledger yet";
  $("snapshot-time").textContent = "Refreshed " + formatDate(meta.generated_at);
  fillOptions($("filter-profile"), snap.summary.profiles.map(slug => [slug, slug]), "All profiles", state.filters.profile);
  fillOptions($("filter-run"), snap.runs.map(run => [run.run_id, run.run_id + " " + run.profile_slug]),
              "All runs", state.filters.run);
  syncControls();
  const totals = {
    jobs: snap.jobs.length, runs: snap.runs.length, seeds: snap.seed_yields.length,
    ops: snap.saved_searches.length + snap.diagnostics.length, watch: snap.watch_pending.length,
  };
  for (const key of TABS) $("tab-count-" + key).textContent = totals[key];
  renderJobsAndCharts();
  renderRuns(snap, showRun);
  renderSeeds(snap);
  renderOps(snap);
  renderWatch(snap, state, actions);
}

async function loadSnapshot() {
  const sequence = ++requestSequence;
  try {
    const response = await fetch("/api/ledger", {cache: "no-store"});
    if (!response.ok) throw new Error();
    const snapshot = await response.json();
    if (sequence === requestSequence) {
      state.snapshot = snapshot;
      renderSnapshot();
    }
  } catch {
    if (sequence === requestSequence) $("db-status").textContent = "Could not load the ledger.";
  }
}

function moveSelection(direction) {
  const jobs = filteredJobs();
  if (!jobs.length) return;
  const index = jobs.findIndex(job => job.identity_key === state.selected);
  const target = index < 0 ? (direction < 0 ? jobs.length - 1 : 0) : Math.max(0, Math.min(jobs.length - 1, index + direction));
  state.selected = jobs[target].identity_key;
  paintSelection(true);
}

function handleKeys(event) {
  if (event.defaultPrevented || event.ctrlKey || event.metaKey || event.altKey) return;
  if (event.target.closest("input, textarea, select, [contenteditable]")) return;
  const key = event.key;
  const help = $("shortcuts");
  if (key === "?" && !$("detail-drawer").open) {
    event.preventDefault();
    if (help.open) help.close(); else help.showModal();
    return;
  }
  if (help.open || $("copy-dialog").open) return;
  if (key === "/" && !$("detail-drawer").open) {
    event.preventDefault();
    switchTab("jobs");
    $("filter-query").focus();
    return;
  }
  if (state.activeTab !== "jobs") return;
  if (key === "Enter" && event.target.closest("button, a, summary")) return;
  if ($("detail-drawer").open && key !== "Enter") return;
  const job = getJob(state.selected) || filteredJobs()[0];
  const commands = {
    j: () => moveSelection(1), k: () => moveSelection(-1),
    v: () => changeView(VIEWS[(VIEWS.indexOf(state.view) + 1) % VIEWS.length]),
    Enter: () => $("detail-drawer").open ? closeDetails() : openJob(job),
    o: () => {
      const url = safeHttpUrl(job?.canonical_url);
      if (url) globalThis.open(url, "_blank", "noopener,noreferrer");
    },
  };
  if (actions.editable()) {
    for (const [shortcut, reviewState] of Object.entries(HOTKEY_STATES)) {
      commands[shortcut] = () => actions.setState(job, reviewState);
    }
    commands.z = actions.undo;
  }
  if (!Object.hasOwn(commands, key)) return;
  event.preventDefault();
  commands[key]();
}

function bindEvents() {
  $("refresh-btn").addEventListener("click", loadSnapshot);
  $("theme-btn").addEventListener("click", () => applyTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark"));
  $("density-btn").addEventListener("click", () => {
    const compact = document.documentElement.classList.toggle("compact-density");
    $("density-btn").setAttribute("aria-pressed", String(compact));
    $("density-btn").textContent = compact ? "Comfortable" : "Compact";
  });
  for (const [field, id] of Object.entries(CONTROLS)) {
    $(id).addEventListener(field === "query" ? "input" : "change", event => {
      state.filters[field] = field === "newOnly" ? event.target.checked : event.target.value;
      if (field === "band") state.filters.priority = "";
      if (field === "sort") state.reverse = false;
      if (field === "run" || field === "profile") writeHashFilters();
      if (field === "reviewState" || field === "band") renderJobsAndCharts();
      else renderJobs();
    });
  }
  for (const button of document.querySelectorAll("[data-tab]")) {
    button.addEventListener("click", () => switchTab(button.dataset.tab));
  }
  for (const button of document.querySelectorAll("[data-view]")) {
    button.addEventListener("click", () => changeView(button.dataset.view));
  }
  $("clear-filters-btn").addEventListener("click", () => {
    state.filters = {...DEFAULT_FILTERS};
    state.reverse = false;
    writeHashFilters();
    syncControls();
    renderJobsAndCharts();
  });
  $("export-csv").addEventListener("click", () => downloadCsv(filteredJobs()));
  $("copy-urls").addEventListener("click", () => copyUrls(filteredJobs(), actions.notify));
  $("drawer-close").addEventListener("click", closeDetails);
  $("close-copy").addEventListener("click", () => $("copy-dialog").close());
  $("undo-btn").addEventListener("click", actions.undo);
  $("show-shortcuts").addEventListener("click", () => $("shortcuts").showModal());
  $("close-shortcuts").addEventListener("click", () => $("shortcuts").close());
  document.addEventListener("keydown", handleKeys);
  globalThis.addEventListener("hashchange", () => {
    readHashFilters();
    syncControls();
    renderJobs();
  });
}

const narrowLayout = matchMedia("(max-width: 44rem)");
function configureOverview() {
  $("ledger-overview").open = !narrowLayout.matches;
}
narrowLayout.addEventListener("change", configureOverview);
configureOverview();

initTheme();
readHashFilters();
bindEvents();
void loadSnapshot();
