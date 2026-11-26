"""Static offline styles and script with a strict SHA-256 script policy."""

import base64
import hashlib

REPORT_CSS = """
:root{color-scheme:light dark;--paper:#f2f4f7;--surface:#fff;--ink:#16202d;--muted:#566275;--line:#d7dde6;
--accent:#1f5fad;--accent-ink:#fff;--soft:#dce8f6;--soft-ink:#0b3d73;--warm:#fbe2c3;--warm-ink:#7a3e00;
--grey:#e4e7ec;--grey-ink:#3a4452;--new:#fff1bf;--new-ink:#5c4300;--ring-track:#e3e8ef;--focus:#c25e00;
--serif:"Iowan Old Style","Charter","Bitstream Charter","Sitka Text",Cambria,Georgia,serif;
--sans:system-ui,-apple-system,"Segoe UI",Roboto,"Noto Sans","Helvetica Neue",Arial,sans-serif}
@media (prefers-color-scheme:dark){:root:not([data-theme=light]){--paper:#0f151e;--surface:#172030;--ink:#e5eaf1;--muted:#9eabbd;
--line:#2b3748;--accent:#86b6f2;--accent-ink:#0b1a2d;--soft:#1d3554;--soft-ink:#cfe2fb;--warm:#4a2f12;
--warm-ink:#ffd6a6;--grey:#2a3240;--grey-ink:#cdd5e0;--new:#4a3d0c;--new-ink:#ffe9a3;--ring-track:#273244;
--focus:#ffb35c}}
*{box-sizing:border-box}
[hidden]{display:none!important}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--paper);color:var(--ink);font:16px/1.5 var(--sans)}
a{color:var(--accent)}
:focus-visible{outline:3px solid var(--focus);outline-offset:2px}
.wrap{max-width:78rem;margin:0 auto;padding:2.5rem 1rem 3rem}
.skip{position:absolute;left:-999px}.skip:focus{left:1rem;top:1rem;background:var(--surface);padding:.5rem}
header.run h1{font:600 clamp(1.9rem,4.5vw,2.8rem)/1.1 var(--serif);margin:0 0 .4rem;letter-spacing:-.01em}
.meta{color:var(--muted);margin:0 0 1.5rem;font-size:.95rem}
.lede{font:1.15rem/1.45 var(--serif);margin:0 0 1rem;max-width:40rem}
.lede strong{font-variant-numeric:tabular-nums}
.bands{margin:0 0 2rem}
.strip{display:flex;height:.7rem;border-radius:.35rem;overflow:hidden;background:var(--ring-track);margin:0 0 .6rem}
.strip button{height:100%;min-width:0;border:0;border-radius:0;padding:0}
.legend{display:flex;flex-wrap:wrap;gap:.35rem 1.1rem;margin:0;padding:0;list-style:none;font-size:.9rem}
.legend li{display:flex;align-items:center;gap:.4rem}
.sw{width:.8rem;height:.8rem;border-radius:.2rem;display:inline-block;border:1px solid var(--line)}
.b-strong{background:var(--accent);color:var(--accent-ink)}
.b-good{background:var(--soft);color:var(--soft-ink)}
.b-borderline{background:var(--warm);color:var(--warm-ink)}
.b-ineligible,.b-none{background:var(--grey);color:var(--grey-ink)}
.tools{display:flex;flex-wrap:wrap;gap:.6rem 1rem;align-items:center;margin:0 0 1rem}
.tools[hidden]{display:none}
.tools input[type=search],.tools select{font:inherit;color:inherit;background:var(--surface);
border:1px solid var(--line);border-radius:.4rem;padding:.4rem .6rem;min-width:0}
.tools input[type=search]{flex:1 1 14rem}
.tools label{display:flex;align-items:center;gap:.4rem}
.count{color:var(--muted);font-size:.9rem;margin-left:auto}
h2.sec{font:600 1.4rem/1.2 var(--serif);margin:2.5rem 0 1rem}
ol.jobs{list-style:none;margin:0;padding:0;counter-reset:none}
.job{display:grid;grid-template-columns:2.6rem minmax(0,1fr) auto;gap:.2rem 1rem;background:var(--surface);
border:1px solid var(--line);border-radius:.6rem;padding:1.1rem 1.1rem 1rem .6rem;margin:0 0 .75rem}
.rank{font:600 1.35rem/1 var(--serif);color:var(--muted);text-align:right;padding-top:.15rem;
font-variant-numeric:tabular-nums}
.job h3{font:600 1.2rem/1.25 var(--serif);margin:0;overflow-wrap:anywhere}
.org{margin:.15rem 0 .5rem;color:var(--muted);overflow-wrap:anywhere}
.org b{color:var(--ink);font-weight:600}
.pills{display:flex;flex-wrap:wrap;gap:.35rem;margin:0 0 .5rem;padding:0;list-style:none}
.pill{font-size:.8rem;line-height:1;padding:.32rem .55rem;border-radius:1rem;font-weight:600}
.pill.plain{background:transparent;color:var(--muted);border:1px solid var(--line);font-weight:500}
.pill.new{background:var(--new);color:var(--new-ink)}
.fit{margin:0 0 .4rem;font-size:.95rem;overflow-wrap:anywhere}
details{margin:.25rem 0 0;font-size:.92rem}
summary{cursor:pointer;color:var(--accent);width:max-content;max-width:100%}
details p,details dl{margin:.35rem 0 .2rem;overflow-wrap:anywhere}
details dt{font-weight:600}details dd{margin:0 0 .3rem}
.side{display:flex;flex-direction:column;align-items:center;gap:.7rem;min-width:6rem}
.ring{--s:0;width:4.6rem;height:4.6rem;border-radius:50%;display:grid;place-items:center;
background:conic-gradient(var(--ring) calc(var(--s)*1%),var(--ring-track) 0)}
.ring span{width:3.6rem;height:3.6rem;border-radius:50%;background:var(--surface);display:grid;place-items:center;
font:600 1.3rem/1 var(--sans);font-variant-numeric:tabular-nums}
.ring small{display:block;font-size:.65rem;font-weight:500;color:var(--muted);margin-top:.1rem}
.r-strong{--ring:var(--accent)}.r-good{--ring:#5b9be0}.r-borderline{--ring:#e08a2c}.r-ineligible,.r-none{--ring:#8a94a3}
.apply{display:inline-block;background:var(--accent);color:var(--accent-ink);text-decoration:none;font-weight:600;
padding:.5rem 1rem;border-radius:.4rem;white-space:nowrap}
.apply:hover{filter:brightness(1.08)}
.nolink{color:var(--muted);font-size:.85rem}
.empty{background:var(--surface);border:1px dashed var(--line);border-radius:.6rem;padding:1.5rem;margin:0}
.discards>details{background:var(--surface);border:1px solid var(--line);border-radius:.5rem;padding:.6rem .9rem;
margin:0 0 .5rem;font-size:1rem}
.discards>details>summary{color:var(--ink);font-weight:600}
.discards ul{margin:.6rem 0 .2rem;padding-left:1.1rem;font-size:.92rem}
.discards li{margin:0 0 .45rem;overflow-wrap:anywhere}
.discards .why{color:var(--muted)}
footer{margin-top:3rem;color:var(--muted);font-size:.85rem}
@media (max-width:40rem){.wrap{padding-top:1.5rem}
.job{grid-template-columns:minmax(0,1fr);padding:1rem}
.rank{text-align:left;font-size:1rem}
.side{flex-direction:row;justify-content:space-between;min-width:0;margin-top:.5rem}}
@media (prefers-reduced-motion:reduce){*{transition:none!important}}
@media print{:root{--paper:#fff;--surface:#fff;--ink:#000;--muted:#333;--line:#999}
body{font-size:11pt}.tools,.skip{display:none!important}.wrap{max-width:none;padding:0}
.job,.discards>details{break-inside:avoid;box-shadow:none}
.apply{background:none;color:#000;padding:0;text-decoration:underline}
.apply::after{content:" (" attr(href) ")";font-weight:400;font-size:9pt;overflow-wrap:anywhere}
.ring{-webkit-print-color-adjust:exact;print-color-adjust:exact}}

:root[data-theme=dark]{color-scheme:dark;--paper:#0f151e;--surface:#172030;--ink:#e5eaf1;
--muted:#abb7c9;--line:#3a4657;--accent:#86b6f2;--accent-ink:#0b1a2d;--soft:#1d3554;--soft-ink:#cfe2fb;
--warm:#4a2f12;--warm-ink:#ffd6a6;--grey:#2a3240;--grey-ink:#cdd5e0;--new:#4a3d0c;--new-ink:#ffe9a3;
--ring-track:#273244;--focus:#ffb35c}
:root[data-theme=light]{color-scheme:light}
button,input,textarea,select{font:inherit;color:inherit}
button:not(:disabled){cursor:pointer}
:root:not(.enhanced) .needs-js{display:none}
.chip,.tag,.triage button,.actions button{background:var(--surface);border:1px solid var(--line);
border-radius:.4rem;padding:.3rem .65rem;line-height:1.35}
[aria-pressed=true].chip,[aria-pressed=true].tag,.triage button[aria-pressed=true]{
background:var(--accent);color:var(--accent-ink);border-color:var(--accent)}
[aria-pressed=true].segment{outline:2px solid var(--ink);outline-offset:-2px}
.segment:focus-visible{outline-offset:-3px}
.kpis{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));margin:1.8rem 0 1.25rem;padding:0;
border-block:1px solid var(--line);font-variant-numeric:tabular-nums}
.kpi{padding:.9rem 1rem;border-inline-start:1px solid var(--line);min-width:0}
.kpi:first-child{border-inline-start:0}.kpi dt,.kpi small{color:var(--muted);font-size:.82rem}
.kpi dd{margin:.2rem 0 0;font:600 2rem/1 var(--serif)}.kpi .new{background:var(--new);color:var(--new-ink);
display:inline-block;border-radius:.35rem;padding:.1rem .4rem}.kpi small{display:block;margin-top:.4rem}
.bands>p{margin:0 0 .4rem;font-weight:650}
.legend button{font-size:.82rem;display:inline-flex;align-items:center;gap:.4rem}
.tools{padding:.85rem 0;border-block:1px solid var(--line);align-items:baseline}
.tools .quick{flex-basis:100%;display:flex;flex-wrap:wrap;gap:.35rem}
.tools input[type=search]{width:min(100%,20rem)}.count{font-variant-numeric:tabular-nums}
.avatar{width:2.65rem;height:2.65rem;border-radius:.6rem;display:grid;place-items:center;flex:none;
background:var(--soft);color:var(--soft-ink);font-weight:750;letter-spacing:.04em}
.tone-1,.tone-5{background:var(--warm);color:var(--warm-ink)}
.tone-2,.tone-6{background:var(--grey);color:var(--grey-ink)}
.tone-3,.tone-7{background:var(--new);color:var(--new-ink)}
.tone-4{background:var(--accent);color:var(--accent-ink)}
.job-title{display:flex;gap:.75rem;min-width:0;align-items:flex-start}
.rank{font-variant-numeric:tabular-nums}
.tags,.triage,.actions,.toggle{display:flex;gap:.35rem;align-items:center;flex-wrap:wrap}
.tags{margin:.45rem 0}.tag{background:var(--paper);font-size:.8rem}
.triage{margin:.5rem 0 0}.triage button{font-size:.8rem}.triage details{margin:0}.triage input{width:min(100%,20rem)}
.triage input,#commands{color:var(--ink);background:var(--surface);border:1px solid var(--line);
border-radius:.35rem;padding:.4rem .55rem}.dismissed{opacity:.66}
.priority{padding:.42rem .7rem;white-space:nowrap;border-radius:.45rem;font-weight:700;font-size:.9rem}
.salary{font-size:.95rem;max-width:13rem;text-align:right;overflow-wrap:anywhere}
.side{align-items:flex-end}.side .apply{margin-top:.1rem}
.compact{overflow:auto;padding:.5rem;border:1px solid var(--line);border-radius:.5rem;background:var(--surface)}
table{width:100%;border-collapse:collapse;font-size:.87rem}td,th{text-align:left;padding:.6rem .65rem;
border-bottom:1px solid var(--line);vertical-align:top;min-width:5rem}
thead th{background:var(--soft);color:var(--soft-ink)}tbody th{min-width:3rem}
td:nth-child(4){min-width:12rem}.compact .triage{margin:0}.compact .apply{padding:.3rem .55rem}
dialog{width:min(44rem,calc(100% - 2rem));color:var(--ink);background:var(--surface);
border:1px solid var(--line);border-radius:.7rem;padding:1rem 1.25rem}
dialog::backdrop{background:#0f172a9e}dialog h2{margin:0 0 .5rem}
#commands{display:block;width:100%;margin:.65rem 0;min-height:10rem;resize:vertical}
@media(max-width:40rem){.kpis{grid-template-columns:repeat(2,minmax(0,1fr));margin:.9rem 0}
.kpi{padding:.55rem .7rem}.kpi dd{font-size:1.55rem}.kpi small{margin-top:.25rem}
.meta{margin-bottom:.8rem}.bands{margin-bottom:.85rem}.sec{margin:.75rem 0 .5rem}
.tools{gap:.4rem .6rem;padding:.55rem 0}.kpi:nth-child(3){border-inline-start:0}
.kpi:nth-child(n+3){border-top:1px solid var(--line)}.side{flex-wrap:wrap;align-items:center}.salary{text-align:left}
.tools input[type=search]{width:100%}}
@media print{.tools,.triage,dialog,.compact,.skip,.strip{display:none!important}
body .jobs[hidden]{display:block!important}.tag{border:0;padding:0;font:inherit}.job.dismissed{opacity:1}
.kpi dd{font-size:18pt}.b-strong,.b-good,.b-borderline,.b-ineligible,.b-none{color:#111;background:#fff}
.avatar{display:none}.apply{white-space:normal}.side{min-width:8rem}}
"""



REPORT_JS = r"""(() => {
  const get = id => document.getElementById(id);
  const tools = get('tools');
  if (!tools) return;
  document.documentElement.classList.add('enhanced');
  tools.hidden = false;
  const cards = get('jobs');
  const table = get('job-rows');
  const entries = Array.from(cards.children, (card, index) => ({card, row: table.children[index]}));
  const byId = new Map(entries.map(item => [item.card.dataset.id, item.card]));
  const rawStorageKey = 'ultron:html-triage:' + tools.dataset.run;
  const filters = new Map();
  const triage = new Map();
  const states = new Set(['shortlisted', 'applied', 'rejected']);
  const cleanNote = value => typeof value === 'string' ? value.replace(/[\p{Cc}\p{Cf}]/gu, ' ').slice(0, 500) : '';

  function read(key) {
    try { return localStorage.getItem(key); } catch (_) { return null; }
  }

  function write(key, value) {
    try { localStorage.setItem(key, value); return true; } catch (_) { return false; }
  }

  function restoreTriage() {
    try {
      const saved = JSON.parse(read(rawStorageKey) || '{}');
      for (const [id, value] of Object.entries(saved)) {
        if (!id || !byId.has(id) || !value || typeof value !== 'object') continue;
        const state = states.has(value.state) ? value.state : '';
        const note = cleanNote(value.note);
        if (state || note.trim()) triage.set(id, {state, note});
      }
    } catch (_) {
      // Invalid or inaccessible storage is equivalent to an empty report.
    }
  }

  function renderTriage(element, value) {
    const state = value.state || '';
    element.classList.toggle('dismissed', state === 'rejected');
    for (const button of element.querySelectorAll('[data-state]')) {
      button.setAttribute('aria-pressed', String(button.dataset.state === state));
    }
    const note = element.querySelector('[data-note]');
    if (note && note.value !== (value.note || '')) note.value = value.note || '';
  }

  function matches(data) {
    const query = get('q').value.trim().toLowerCase();
    const state = triage.get(data.id)?.state || '';
    if (query && !data.search.includes(query)) return false;
    if (get('hide-dismissed').checked && state === 'rejected') return false;
    for (const [key, value] of filters) {
      if (key === 'tag' && !data.tags.split('\n').includes(value)) return false;
      if (key === 'state' && state !== value) return false;
      if (!['tag', 'state'].includes(key) && data[key] !== value) return false;
    }
    return true;
  }

  function compare(a, b, key) {
    if (key === 'priority') return (Number(a.priority.slice(1)) || 4) - (Number(b.priority.slice(1)) || 4);
    if (key === 'score') return Number(b.score) - Number(a.score);
    if (key === 'date') return b.date.localeCompare(a.date);
    if (key === 'company') return a.company.localeCompare(b.company);
    if (key === 'salary') {
      if (!a.salaryMid || !b.salaryMid) return Number(Boolean(b.salaryMid)) - Number(Boolean(a.salaryMid));
      return a.currency.localeCompare(b.currency) || Number(b.salaryMid) - Number(a.salaryMid);
    }
    return 0;
  }

  function markFilters() {
    for (const button of document.querySelectorAll('[data-filter]')) {
      const {filter, value} = button.dataset;
      const on = filter === 'all' ? filters.size === 0 : filters.get(filter) === value;
      button.setAttribute('aria-pressed', String(on));
    }
  }

  function keepOrder(container, elements) {
    for (let index = 0; index < elements.length; index++) {
      if (container.children[index] !== elements[index]) container.insertBefore(elements[index], container.children[index]);
    }
  }

  function refresh() {
    const key = get('sort').value;
    let visible = 0;
    entries.sort((left, right) => {
      const a = left.card.dataset;
      const b = right.card.dataset;
      return compare(a, b, key) || Number(a.rank) - Number(b.rank);
    });
    keepOrder(cards, entries.map(item => item.card));
    keepOrder(table, entries.map(item => item.row));
    for (const {card, row} of entries) {
      const data = card.dataset;
      const show = matches(data);
      if (show) visible++;
      card.hidden = row.hidden = !show;
      const value = triage.get(data.id) || {};
      renderTriage(card, value);
      renderTriage(row, value);
    }
    markFilters();
    get('count').textContent = visible + ' of ' + entries.length + ' shown';
    get('no-matches').hidden = visible !== 0;
    get('show-sync').textContent = 'Export triage (' + triage.size + ')';
  }

  function saveTriage(element, changes) {
    const id = element?.dataset.id;
    if (!id) return;
    const value = {...triage.get(id), ...changes};
    if (value.state || value.note?.trim()) triage.set(id, value);
    else triage.delete(id);
    persist();
  }

  function persist() {
    const saved = write(rawStorageKey, JSON.stringify(Object.fromEntries(triage)));
    get('storage-status').textContent = saved ? '' : 'Browser storage is unavailable. Copy your triage before closing.';
  }

  function setView(view) {
    cards.hidden = view === 'table';
    get('job-table').hidden = view !== 'table';
    for (const button of tools.querySelectorAll('[data-view]')) {
      button.setAttribute('aria-pressed', String(button.dataset.view === view));
    }
    refresh();
  }

  function applyTheme(theme) {
    document.documentElement.dataset.theme = theme;
    get('theme').value = theme;
  }

  function shellQuote(value, shell) {
    // PowerShell also treats smart single quotation marks as quote characters.
    const literal = shell === 'powershell'
      ? value.replace(/['\u2018\u2019\u201a\u201b]/g, mark => mark + mark)
      : value.replace(/'/g, "'\"'\"'");
    return "'" + literal + "'";
  }

  function syncCommands() {
    const shell = get('command-shell').value;
    const quote = value => shellQuote(value, shell);
    const commands = [];
    for (const [id, {state, note}] of triage) {
      if (state) commands.push('ultron jobs mark -- ' + quote(id) + ' ' + quote(state));
      if (note?.trim()) commands.push('ultron jobs note -- ' + quote(id) + ' ' + quote(note));
    }
    get('commands').value = commands.join('\n');
    get('copy').disabled = !commands.length;
    get('sync-status').textContent = commands.length ? '' : 'No triage to export yet.';
  }

  function changeFilter(button) {
    const {filter, value} = button.dataset;
    if (filter === 'all') {
      filters.clear();
      get('q').value = '';
      get('hide-dismissed').checked = false;
    } else if (filters.get(filter) === value) filters.delete(filter);
    else filters.set(filter, value);
    refresh();
  }

  document.addEventListener('click', event => {
    const button = event.target.closest('button');
    if (!button) return;
    if (button.dataset.filter) changeFilter(button);
    else if (button.dataset.view) setView(button.dataset.view);
    else if (button.dataset.state) {
      const item = button.closest('[data-id]');
      const state = triage.get(item.dataset.id)?.state === button.dataset.state ? '' : button.dataset.state;
      saveTriage(item, {state});
      refresh();
    }
  });

  document.addEventListener('input', event => {
    if (!event.target.matches('[data-note]')) return;
    const item = event.target.closest('[data-id]');
    const note = cleanNote(event.target.value);
    saveTriage(item, {note});
    get('show-sync').textContent = 'Export triage (' + triage.size + ')';
    const pair = entries.find(({card}) => card.dataset.id === item.dataset.id);
    if (pair) renderTriage(pair.card === item ? pair.row : pair.card, triage.get(item.dataset.id) || {});
  });

  get('q').addEventListener('input', refresh);
  get('sort').addEventListener('change', refresh);
  get('hide-dismissed').addEventListener('change', refresh);
  get('theme').addEventListener('change', event => {
    applyTheme(event.target.value);
    write('ultron:html-theme', event.target.value);
  });
  get('show-sync').addEventListener('click', () => {
    syncCommands();
    get('sync').showModal();
  });
  get('close-sync').addEventListener('click', () => get('sync').close());
  get('command-shell').addEventListener('change', syncCommands);
  get('copy').addEventListener('click', async () => {
    try {
      await navigator.clipboard.writeText(get('commands').value);
      get('sync-status').textContent = 'Commands copied.';
    } catch (_) {
      get('commands').focus();
      get('commands').select();
      get('sync-status').textContent = 'Commands selected. Use your browser copy shortcut.';
    }
  });
  get('reset').addEventListener('click', () => {
    if (!confirm('Clear local triage and notes for this report?')) return;
    triage.clear();
    persist();
    refresh();
    syncCommands();
  });

  const theme = read('ultron:html-theme');
  applyTheme(theme === 'dark' || theme === 'light' ? theme : 'system');
  restoreTriage();
  refresh();
})();"""

_JS_HASH = "sha256-" + base64.b64encode(hashlib.sha256(REPORT_JS.encode("utf-8")).digest()).decode("ascii")
CSP = (
    "default-src 'none'; style-src 'unsafe-inline'; "
    f"script-src '{_JS_HASH}'; img-src 'none'; base-uri 'none'; form-action 'none'"
)
