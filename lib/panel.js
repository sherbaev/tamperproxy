"use strict";
// Core of the Tamper panel: shared state, helpers, theme, header, tabs, polling and boot.
// TOKEN is defined inline in panel.html (the server injects a per-run value).
// Feature files (panel-rules/traffic/endpoints/pause.js) load after this one.
const POLL_MS = 1500;
const PAUSE_POLL_MS = 700;
const CONFIG_POLL_MS = 3000;
const TOAST_MS = 2800;
const UNDO_MS = 8000;
const RECENT_LIMIT = 200;
const $ = (id) => document.getElementById(id);
const state = {
  rules: [], recent: [], endpoints: [], bases: {}, library: [], stock: [], paused: [], proxy: {},
  settings: { ssl: { mode: "all", hosts: [] }, focus: { enabled: false, hosts: [] }, block: { enabled: false, hosts: [] },
              no_cache: false, latency_ms: 0, app_timeout_s: 30 },
  editing: null, action: "map_local", source: "stock", localFile: "", replaces: [], pauseOn: "response",
  filter: "all", livePaused: false, env: "",
};

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function icon(name) { return `<svg class="i" aria-hidden="true"><use href="#i-${name}"/></svg>`; }
function toast(message, { error = false, action = null } = {}) {
  const el = $("toast");
  $("toastText").textContent = message;
  el.classList.toggle("error", error);
  const button = $("toastAction");
  button.hidden = !action;
  button.onclick = action ? () => { el.classList.remove("show"); action.run(); } : null;
  if (action) button.textContent = action.label;
  el.classList.add("show");
  toast.duration = action ? UNDO_MS : TOAST_MS;
  scheduleToastHide();
}
function scheduleToastHide() {
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => $("toast").classList.remove("show"), toast.duration);
}
// Timing stays adjustable: the toast (and its Undo) stays open while hovered or focused.
for (const type of ["mouseenter", "focusin"]) $("toast").addEventListener(type, () => clearTimeout(toast.timer));
for (const type of ["mouseleave", "focusout"]) $("toast").addEventListener(type, scheduleToastHide);

async function api(path, body) {
  const options = { headers: { "X-Panel-Token": TOKEN } };
  if (body !== undefined) {
    options.method = "POST";
    options.headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(body);
  }
  const response = await fetch(path, options);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
  return data;
}
function escapeRegex(text) { return text.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"); }
function pathToRegex(path) {
  // {param} matches one path segment; the suffix stops /get-x matching /get-x-other.
  const body = path.split(/(\{[^}]+\})/).map((part) => part.startsWith("{") ? "[^/]+" : escapeRegex(part)).join("");
  return `${body}(\\?|$)`;
}
function fileName(path) { return String(path || "").split("/").pop(); }
function statusFromName(name) { const m = /^(\d{3})/.exec(fileName(name)); return m ? Number(m[1]) : 200; }
function actionLabel(action) {
  return { map_local: "Map Local", map_remote: "Map Remote", rewrite: "Rewrite", delay: "Delay",
           breakpoint: "Breakpoint", blocked: "Blocked" }[action] || action;
}
function copy(text) { navigator.clipboard.writeText(text).then(() => toast(`Copied ${text}`), () => toast("Copy failed", { error: true })); }
// Same host-pattern rules as settings.py: "api.example.com", "*.example.com" (subdomains), "*".
function hostMatches(pattern, host) {
  const h = String(host || "").toLowerCase().replace(/:\d+$/, "");
  if (pattern === "*") return true;
  if (pattern.startsWith("*.")) return h.endsWith(pattern.slice(1)) && h.length > pattern.length - 1;
  return h === pattern;
}
function matchesAnyHost(patterns, host) { return (patterns || []).some((p) => hostMatches(p, host)); }
function appTimeout() { return state.settings.app_timeout_s || 30; }
const EMULATOR_HOST = "10.0.2.2";  // the Android emulator's alias for the Mac
function proxyAddress(device) {
  return `${device === "android-emu" ? EMULATOR_HOST : state.proxy.host || "?"}:${state.proxy.port || "8080"}`;
}
function setupDevice() { return $("setupTabs").querySelector('[aria-selected="true"]').dataset.device; }
function urlPath(url) { try { return new URL(url).pathname; } catch (error) { return url; } }
function urlHost(url) { try { return new URL(url).host; } catch (error) { return ""; } }
function headersToText(headers) { return (headers || []).map(([k, v]) => `${k}: ${v ?? ""}`).join("\n"); }
function textToPairs(text) {
  return text.split("\n").map((line) => {
    const at = line.indexOf(":");
    return at > 0 ? [line.slice(0, at).trim(), line.slice(at + 1).trim()] : null;
  }).filter(Boolean);
}
function prettyBody(body) {
  if (body == null) return body;
  try { return JSON.stringify(JSON.parse(body), null, 2); } catch (error) { return body; }
}
function stored(key, fallback) { try { return localStorage.getItem(key) || fallback; } catch (error) { return fallback; } }
function store(key, value) { try { localStorage.setItem(key, value); } catch (error) { /* private mode: not remembered */ } }
function setPressed(container, attr, value) {
  for (const button of container.querySelectorAll(`[data-${attr}]`)) {
    button.setAttribute("aria-pressed", String(button.dataset[attr] === value));
  }
}

/* ---------- theme ---------- */
function applyTheme(theme) {
  document.documentElement.dataset.theme = theme;
  $("themeBtn").setAttribute("aria-label", `Switch to ${theme === "dark" ? "light" : "dark"} theme`);
  $("themeBtn").innerHTML = `<svg class="i"><use href="#i-${theme === "dark" ? "sun" : "moon"}"/></svg>`;
}
$("themeBtn").addEventListener("click", () => {
  const theme = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
  store("tamper-theme", theme);
  applyTheme(theme);
});
applyTheme(stored("tamper-theme", "dark"));

/* ---------- tabs ---------- */
function showTab(name) {
  const traffic = name === "traffic";
  $("tabTraffic").setAttribute("aria-selected", String(traffic));
  $("tabEndpoints").setAttribute("aria-selected", String(!traffic));
  $("paneTraffic").hidden = !traffic;
  $("paneEndpoints").hidden = traffic;
  store("tamper-tab", name);
}
$("tabTraffic").addEventListener("click", () => showTab("traffic"));
$("tabEndpoints").addEventListener("click", () => showTab("endpoints"));

/* ---------- header, setup, shortcuts ---------- */
for (const button of document.querySelectorAll("[data-close]")) button.addEventListener("click", () => button.closest("dialog").close());
$("copyProxy").addEventListener("click", () => copy(proxyAddress()));
$("copySetup").addEventListener("click", () => copy(proxyAddress(setupDevice())));
$("setupBtn").addEventListener("click", () => $("setup").showModal());
$("setupTabs").addEventListener("click", (event) => {
  const tab = event.target.closest("[data-device]");
  if (!tab) return;
  for (const b of $("setupTabs").querySelectorAll("button")) b.setAttribute("aria-selected", String(b === tab));
  for (const list of document.querySelectorAll("[data-steps]")) list.hidden = list.dataset.steps !== tab.dataset.device;
  $("setupAddr").textContent = proxyAddress(tab.dataset.device);
});
document.addEventListener("keydown", (event) => {
  if (document.querySelector("dialog[open]") || event.ctrlKey || event.metaKey || event.altKey) return;
  if (event.target.matches("input, textarea, select")) return;
  if (event.key === "n" || event.key === "N") { event.preventDefault(); openEditor(null); }
  if (event.key === "/") { event.preventDefault(); showTab("traffic"); $("trafficFilter").focus(); }
});

function showBanner(html) {
  $("errorBanner").innerHTML = html;
  $("errorBanner").hidden = false;
}
function showRestartBanner() {
  showBanner(`${icon("alert")}<span>The proxy was restarted. Reload this page to reconnect.</span>
    <button class="btn small" id="reloadBtn">Reload</button>`);
  $("reloadBtn").addEventListener("click", () => location.reload());
}

/* ---------- polling ---------- */
// Returns false once the proxy refuses this tab's token (it restarted): polling stops then.
async function poll(path, onData) {
  try {
    onData(await api(path));
    $("status").classList.remove("down");
    return true;
  } catch (error) {
    $("status").classList.add("down");
    if (error.message === "forbidden") { showRestartBanner(); return false; }
    return true;
  }
}
async function pollTraffic() {
  const alive = await poll("/api/recent", (recent) => { if (!state.livePaused) { state.recent = recent; renderTraffic(); } });
  if (alive) setTimeout(pollTraffic, POLL_MS);
}
async function pollPaused() {
  const alive = await poll("/api/paused", (paused) => updatePaused(paused));
  if (alive) setTimeout(pollPaused, PAUSE_POLL_MS);
}
const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);
function showRulesError(error) {
  if (error) showBanner(`${icon("alert")}<span>rules.json problem: ${esc(error)}. The proxy keeps the last valid rules.</span>`);
  else if (state.rulesError) $("errorBanner").hidden = true;
  state.rulesError = error;
}
// Rules and settings files can also be edited by hand or by an assistant: keep this page in step.
// Skipped while a dialog is open, so an editor never changes under the person using it.
async function pollConfig() {
  const alive = await poll("/api/state", (st) => {
    if (document.querySelector("dialog[open]")) return;
    // A broken file comes back as no rules: keep the last good list, like the proxy does.
    if (!st.error && !same(st.rules, state.rules)) { state.rules = st.rules; renderRules(); }
    if (st.error !== state.rulesError) showRulesError(st.error);
  });
  if (alive) {
    await poll("/api/settings", (settings) => {
      if (document.querySelector("dialog[open]") || same(settings, state.settings)) return;
      state.settings = settings;
      applySettingsToUi();
    });
    setTimeout(pollConfig, CONFIG_POLL_MS);
  }
}

/* ---------- boot ---------- */
async function boot() {
  showTab(stored("tamper-tab", "traffic"));
  try {
    const [st, eps, stock, library, settings] = await Promise.all([
      api("/api/state"), api("/api/endpoints"), api("/api/files?q="), api("/api/library"), api("/api/settings")]);
    state.settings = settings;
    state.rules = st.rules;
    state.endpoints = eps.endpoints;
    state.bases = eps.bases;
    state.library = library;
    state.stock = stock.filter((f) => f.root === "responses");
    state.proxy = st.proxy || {};
    $("proxyAddr").textContent = proxyAddress();
    $("setupAddr").textContent = proxyAddress(setupDevice());
    if (state.proxy.web_url) { $("webLink").href = state.proxy.web_url; $("webLink").hidden = false; }
    showRulesError(st.error);
    $("endpointOptions").innerHTML = state.endpoints.map((e) => `<option value="${esc(e.method)} ${esc(e.path)}">${esc(e.group)}</option>`).join("");
    renderRules();
    renderStock();
    renderTraffic();
    initEndpoints();
    fillStockSelect();
    applySettingsToUi();
  } catch (error) {
    $("status").classList.add("down");
    toast(`Panel cannot reach the proxy: ${error.message}`, { error: true });
  }
  pollTraffic();
  pollPaused();
  setTimeout(pollConfig, CONFIG_POLL_MS);
}
window.addEventListener("DOMContentLoaded", boot);
