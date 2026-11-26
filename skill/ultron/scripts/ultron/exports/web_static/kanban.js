import {STATES, button, el} from "./dom.js";
import {stateSelect} from "./actions.js";

export function renderKanban(container, jobs, context) {
  const board = el("div", "kanban");
  board.setAttribute("aria-label", "Review pipeline");
  const lanes = STATES.filter(state => state.key !== "archived" || jobs.some(job => job.review_state === "archived"));
  const byId = new Map(jobs.map(job => [job.identity_key, job]));
  for (const {key, label} of lanes) {
    const column = el("section", "lane");
    const rows = jobs.filter(job => job.review_state === key);
    column.append(el("h3", "lane-title", label + " (" + rows.length + ")"));
    for (const job of rows) column.append(kanbanCard(job, context));
    if (context.actions.editable()) {
      column.addEventListener("dragover", event => {
        event.preventDefault();
        event.dataTransfer.dropEffect = "move";
      });
      column.addEventListener("drop", event => {
        event.preventDefault();
        const job = byId.get(event.dataTransfer.getData("application/x-ultron-job"));
        if (job) context.actions.setState(job, key);
      });
    }
    board.append(column);
  }
  container.append(board);
}

function kanbanCard(job, context) {
  const card = el("article", "lane-card");
  card.dataset.jobKey = job.identity_key;
  if (context.selected === job.identity_key) card.classList.add("is-current");
  card.append(button(job.title, () => context.open(job), "job-title-btn"),
              el("p", "job-org", job.company || "Unknown company"));
  if (job.priority) card.append(el("span", "pill", "P" + job.priority));
  if (job.salary) card.append(el("p", "job-org", job.salary));
  if (context.actions.editable()) {
    card.draggable = true;
    card.addEventListener("dragstart", event => {
      event.dataTransfer.setData("application/x-ultron-job", job.identity_key);
      event.dataTransfer.effectAllowed = "move";
    });
    card.append(stateSelect(job, context.actions));
  }
  return card;
}
