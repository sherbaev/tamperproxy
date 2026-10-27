"use strict";
// Recent traffic list and the Inspect dialog (full request/response of one call).

function visibleTraffic() {
  const text = $("trafficFilter").value.trim().toLowerCase();
  return state.recent.filter((r) => {
    if (state.filter === "focus" && !matchesAnyHost(state.settings.focus.hosts, urlHost(r.url))) return false;
    if (state.filter === "tampered" && !r.action) return false;
    if (state.filter === "errors" && !(r.status >= 400 || r.aborted)) return false;
    return !text || r.url.toLowerCase().includes(text);
  }).slice(0, RECENT_LIMIT);
}

function requestTags(r) {
  const tags = r.action ? [`<span class="badge b-${esc(r.action)}">${esc(actionLabel(r.action))}</span>`] : [];
  if (r.paused && r.action !== "breakpoint") tags.push(`<span class="badge b-breakpoint">Paused</span>`);
  if (r.aborted) tags.push(`<span class="badge b-aborted">Aborted</span>`);
  if (r.remote) tags.push(`<span class="badge b-map_remote" title="${esc(r.remote)}">→ ${esc(urlHost(r.remote))}</span>`);
  return tags.join("");
}

function renderTraffic() {
  const rows = visibleTraffic();
  $("trafficCount").textContent = String(state.recent.length);
  if (!rows.length) {
    const hasAny = state.recent.length > 0;
    $("trafficList").innerHTML = `<li class="empty">${icon("radio")}<strong>${hasAny ? "Nothing matches the filter" : "Waiting for traffic"}</strong>
      <span>${hasAny ? "Change the filter above." : `Set the device's proxy to <span class="mono">${esc(proxyAddress())}</span>.`}</span>
      ${hasAny ? "" : `<button class="btn" data-open-setup>${icon("phone")}Device setup</button>`}</li>`;
    return;
  }
  $("trafficList").innerHTML = rows.map((r) => {
    const time = new Date(r.ts * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23" });
    const code = r.status ? `<span class="code c${esc(String(r.status)[0])}">${esc(r.status)}</span>` : `<span class="code">–</span>`;
    const path = urlPath(r.url);
    return `<li class="req">
      <span class="time">${esc(time)}</span>
      <span class="method">${esc(r.method)}</span>
      <button class="req-open" data-inspect="${esc(r.id)}" aria-label="Inspect ${esc(r.method)} ${esc(path)}">
        <span class="path">${esc(path)}</span><span class="host">${esc(urlHost(r.url))}</span></button>
      <div class="req-status">${code}${requestTags(r)}</div>
      <div class="req-actions">
        <button class="icon-btn focus-btn" data-focus-host="${esc(hostOf(r.url))}" aria-pressed="${isFocused(r.url)}"
          aria-label="${isFocused(r.url) ? "Unfocus" : "Focus"} host ${esc(hostOf(r.url))}" title="${isFocused(r.url) ? "Unfocus" : "Focus"} ${esc(hostOf(r.url))}">${icon("target")}</button>
        <button class="btn small subtle" data-mock="${esc(r.id)}" aria-label="Mock ${esc(path)}">Mock</button>
        <button class="btn small subtle" data-rewrite="${esc(r.id)}" aria-label="Rewrite ${esc(path)}">Rewrite</button>
        <button class="icon-btn" data-break="${esc(r.id)}" aria-label="Breakpoint on ${esc(path)}" title="Breakpoint">${icon("pause")}</button>
      </div></li>`;
  }).join("");
}

function ruleFromRequest(request, action) {
  const preset = { url_regex: `${escapeRegex(urlPath(request.url))}(\\?|$)`, methods: [request.method], action };
  return action === "breakpoint" ? { ...preset, pause_on: "response" } : preset;
}

$("trafficFilter").addEventListener("input", renderTraffic);
$("paneTraffic").querySelector(".filters").addEventListener("click", (event) => {
  const filter = event.target.dataset.filter;
  if (!filter) return;
  state.filter = filter;
  setPressed(event.currentTarget, "filter", filter);
  renderTraffic();
});
$("pauseBtn").addEventListener("click", () => {
  state.livePaused = !state.livePaused;
  $("pauseBtn").setAttribute("aria-pressed", String(state.livePaused));
  $("pauseBtn").setAttribute("aria-label", state.livePaused ? "Resume live updates" : "Pause live updates");
  $("pauseBtn").innerHTML = `<svg class="i"><use href="#i-${state.livePaused ? "play" : "pause"}"/></svg>`;
  $("paneTraffic").classList.toggle("paused", state.livePaused);
});
function hostOf(url) { return urlHost(url).replace(/:\d+$/, ""); }
function isFocused(url) { return matchesAnyHost(state.settings.focus.hosts, urlHost(url)); }

$("trafficList").addEventListener("click", (event) => {
  if (event.target.closest("[data-open-setup]")) return $("setup").showModal();
  const focusButton = event.target.closest("[data-focus-host]");
  if (focusButton) return toggleFocus(focusButton.dataset.focusHost);
  const button = event.target.closest("[data-inspect], [data-mock], [data-rewrite], [data-break]");
  if (!button) return;
  const id = button.dataset.inspect || button.dataset.mock || button.dataset.rewrite || button.dataset.break;
  const request = state.recent.find((r) => r.id === id);
  if (!request) return;
  if (button.dataset.inspect) return openInspect(request);
  const action = button.dataset.mock ? "map_local" : (button.dataset.rewrite ? "rewrite" : "breakpoint");
  openEditor(null, ruleFromRequest(request, action));
});

/* ---------- inspect ---------- */
const inspectState = { request: null, detail: null, part: "response" };

function renderPart() {
  const part = inspectState.detail ? inspectState.detail[inspectState.part] : null;
  for (const b of $("inspectTabs").querySelectorAll("button")) b.setAttribute("aria-selected", String(b.dataset.part === inspectState.part));
  if (!part) {
    $("inspectPart").innerHTML = `<p class="muted">${inspectState.part === "response" ? "No response (the call was aborted or failed)." : "Not available."}</p>`;
    return;
  }
  const rows = part.headers.map(([k, v]) => `<tr><th>${esc(k)}</th><td>${esc(v)}</td></tr>`).join("");
  const body = part.binary ? `(binary body, ${part.size} bytes)` : (part.body ? prettyBody(part.body) : "(empty body)");
  $("inspectPart").innerHTML = `<table class="kv"><tbody>${rows || `<tr><td class="muted">no headers</td></tr>`}</tbody></table>
    <div class="label-row"><span class="label">Body${part.truncated ? " (first 128 KB)" : ""}</span>
      ${part.body ? `<button class="btn small subtle" id="copyBody">${icon("copy")}Copy</button>` : ""}</div>
    <pre class="preview tall">${esc(body)}</pre>`;
  if (part.body) $("copyBody").addEventListener("click", () => navigator.clipboard.writeText(part.body).then(() => toast("Body copied")));
}

async function openInspect(request) {
  inspectState.request = request;
  inspectState.part = "response";
  $("inspectTitle").innerHTML = `<span class="method">${esc(request.method)}</span> ${esc(urlPath(request.url))}`;
  const code = request.status ? `<span class="code c${esc(String(request.status)[0])}">${esc(request.status)}</span>` : "";
  $("inspectMeta").innerHTML = `${code}<span class="mono">${esc(request.url)}</span>${requestTags(request)}`;
  $("inspectHost").textContent = urlHost(request.url);
  $("inspectPart").innerHTML = `<p class="muted">Loading…</p>`;
  $("inspectHint").textContent = "";
  $("inspect").showModal();
  try {
    inspectState.detail = await api(`/api/flow?id=${encodeURIComponent(request.id)}`);
  } catch (error) {
    inspectState.detail = null;
    $("inspectHint").textContent = error.message;
  }
  $("inspectSave").disabled = !(inspectState.detail && inspectState.detail.response && inspectState.detail.response.body);
  renderPart();
}
$("inspectTabs").addEventListener("click", (event) => {
  const b = event.target.closest("[data-part]");
  if (b) { inspectState.part = b.dataset.part; renderPart(); }
});
function fromInspect(action, options) {
  $("inspect").close();
  openEditor(null, ruleFromRequest(inspectState.request, action), options);
}
$("hostActions").addEventListener("click", (event) => {
  const button = event.target.closest("[data-host-action]");
  if (!button) return;
  const host = hostOf(inspectState.request.url);
  return button.dataset.hostAction === "focus" ? toggleFocus(host) : hostAction(button.dataset.hostAction, host);
});
$("inspectMock").addEventListener("click", () => fromInspect("map_local"));
$("inspectRewrite").addEventListener("click", () => fromInspect("rewrite"));
$("inspectBreak").addEventListener("click", () => fromInspect("breakpoint"));
$("inspectSave").addEventListener("click", async () => {
  const response = inspectState.detail.response;
  const name = `${urlPath(inspectState.request.url).split("/").filter(Boolean).pop() || "response"}_${response.status}.json`;
  try {
    const saved = await api("/api/mock", { name, content: prettyBody(response.body) });
    toast(`Saved as mocks/${saved.name}`);
    $("inspect").close();
    openEditor(null, { ...ruleFromRequest(inspectState.request, "map_local"), local_file: saved.path, status_code: response.status });
  } catch (error) { toast(error.message, { error: true }); }
});
