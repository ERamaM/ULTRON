"""Offline tests for browser interactions using the shipped JavaScript modules."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from ultron.exports import web_data


def _run_js(script: str) -> None:
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required to execute the viewer's JavaScript")
    assets = Path(web_data.__file__).with_name("web_static")
    result = subprocess.run(
        [node, "--input-type=module", "-", str(assets)], input=_HARNESS + script,
        capture_output=True, text=True, check=False, timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr


_HARNESS = r"""
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import vm from "node:vm";

class Element {
  constructor(className = "", text = "") {
    this.className = className;
    this.textContent = text;
    this.children = [];
    this.dataset = {};
    this.open = false;
  }
  append(...nodes) { this.children.push(...nodes); }
  appendChild(node) { this.append(node); return node; }
  replaceChildren() { this.children = []; }
  setAttribute() {}
  addEventListener() {}
  showModal() { this.open = true; }
  querySelectorAll(selector) {
    const matches = node => selector.startsWith(".") && node.className.split(" ").includes(selector.slice(1));
    return this.children.flatMap(node => [...(matches(node) ? [node] : []), ...node.querySelectorAll(selector)]);
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0]; }
}
const nodes = new Map();
const context = vm.createContext({
  assert,
  $: key => {
    if (!nodes.has(key)) nodes.set(key, new Element());
    return nodes.get(key);
  },
  el: (tag, style, text) => new Element(style, text),
  button: text => new Element("", text),
  createExternalLink: () => new Element(),
  renderCharts: () => {},
  setTimeout: () => 0,
  clearTimeout: () => {},
  STATES: ["unreviewed", "shortlisted", "applied"].map(key => ({key, label: key})),
});
nodes.set("detail-drawer", new Element());
nodes.set("drawer-body", nodes.get("detail-drawer").appendChild(new Element()));
function load(name) {
  const source = fs.readFileSync(path.join(process.argv[2], name), "utf8")
    .replace(/^import .+\n/gm, "").replace(/^export /gm, "");
  vm.runInContext(source.split("const narrowLayout =")[0], context, {filename: name});
}
load("actions.js");
load("views.js");
load("app.js");
"""


def test_profile_and_run_filters_use_the_same_occurrence():
    _run_js(r"""
vm.runInContext(`
  const job = {
    profile_slug: "later",
    run_ids: ["first", "second"],
    occurrences: [{run_id: "first", profile_slug: "earlier"}, {run_id: "second", profile_slug: "later"}],
  };
  state.snapshot = {jobs: [job]};
  state.filters.profile = "earlier";
  state.filters.run = "first";
  assert.equal(filteredJobs().length, 1);
  state.filters.run = "";
  assert.equal(filteredJobs().length, 1);
  state.filters.profile = "later";
  state.filters.run = "first";
  assert.equal(filteredJobs().length, 0);
`, context);
""")


def test_drawer_shows_committed_status_after_success_and_rejection():
    _run_js(r"""
await vm.runInContext(`(async () => {
  token = "synthetic";
  renderJobs = () => {};
  const job = {identity_key: "synthetic", title: "Engineer", company: "Demo", location: "Spain",
    match_score: null, fit_signals: [], review_state: "unreviewed"};
  state.snapshot = {jobs: [job], meta: {editable: true}, summary: {state_counts: {}}};
  openJob(job);
  const select = () => $("drawer-body").querySelector(".state-select");
  const pills = () => $("drawer-body").querySelectorAll(".pill").map(pill => pill.textContent);
  fetch = async () => ({ok: true, json: async () => ({job: {review_state: "shortlisted"}, state_counts: {}})});
  select().value = "shortlisted";
  await actions.setState(job, "shortlisted");
  assert.ok(pills().includes("shortlisted"));
  assert.equal(select().value, "shortlisted");
  fetch = async () => ({ok: false, json: async () => ({})});
  select().value = "applied";
  await actions.setState(job, "applied");
  assert.ok(pills().includes("shortlisted"));
  assert.equal(select().value, "shortlisted");
  assert.match($("toast-text").textContent, /could not save/);
  assert.equal($("detail-drawer").open, true);
})()`, context);
""")
