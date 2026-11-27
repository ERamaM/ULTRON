import {$, button, createExternalLink, createScoreRing, el, formatDate, monogram, safeHttpUrl} from "./dom.js";
import {quickActions, reviewForms, stateSelect} from "./actions.js";

const priorityLabels = {1: "P1 \u00b7 Apply first", 2: "P2 \u00b7 Strong", 3: "P3 \u00b7 Backup"};

function priority(job) {
  return Number.isInteger(job.priority) && Object.hasOwn(priorityLabels, job.priority)
    ? el("span", "priority-badge priority-" + job.priority, priorityLabels[job.priority])
    : null;
}

function jobPills(job, context) {
  const list = el("ul", "pill-list");
  const add = (text, shade = "") => {
    if (!text) return;
    const item = el("li");
    item.append(el("span", "pill " + shade, text));
    list.append(item);
  };
  if (job.match_score !== null) add(job.match_band, "band-" + job.match_band);
  add(job.review_state, "pill-review-state");
  if (context.isNew(job)) add("New", "pill-new");
  if (job.is_closed_watch) add("Closed", "pill-warning");
  add(job.remote_label);
  add(job.best_date?.label);
  if (job.eligibility) add("Eligibility: " + job.eligibility);
  return list;
}

function fitTags(job, context) {
  const box = el("div", "fit-tags");
  for (const signal of job.fit_signals || []) {
    const tag = button(signal, () => context.filter("tag", signal), "filter-pill");
    const active = signal === context.tag;
    tag.classList.toggle("is-selected", active);
    tag.setAttribute("aria-pressed", String(active));
    box.append(tag);
  }
  return box;
}

function selection(node, job, context) {
  node.dataset.jobKey = job.identity_key;
  node.classList.toggle("is-current", context.selected === job.identity_key);
}

export function renderCards(container, jobs, context) {
  const list = el("ul", "jobs-list");
  for (const job of jobs) {
    const card = el("li", "job-card");
    selection(card, job, context);
    const body = el("div");
    const head = el("div", "job-heading");
    const title = el("div");
    title.append(button(job.title, () => context.open(job), "job-title-btn"),
                 el("p", "job-org", [job.company || "Unknown company", job.location].filter(Boolean).join(" \u00b7 ")));
    head.append(monogram(job.monogram), title);
    body.append(head, jobPills(job, context), fitTags(job, context));
    if (context.actions.editable()) body.append(quickActions(job, context.actions));
    const side = el("div", "job-side");
    if (job.match_score !== null) side.append(createScoreRing(job.match_score));
    else {
      const badge = priority(job);
      if (badge) side.append(badge);
    }
    if (job.salary) side.append(el("strong", "salary-callout", job.salary));
    side.append(createExternalLink(job.canonical_url, "Open \u2197", "btn btn-primary"));
    card.append(body, side);
    list.append(card);
  }
  container.append(list);
}

export function renderTable(container, jobs, context) {
  const wrap = el("div", "table-wrap");
  wrap.tabIndex = 0;
  wrap.setAttribute("aria-label", "Jobs table");
  const table = el("table", "data-table");
  const head = el("thead");
  const headings = el("tr");
  const columns = [
    ["Priority", "priority"], ["Score", "score"], ["Title", "title"], ["Company", "company"],
    ["Status", "status"], ["Salary", "salary"], ["Posted", "posted"], ["Remote", "remote"], ["Open", ""],
  ];
  for (const [label, sort] of columns) {
    const cell = el("th");
    cell.scope = "col";
    if (sort) {
      cell.append(button(label, () => context.sortBy(sort), "job-title-btn"));
      if (context.sort === sort) cell.setAttribute("aria-sort", context.reverse ? "descending" : "ascending");
    } else cell.textContent = label;
    headings.append(cell);
  }
  head.append(headings);
  const body = el("tbody");
  for (const job of jobs) {
    const tr = el("tr");
    selection(tr, job, context);
    const cells = [
      priority(job) || "", job.match_score ?? "", button(job.title, () => context.open(job), "job-title-btn"),
      job.company || "Unknown", context.actions.editable() ? stateSelect(job, context.actions) : job.review_state,
      job.salary, job.best_date?.iso || "", job.remote_label,
      createExternalLink(job.canonical_url, "Open \u2197", "btn btn-secondary"),
    ];
    for (const value of cells) {
      const td = el("td");
      if (value instanceof Node) td.append(value);
      else td.textContent = value ?? "";
      tr.append(td);
    }
    body.append(tr);
  }
  table.append(head, body);
  wrap.append(table);
  container.append(wrap);
}

function detailDates(job) {
  const rows = [
    ["Published", job.published_at], ["Updated", job.updated_at],
    ["Unverified source date", job.unverified_date], ["First seen", job.first_seen], ["Last seen", job.last_seen],
  ];
  return rows.filter(([, value]) => value).map(([label, value]) => el("p", "job-org", label + ": " + formatDate(value)));
}

export function showDetails(job, context) {
  const drawer = $("detail-drawer");
  drawer.dataset.jobKey = job.identity_key;
  const body = $("drawer-body");
  $("drawer-company").textContent = [job.company, job.location].filter(Boolean).join(" \u00b7 ");
  $("drawer-title").textContent = job.title;
  body.replaceChildren();

  const overview = el("section", "drawer-section");
  overview.append(el("h3", "", "Overview"), jobPills(job, context), ...detailDates(job));
  if (job.salary) overview.append(el("p", "salary-callout", job.salary));
  if (job.eligibility_evidence) overview.append(el("p", "", job.eligibility_evidence));
  overview.append(fitTags(job, context), createExternalLink(job.canonical_url, "Open posting \u2197", "btn btn-primary"));
  body.append(overview);

  if (job.notes || job.feedback_relevance) {
    const feedback = el("section", "drawer-section");
    feedback.append(el("h3", "", "Notes & Feedback"));
    if (job.notes) feedback.append(el("pre", "notes-pre", job.notes));
    if (job.feedback_relevance) feedback.append(el("p", "", "Feedback: " + job.feedback_relevance));
    if (job.feedback_note) feedback.append(el("p", "notes-pre", job.feedback_note));
    body.append(feedback);
  }
  if (context.actions.editable()) {
    body.append(reviewForms(job, context.actions, context.profile || job.profile_slug, () => context.reopen(job.identity_key)));
  }

  const history = el("section", "drawer-section");
  const occurrences = job.occurrences || [];
  history.append(el("h3", "", "Run History (" + occurrences.length + ")"));
  for (const occ of occurrences) history.append(el("p", "mono", occ.run_id + (occ.is_new ? " (new)" : "")));
  history.append(el("p", "mono", "Key: " + job.identity_key));
  body.append(history);
  if (!drawer.open) drawer.showModal();
}

export function updateDrawerStatus(job) {
  const drawer = $("detail-drawer");
  if (!job || !drawer.open || drawer.dataset.jobKey !== job.identity_key) return;
  const body = $("drawer-body");
  const badge = body.querySelector(".pill-review-state");
  const select = body.querySelector(".state-select");
  if (badge) badge.textContent = job.review_state;
  if (select) select.value = job.review_state;
}

export function closeDetails() {
  if ($("detail-drawer").open) $("detail-drawer").close();
}

export function csvCell(value) {
  let text = String(value ?? "").replace(/[\p{Cc}\p{Cf}]/gu, "").trimStart();
  if (/^[=+\-@]/.test(text)) text = "'" + text;
  return '"' + text.replace(/"/g, '""') + '"';
}

export function downloadCsv(jobs) {
  const columns = [
    ["Priority", "priority"], ["Score", "match_score"], ["Title", "title"], ["Company", "company"],
    ["Location", "location"], ["Status", "review_state"], ["Salary", "salary"], ["Remote", "remote_label"],
    ["Posted", job => job.best_date?.iso], ["URL", job => safeHttpUrl(job.canonical_url)],
  ];
  const lines = [columns.map(([label]) => csvCell(label)).join(",")];
  for (const job of jobs) {
    lines.push(columns.map(([, key]) => csvCell(typeof key === "function" ? key(job) : job[key])).join(","));
  }
  const url = URL.createObjectURL(new Blob(["\uFEFF", lines.join("\r\n"), "\r\n"], {type: "text/csv;charset=utf-8"}));
  const link = el("a");
  link.href = url;
  link.download = "ultron-jobs.csv";
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

export async function copyUrls(jobs, notify) {
  const urls = [...new Set(jobs.map(job => safeHttpUrl(job.canonical_url)).filter(Boolean))];
  if (!urls.length) {
    notify("No job URLs to copy.");
    return;
  }
  try {
    await navigator.clipboard.writeText(urls.join("\n"));
    notify(urls.length + " URLs copied");
  } catch {
    const box = $("url-list");
    box.value = urls.join("\n");
    $("copy-dialog").showModal();
    box.select();
  }
}
