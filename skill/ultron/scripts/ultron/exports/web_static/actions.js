import {$, STATES, button, el} from "./dom.js";

let token;
let pairing;

async function redeemLaunch(access) {
  try {
    const response = await fetch("/", {headers: {"X-ULTRON-Launch": access}, cache: "no-store"});
    if (!response.ok) return;
    const page = new DOMParser().parseFromString(await response.text(), "text/html");
    const params = new URLSearchParams(location.hash.slice(1));
    if (params.get("access") !== access) return;
    token = page.querySelector('meta[name="ultron-csrf"]')?.content;
    if (!token) return;
    params.delete("access");
    const hash = params.toString();
    history.replaceState(null, "", location.pathname + location.search + (hash ? "#" + hash : ""));
  } catch {
    // Access remains read-only when the launch cannot be paired.
  }
}

export async function pairLaunch() {
  if (pairing) await pairing;
  if (token) return;
  const access = new URLSearchParams(location.hash.slice(1)).get("access");
  if (!access) return;
  pairing = redeemLaunch(access);
  try {
    await pairing;
  } finally {
    pairing = undefined;
  }
}

async function post(path, body) {
  if (!token) throw new Error("This ledger is read-only.");
  const reply = await fetch(path, {
    method: "POST",
    headers: {"Content-Type": "application/json", "X-ULTRON-Token": token},
    body: JSON.stringify(body),
    cache: "no-store",
  });
  if (!reply.ok) {
    const codes = {
      forbidden: "This edit belongs to another session. Open the ledger from its launch URL.",
      profile_not_found: "The selected profile is unavailable.",
      invalid_board: "Enter a supported ATS board URL or vendor:board key.",
      job_not_found: "The job is no longer in the ledger.",
    };
    const data = await reply.json().catch(() => ({}));
    throw new Error(Object.hasOwn(codes, data.error) ? codes[data.error] : "The ledger could not save this change.");
  }
  return reply.json();
}

export function createActions({getSnapshot, getJob, redraw, refresh}) {
  let busy = false;
  let last = null;
  let timer;

  function notify(message, allowUndo = false) {
    clearTimeout(timer);
    const toast = $("toast");
    $("toast-text").textContent = message;
    $("undo-btn").hidden = !allowUndo;
    toast.hidden = false;
    timer = setTimeout(() => {
      toast.hidden = true;
      last = null;
    }, 5000);
  }

  const editable = () => Boolean(token && getSnapshot()?.meta?.editable);

  async function persistState(job, next, allowUndo) {
    if (!editable() || !job || busy || job.review_state === next) return;
    const previous = job.review_state;
    const identity = job.identity_key;
    busy = true;
    job.review_state = next;
    redraw();
    try {
      const result = await post("/api/jobs/state", {identity_key: identity, state: next});
      Object.assign(getJob(identity) || job, result.job);
      Object.assign(getSnapshot().summary.state_counts, result.state_counts);
      last = allowUndo && result.job.review_state === next ? {identity, previous, next} : null;
      notify("Status saved", Boolean(last));
    } catch (error) {
      (getJob(identity) || job).review_state = previous;
      last = null;
      notify(error.message);
    } finally {
      busy = false;
      redraw();
    }
  }

  function setState(job, next) {
    return persistState(job, next, true);
  }

  function undo() {
    if (!last || busy) return;
    const {identity, previous, next} = last;
    const current = getJob(identity);
    last = null;
    if (current?.review_state === next) void persistState(current, previous, false);
    else notify("The status has already changed.");
  }

  async function edit(path, fields, success, after) {
    if (!editable() || busy) return;
    busy = true;
    try {
      const result = await post(path, fields);
      await refresh();
      if (after) after(result);
      notify(success);
    } catch (error) {
      notify(error.message);
    } finally {
      busy = false;
    }
  }

  return {editable, setState, undo, notify, edit};
}

export function stateSelect(job, actions) {
  const select = el("select", "state-select");
  select.setAttribute("aria-label", "Review status for " + job.title);
  for (const {key, label} of STATES) {
    select.appendChild(el("option", "", label)).value = key;
  }
  select.value = job.review_state;
  select.addEventListener("change", () => actions.setState(job, select.value));
  return select;
}

export function quickActions(job, actions) {
  const wrap = el("div", "quick-actions");
  for (const [key, label] of [["shortlisted", "Shortlist"], ["applied", "Applied"],
                              ["interview", "Interview"], ["rejected", "Reject"]]) {
    const item = button(label, () => actions.setState(job, key), "btn btn-ghost");
    const selected = job.review_state === key;
    item.setAttribute("aria-pressed", String(selected));
    item.disabled = selected;
    wrap.append(item);
  }
  return wrap;
}

export function reviewForms(job, actions, profile, after) {
  const wrap = el("section", "drawer-section");
  wrap.append(el("h3", "", "Edit notes and feedback"));
  const note = el("textarea");
  note.rows = 2;
  note.maxLength = 2000;
  note.setAttribute("aria-label", "New note");
  const noteButton = button("Add note", () => {
    if (!note.value.trim()) return;
    actions.edit("/api/jobs/notes", {identity_key: job.identity_key, text: note.value}, "Note saved", after);
  });
  const feedback = el("div", "quick-actions");
  for (const [value, label] of [["relevant", "Relevant"], ["uncertain", "Uncertain"], ["not_relevant", "Not relevant"]]) {
    const buttonNode = button(label, () => actions.edit("/api/jobs/feedback", {
      identity_key: job.identity_key, relevance: value,
      ...(profile ? {profile} : {}),
    }, "Feedback saved", after));
    buttonNode.setAttribute("aria-pressed", String(job.feedback_relevance === value));
    feedback.append(buttonNode);
  }
  wrap.append(stateSelect(job, actions), note, noteButton, feedback);
  return wrap;
}

export function watchForm(profiles, actions, selectedProfile) {
  const form = el("form", "watch-form");
  const profile = el("select");
  profile.setAttribute("aria-label", "Watch profile");
  for (const slug of profiles) profile.appendChild(el("option", "", slug)).value = slug;
  if (profiles.includes(selectedProfile)) profile.value = selectedProfile;
  const board = el("input");
  board.required = true;
  board.maxLength = 1024;
  board.placeholder = "ATS board URL or vendor:board";
  board.setAttribute("aria-label", "Board");
  const add = el("button", "btn btn-secondary", "Add board");
  add.type = "submit";
  form.addEventListener("submit", event => {
    event.preventDefault();
    if (!board.value.trim()) return;
    actions.edit("/api/watchlist/add", {profile: profile.value, board: board.value}, "Board added");
  });
  form.append(profile, board, add);
  return form;
}
