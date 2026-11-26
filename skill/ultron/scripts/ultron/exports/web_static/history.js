import {clearNode, createExternalLink, el, formatDate} from "./dom.js";

export function renderRuns(snapshot, showRun) {
  const container = document.getElementById("runs-container");
  clearNode(container);
  const items = snapshot?.runs || [];
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
      showRun(run.run_id);
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

export function renderSeeds(snapshot) {
  const container = document.getElementById("seeds-container");
  clearNode(container);
  const items = snapshot?.seed_yields || [];
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

export function renderOps(snapshot) {
  const container = document.getElementById("ops-container");
  clearNode(container);
  const stack = el("div", "cards-stack");

  const saved = snapshot?.saved_searches || [];
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

  const diags = snapshot?.diagnostics || [];
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
