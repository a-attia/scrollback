"use strict";

// ====================================================================
// helpers
// ====================================================================

const $ = (sel) => document.querySelector(sel);
const el = (tag, props = {}, ...kids) => {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (k === "class") n.className = v;
    else if (k === "dataset") Object.assign(n.dataset, v);
    else if (k.startsWith("on")) n.addEventListener(k.slice(2), v);
    else if (v !== null && v !== undefined && v !== false) n.setAttribute(k, v);
  }
  for (const kid of kids) {
    if (kid == null) continue;
    n.append(kid.nodeType ? kid : document.createTextNode(kid));
  }
  return n;
};

const fmtDate = (iso) => {
  if (!iso) return "?";
  return new Date(iso).toLocaleString(undefined, {
    year: "2-digit", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit",
  });
};

// Compact relative time for list rows: "3m", "2h", "5d", "3w", "4mo", "2y".
const fmtRelative = (iso) => {
  if (!iso) return "?";
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return "?";
  const s = Math.max(0, (Date.now() - then) / 1000);
  if (s < 60) return "just now";
  const m = s / 60;
  if (m < 60) return `${Math.floor(m)}m ago`;
  const h = m / 60;
  if (h < 24) return `${Math.floor(h)}h ago`;
  const d = h / 24;
  if (d < 7) return `${Math.floor(d)}d ago`;
  if (d < 30) return `${Math.floor(d / 7)}w ago`;
  if (d < 365) return `${Math.floor(d / 30)}mo ago`;
  return `${Math.floor(d / 365)}y ago`;
};

const fmtTokens = (n) => {
  if (n == null) return "";
  if (n < 1000) return String(n);
  if (n < 1e6) return (n / 1e3).toFixed(1) + "k";
  return (n / 1e6).toFixed(1) + "M";
};

const baseName = (p) => (p ? p.split("/").filter(Boolean).slice(-1)[0] || p : "");

// ---- math spans (delimited LaTeX) ----------------------------------------
// Mirror of the Python `mathspan` module: detect $...$, $$...$$, \(...\), and
// \[...\] and shield them from the Markdown pass (which would otherwise mangle
// `\`, `_`, `*`, `^`). Placeholders use private-use-area sentinels that marked
// treats as inert text and DOMPurify preserves.
const MATH_PH_OPEN = "\uE000MATH";
const MATH_PH_CLOSE = "\uE001";
const MATH_PH_RE = /\uE000MATH(\d+)\uE001/g;

const _esc = (s) => s.replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

function findMathSpans(text) {
  // Exclude fenced + inline code regions, then collect non-overlapping spans.
  const code = [];
  const fence = /^[ \t]*(`{3,}|~{3,})[\s\S]*?(?:^[ \t]*\1[ \t]*$|$(?![\s\S]))/gm;
  let m;
  while ((m = fence.exec(text))) code.push([m.index, m.index + m[0].length]);
  const inFence = (p) => code.some(([a, b]) => a <= p && p < b);
  const inlineCode = /(`+)[\s\S]+?\1/g;
  while ((m = inlineCode.exec(text))) {
    if (!inFence(m.index)) code.push([m.index, m.index + m[0].length]);
  }
  const inCode = (s, e) => code.some(([a, b]) => s < b && a < e);

  const patterns = [
    [/\$\$([\s\S]+?)\$\$/g, true],
    [/\\\[([\s\S]+?)\\\]/g, true],
    [/\\\(([\s\S]+?)\\\)/g, false],
    [/\$(?!\s)([^$\n]*[^$\s])\$(?!\d)/g, false],
  ];
  const cands = [];
  for (const [re, display] of patterns) {
    re.lastIndex = 0;
    while ((m = re.exec(text))) {
      if (inCode(m.index, m.index + m[0].length)) continue;
      cands.push({ start: m.index, end: m.index + m[0].length, body: m[1], display, raw: m[0] });
    }
  }
  cands.sort((a, b) => a.start - b.start || (b.end - b.start) - (a.end - a.start));
  const chosen = [];
  let claimed = -1;
  for (const c of cands) {
    if (c.start >= claimed) { chosen.push(c); claimed = c.end; }
  }
  return chosen;
}

function protectMath(text) {
  const spans = findMathSpans(text);
  if (!spans.length) return { masked: text, tokens: [] };
  let out = "";
  let last = 0;
  spans.forEach((s, i) => {
    out += text.slice(last, s.start) + MATH_PH_OPEN + i + MATH_PH_CLOSE;
    last = s.end;
  });
  out += text.slice(last);
  return { masked: out, tokens: spans };
}

// Replace placeholders in the *sanitized HTML string* with per-mode markup.
function restoreMathHtml(html, tokens, mode) {
  if (!tokens.length) return html;
  return html.replace(MATH_PH_RE, (_, idx) => {
    const s = tokens[+idx];
    if (mode === "rendered") {
      const cls = s.display ? "math-tex math-display" : "math-tex";
      return `<span class="${cls}" data-display="${s.display}">${_esc(s.body)}</span>`;
    }
    if (mode === "latex") return `<code class="math-src">${_esc(s.raw)}</code>`;
    return _esc(s.raw); // raw: verbatim source
  });
}

// ---- markdown rendering (vendored marked + highlight.js) -----------------

let _mdReady = false;
function setupMarkdown() {
  if (_mdReady || typeof marked === "undefined") return _mdReady;
  marked.setOptions({
    gfm: true,
    breaks: true,
    highlight: (code, lang) => {
      if (typeof hljs === "undefined") return code;
      try {
        if (lang && hljs.getLanguage(lang)) return hljs.highlight(code, { language: lang }).value;
        return hljs.highlightAuto(code).value;
      } catch { return code; }
    },
  });
  _mdReady = true;
  return true;
}

function renderMarkdownInto(node, text) {
  // Render `text` as markdown into `node`. Transcript text is UNTRUSTED (the
  // model/user can write arbitrary HTML/script into a message), so the marked
  // output MUST be sanitized before it touches innerHTML. We require both
  // marked and DOMPurify; if either is missing we fall back to plain text so
  // we never inject unsanitized HTML.
  if (setupMarkdown() && typeof DOMPurify !== "undefined") {
    node.classList.add("md");
    // Shield delimited-math spans before Markdown so the renderer can't
    // mangle them; the placeholders are restored after sanitizing.
    const mode = state.math || "raw";
    const { masked, tokens } = protectMath(text);
    const dirty = restoreMathHtml(marked.parse(masked), tokens, mode);
    node.innerHTML = DOMPurify.sanitize(dirty, {
      // Allow normal markdown output but strip scripts, event handlers, and
      // dangerous URI schemes. Forbid iframe/object/embed/form outright.
      FORBID_TAGS: ["script", "style", "iframe", "object", "embed", "form"],
      FORBID_ATTR: ["style"],
    });
    // Highlight any code blocks marked.highlight missed (older marked APIs).
    if (typeof hljs !== "undefined") {
      node.querySelectorAll("pre code:not(.hljs)").forEach((b) => {
        try { hljs.highlightElement(b); } catch { /* ignore */ }
      });
    }
    if (mode === "rendered") typesetMath(node);
  } else {
    node.textContent = text;
  }
}

// Typeset every .math-tex placeholder under `root` with KaTeX (vendored). The
// LaTeX body lives in textContent (escaped on the way in), so it is inert
// until KaTeX reads it. Failures degrade to showing the source.
function typesetMath(root) {
  if (typeof katex === "undefined") return;
  root.querySelectorAll(".math-tex").forEach((node) => {
    if (node.dataset.mathDone) return;
    const src = node.textContent;
    try {
      katex.render(src, node, {
        displayMode: node.dataset.display === "true",
        throwOnError: false,
        output: "html",
      });
      node.dataset.mathDone = "1";
    } catch {
      node.textContent = src; // leave the source visible on failure
    }
  });
}

const debounce = (fn, ms) => {
  let t;
  return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
};

async function getJSON(url) {
  const r = await fetch(url);
  if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
  return r.json();
}

function toast(msg) {
  const t = $("#toast");
  t.textContent = msg;
  t.hidden = false;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => (t.hidden = true), 2200);
}

const _SRC_COLORS = {
  opencode: "var(--opencode)",
  claudecode: "var(--claudecode)",
  codex: "var(--codex)",
  aider: "var(--aider)",
};
const _SRC_SOFTS = {
  opencode: "var(--opencode-soft)",
  claudecode: "var(--claudecode-soft)",
  codex: "var(--codex-soft)",
  aider: "var(--aider-soft)",
};
const srcColor = (name) => _SRC_COLORS[name] || "var(--focus)";
const srcSoft = (name) => _SRC_SOFTS[name] || "var(--focus-soft)";

// ====================================================================
// state
// ====================================================================

const PAGE = 50;            // session list page size
const MSG_PAGE = 40;        // transcript message window size

const state = {
  sources: [],
  enabled: new Set(),
  // Top-level browse mode: "live" (default) | "archive" | "all". Drives the
  // session list, search, and the stats viewer via the API `mode` param.
  mode: "live",
  // Archive-landing drill-down filter: null | "deleted" | "source".
  // Applied client-side on top of the mode's results.
  archiveFilterKind: null,
  archiveFilterSource: null,
  // search scope: which targets the query is matched against.
  scope: { titles: true, contents: false },
  query: "",
  since: "",
  until: "",
  // list pagination
  list: { offset: 0, hasMore: false, loading: false, kind: "sessions" },
  // open transcript
  current: null,            // {source, id}
  msg: { offset: 0, hasMore: false, loading: false },
  reasoning: false,
  tools: true,
  math: "raw",            // raw | latex | rendered (persisted like theme)
  headAutoCollapsed: false, // transient: auto-collapse state when no manual pref
};

// ====================================================================
// theme
// ====================================================================

function applyHljsTheme(theme) {
  const dark = $("#hljs-dark"), light = $("#hljs-light");
  if (!dark || !light) return;
  dark.disabled = theme !== "dark";
  light.disabled = theme !== "light";
}
function initTheme() {
  const saved = localStorage.getItem("scrollback-theme");
  const theme = saved || (matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark");
  document.documentElement.dataset.theme = theme;
  applyHljsTheme(theme);
}
function toggleTheme() {
  const next = document.documentElement.dataset.theme === "light" ? "dark" : "light";
  document.documentElement.dataset.theme = next;
  localStorage.setItem("scrollback-theme", next);
  applyHljsTheme(next);
}

// math render mode (raw | latex | rendered), persisted like the theme.
const MATH_MODES = ["raw", "latex", "rendered"];
function initMath() {
  const saved = localStorage.getItem("scrollback-math");
  state.math = MATH_MODES.includes(saved) ? saved : "raw";
}
function setMath(mode) {
  if (!MATH_MODES.includes(mode)) return;
  state.math = mode;
  localStorage.setItem("scrollback-math", mode);
  // Repaint any export buttons whose label reflects the math mode.
  document.querySelectorAll(".btn.math-follow").forEach(
    (b) => b.dispatchEvent(new CustomEvent("scrollback:math")));
  rerenderMessages();
}

// ====================================================================
// sources + filter chips
// ====================================================================

async function loadSources() {
  state.sources = await getJSON("/api/sources");
  // Only sources with data are enabled/filterable; unavailable ones render
  // greyed-out so users can see what scrollback could read.
  state.sources.forEach((s) => { if (s.available) state.enabled.add(s.name); });
  const wrap = $("#srcfilter");
  wrap.replaceChildren(
    ...state.sources.map((s) => {
      if (!s.available) {
        return el("button", {
          class: "src-toggle src-unavailable",
          disabled: "disabled",
          "aria-pressed": "false",
          dataset: { source: s.name },
          title: `${s.label || s.name}: no sessions found on this machine`,
          style: `--src:${srcColor(s.name)};--src-soft:${srcSoft(s.name)}`,
        },
          el("span", { class: "dot" }),
          s.label || s.name
        );
      }
      return el("button", {
        class: "src-toggle",
        "aria-pressed": "true",
        dataset: { source: s.name },
        title: s.location || s.name,
        style: `--src:${srcColor(s.name)};--src-soft:${srcSoft(s.name)}`,
        onclick: (e) => toggleSource(e.currentTarget, s.name),
      },
        el("span", { class: "dot" }),
        el("span", { class: "check" }, "\u2713"),
        s.label || s.name
      );
    })
  );
}

function toggleSource(btn, name) {
  if (state.enabled.has(name)) state.enabled.delete(name);
  else state.enabled.add(name);
  const on = state.enabled.has(name);
  btn.setAttribute("aria-pressed", on ? "true" : "false");
  btn.querySelector(".check").textContent = on ? "\u2713" : "";
  resetAndLoad();
}

function enabledParam() {
  // If exactly one source is enabled, pass it to the API for efficiency.
  return state.enabled.size === 1 ? [...state.enabled][0] : null;
}

// Client-side drill-down from the archive landing (deleted-only, or a chosen
// source). Cleared by clearArchiveDrill().
// The "deleted" drill is applied SERVER-side (a `deleted=true` param), because
// deleted sessions are interleaved by date with everything else and a
// client-side filter on one 50-row page usually yields an empty list.
function passesArchiveDrill(s) {
  if (state.archiveFilterKind === "source") return s.source === state.archiveFilterSource;
  return true;
}

function clearArchiveDrill() {
  state.archiveFilterKind = null;
  state.archiveFilterSource = null;
  resetAndLoad();
}

// #6 "archive these": show the bulk button when the list is narrowed by a
// filter/search/drill AND some shown sessions aren't up-to-date in the vault.
let _matchingKeys = [];
function updateArchiveMatching(rows) {
  const btn = $("#archive-matching");
  if (!btn) return;
  const narrowed = !!(state.query || state.since || state.until
    || enabledParam() || state.archiveFilterKind);
  // Candidates: live sessions not already archived-and-current.
  const todo = rows.filter((s) => !s.archived_only
    && (s.archive_status === "none" || s.archive_status === "stale"));
  _matchingKeys = todo.map((s) => [s.source, s.id]);
  if (narrowed && todo.length) {
    btn.hidden = false;
    btn.textContent = `\u2b07 archive these ${todo.length}`;
  } else {
    btn.hidden = true;
  }
}

async function archiveMatching() {
  if (!_matchingKeys.length) return;
  await runSyncJob("/api/archive/sync/batch", `Archiving ${_matchingKeys.length} sessions\u2026`,
    { json: { keys: _matchingKeys } });
  resetAndLoad();
}

// A dismissible banner shown atop the list when an archive drill-down is on.
function archiveDrillBanner() {
  if (!state.archiveFilterKind) return null;
  const label = state.archiveFilterKind === "deleted"
    ? "deleted from agent (archive only)"
    : `source: ${SRC_LABEL[state.archiveFilterSource] || state.archiveFilterSource}`;
  return el("li", { class: "drill-banner" },
    el("span", {}, "filtered: " + label),
    el("button", { class: "drill-clear", title: "Clear this filter",
      onclick: clearArchiveDrill }, "\u00d7 clear"));
}

// -- browse mode (live / archive / all) ------------------------------------

function initMode() {
  const saved = localStorage.getItem("scrollback-mode");
  state.mode = ["live", "archive", "all"].includes(saved) ? saved : "live";
  document.querySelectorAll("#modeswitch .mode-btn").forEach((btn) => {
    btn.setAttribute("aria-checked", btn.dataset.mode === state.mode ? "true" : "false");
    // A manual mode switch clears any archive-landing drill-down filter.
    btn.onclick = () => { clearArchiveDrillState(); setMode(btn.dataset.mode); resetAndLoad(); };
  });
}

// Clear drill state WITHOUT reloading (callers reload).
function clearArchiveDrillState() {
  state.archiveFilterKind = null;
  state.archiveFilterSource = null;
}

function setMode(mode) {
  if (mode === state.mode) return;
  state.mode = mode;
  localStorage.setItem("scrollback-mode", mode);
  document.querySelectorAll("#modeswitch .mode-btn").forEach((btn) => {
    btn.setAttribute("aria-checked", btn.dataset.mode === mode ? "true" : "false");
  });
  // Switching mode is a top-level "change what I'm browsing" action: close any
  // open transcript and return to the mode's home. The stats view is
  // mode-independent, so if it's open we leave it up.
  if (!isStatsOpen()) deselectSession();
  resetAndLoad();
}

// ====================================================================
// query / search
// ====================================================================

const searchInput = $("#search-input");
const scopeTitlesBtn = $("#scope-titles");
const scopeContentsBtn = $("#scope-contents");

function updateScopeButtons() {
  scopeTitlesBtn.setAttribute("aria-pressed", String(state.scope.titles));
  scopeContentsBtn.setAttribute("aria-pressed", String(state.scope.contents));
  // Placeholder reflects the active scope so intent is always clear.
  const where =
    state.scope.titles && state.scope.contents ? "titles + contents"
    : state.scope.contents ? "message contents"
    : "titles";
  searchInput.placeholder = `search ${where}\u2026`;
}

function toggleScope(which) {
  state.scope[which] = !state.scope[which];
  // Never allow an empty scope; fall back to the other target.
  if (!state.scope.titles && !state.scope.contents) {
    state.scope[which === "titles" ? "contents" : "titles"] = true;
  }
  updateScopeButtons();
  resetAndLoad();
}

const onSearchInput = debounce(() => {
  state.query = searchInput.value.trim();
  resetAndLoad();
}, 200);

// ====================================================================
// session list (paginated + infinite scroll)
// ====================================================================

const railEl = $("#rail");
const sessionsEl = $("#sessions");

function resetAndLoad() {
  state.list.offset = 0;
  state.list.hasMore = false;
  sessionsEl.replaceChildren(el("li", { class: "loading" }, "loading\u2026"));
  loadListPage(true);
}

async function loadListPage(reset = false) {
  if (state.list.loading) return;
  state.list.loading = true;
  try {
    const q = state.query;
    const wantContents = state.scope.contents && q;
    const wantTitles = state.scope.titles;
    if (wantContents && wantTitles && q) {
      await loadCombined(reset);          // titles + contents
    } else if (wantContents) {
      await loadSearch(reset);            // contents only
    } else {
      await loadSessions(reset);          // titles only (or no query)
    }
  } catch (err) {
    if (reset) sessionsEl.replaceChildren(el("li", { class: "loading" }, "error: " + err.message));
  } finally {
    state.list.loading = false;
  }
}

async function loadSessions(reset) {
  state.list.kind = "sessions";
  const p = new URLSearchParams({ offset: String(state.list.offset), limit: String(PAGE), fold: "true" });
  p.set("mode", state.mode);
  if (state.archiveFilterKind === "deleted") p.set("deleted", "true");
  if (state.query) p.set("q", state.query);
  if (state.since) p.set("since", state.since);
  if (state.until) p.set("until", state.until);
  const src = enabledParam();
  if (src) p.set("source", src);

  const data = await getJSON("/api/sessions?" + p.toString());
  let rows = data.sessions.filter((s) => state.enabled.has(s.source) && passesArchiveDrill(s));
  state.list.hasMore = data.has_more;
  state.list.offset += data.sessions.length;
  updateArchiveMatching(rows);

  if (reset) {
    sessionsEl.replaceChildren();
    const drill = archiveDrillBanner();
    if (drill) sessionsEl.append(drill);
    $("#count").textContent = `${rows.length}${data.has_more ? "+" : ""} sessions`;
  } else {
    const prev = parseInt($("#count").dataset.n || "0", 10) + rows.length;
    $("#count").textContent = `${prev}${data.has_more ? "+" : ""} sessions`;
  }
  $("#count").dataset.n = String((parseInt($("#count").dataset.n || "0", 10)) + rows.length);
  rows.forEach((s) => sessionsEl.append(sessionRow(s)));
  if (!sessionsEl.children.length) sessionsEl.append(emptyListNode());
}

function emptyListNode() {
  // No sources at all -> onboarding help; sources present -> just no matches.
  if (!state.sources.length) {
    return el("li", { class: "empty-list" },
      el("div", { class: "empty-list-title" }, "No AI-agent sessions found"),
      el("div", { class: "empty-list-body" },
        "scrollback reads, by default:"),
      el("ul", { class: "empty-list-paths" },
        el("li", {}, el("code", {}, "~/.local/share/opencode/opencode.db")),
        el("li", {}, el("code", {}, "~/.claude/projects/"))),
      el("div", { class: "empty-list-body" },
        "Run ", el("code", {}, "scrollback doctor"), " to see what was detected."));
  }
  return el("li", { class: "loading" }, "no sessions");
}

async function loadSearch(reset) {
  state.list.kind = "search";
  // Search is not offset-paginated server-side; fetch a generous cap once.
  if (!reset) return;
  const p = new URLSearchParams({ q: state.query, limit: "200" });
  p.set("mode", state.mode);
  if (state.since) p.set("since", state.since);
  if (state.until) p.set("until", state.until);
  const hits = (await getJSON("/api/search?" + p.toString()))
    .filter((h) => state.enabled.has(h.source));
  state.list.hasMore = false;
  $("#count").textContent = `${hits.length} content matches`;
  $("#count").dataset.n = String(hits.length);
  sessionsEl.replaceChildren();
  if (!hits.length) { sessionsEl.append(el("li", { class: "loading" }, "no matches")); return; }
  hits.forEach((h) => sessionsEl.append(searchRow(h)));
}

async function loadCombined(reset) {
  state.list.kind = "search";        // single-shot, no infinite scroll
  if (!reset) return;
  // Fetch title matches and content matches in parallel.
  const sp = new URLSearchParams({ offset: "0", limit: "200", fold: "true", q: state.query });
  sp.set("mode", state.mode);
  if (state.since) sp.set("since", state.since);
  if (state.until) sp.set("until", state.until);
  const src = enabledParam();
  if (src) sp.set("source", src);

  const cp = new URLSearchParams({ q: state.query, limit: "200" });
  cp.set("mode", state.mode);
  if (state.since) cp.set("since", state.since);
  if (state.until) cp.set("until", state.until);

  const [titleData, contentHits] = await Promise.all([
    getJSON("/api/sessions?" + sp.toString()),
    getJSON("/api/search?" + cp.toString()),
  ]);
  const titleRows = titleData.sessions.filter((s) => state.enabled.has(s.source));
  const titleIds = new Set(titleRows.map((s) => s.source + ":" + s.id));
  // Drop content hits whose session already appears as a title match.
  const hits = contentHits.filter(
    (h) => state.enabled.has(h.source) && !titleIds.has(h.source + ":" + h.session_id)
  );

  state.list.hasMore = false;
  $("#count").textContent = `${titleRows.length} title + ${hits.length} content`;
  $("#count").dataset.n = String(titleRows.length + hits.length);
  sessionsEl.replaceChildren();
  if (!titleRows.length && !hits.length) {
    sessionsEl.append(el("li", { class: "loading" }, "no matches"));
    return;
  }
  if (titleRows.length) {
    sessionsEl.append(el("li", { class: "group-label" }, "title matches"));
    titleRows.forEach((s) => sessionsEl.append(sessionRow(s)));
  }
  if (hits.length) {
    sessionsEl.append(el("li", { class: "group-label" }, "content matches"));
    hits.forEach((h) => sessionsEl.append(searchRow(h)));
  }
}

railEl.addEventListener("scroll", () => {
  if (state.list.kind !== "sessions" || !state.list.hasMore || state.list.loading) return;
  if (railEl.scrollTop + railEl.clientHeight >= railEl.scrollHeight - 200) {
    loadListPage(false);
  }
});

function sessionRow(s) {
  const li = el("li", {
    class: "session" + (isCurrent(s) ? " active" : ""),
    style: `--src:${srcColor(s.source)}`,
    dataset: { source: s.source, id: s.id },
    onclick: (e) => { if (e.target.closest(".s-children-toggle")) return; openSession(s.source, s.id); },
  },
    el("div", { class: "s-title", title: s.title }, s.title || "(untitled)"),
    metaLine(s)
  );

  if (s.children && s.children.length) {
    const childWrap = el("ul", { class: "s-children", hidden: true });
    s.children.forEach((c) => childWrap.append(childRow(c)));
    const toggle = el("button", { class: "s-children-toggle",
      onclick: () => {
        const open = childWrap.hidden;
        childWrap.hidden = !open;
        toggle.firstChild.textContent = open ? "\u25be" : "\u25b8";
      },
    }, el("span", {}, "\u25b8"), ` ${s.children.length} subagent${s.children.length === 1 ? "" : "s"}`);
    li.append(toggle, childWrap);
  }
  return li;
}

function childRow(c) {
  return el("li", {
    class: "s-child" + (isCurrent(c) ? " active" : ""),
    style: `--src:${srcColor(c.source)}`,
    dataset: { source: c.source, id: c.id },
    onclick: () => openSession(c.source, c.id),
  },
    el("div", { class: "s-title", title: c.title }, c.title || "(untitled)"),
    metaLine(c)
  );
}

function metaLine(s) {
  return el("div", { class: "s-meta" },
    el("span", { class: "s-src" }, s.source),
    el("span", { title: fmtDate(s.updated) }, fmtRelative(s.updated)),
    s.message_count != null ? el("span", {}, `${s.message_count} msgs`) : null,
    s.tokens_input != null ? el("span", { class: "s-badge", title: "tokens in/out" },
      `${fmtTokens(s.tokens_input)}/${fmtTokens(s.tokens_output)}`) : null,
    provenanceTag(s),
    s.directory ? el("span", { class: "s-dir", title: s.directory }, baseName(s.directory)) : null
  );
}

// A provenance tag identifying where a session comes from. Always shown so
// that in "all" mode live and archived rows are visually distinguishable.
//   deleted  -> in the vault only (removed from its agent)
//   stale    -> live, archived copy is out of date
//   archived -> live + up-to-date archived copy
//   live     -> not archived
function provenanceTag(s) {
  if (s.archived_only) {
    return el("span", { class: "s-tag tag-deleted",
      title: "In your archive only \u2014 deleted from its agent" }, "deleted");
  }
  if (s.archive_status === "stale") {
    return el("span", { class: "s-tag tag-stale",
      title: "Live + archived, but the archived copy is out of date (has newer messages)" },
      "live + stale");
  }
  if (s.archived || s.archive_status === "archived") {
    return el("span", { class: "s-tag tag-archived",
      title: "This session is live AND kept in your durable archive" }, "live + archived");
  }
  return el("span", { class: "s-tag tag-live",
    title: "Live \u2014 not in your archive" }, "live");
}

function searchRow(h) {
  return el("li", {
    class: "session",
    style: `--src:${srcColor(h.source)}`,
    dataset: { source: h.source, id: h.session_id },
    onclick: () => openSession(h.source, h.session_id, h.message_id),
  },
    el("div", { class: "s-title", title: h.title }, h.title || "(untitled)"),
    el("div", { class: "s-meta" },
      el("span", { class: "s-src" }, h.source),
      el("span", {}, `[${h.role}]`),
      h.tool_name ? el("span", {}, h.tool_name) : null),
    snippetNode(h.snippet, state.query)
  );
}

function snippetNode(snippet, q) {
  const div = el("div", { class: "s-snippet" });
  const lc = snippet.toLowerCase();
  const ql = q.trim().toLowerCase();
  let i = 0, pos = ql ? lc.indexOf(ql) : -1;
  if (pos !== -1) {
    while (pos !== -1) {
      div.append(snippet.slice(i, pos));
      div.append(el("mark", {}, snippet.slice(pos, pos + ql.length)));
      i = pos + ql.length;
      pos = lc.indexOf(ql, i);
    }
    div.append(snippet.slice(i));
  } else div.append(snippet);
  return div;
}

function isCurrent(s) {
  return state.current && state.current.source === s.source && state.current.id === s.id;
}
function markActiveRow() {
  updateClearSelection();
  document.querySelectorAll(".session.active, .s-child.active").forEach((n) => n.classList.remove("active"));
  if (!state.current) return;
  const sel = `[data-source="${CSS.escape(state.current.source)}"][data-id="${CSS.escape(state.current.id)}"]`;
  document.querySelectorAll(sel).forEach((n) => n.classList.add("active"));
}

// ====================================================================
// transcript reader (meta + windowed messages)
// ====================================================================

let transcriptMeta = null;

async function openSession(source, id, focusMessageId) {
  state.current = { source, id };
  state.msg = { offset: 0, hasMore: false, loading: false };
  state.headAutoCollapsed = false;   // a freshly opened session starts expanded
  const hash = `#${source}/${id}`;
  if (location.hash !== hash) history.replaceState(null, "", hash);
  markActiveRow();

  $("#empty").hidden = true;
  $("#stats").hidden = true;
  markView("browse");
  closeRail();   // collapse the mobile drawer once a session is chosen
  const t = $("#transcript");
  t.hidden = false;
  t.replaceChildren(el("div", { class: "loading" }, "loading transcript\u2026"));
  $("#reader").scrollTop = 0;

  let meta;
  try {
    meta = await getJSON(`/api/sessions/${enc(source)}/${enc(id)}/meta`);
  } catch (err) {
    t.replaceChildren(el("div", { class: "loading" }, "error: " + err.message));
    return;
  }
  transcriptMeta = meta;
  renderHeader(meta);
  await loadMessages(true, focusMessageId);
}

function enc(s) { return encodeURIComponent(s); }

// ====================================================================
// help affordances (UI-wide)
// ====================================================================

// A small "?" marker with an explanatory tooltip. Used across the UI to
// explain terms (archive states, token buckets, modes, ...).
function helpIcon(text) {
  return el("span", { class: "help-icon", tabindex: "0", role: "img",
    "aria-label": text, title: text }, "\u24d8");
}

// ====================================================================
// archive sync (web-driven; writes to the vault only, never to agents)
// ====================================================================

// Archive/update a single session, then refresh its header to show new status.
async function syncOneSession(meta) {
  await runSyncJob(
    `/api/archive/sync/${enc(meta.source)}/${enc(meta.id)}`,
    `Archiving ${meta.short_id || meta.id}\u2026`,
  );
  // Re-fetch meta so the status + button reflect the new archived state.
  // Update ONLY the archive zone -- re-rendering the whole header would clear
  // the loaded message body (transcript rebuild).
  try {
    const fresh = await getJSON(`/api/sessions/${enc(meta.source)}/${enc(meta.id)}/meta`);
    transcriptMeta = fresh;
    refreshArchiveZone(fresh);
  } catch (_e) { /* status refresh is best-effort */ }
  // Also refresh the session list so the row's provenance tag updates.
  if (state.list.kind === "sessions") resetAndLoad();
}

// Full incremental sync of everything live -> vault.
async function syncAll() {
  const summary = await runSyncJob("/api/archive/sync", "Syncing all sessions\u2026");
  if (isStatsOpen()) loadStats();               // refresh archive-group counts
  else if (state.mode === "archive") showArchiveLanding();
  resetAndLoad();   // refresh list so new archive statuses show
  return summary;
}

// POST to a sync endpoint, then consume its SSE progress stream, driving the
// shared progress-bar overlay. Resolves with the job's result summary.
// `opts.body` = a File/Blob (raw upload, e.g. import); `opts.json` = a JSON
// payload (e.g. batch keys).
async function runSyncJob(postUrl, title, opts = {}) {
  const bar = showProgress(title);
  let job;
  try {
    const init = { method: "POST" };
    if (opts.body) init.body = opts.body;
    else if (opts.json) { init.body = JSON.stringify(opts.json); init.headers = { "Content-Type": "application/json" }; }
    job = await (await fetch(postUrl, init)).json();
  } catch (err) {
    bar.fail("could not start sync: " + err.message);
    throw err;
  }
  return await new Promise((resolve) => {
    const es = new EventSource(`/api/archive/jobs/${enc(job.job_id)}/events`);
    es.onmessage = (ev) => {
      let snap;
      try { snap = JSON.parse(ev.data); } catch (_e) { return; }
      bar.update(snap.done, snap.total, snap.phase);
      if (snap.finished) {
        es.close();
        if (snap.error) bar.fail(snap.error);
        else bar.done(snap.result);
        resolve(snap.result);
      }
    };
    es.onerror = () => {
      // Stream dropped; fall back to a single status poll before giving up.
      es.close();
      getJSON(`/api/archive/jobs/${enc(job.job_id)}`).then((snap) => {
        if (snap.error) bar.fail(snap.error); else bar.done(snap.result);
        resolve(snap.result);
      }).catch(() => { bar.fail("progress stream lost"); resolve(null); });
    };
  });
}

// A lightweight progress overlay. Returns handles to update / finish it.
function showProgress(title) {
  let host = $("#progress");
  if (!host) {
    host = el("div", { id: "progress", class: "progress-host", hidden: true });
    document.body.append(host);
  }
  const fill = el("div", { class: "progress-fill" });
  const label = el("div", { class: "progress-label" }, title);
  const pct = el("div", { class: "progress-pct" }, "");
  const card = el("div", { class: "progress-card" },
    el("div", { class: "progress-title" }, title),
    el("div", { class: "progress-track" }, fill),
    el("div", { class: "progress-meta" }, label, pct));
  host.replaceChildren(card);
  host.hidden = false;

  const setPct = (frac, indeterminate) => {
    fill.classList.toggle("indeterminate", !!indeterminate);
    if (!indeterminate) fill.style.width = Math.round(frac * 100) + "%";
  };
  setPct(0, true);

  const hideSoon = (ms) => setTimeout(() => { host.hidden = true; }, ms);
  return {
    update(done, total, phase) {
      if (total > 0) { setPct(done / total, false); pct.textContent = `${done}/${total}`; }
      else setPct(0, true);
      label.textContent = phase === "syncing" ? "syncing\u2026" : (phase || "");
    },
    done(result) {
      setPct(1, false);
      const parts = result && typeof result === "object"
        ? Object.entries(result).filter(([, v]) => typeof v === "number" && v)
            .map(([k, v]) => `${v} ${k.replace("_", " ")}`)
        : [];
      label.textContent = "done" + (parts.length ? ": " + parts.join(", ") : "");
      pct.textContent = "";
      hideSoon(3500);
    },
    fail(msg) { label.textContent = "failed: " + msg; fill.classList.add("failed"); hideSoon(6000); },
  };
}

function fmtBytes(n) {
  if (!n) return "0 B";
  const u = ["B", "KB", "MB", "GB"]; let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return `${n < 10 && i ? n.toFixed(1) : Math.round(n)} ${u[i]}`;
}

// Filter the session list to one provenance category and switch to the mode
// that shows it (deleted -> archive; archived -> all). Drives the landing's
// clickable stats + the "deleted" quick view.
function showArchiveCategory(kind, source) {
  state.archiveFilterKind = kind;      // "deleted" | "source" | null
  state.archiveFilterSource = source || null;
  const target = kind === "deleted" ? "archive" : "all";
  if (target === state.mode) resetAndLoad();   // mode unchanged -> reload manually
  else setMode(target);                        // setMode reloads
}

// Deselect the open session and return the right panel to the mode's home
// (archive landing for archive mode, the empty reader otherwise). No-op on the
// stats view. Used by the rail "clear" button and by mode switches.
function deselectSession() {
  if (isStatsOpen()) return;
  if (state.current) {
    state.current = null;
    markActiveRow();
    $("#transcript").hidden = true;
    history.replaceState(null, "", location.pathname);
  }
  if (state.mode === "archive") showArchiveLanding();
  else showEmptyReader();
}

// Show the rail "clear" button only when a session is selected.
function updateClearSelection() {
  const btn = $("#clear-selection");
  if (btn) btn.hidden = !state.current;
}

// The default empty reader (live/all mode with nothing open).
function showEmptyReader() {
  $("#transcript").hidden = true;
  $("#stats").hidden = true;
  const empty = $("#empty");
  empty.hidden = false;
  empty.replaceChildren(
    el("img", { class: "empty-icon", src: "/favicon.svg", width: "112", height: "112", alt: "scrollback" }),
    el("p", {}, "Pick a session on the left, or search to dig through everything."),
    el("p", { class: "empty-sub" }, "Reading is local; scrollback never writes to your agents."));
}

// The archive landing view (shown in Archive mode with nothing open).
async function showArchiveLanding() {
  state.current = null;
  markActiveRow();
  markView("browse");
  $("#transcript").hidden = true;
  $("#stats").hidden = true;
  const empty = $("#empty");
  empty.hidden = false;
  empty.replaceChildren(el("div", { class: "loading" }, "loading archive\u2026"));

  let data;
  try {
    data = await getJSON("/api/archive");
  } catch (err) {
    empty.replaceChildren(el("p", {}, "could not load archive: " + err.message));
    return;
  }

  // Onboarding: no vault yet.
  if (!data.exists) {
    empty.replaceChildren(el("div", { class: "archive-landing archive-empty" },
      el("img", { class: "empty-icon", src: "/favicon.svg", width: "88", height: "88", alt: "" }),
      el("h1", {}, "Keep your sessions forever"),
      el("p", { class: "empty-sub" },
        "Your agents delete old sessions (Claude Code prunes after ~30 days). "
        + "Archive copies them into a durable vault at ", el("code", {}, data.path),
        " \u2014 lossless, and yours to keep."),
      el("div", { class: "archive-actions" },
        el("button", { class: "btn arc-btn arc-primary",
          title: "Copy every live session into your durable vault",
          onclick: () => syncAll() }, "\u2b07 Archive all sessions now"),
        importButton())));
    return;
  }

  // Populated vault: stat cards (clickable) + integrity + actions.
  const card = (n, label, opts = {}) => el("button", {
    class: "archive-card" + (opts.accent ? " archive-card-" + opts.accent : "")
      + (opts.onclick ? " archive-card-clickable" : ""),
    disabled: opts.onclick ? undefined : "disabled",
    title: opts.title || "",
    onclick: opts.onclick,
  }, el("b", {}, String(n)), el("span", {}, label),
     opts.help ? helpIcon(opts.help) : null);

  const cards = el("div", { class: "archive-cards" },
    card(data.sessions, "sessions kept", {
      title: "Browse everything in your archive", onclick: () => showArchiveCategory(null) }),
    card(data.orphans, "deleted from agent", {
      accent: "deleted",
      help: "Kept in your archive but removed from their agent \u2014 you'd have lost these.",
      title: "Show only sessions your agents have deleted",
      onclick: data.orphans ? () => showArchiveCategory("deleted") : null }),
    card(data.stale, "need updating", {
      accent: "stale",
      help: "Archived, but the live session has newer messages since.",
      title: "Update every stale archived session",
      onclick: data.stale ? () => updateAllStale() : null }));

  // Per-source breakdown, each row clickable to filter.
  const perSource = Object.entries(data.per_source || {}).map(([src, n]) =>
    el("button", { class: "archive-source-row",
      title: `Show archived ${SRC_LABEL[src] || src} sessions`,
      onclick: () => showArchiveCategory("source", src) },
      el("span", { class: "src", style: `--src:${srcColor(src)}` }, SRC_LABEL[src] || src),
      el("span", { class: "archive-source-n" }, `${n}`)));

  const integrity = el("div", { class: "archive-integrity" },
    el("span", { class: "loading-inline" }, "checking integrity\u2026"));
  verifyInto(integrity);

  empty.replaceChildren(el("div", { class: "archive-landing" },
    el("h1", {}, "Your archive"),
    el("p", { class: "empty-sub archive-path" }, data.path,
      el("span", { class: "archive-size" }, "  \u00b7  " + fmtBytes(data.bytes))),
    cards,
    perSource.length ? el("div", { class: "archive-sources-block" },
      el("div", { class: "archive-sub-label" }, "by source"),
      el("div", { class: "archive-sources" }, ...perSource)) : null,
    integrity,
    el("div", { class: "archive-actions" },
      el("button", { class: "btn arc-btn arc-primary",
        title: "Archive every live session (adds new + updates changed)",
        onclick: () => syncAll() }, "\u2b07 Archive all"),
      data.stale ? el("button", { class: "btn arc-btn",
        title: "Update the archived copies that have newer messages",
        onclick: () => updateAllStale() }, `Update ${data.stale} stale`) : null,
      el("a", { class: "btn", href: "/api/archive/export", download: "scrollback-archive.zip",
        title: "Download the whole vault as a .zip backup (re-importable)" },
        "\u2b07 Export .zip"),
      importButton())));
}

// A file-picker button that uploads a vault .zip and merges it in.
function importButton() {
  const input = el("input", { type: "file", accept: ".zip", hidden: true,
    onchange: (e) => { const f = e.target.files[0]; if (f) importVault(f); } });
  const btn = el("button", { class: "btn",
    title: "Merge a vault .zip from another machine (larger/newer copy wins)",
    onclick: () => input.click() }, "\u2b06 Import .zip");
  return el("span", { class: "import-wrap" }, btn, input);
}

async function importVault(file) {
  await runSyncJob("/api/archive/import", `Merging ${file.name}\u2026`, { body: file });
  showArchiveLanding();
  resetAndLoad();
}

async function updateAllStale() {
  await runSyncJob("/api/archive/sync/stale", "Updating stale sessions\u2026");
  if (state.mode === "archive" && !state.current) showArchiveLanding();
  resetAndLoad();
}

// Fill an element with the integrity summary (ok / missing / unreadable).
//
// The landing does the SHALLOW check (every file present and non-empty), which
// is near-instant. The deep check parses the whole vault -- tens of seconds on
// a large archive -- so it is offered as an explicit action running as a
// background job rather than blocking the page on every render.
async function verifyInto(node) {
  let v;
  try { v = await getJSON("/api/archive/verify"); }
  catch { node.replaceChildren(el("span", {}, "integrity: unavailable")); return; }
  if (!v.exists) { node.replaceChildren(); return; }
  renderIntegrity(node, v);
}

function renderIntegrity(node, v) {
  const bad = v.missing.length + v.unreadable.length;
  const deepBtn = el("button", {
    class: "integrity-deep",
    title: "Parse every archived file to detect corruption (can take a while)",
    onclick: () => runDeepVerify(node),
  }, "run full check");

  if (!bad) {
    node.className = "archive-integrity ok";
    node.replaceChildren(
      el("span", {}, `\u2713 integrity: all ${v.ok} files present`),
      v.deep ? null : deepBtn);
  } else {
    node.className = "archive-integrity bad";
    node.replaceChildren(
      el("span", {}, `\u26a0 integrity: ${bad} problem${bad === 1 ? "" : "s"} `),
      helpIcon(`${v.missing.length} missing file(s), ${v.unreadable.length} unreadable. `
        + "Run 'scrollback archive --verify' for the list."),
      v.deep ? null : deepBtn);
  }
}

async function runDeepVerify(node) {
  const r = await runSyncJob("/api/archive/verify", "Checking every archived file\u2026");
  if (!r) return;
  renderIntegrity(node, {
    exists: true, deep: true, ok: r.ok,
    missing: new Array(r.missing || 0), unreadable: new Array(r.unreadable || 0),
  });
}

// ====================================================================
// stats view (usage per tool + overall)
// ====================================================================

const SRC_LABEL = {
  opencode: "opencode", claudecode: "Claude Code",
  codex: "Codex", aider: "Aider",
};
const fmtCost = (c) => (c == null ? "\u2014" : "$" + c.toFixed(2));

// Reflect the active view in the browse|stats radio switch.
function markView(view) {
  const b = $("#view-browse"), s = $("#view-stats");
  if (b) b.setAttribute("aria-checked", String(view === "browse"));
  if (s) s.setAttribute("aria-checked", String(view === "stats"));
  // In Stats view the session rail and the browse-only scope controls
  // (mode + source) are irrelevant; the date range still applies to both.
  document.body.classList.toggle("viewing-stats", view === "stats");
}

// Switch to the browse view (session list + reader). Does not clear filters.
function showBrowse() {
  markView("browse");
  $("#stats").hidden = true;
  if (!state.current) $("#empty").hidden = false;
}

// -- about dialog ---------------------------------------------------------
let _aboutVersionLoaded = false;
async function openAbout() {
  const back = $("#about-backdrop");
  back.hidden = false;
  $("#about-close").focus();
  if (!_aboutVersionLoaded) {
    try {
      const h = await getJSON("/api/health");
      if (h && h.version) $("#about-version").textContent = "Version " + h.version;
      _aboutVersionLoaded = true;
    } catch { /* leave the placeholder */ }
  }
}
function closeAbout() { $("#about-backdrop").hidden = true; }

// -- narrow-screen session drawer -----------------------------------------
// On wide screens the rail is a static column and these are no-ops in effect
// (the CSS keeps the backdrop hidden); on narrow screens they open/close the
// slide-in list.
function openRail() {
  $("#rail").classList.add("show");
  $("#rail-backdrop").hidden = false;
  $("#rail-toggle")?.setAttribute("aria-expanded", "true");
}
function closeRail() {
  $("#rail").classList.remove("show");
  $("#rail-backdrop").hidden = true;
  $("#rail-toggle")?.setAttribute("aria-expanded", "false");
}
function toggleRail() {
  if ($("#rail").classList.contains("show")) closeRail();
  else openRail();
}

function isStatsOpen() {
  const panel = $("#stats");
  return panel && !panel.hidden;
}

async function openStats() {
  markView("stats");
  state.current = null;
  markActiveRow();
  history.replaceState(null, "", location.pathname);
  $("#transcript").hidden = true;
  $("#empty").hidden = true;
  const panel = $("#stats");
  panel.hidden = false;
  $("#reader").scrollTop = 0;
  await loadStats();
}

// Fetch + render stats. The stats view always shows TWO groups -- live and
// archive -- so the two corpora are clearly distinguished (independent of the
// browse mode). Re-callable when date filters change.
async function loadStats() {
  const panel = $("#stats");
  if (!panel || panel.hidden) return;
  panel.replaceChildren(el("div", { class: "loading" }, "computing statistics\u2026"));

  const qp = (mode) => {
    const p = new URLSearchParams();
    p.set("mode", mode);
    if (state.since) p.set("since", state.since);
    if (state.until) p.set("until", state.until);
    return "/api/stats?" + p.toString();
  };

  let live, arch;
  try {
    [live, arch] = await Promise.all([getJSON(qp("live")), getJSON(qp("archive"))]);
  } catch (err) {
    panel.replaceChildren(el("div", { class: "loading" }, "error: " + err.message));
    return;
  }
  panel.replaceChildren(renderStats(live, arch));
}

function statCell(n) { return el("td", { class: "num" }, fmtTokens(n)); }

function renderStats(live, arch) {
  const wrap = el("div", { class: "stats-wrap" });

  const dateScope = (state.since || state.until)
    ? `filtered ${state.since || "\u2026"} \u2192 ${state.until || "now"}`
    : "all time";

  wrap.append(el("div", { class: "stats-head" },
    el("h1", {}, "Usage statistics"),
    el("p", { class: "stats-sub" }, dateScope,
      helpIcon("Two groups: your live agent sessions, and your durable "
        + "archive (which may include sessions the agent has since deleted). "
        + "Date filters apply to both."))));

  // Two labelled groups: live sessions, then the archive.
  wrap.append(renderStatsSection("live sessions", "live", live,
    "Sessions currently present in your agents."));
  wrap.append(renderStatsSection("archive", "archived", arch,
    "Sessions kept in your durable vault (may include deleted ones)."));

  // Explanatory note (the four-bucket / cache-dominates caveat).
  wrap.append(el("p", { class: "stats-note" },
    "Tokens are counted in four buckets. In agentic sessions the conversation " +
    "context is re-sent each turn but served from the prompt cache, so cache " +
    "reads usually dominate volume while costing a fraction of fresh input. " +
    "Cost is shown only where the tool records it (\u2014 = not reported)."));
  return wrap;
}

// Render one stats group (live or archive) with a labelled, tinted heading.
function renderStatsSection(title, tone, d, help) {
  const section = el("section", { class: `stats-section stats-${tone}` });
  section.append(el("div", { class: "stats-section-head" },
    el("span", { class: `s-tag tag-${tone}` }, title),
    el("span", { class: "stats-section-sub" },
      `${d.sessions} sessions \u00b7 ${fmtTokens(d.messages)} messages` +
      (d.oldest ? ` \u00b7 ${fmtDate(d.oldest)} \u2192 ${fmtDate(d.newest)}` : "")),
    helpIcon(help)));

  if (!d.sessions) {
    section.append(el("p", { class: "stats-empty" },
      tone === "archived" ? "Nothing archived yet." : "No live sessions."));
    return section;
  }

  // Per-tool table.
  const head = el("tr", {},
    el("th", {}, "tool"),
    el("th", { class: "num" }, "sessions"),
    el("th", { class: "num" }, "messages"),
    el("th", { class: "num" }, "input"),
    el("th", { class: "num" }, "output"),
    el("th", { class: "num" }, "cache read"),
    el("th", { class: "num" }, "cache write"),
    el("th", { class: "num" }, "reasoning"),
    el("th", { class: "num" }, "cost"));

  const body = el("tbody", {});
  for (const r of d.per_source) {
    body.append(el("tr", {},
      el("td", {}, el("span", { class: "src", style: `--src:${srcColor(r.source)}` },
        SRC_LABEL[r.source] || r.source)),
      el("td", { class: "num" }, String(r.sessions)),
      statCell(r.messages),
      statCell(r.tokens_input), statCell(r.tokens_output),
      statCell(r.tokens_cache_read), statCell(r.tokens_cache_write),
      statCell(r.tokens_reasoning),
      el("td", { class: "num" }, fmtCost(r.cost))));
  }

  const t = d.totals;
  const foot = el("tfoot", {}, el("tr", { class: "totals" },
    el("td", {}, "all tools"),
    el("td", { class: "num" }, String(d.sessions)),
    statCell(d.messages),
    statCell(t.tokens_input), statCell(t.tokens_output),
    statCell(t.tokens_cache_read), statCell(t.tokens_cache_write),
    statCell(t.tokens_reasoning),
    el("td", { class: "num" }, fmtCost(t.cost))));

  section.append(el("table", { class: "stats-table" }, el("thead", {}, head), body, foot));
  return section;
}

function renderHeader(meta) {
  const t = $("#transcript");
  t.style.setProperty("--src", srcColor(meta.source));
  const copyId = el("button", { class: "copy-id", title: "copy session id",
    onclick: () => { navigator.clipboard.writeText(meta.id); toast("session id copied"); } },
    meta.short_id + " \u29c9");

  // Collapse/expand toggle: frees vertical space for the transcript by hiding
  // the meta / find / action rows, leaving just the title. Persisted.
  const collapseBtn = el("button", { class: "head-collapse", id: "head-collapse",
    title: "Collapse / expand the session header",
    "aria-label": "Collapse or expand the session header",
    onclick: () => toggleHeaderCollapsed() });

  // Compact summary shown only while the header is collapsed, so a little
  // context (source + message count) survives the collapse.
  const miniMeta = el("span", { class: "t-mini-meta" },
    el("span", { class: "src" }, meta.source),
    el("span", {}, `${meta.message_count} msgs`));

  const head = el("div", { class: "t-head" },
    el("div", { class: "t-titlebar" },
      el("h1", { class: "t-title" }, meta.title || "(untitled)"),
      miniMeta,
      collapseBtn),
    el("div", { class: "t-meta" },
      el("span", { class: "src" }, meta.source),
      copyId,
      meta.model ? el("span", {}, "model: " + meta.model) : null,
      meta.git_branch ? el("span", {}, "branch: " + meta.git_branch) : null,
      provenanceTag(meta),
      meta.tokens_input != null ? el("span", { title: "input / output tokens" }, `tokens ${fmtTokens(meta.tokens_input)}/${fmtTokens(meta.tokens_output)}`) : null,
      meta.tokens_cache_read != null && (meta.tokens_cache_read || meta.tokens_cache_write)
        ? el("span", { title: "prompt cache read / write" }, `cache ${fmtTokens(meta.tokens_cache_read)}/${fmtTokens(meta.tokens_cache_write)}`) : null,
      el("span", {}, fmtDate(meta.created)),
      el("span", {}, `${meta.message_count} messages`),
      meta.directory ? el("span", {}, meta.directory) : null
    ),
    findBar(),
    actionBar(meta)
  );
  const body = el("div", { class: "t-body", id: "t-body" });
  body.addEventListener("scroll", () => {
    // Load more messages as the (frozen-header) message body nears its bottom.
    if (state.current && state.msg.hasMore && !state.msg.loading) {
      if (body.scrollTop + body.clientHeight >= body.scrollHeight - 400) loadMessages(false);
    }
    autoCollapseOnScroll(body.scrollTop);
  });
  t.replaceChildren(head, body);
  applyHeaderCollapsed();
}

// Header collapse: a manual preference (persisted, "1"/"0") always wins. When
// no manual preference is set we are in AUTO mode -- the header collapses once
// the transcript is scrolled down and expands again near the top.
function headerPref() {
  return localStorage.getItem("scrollback-head-collapsed");   // "1" | "0" | null
}
function isHeaderCollapsed() {
  const pref = headerPref();
  if (pref === "1") return true;
  if (pref === "0") return false;
  return state.headAutoCollapsed === true;   // auto mode
}
function applyHeaderCollapsed() {
  const t = $("#transcript");
  if (!t) return;
  const on = isHeaderCollapsed();
  t.classList.toggle("head-collapsed", on);
  const btn = $("#head-collapse");
  if (btn) {
    btn.textContent = on ? "\u25be" : "\u25b4";   // down (expand) / up (collapse)
    btn.setAttribute("aria-expanded", String(!on));
    btn.title = on ? "Expand the session header (h)" : "Collapse the session header (h)";
  }
}
function toggleHeaderCollapsed() {
  // A manual toggle pins the opposite of the current visible state.
  localStorage.setItem("scrollback-head-collapsed", isHeaderCollapsed() ? "0" : "1");
  applyHeaderCollapsed();
}
function autoCollapseOnScroll(scrollTop) {
  if (headerPref() !== null) return;   // manual preference set -> no auto behaviour
  const want = scrollTop > 120;
  if (want !== state.headAutoCollapsed) {
    state.headAutoCollapsed = want;
    applyHeaderCollapsed();
  }
}

function findBar() {
  const input = el("input", { id: "find-input", type: "search", placeholder: "find in transcript\u2026",
    autocomplete: "off", spellcheck: "false",
    oninput: debounce(() => runFind(input.value), 150),
    onkeydown: (e) => { if (e.key === "Enter") { e.preventDefault(); stepFind(e.shiftKey ? -1 : 1); } } });
  return el("div", { class: "t-find" },
    input,
    el("span", { class: "find-count", id: "find-count" }, ""),
    el("button", { class: "btn", title: "Previous match", onclick: () => stepFind(-1) }, "\u2191"),
    el("button", { class: "btn", title: "Next match", onclick: () => stepFind(1) }, "\u2193"));
}

// A checkbox-style toggle: a leading box (checked/unchecked) + a label, so
// it is obvious at a glance what is shown vs hidden.
function checkToggle(label, key) {
  const render = (btn) => {
    const on = state[key];
    btn.classList.toggle("on", on);
    btn.setAttribute("aria-pressed", String(on));
    btn.replaceChildren(
      el("span", { class: "chk", "aria-hidden": "true" }, on ? "\u2611" : "\u2610"),
      label,
    );
  };
  const btn = el("button", {
    class: "toggle", role: "checkbox",
    title: `Show or hide ${label}`,
    onclick: (e) => { state[key] = !state[key]; render(e.currentTarget); rerenderMessages(); },
  });
  render(btn);
  return btn;
}

// Short suffix naming the active math mode, appended to math-following export
// buttons (print / html). Markdown / copy / JSON are always verbatim source.
const MATH_SUFFIX = { raw: "", latex: " \u00b7 LaTeX", rendered: " \u00b7 typeset" };

function actionBar(meta) {
  // -- VIEW zone: how the transcript is shown on screen ------------------
  const show = el("div", { class: "ctrl-grp" },
    el("span", { class: "ctrl-label" }, "show"),
    checkToggle("reasoning", "reasoning"),
    checkToggle("tool calls", "tools"));

  const mathSel = el("select", { class: "select", id: "math-select",
    title: "How LaTeX math is displayed on screen, and in print / HTML export",
    onchange: (e) => setMath(e.currentTarget.value) },
    el("option", { value: "raw" }, "source ($\u2026$)"),
    el("option", { value: "latex" }, "LaTeX (paste-ready)"),
    el("option", { value: "rendered" }, "typeset"));
  mathSel.value = state.math;
  const math = el("div", { class: "ctrl-grp" },
    el("label", { class: "ctrl-label", for: "math-select" }, "math"),
    mathSel);

  const view = el("div", { class: "bar-zone" },
    el("span", { class: "zone-label" }, "view"), show, math);

  // -- EXPORT zone: two sub-groups make the math relationship explicit --
  //   * verbatim group (copy / md / json): always keep LaTeX as source.
  //   * math group (print / html): output follows the "math" setting above.
  // A plain download button (math-independent).
  const exp = (fmt, base) => el("button", {
    class: "btn",
    title: `Download this session as ${base.toUpperCase()} (LaTeX kept as source)`,
    onclick: () => downloadExport(meta, fmt),
  }, "\u2193 " + base);

  // A math-following export button: label carries the active math mode, and
  // it repaints when the mode changes.
  const mathExp = (make, baseLabel, onclick, titleBase) => {
    const btn = el("button", { class: "btn math-follow", onclick,
      title: `${titleBase} \u2014 follows the math setting above` });
    const paint = () => { btn.textContent = make() + MATH_SUFFIX[state.math]; };
    paint();
    btn.addEventListener("scrollback:math", paint);
    return btn;
  };

  const verbatimGrp = el("div", { class: "ctrl-grp export-grp" },
    el("span", { class: "ctrl-label" }, "verbatim"),
    el("button", { class: "btn", title: "Copy as Markdown (LaTeX kept as source)",
      onclick: () => copySession(meta, "markdown") },
      "copy ", el("span", { class: "k" }, "md")),
    exp("markdown", "md"), exp("json", "json"),
    helpIcon("Copy, Markdown, and JSON always keep LaTeX as verbatim source \u2014 "
      + "the math setting does not change them."));

  const mathGrp = el("div", { class: "ctrl-grp export-grp export-math" },
    el("span", { class: "ctrl-label" }, "math \u2192"),
    mathExp(() => "\u2399 print", "print", () => printSession(meta),
      "Open a print-friendly view"),
    mathExp(() => "\u2193 html", "html", () => downloadExport(meta, "html"),
      "Download this session as HTML"),
    helpIcon("Print and HTML render math per the \u201cmath\u201d setting above "
      + "(source / paste-ready / typeset)."));

  const exportZone = el("div", { class: "bar-zone" },
    el("span", { class: "zone-label" }, "export"), verbatimGrp, mathGrp);

  return el("div", { class: "t-actions" }, view, archiveZone(meta), exportZone);
}

// ARCHIVE zone: per-session status + an archive/update button that writes the
// session to the durable vault (never to agent data), with a progress bar.
function archiveZone(meta) {
  const status = meta.archived_only ? "deleted" : (meta.archive_status || "none");
  const label = {
    none: "not archived", archived: "archived",
    stale: "archived (stale)", deleted: "archived (deleted from agent)",
  }[status];
  const statusEl = el("span", { class: `arc-status arc-${status}` }, label,
    helpIcon({
      none: "This live session is not yet in your durable archive.",
      archived: "A copy is kept in your durable archive and is up to date.",
      stale: "Archived, but the live session has newer messages \u2014 update to refresh.",
      deleted: "This session exists only in your archive; its agent deleted it.",
    }[status]));

  // Archive-only (deleted) sessions have no live source to re-sync from.
  const canSync = status === "none" || status === "stale";
  const btnLabel = status === "stale" ? "update archive" : "archive this";
  const btn = el("button", {
    class: "btn arc-btn", disabled: canSync ? undefined : "disabled",
    title: canSync ? "Copy this session into your durable archive"
                   : "Nothing to do \u2014 this session is up to date in your archive",
    onclick: () => syncOneSession(meta),
  }, btnLabel);

  return el("div", { class: "bar-zone", id: "archive-zone" },
    el("span", { class: "zone-label" }, "archive"),
    el("div", { class: "ctrl-grp arc-grp" }, statusEl, canSync ? btn : null));
}

// Replace ONLY the archive zone in place, so refreshing a session's archive
// status after a sync does not rebuild the transcript (which would wipe the
// already-loaded message body). No-op if the header isn't showing this meta.
function refreshArchiveZone(meta) {
  const old = document.getElementById("archive-zone");
  if (old) old.replaceWith(archiveZone(meta));
}

let loadedMessages = [];   // accumulates message objects as we page

async function loadMessages(reset, focusMessageId) {
  if (state.msg.loading) return;
  state.msg.loading = true;
  const body = $("#t-body");
  if (reset) { loadedMessages = []; body.replaceChildren(el("div", { class: "loading" }, "loading messages\u2026")); }
  try {
    const { source, id } = state.current;
    const p = new URLSearchParams({ offset: String(state.msg.offset), limit: String(MSG_PAGE) });
    const data = await getJSON(`/api/sessions/${enc(source)}/${enc(id)}/messages?` + p.toString());
    state.msg.hasMore = data.has_more;
    state.msg.offset += data.messages.length;
    loadedMessages.push(...data.messages);
    renderMessages(reset);
    if (focusMessageId) {
      const node = body.querySelector(`[data-mid="${CSS.escape(focusMessageId)}"]`);
      if (node) node.scrollIntoView({ block: "center" });
    }
  } catch (err) {
    if (reset) body.replaceChildren(el("div", { class: "loading" }, "error: " + err.message));
  } finally {
    state.msg.loading = false;
  }
}

function renderMessages(reset) {
  const body = $("#t-body");
  if (reset) body.replaceChildren();
  else body.querySelector(".load-more")?.remove();

  const start = body.querySelectorAll(".msg").length;
  for (let i = start; i < loadedMessages.length; i++) {
    const node = messageNode(loadedMessages[i]);
    if (node) body.append(node);
  }
  if (state.msg.hasMore) {
    body.append(el("button", { class: "load-more",
      onclick: () => loadMessages(false) },
      `load more (${state.msg.offset} of ${transcriptMeta.message_count} loaded)`));
  }
}

function rerenderMessages() {
  // Re-render already-loaded messages in place (toggle reasoning/tools).
  const body = $("#t-body");
  if (!body) return;
  body.querySelectorAll(".msg").forEach((n) => n.remove());
  const more = body.querySelector(".load-more");
  loadedMessages.forEach((m) => { const n = messageNode(m); if (n) more ? body.insertBefore(n, more) : body.append(n); });
  if (findState.term) runFind(findState.term);
}

function messageToText(m) {
  // Plain-text rendering of a single message, honouring current toggles.
  const lines = [];
  for (const p of m.parts) {
    if (p.type === "text" && p.text) lines.push(p.text);
    else if (p.type === "reasoning" && state.reasoning && p.text) lines.push("[reasoning] " + p.text);
    else if (p.type === "tool" && state.tools && p.text)
      lines.push(`[${p.tool_name || p.tool_status || "tool"}] ${p.text}`);
  }
  return lines.join("\n\n");
}

async function copyMessage(m, btn) {
  const text = messageToText(m);
  try {
    await navigator.clipboard.writeText(text);
    const prev = btn.textContent;
    btn.textContent = "\u2713";
    setTimeout(() => (btn.textContent = prev), 1100);
    toast(`copied message (${text.length} chars)`);
  } catch (err) { toast("copy failed: " + err.message); }
}

function messageNode(m) {
  const parts = [];
  for (const p of m.parts) {
    if (p.type === "text" && p.text) {
      const box = el("div", { class: "part text" });
      renderMarkdownInto(box, p.text);
      parts.push(box);
    } else if (p.type === "reasoning" && state.reasoning && p.text)
      parts.push(el("div", { class: "part reasoning" }, el("span", { class: "tag" }, "reasoning"), el("pre", {}, p.text)));
    else if (p.type === "tool" && state.tools && p.text) {
      const err = p.tool_status === "error";
      parts.push(el("div", { class: "part tool" + (err ? " is-error" : "") },
        el("span", { class: "tag" }, p.tool_name || p.tool_status || "tool"), el("pre", {}, p.text)));
    }
  }
  if (!parts.length) return null;
  const cls = m.role === "user" ? "user" : "assistant";
  const copyBtn = el("button", {
    class: "msg-copy", title: "copy this message",
    onclick: (e) => { e.stopPropagation(); copyMessage(m, e.currentTarget); },
  }, "\u29c9");
  return el("div", { class: "msg " + cls, dataset: { mid: m.id } },
    copyBtn,
    el("div", { class: "m-role" }, el("span", {}, m.role),
      m.created ? el("span", { class: "m-time" }, fmtDate(m.created)) : null),
    ...parts);
}

// (message-body scroll handler is attached per-render in renderHeader, since
//  the .t-body element is recreated for each opened session)

// ====================================================================
// in-transcript find
// ====================================================================

const findState = { term: "", hits: [], idx: -1 };

function clearFindMarks() {
  document.querySelectorAll("mark.find-hit").forEach((m) => {
    const parent = m.parentNode;
    parent.replaceChild(document.createTextNode(m.textContent), m);
    parent.normalize();
  });
}

function runFind(term) {
  clearFindMarks();
  findState.term = term;
  findState.hits = [];
  findState.idx = -1;
  const tl = term.trim().toLowerCase();
  if (!tl) { $("#find-count").textContent = ""; return; }
  const body = $("#t-body");
  const walker = document.createTreeWalker(body, NodeFilter.SHOW_TEXT);
  const targets = [];
  let node;
  while ((node = walker.nextNode())) {
    if (node.parentElement.closest("mark")) continue;
    if (node.nodeValue.toLowerCase().includes(tl)) targets.push(node);
  }
  for (const text of targets) {
    const frag = document.createDocumentFragment();
    const val = text.nodeValue;
    const low = val.toLowerCase();
    let i = 0, pos = low.indexOf(tl);
    while (pos !== -1) {
      if (pos > i) frag.append(val.slice(i, pos));
      const mark = el("mark", { class: "find-hit" }, val.slice(pos, pos + tl.length));
      frag.append(mark);
      findState.hits.push(mark);
      i = pos + tl.length;
      pos = low.indexOf(tl, i);
    }
    if (i < val.length) frag.append(val.slice(i));
    text.parentNode.replaceChild(frag, text);
  }
  $("#find-count").textContent = findState.hits.length ? `${findState.hits.length} found` : "no matches";
  if (findState.hits.length) stepFind(1);
}

function stepFind(dir) {
  if (!findState.hits.length) return;
  if (findState.idx >= 0) findState.hits[findState.idx]?.classList.remove("current");
  findState.idx = (findState.idx + dir + findState.hits.length) % findState.hits.length;
  const cur = findState.hits[findState.idx];
  cur.classList.add("current");
  cur.scrollIntoView({ block: "center" });
  $("#find-count").textContent = `${findState.idx + 1} / ${findState.hits.length}`;
}

// ====================================================================
// export / copy
// ====================================================================

// True when running inside the native pywebview window (no browser download
// manager or working window.print()); the Python bridge handles those.
function nativeApi() {
  return (window.pywebview && window.pywebview.api) || null;
}

function exportUrl(meta, fmt, download) {
  const p = new URLSearchParams({
    format: fmt, reasoning: String(state.reasoning), tools: String(state.tools),
    math: state.math,
  });
  if (download) p.set("download", "true");
  return `/api/export/${enc(meta.source)}/${enc(meta.id)}?${p}`;
}

const _EXT = { markdown: "md", md: "md", json: "json", html: "html", text: "txt", txt: "txt" };

async function downloadExport(meta, fmt) {
  const ext = _EXT[fmt] || "txt";
  const fname = `${meta.source}_${meta.short_id}.${ext}`;
  let text;
  try {
    const r = await fetch(exportUrl(meta, fmt, false));
    if (!r.ok) throw new Error(`${r.status}`);
    text = await r.text();
  } catch (err) { toast("export failed: " + err.message); return; }

  const api = nativeApi();
  if (api) {
    // Native window: save via a real OS dialog through the Python bridge.
    try {
      const res = await api.save_file(fname, text);
      toast(res.startsWith("saved") ? `saved ${fname}` : res);
    } catch (err) { toast("save failed: " + err.message); }
    return;
  }
  // Browser: force a real download via a Blob URL + the download attribute
  // (reliable regardless of the response Content-Type).
  const blob = new Blob([text], { type: "application/octet-stream" });
  const objUrl = URL.createObjectURL(blob);
  const a = el("a", { href: objUrl, download: fname });
  document.body.append(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(objUrl), 2000);
}

async function copySession(meta, fmt) {
  try {
    const r = await fetch(exportUrl(meta, fmt, false));
    const text = await r.text();
    await navigator.clipboard.writeText(text);
    toast(`copied ${text.length} chars (${fmt})`);
  } catch (err) { toast("copy failed: " + err.message); }
}

async function printSession(meta) {
  const api = nativeApi();
  if (api) {
    // The native webview can't reliably open the OS print dialog; open a
    // dedicated print page in the user's real browser, which can print.
    const q = `/print/${enc(meta.source)}/${enc(meta.id)}?` +
      new URLSearchParams({ reasoning: String(state.reasoning), tools: String(state.tools), math: state.math });
    try {
      await api.open_external(q);
      toast("opened print view in your browser");
    } catch (err) { toast("print failed: " + err.message); }
    return;
  }

  // Browser: load the print-friendly HTML in a hidden iframe and print it.
  toast("preparing print\u2026");
  let html;
  try {
    const r = await fetch(exportUrl(meta, "html", false));
    html = await r.text();
  } catch (err) { toast("print failed: " + err.message); return; }

  const frame = el("iframe", { class: "print-frame", "aria-hidden": "true" });
  document.body.append(frame);
  const doc = frame.contentWindow.document;
  doc.open();
  doc.write(html);
  doc.close();
  const go = () => {
    frame.contentWindow.focus();
    frame.contentWindow.print();
    setTimeout(() => frame.remove(), 1000);
  };
  if (doc.readyState === "complete") setTimeout(go, 150);
  else frame.onload = () => setTimeout(go, 150);
}

// ====================================================================
// keyboard navigation
// ====================================================================

function rowList() {
  return [...document.querySelectorAll(".session, .s-child")].filter((n) => n.offsetParent !== null);
}
function moveSelection(dir) {
  const rows = rowList();
  if (!rows.length) return;
  let idx = rows.findIndex((r) => r.classList.contains("kbd-sel"));
  rows.forEach((r) => r.classList.remove("kbd-sel"));
  idx = Math.max(0, Math.min(rows.length - 1, (idx === -1 ? 0 : idx + dir)));
  const row = rows[idx];
  row.classList.add("kbd-sel");
  row.style.outline = "1px solid var(--focus)";
  setTimeout(() => (row.style.outline = ""), 600);
  row.scrollIntoView({ block: "nearest" });
}
function openSelection() {
  const row = document.querySelector(".session.kbd-sel, .s-child.kbd-sel") || rowList()[0];
  if (row) openSession(row.dataset.source, row.dataset.id);
}

document.addEventListener("keydown", (e) => {
  const typing = ["INPUT", "TEXTAREA"].includes(document.activeElement.tagName);
  if (e.key === "/" && !typing) { e.preventDefault(); searchInput.focus(); searchInput.select(); return; }
  // Esc closes the About dialog, then the mobile drawer, wherever focus is.
  if (e.key === "Escape" && !$("#about-backdrop").hidden) {
    closeAbout(); return;
  }
  if (e.key === "Escape" && $("#rail").classList.contains("show")) {
    closeRail(); return;
  }
  if (typing) {
    if (e.key === "Escape") document.activeElement.blur();
    return;
  }
  if (e.key === "j") { e.preventDefault(); moveSelection(1); }
  else if (e.key === "k") { e.preventDefault(); moveSelection(-1); }
  else if (e.key === "Enter") { e.preventDefault(); openSelection(); }
  else if (e.key === "f" && state.current) {
    e.preventDefault(); $("#find-input")?.focus();
  }
  else if (e.key === "h" && state.current) {
    e.preventDefault(); toggleHeaderCollapsed();
  }
});

// ====================================================================
// boot
// ====================================================================

function clearAll() {
  // Reset to the initial "home" state: no query, default scope, all sources
  // on, no date filters, no open transcript.
  searchInput.value = "";
  state.query = "";
  state.scope = { titles: true, contents: false };
  updateScopeButtons();

  state.enabled = new Set(state.sources.map((s) => s.name));
  document.querySelectorAll(".src-toggle").forEach((b) => {
    b.setAttribute("aria-pressed", "true");
    const c = b.querySelector(".check");
    if (c) c.textContent = "\u2713";
  });

  state.since = ""; state.until = "";
  const since = $("#since"), until = $("#until");
  if (since) since.value = "";
  if (until) until.value = "";

  // Close the open transcript / stats view; return to browse.
  state.current = null;
  $("#transcript").hidden = true;
  $("#stats").hidden = true;
  $("#empty").hidden = false;
  markView("browse");
  history.replaceState(null, "", location.pathname);

  resetAndLoad();
  $("#rail").scrollTop = 0;
}

searchInput.addEventListener("input", onSearchInput);
scopeTitlesBtn.addEventListener("click", () => toggleScope("titles"));
scopeContentsBtn.addEventListener("click", () => toggleScope("contents"));
$("#view-browse").addEventListener("click", showBrowse);
$("#view-stats").addEventListener("click", openStats);
$("#sync-all").addEventListener("click", () => syncAll());
$("#archive-matching").addEventListener("click", archiveMatching);
$("#clear-selection").addEventListener("click", deselectSession);
$("#rail-toggle").addEventListener("click", toggleRail);
$("#rail-backdrop").addEventListener("click", closeRail);
$("#about-btn").addEventListener("click", openAbout);
$("#about-close").addEventListener("click", closeAbout);
$("#about-backdrop").addEventListener("click", (e) => {
  if (e.target === $("#about-backdrop")) closeAbout();  // click outside the card
});
// The native (pywebview) window traps target="_blank" in an in-app window, so
// route external links through the bridge to the user's real browser. In a
// normal browser tab this handler is skipped and the plain <a> opens a tab.
$("#about-repo").addEventListener("click", (e) => {
  const api = nativeApi();
  if (api) {
    e.preventDefault();
    api.open_link(e.currentTarget.href).catch(() => {});
  }
});
// Normalize drawer state when crossing the wide/narrow breakpoint, so an
// open drawer doesn't get "stuck" (stale .show / aria-expanded / backdrop)
// after a resize back and forth.
matchMedia("(min-width: 881px)").addEventListener("change", (e) => {
  if (e.matches) closeRail();
});
$("#brand").addEventListener("click", clearAll);
$("#brand").style.cursor = "pointer";
$("#theme-toggle").addEventListener("click", toggleTheme);
// Reflect whether a date filter is active, so the UI shows "all time" (and
// hides the clear button) when neither bound is set -- an empty native date
// input misleadingly renders today's date as a placeholder.
function syncDateActive() {
  const active = !!(state.since || state.until);
  const el = $("#date-filters");
  if (el) el.setAttribute("data-active", active ? "true" : "false");
}

// Date filters drive both views: refresh the stats page in place when it is
// open, otherwise reload the browse list.
function onDateChange() {
  syncDateActive();
  if (!$("#stats").hidden) openStats();
  else resetAndLoad();
}
$("#since").addEventListener("change", (e) => { state.since = e.target.value; onDateChange(); });
$("#until").addEventListener("change", (e) => { state.until = e.target.value; onDateChange(); });
$("#date-clear").addEventListener("click", () => {
  state.since = ""; state.until = "";
  $("#since").value = ""; $("#until").value = "";
  onDateChange();
});

function openFromHash() {
  const h = decodeURIComponent(location.hash.replace(/^#/, ""));
  const slash = h.indexOf("/");
  if (slash > 0) openSession(h.slice(0, slash), h.slice(slash + 1));
}
function prefillSearchFromUrl() {
  const q = new URLSearchParams(location.search).get("q");
  if (q) {
    // A ?q= deep link implies a content search.
    searchInput.value = q;
    state.query = q;
    state.scope.contents = true;
    return true;
  }
  return false;
}

// Heartbeat: when the server is launched with auto-shutdown, ping it on an
// interval so it knows the window is still open. When the window/tab closes,
// pings stop and the server shuts itself down (freeing the port).
async function startHeartbeat() {
  let cfg;
  try {
    cfg = await getJSON("/api/heartbeat-config");
  } catch {
    return;
  }
  if (!cfg || !cfg.enabled) return;
  const ms = Math.max((cfg.interval || 3) * 1000, 1000);
  const ping = () => { fetch("/api/heartbeat", { method: "POST", keepalive: true }).catch(() => {}); };
  ping();
  setInterval(ping, ms);
}

(async function boot() {
  initTheme();
  initMath();
  initMode();
  try {
    await loadSources();
    prefillSearchFromUrl();
    updateScopeButtons();
    await loadListPage(true);
    if (location.hash) openFromHash();
    else if (state.mode === "archive") showArchiveLanding();
    startHeartbeat();
  } catch (err) {
    sessionsEl.replaceChildren(el("li", { class: "loading" }, "failed to load: " + err.message));
  }
})();
