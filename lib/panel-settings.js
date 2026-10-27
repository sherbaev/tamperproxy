"use strict";
// Proxy settings (Charles' Proxy menu): SSL proxying hosts, Focus, Block list, No caching, latency.

function linesToHosts(text) { return text.split("\n").map((h) => h.trim()).filter(Boolean); }

function settingsChips() {
  const s = state.settings;
  const chips = [];
  if (s.ssl.mode === "only" && s.ssl.hosts.length) chips.push(["ssl", `SSL: only ${s.ssl.hosts.length} host${s.ssl.hosts.length > 1 ? "s" : ""}`]);
  if (s.ssl.mode === "except" && s.ssl.hosts.length) chips.push(["ssl", `SSL off: ${s.ssl.hosts.length} host${s.ssl.hosts.length > 1 ? "s" : ""}`]);
  if (s.block.enabled && s.block.hosts.length) chips.push(["block", `Blocking ${s.block.hosts.length}`]);
  if (s.no_cache) chips.push(["cache", "No cache"]);
  if (s.latency_ms) chips.push(["latency", `+${s.latency_ms} ms`]);
  return chips;
}

function applySettingsToUi() {
  $("settingsChips").innerHTML = settingsChips()
    .map(([kind, label]) => `<button class="setting-chip c-${kind}" data-open-settings title="Open settings">${esc(label)}</button>`).join("");
  const focusHosts = state.settings.focus.enabled ? state.settings.focus.hosts : [];
  const focusReady = focusHosts.length > 0;
  $("focusFilter").hidden = !focusReady;
  $("focusBar").hidden = !focusReady;
  $("focusChips").innerHTML = focusHosts.map((host) =>
    `<span class="focus-chip">${esc(host)}<button class="chip-x" data-unfocus="${esc(host)}" aria-label="Unfocus ${esc(host)}">${icon("x")}</button></span>`).join("");
  if (!focusReady && state.filter === "focus") setFilter("all");
  renderTraffic();
}

function openSettings() {
  const s = state.settings;
  for (const radio of document.querySelectorAll('input[name="sslMode"]')) radio.checked = radio.value === s.ssl.mode;
  $("sslHosts").value = s.ssl.hosts.join("\n");
  $("blockOn").checked = s.block.enabled;
  $("blockHosts").value = s.block.hosts.join("\n");
  $("noCache").checked = s.no_cache;
  $("latencyMs").value = s.latency_ms;
  $("appTimeout").value = s.app_timeout_s;
  $("settingsDlg").showModal();
}

async function saveSettings(next, message) {
  try {
    state.settings = await api("/api/settings", next);
    applySettingsToUi();
    if (message) toast(message);
    return true;
  } catch (error) {
    toast(`Not saved: ${error.message}`, { error: true });
    return false;
  }
}

function settingsFromDialog() {
  const mode = document.querySelector('input[name="sslMode"]:checked');
  return {
    ssl: { mode: mode ? mode.value : "all", hosts: linesToHosts($("sslHosts").value) },
    focus: state.settings.focus,  // managed from the Traffic list
    block: { enabled: $("blockOn").checked, hosts: linesToHosts($("blockHosts").value) },
    no_cache: $("noCache").checked,
    latency_ms: Number($("latencyMs").value) || 0,
    app_timeout_s: Number($("appTimeout").value) || 30,
  };
}

function setFilter(filter) {
  state.filter = filter;
  setPressed($("paneTraffic").querySelector(".filters"), "filter", filter);
}

// Focus / unfocus a host from the Traffic list (like Charles' "Focus" on a host).
function toggleFocus(host) {
  const s = state.settings;
  const focused = s.focus.enabled && s.focus.hosts.includes(host);
  const hosts = focused ? s.focus.hosts.filter((h) => h !== host) : [...new Set([...(s.focus.enabled ? s.focus.hosts : []), host])];
  if (!focused) setFilter("focus");
  return saveSettings({ ...s, focus: { enabled: hosts.length > 0, hosts } }, focused ? `Unfocused ${host}` : `Focusing on ${host}`);
}

$("focusChips").addEventListener("click", (event) => {
  const button = event.target.closest("[data-unfocus]");
  if (button) toggleFocus(button.dataset.unfocus);
});
$("unfocusAll").addEventListener("click", () => {
  setFilter("all");
  saveSettings({ ...state.settings, focus: { enabled: false, hosts: [] } }, "Focus cleared");
});

// One-click host actions from Inspect, like Charles' right-click menu on a host.
function hostAction(kind, host) {
  const s = state.settings;
  const add = (list) => (list.includes(host) ? list : [...list, host]);
  if (kind === "block") {
    return saveSettings({ ...s, block: { enabled: true, hosts: add(s.block.hosts) } }, `Blocking ${host} (403)`);
  }
  // SSL off for this host: drop it from an "only" list, otherwise add it to the "except" list.
  const ssl = s.ssl.mode === "only"
    ? { mode: "only", hosts: s.ssl.hosts.filter((h) => h !== host) }
    : { mode: "except", hosts: add(s.ssl.mode === "except" ? s.ssl.hosts : []) };
  return saveSettings({ ...s, ssl }, `${host} is no longer decrypted (new connections)`);
}

$("settingsBtn").addEventListener("click", openSettings);
$("settingsChips").addEventListener("click", (event) => { if (event.target.closest("[data-open-settings]")) openSettings(); });
$("settingsSave").addEventListener("click", async () => {
  if (await saveSettings(settingsFromDialog(), "Settings saved")) $("settingsDlg").close();
});
$("settingsDlg").addEventListener("keydown", (event) => {
  if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) { event.preventDefault(); $("settingsSave").click(); }
});
