export const BANDS = [
  { key: "strong", label: "Strong" },
  { key: "good", label: "Good" },
  { key: "borderline", label: "Borderline" },
  { key: "ineligible", label: "Ineligible" },
  { key: "unscored", label: "Unscored" },
];

export const STATES = [
  { key: "unreviewed", label: "Unreviewed" },
  { key: "shortlisted", label: "Shortlisted" },
  { key: "applied", label: "Applied" },
  { key: "interview", label: "Interview" },
  { key: "rejected", label: "Rejected" },
  { key: "archived", label: "Archived" },
];

export function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) {
    node.className = className;
  }
  if (text !== undefined && text !== null) {
    node.textContent = String(text);
  }
  return node;
}

export function clearNode(node) {
  while (node.firstChild) {
    node.removeChild(node.firstChild);
  }
}

export function safeHttpUrl(raw) {
  if (typeof raw !== "string" || !raw) {
    return null;
  }
  try {
    const parsed = new URL(raw);
    if ((parsed.protocol === "http:" || parsed.protocol === "https:") && parsed.hostname && !parsed.username && !parsed.password) {
      return parsed.href;
    }
  } catch {
    return null;
  }
  return null;
}

export function formatDate(raw) {
  if (!raw) {
    return "—";
  }
  const text = String(raw);
  return text.length >= 16 ? text.slice(0, 16).replace("T", " ") : text;
}

export function createExternalLink(url, label, className) {
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

export function createScoreRing(score) {
  if (typeof score !== "number" || !Number.isFinite(score)) {
    return el("span", "panel-hint", "\u2014");
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



export const $ = id => document.getElementById(id);
export const PRIORITIES = [
  {key: 'p1', label: 'P1'}, {key: 'p2', label: 'P2'}, {key: 'p3', label: 'P3'}, {key: 'unset', label: 'Unprioritized'}
];

export function button(label, func, className = 'btn btn-secondary') {
  const node = el('button', className, label);
  node.type = 'button';
  node.addEventListener('click', func);
  return node;
}

export function monogram(company) {
  const data = company || {};
  const tone = Number.isInteger(data.tone) && data.tone >= 0 && data.tone < 8 ? data.tone : 0;
  const badge = el('span', 'avatar tone-' + tone, data.initials || '?');
  badge.setAttribute('aria-hidden', 'true');
  return badge;
}

export function empty(message) {
  return el('div', 'empty-box', message);
}
