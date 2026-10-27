"use strict";
// Endpoints tab: every backend endpoint from endpoints.txt with its base URL per environment,
// the tamper files named after it, and one-click Map Local / Rewrite / Breakpoint.

const IGNORED_SEGMENTS = /^(api|v\d+(\.\d+)*|\{.*\})$/i;
const MIN_KEY_LENGTH = 4;

// "get-orders" for /v2/shop/get-orders: the name tamper files are usually called after.
function endpointKey(path) {
  const segments = path.split("/").filter((s) => s && !IGNORED_SEGMENTS.test(s));
  return (segments.pop() || "").toLowerCase();
}

// Optional plural "s": a file like "users_admin.json" also belongs to an endpoint named "user".
function namePattern(key) { return new RegExp(`(^|[^a-z0-9])${escapeRegex(key)}s?(?![a-z0-9])`, "i"); }

// Files named after the endpoint, including variants ("get-orders-cancelled.json"), minus
// files of a longer endpoint sharing the name ("get-orders-summary").
function tampersFor(path) {
  const key = endpointKey(path);
  if (key.length < MIN_KEY_LENGTH) return [];
  const pattern = namePattern(key);
  const longer = [...new Set(state.endpoints.map((e) => endpointKey(e.path)))]
    .filter((other) => other.length > key.length && other.startsWith(key))
    .map(namePattern);
  return state.library.filter((f) => {
    const name = fileName(f.name);
    return pattern.test(name) && !longer.some((other) => other.test(name));
  });
}

function fullUrl(endpoint) {
  const base = (state.bases[endpoint.base] || {})[state.env];
  if (!base) return endpoint.path;
  return new URL(endpoint.path, base).toString().replace(/%7B/g, "{").replace(/%7D/g, "}");
}

function visibleEndpoints() {
  const words = $("endpointFilter").value.trim().toLowerCase().split(/\s+/).filter(Boolean);
  const onlyTampered = $("withTampers").checked;
  return state.endpoints
    .map((e, index) => ({ ...e, index, tampers: tampersFor(e.path) }))
    .filter((e) => words.every((w) => `${e.method} ${e.path} ${e.group} ${e.base}`.toLowerCase().includes(w)))
    .filter((e) => !onlyTampered || e.tampers.length);
}

function renderEndpoints() {
  const rows = visibleEndpoints();
  $("endpointCount").textContent = String(state.endpoints.length);
  if (!state.endpoints.length) {
    $("endpointList").innerHTML = `<div class="empty">${icon("list")}<strong>No endpoints.txt</strong><span>Start with --profile pointing at a folder with endpoints.txt and base-urls.txt.</span></div>`;
    return;
  }
  if (!rows.length) {
    $("endpointList").innerHTML = `<div class="empty">${icon("search")}<strong>No endpoint matches</strong><span>Try another word, or untick “with tampers”.</span></div>`;
    return;
  }
  const groups = new Map();
  for (const e of rows) {
    const key = `${e.group}·${e.base}`;
    groups.set(key, [...(groups.get(key) || []), e]);
  }
  $("endpointList").innerHTML = [...groups.values()].map((items) => {
    const { group, base } = items[0];
    const baseUrl = (state.bases[base] || {})[state.env] || "";
    return `<div class="ep-group">
      <div class="ep-head"><strong>${esc(group)}</strong><span class="method">${esc(base)}</span><span class="host">${esc(baseUrl)}</span></div>
      <ul class="list">${items.map(endpointRow).join("")}</ul></div>`;
  }).join("");
}

function endpointRow(e) {
  const count = e.tampers.length;
  const tamperChip = count
    ? `<button class="chip-btn" data-ep-tampers="${e.index}" aria-label="${count} tamper files for ${esc(e.path)}">${count} tamper${count > 1 ? "s" : ""}</button>`
    : "";
  return `<li class="ep-row">
    <span class="method">${esc(e.method)}</span>
    <div class="ep-path"><span class="path">${esc(e.path)}</span>${tamperChip}</div>
    <div class="req-actions">
      <button class="icon-btn" data-ep-copy="${e.index}" aria-label="Copy full URL of ${esc(e.path)}" title="Copy full URL">${icon("copy")}</button>
      <button class="btn small subtle" data-ep-mock="${e.index}" aria-label="Map Local ${esc(e.path)}">Map Local</button>
      <button class="btn small subtle" data-ep-rewrite="${e.index}" aria-label="Rewrite ${esc(e.path)}">Rewrite</button>
      <button class="icon-btn" data-ep-break="${e.index}" aria-label="Breakpoint on ${esc(e.path)}" title="Breakpoint">${icon("pause")}</button>
    </div></li>`;
}

function ruleFromEndpoint(endpoint, action) {
  const preset = { url_regex: pathToRegex(endpoint.path), methods: [endpoint.method], action, note: endpoint.group };
  return action === "breakpoint" ? { ...preset, pause_on: "response" } : preset;
}

// Environment buttons come from base-urls.txt (any names: dev/prod, staging/production, …).
function initEndpoints() {
  const envs = [...new Set(Object.values(state.bases).flatMap((urls) => Object.keys(urls)))];
  const saved = stored("tamper-env", envs[0] || "");
  state.env = envs.includes(saved) ? saved : (envs[0] || "");
  $("envFilters").innerHTML = envs.map((env) => `<button data-env="${esc(env)}" aria-pressed="${env === state.env}">${esc(env.toUpperCase())}</button>`).join("");
  $("envFilters").hidden = envs.length < 2;
  renderEndpoints();
}

$("envFilters").addEventListener("click", (event) => {
  const env = event.target.dataset.env;
  if (!env) return;
  state.env = env;
  store("tamper-env", env);
  setPressed(event.currentTarget, "env", env);
  renderEndpoints();
});
let endpointTimer;
$("endpointFilter").addEventListener("input", () => { clearTimeout(endpointTimer); endpointTimer = setTimeout(renderEndpoints, 120); });
$("withTampers").addEventListener("change", renderEndpoints);
$("endpointList").addEventListener("click", (event) => {
  const button = event.target.closest("button");
  if (!button) return;
  const d = button.dataset;
  const index = d.epTampers ?? d.epCopy ?? d.epMock ?? d.epRewrite ?? d.epBreak;
  if (index === undefined) return;
  const endpoint = state.endpoints[Number(index)];
  if (d.epCopy !== undefined) return copy(fullUrl(endpoint));
  if (d.epRewrite !== undefined) return openEditor(null, ruleFromEndpoint(endpoint, "rewrite"));
  if (d.epBreak !== undefined) return openEditor(null, ruleFromEndpoint(endpoint, "breakpoint"));
  // Map Local (or the tamper chip): open on exactly the tamper files counted for this endpoint.
  const files = tampersFor(endpoint.path);
  openEditor(null, ruleFromEndpoint(endpoint, "map_local"), files.length ? { files } : {});
});
