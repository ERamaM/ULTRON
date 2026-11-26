import {$, BANDS, PRIORITIES, STATES, button, createExternalLink, el, monogram} from "./dom.js";
import {watchForm} from "./actions.js";

const remoteLabels = {
  remote_spain: "Remote \u00b7 Spain", remote_eu: "Remote \u00b7 EU", remote_europe: "Remote \u00b7 Europe",
  remote_emea: "Remote \u00b7 EMEA", remote_other: "Remote \u00b7 Other", onsite: "On-site",
  hybrid: "Hybrid", unknown: "Unspecified",
};

function kpi(label, value, detail) {
  const card = el("article", "kpi-card");
  card.append(el("p", "kpi-label", label), el("p", "kpi-value", value));
  if (detail) card.append(el("p", "kpi-sub", detail));
  return card;
}

function renderKpis(summary) {
  const counts = summary.state_counts;
  const toolkit = summary.display_mode !== "scored";
  const top = toolkit ? summary.priority_counts.p1 : summary.band_counts.strong;
  $("kpi-grid").replaceChildren(
    kpi("Tracked jobs", summary.total_jobs, summary.new_jobs + " seen once"),
    kpi(toolkit ? "Apply first" : "Strong matches", top || 0),
    kpi("In progress", (counts.shortlisted || 0) + (counts.applied || 0) + (counts.interview || 0),
        (counts.interview || 0) + " interviews"),
    kpi("Disclosed pay", summary.salary_summary.disclosed_count),
    kpi("Runs", summary.total_runs, summary.profile_count + " profiles"),
  );
}

function filterPill(label, key, token, active, callback, color = "") {
  const item = el("li");
  const pill = button(label, () => callback(key, token), "filter-pill " + color);
  pill.classList.toggle("is-selected", active);
  pill.setAttribute("aria-pressed", String(active));
  item.append(pill);
  return item;
}

function distribution(snapshot, state, callbacks) {
  const summary = snapshot.summary;
  const mixed = summary.display_mode === "mixed";
  const isPriority = summary.display_mode === "toolkit" || (mixed && state.ranking === "priority");
  const key = isPriority ? "priority" : "band";
  const other = isPriority ? "band" : "priority";
  const data = isPriority ? summary.priority_counts : summary.band_counts;
  const metric = isPriority ? "Priority" : "Match Bands";
  $("band-heading").textContent = metric;
  const switcher = $("rank-switch");
  switcher.hidden = !mixed;
  switcher.textContent = isPriority ? "Match bands" : "Priority";
  switcher.onclick = () => callbacks.rank(other);
  const bar = $("band-bar");
  const legend = $("band-legend");
  bar.replaceChildren();
  legend.replaceChildren();
  for (const {key: token, label} of isPriority ? PRIORITIES : BANDS) {
    const count = data[token] || 0;
    const shade = "band-" + token;
    if (count) {
      const seg = button("", () => callbacks.filter(key, token), "segment " + shade);
      seg.style.flexGrow = String(count);
      seg.setAttribute("aria-label", label + ": " + count);
      seg.setAttribute("aria-pressed", String(state.filters[key] === token));
      bar.append(seg);
    }
    legend.append(filterPill(label + " " + count, key, token, state.filters[key] === token, callbacks.filter, shade));
  }
}

function filters(summary, state, callback) {
  $("state-pills").replaceChildren(...STATES.map(({key, label}) =>
    filterPill(label + " " + (summary.state_counts[key] || 0), "reviewState", key,
      state.filters.reviewState === key, callback)));
  const remote = Object.entries(summary.remote_counts || {}).sort((a, b) => b[1] - a[1]);
  $("remote-pills").replaceChildren(...remote.map(([key, count]) =>
    filterPill((remoteLabels[key] || key) + " " + count, "remote", key, state.filters.remote === key, callback)));
}

function companies(snapshot, callback) {
  const list = $("top-companies");
  const entries = snapshot.summary.top_companies || [];
  const marks = new Map(snapshot.jobs.map(job => [job.company, job.monogram]));
  list.replaceChildren();
  for (const data of entries.slice(0, 6)) {
    const item = el("li");
    const row = button("", () => callback("query", data.name), "bar-row-btn");
    const progress = el("progress", "company-progress");
    progress.max = Math.max(1, entries[0].count);
    progress.value = data.count;
    progress.setAttribute("aria-hidden", "true");
    row.append(monogram(marks.get(data.name)), el("span", "bar-label", data.name), progress, el("span", "bar-num", data.count));
    item.append(row);
    list.append(item);
  }
  if (!entries.length) list.append(el("li", "panel-hint", "No employers recorded."));
}

export function renderCharts(snapshot, state, callbacks) {
  renderKpis(snapshot.summary);
  distribution(snapshot, state, callbacks);
  filters(snapshot.summary, state, callbacks.filter);
  companies(snapshot, callbacks.filter);
}

function panel(title) {
  const article = el("article", "panel watch-panel");
  article.append(el("h2", "", title));
  return article;
}

function watchBoards(snapshot, state, actions) {
  const article = panel("Watchlist");
  const list = el("ul", "watch-items");
  for (const entry of snapshot.watchlist || []) {
    const row = el("li");
    const description = [entry.profile_slug, entry.state, entry.added_by, (entry.keeps || 0) + " kept"].join(" \u00b7 ");
    row.append(el("strong", "", entry.company || entry.seed_key), el("span", "job-org", description));
    if (actions.editable() && entry.state !== "blocked") {
      row.append(button("Remove", () => actions.edit("/api/watchlist/remove", {
        profile: entry.profile_slug, seed_key: entry.seed_key,
      }, "Board removed")));
    }
    list.append(row);
  }
  article.append(list.children.length ? list : el("p", "panel-hint", "There are no watched boards."));
  const profiles = snapshot.watch_profiles || [];
  if (actions.editable() && profiles.length) article.append(watchForm(profiles, actions, state.filters.profile));
  return article;
}

function postingPanel(title, jobs, emptyMessage) {
  const article = panel(title);
  const list = el("ul", "watch-items");
  for (const job of jobs) {
    const row = el("li");
    row.append(createExternalLink(job.url, job.title || "Open posting", "job-title-btn"),
               el("span", "job-org", [job.company, job.profile_slug, job.location].filter(Boolean).join(" \u00b7 ")));
    list.append(row);
  }
  article.append(jobs.length ? list : el("p", "panel-hint", emptyMessage));
  return article;
}

function salaries(snapshot) {
  const article = panel("Market salaries");
  const byCurrency = Object.entries(snapshot.summary.salary_summary.by_currency);
  const numbers = new Intl.NumberFormat(undefined, {maximumFractionDigits: 0});
  for (const [currency, value] of byCurrency) {
    const group = el("div", "salary-range");
    group.append(el("strong", "", currency + " annual \u00b7 " + value.count + " roles"));
    const line = el("div", "salary-track");
    const fill = el("span", "salary-band");
    const marker = el("span", "salary-median");
    const max = Math.max(1, value.max);
    fill.style.left = Math.max(0, 100 * value.min / max) + "%";
    marker.style.left = Math.max(0, 100 * value.median / max) + "%";
    line.append(fill, marker);
    group.append(line, el("span", "job-org",
      numbers.format(value.min) + " min \u00b7 " + numbers.format(value.median) + " median \u00b7 " + numbers.format(value.max) + " max"));
    article.append(group);
  }
  if (!byCurrency.length) article.append(el("p", "panel-hint", "No comparable annual disclosures."));
  const disclosed = el("ul", "watch-items");
  for (const job of snapshot.jobs.filter(job => job.salary)) {
    const row = el("li");
    row.append(createExternalLink(job.canonical_url, job.title, "job-title-btn"), el("span", "", job.salary));
    disclosed.append(row);
  }
  article.append(disclosed);
  return article;
}

export function renderWatch(snapshot, state, actions) {
  $("watch-container").replaceChildren(
    watchBoards(snapshot, state, actions),
    postingPanel("Pending postings", snapshot.watch_pending || [], "No postings are awaiting review."),
    postingPanel("Recently closed", snapshot.closed_watch || [], "No recent closures."),
    salaries(snapshot),
  );
}
