"use strict";
// Rules list and rule editor (Map Local / Rewrite / Delay / Breakpoint).

function describe(rule) {
  const delay = rule.delay_ms ? ` · +${rule.delay_ms} ms` : "";
  const action = rule.action || "map_local";
  if (action === "map_local") return `${rule.status_code || 200} · ${fileName(rule.local_file)}${delay}`;
  if (action === "breakpoint") return `pause on ${rule.pause_on || "response"}`;
  if (action === "map_remote") return `→ ${rule.target || "?"}${delay}`;
  if (action === "rewrite") {
    const rw = rule.rewrite || {};
    const parts = [];
    if (rw.status_code) parts.push(`status → ${rw.status_code}`);
    const requestHeaders = Object.keys(rw.request_headers || {}).length;
    if (requestHeaders) parts.push(`${requestHeaders} request header${requestHeaders > 1 ? "s" : ""}`);
    const headers = Object.keys(rw.headers || {}).length;
    if (headers) parts.push(`${headers} response header${headers > 1 ? "s" : ""}`);
    const replaces = (rw.body_replace || []).length;
    if (replaces) parts.push(`${replaces} body replace${replaces > 1 ? "s" : ""}`);
    return (parts.join(", ") || "no change") + delay;
  }
  return `real response${delay}`;
}

function renderRules() {
  const active = state.rules.filter((r) => r.enabled !== false).length;
  $("rulesCount").textContent = `${active} / ${state.rules.length}`;
  if (!state.rules.length) {
    $("rulesList").innerHTML = `<li class="empty">${icon("rules")}<strong>No rules yet</strong>
      <span>Add one, pick an endpoint in <b>Endpoints</b>, or click <b>Mock</b> on a request.</span>
      <button class="btn primary" data-empty-add>${icon("plus")}New rule</button></li>`;
    return;
  }
  const last = state.rules.length - 1;
  $("rulesList").innerHTML = state.rules.map((rule, i) => {
    const action = rule.action || "map_local";
    const on = rule.enabled !== false;
    const name = rule.note || rule.url_regex;
    return `<li class="rule ${on ? "" : "off"}">
      <label class="switch"><input type="checkbox" role="switch" data-toggle="${i}" ${on ? "checked" : ""}
        aria-label="${esc(on ? "Disable" : "Enable")} rule: ${esc(name)}"><span></span></label>
      <div class="rule-main">
        <div class="rule-line"><span class="method">${esc((rule.methods || []).join(",") || "ANY")}</span>
          <span class="rule-regex">${esc(rule.url_regex)}</span></div>
        <div class="rule-meta"><span class="badge b-${esc(action)}">${esc(actionLabel(action))}</span>
          <span class="mono">${esc(describe(rule))}</span>${rule.note ? `<span>${esc(rule.note)}</span>` : ""}</div>
      </div>
      <div class="rule-actions">
        <button class="icon-btn" data-move="${i}:-1" ${i === 0 ? "disabled" : ""} aria-label="Move up: ${esc(name)}" title="Move up">${icon("up")}</button>
        <button class="icon-btn" data-move="${i}:1" ${i === last ? "disabled" : ""} aria-label="Move down: ${esc(name)}" title="Move down">${icon("down")}</button>
        <button class="icon-btn" data-edit="${i}" aria-label="Edit: ${esc(name)}" title="Edit">${icon("edit")}</button>
        <button class="icon-btn" data-dup="${i}" aria-label="Duplicate: ${esc(name)}" title="Duplicate (disabled copy)">${icon("copy")}</button>
        <button class="icon-btn danger" data-del="${i}" aria-label="Delete: ${esc(name)}" title="Delete">${icon("trash")}</button>
      </div></li>`;
  }).join("");
}

async function saveRules(next, message, undoRules) {
  try {
    const data = await api("/api/rules", next);
    state.rules = data.rules;
    $("errorBanner").hidden = true;
    renderRules();
    renderTraffic();
    if (message) toast(message, undoRules ? { action: { label: "Undo", run: () => saveRules(undoRules, "Restored") } } : {});
    return true;
  } catch (error) {
    toast(`Not saved: ${error.message}`, { error: true });
    return false;
  }
}

$("rulesList").addEventListener("change", (event) => {
  const index = event.target.dataset.toggle;
  if (index === undefined) return;
  const next = state.rules.map((rule, i) => i === Number(index) ? { ...rule, enabled: event.target.checked } : rule);
  saveRules(next, event.target.checked ? "Rule enabled" : "Rule disabled");
});
$("rulesList").addEventListener("click", (event) => {
  const button = event.target.closest("button");
  if (!button) return;
  const data = button.dataset;
  if (data.emptyAdd !== undefined) return openEditor(null);
  if (data.edit !== undefined) return openEditor(Number(data.edit));
  if (data.dup !== undefined) {
    const at = Number(data.dup);
    const copyRule = { ...state.rules[at], id: undefined, enabled: false };
    return saveRules([...state.rules.slice(0, at + 1), copyRule, ...state.rules.slice(at + 1)], "Duplicated (disabled)");
  }
  if (data.del !== undefined) {
    const before = state.rules;
    return saveRules(before.filter((_, i) => i !== Number(data.del)), "Rule deleted", before);
  }
  if (data.move !== undefined) {
    const [from, step] = data.move.split(":").map(Number);
    const to = from + step;
    if (to < 0 || to >= state.rules.length) return;
    const next = [...state.rules];
    [next[from], next[to]] = [next[to], next[from]];
    return saveRules(next);
  }
});
$("addBtn").addEventListener("click", () => openEditor(null));

/* ---------- editor ---------- */
function setAction(action) {
  state.action = action;
  setPressed($("actionChoice"), "action", action);
  $("mapLocalBox").hidden = action !== "map_local";
  $("rewriteBox").hidden = action !== "rewrite";
  $("mapRemoteBox").hidden = action !== "map_remote";
  $("breakpointBox").hidden = action !== "breakpoint";
  $("timingBox").hidden = action === "breakpoint";
  $("timingNum").textContent = action === "delay" ? "3" : "4";
}
function setSource(source) {
  state.source = source;
  for (const button of $("sourceTabs").querySelectorAll("button")) button.setAttribute("aria-selected", String(button.dataset.source === source));
  $("stockBox").hidden = source !== "stock";
  $("fileBox").hidden = source !== "file";
  $("pasteBox").hidden = source !== "paste";
  $("selectedLine").hidden = source === "paste";
  if (source === "paste") $("filePreview").hidden = true;
  if (source === "file") refreshFiles();
}
async function selectFile(path, setStatus) {
  state.localFile = path;
  $("selectedFile").textContent = path ? fileName(path) : "none";
  renderStock();
  if (setStatus) $("statusInput").value = statusFromName(path);
  if (!path) { $("filePreview").hidden = true; return; }
  try {
    const data = await api(`/api/file?path=${encodeURIComponent(path)}`);
    $("filePreview").textContent = data.content || "(empty body)";
  } catch (error) {
    $("filePreview").textContent = `Cannot preview: ${error.message}`;
  }
  $("filePreview").hidden = false;
}
function stockLabel(file) { return file.name.replace(/\.json$/, "").replace(/^\d{3}_?/, "").replace(/_/g, " ") || "body"; }
function renderStock() {
  $("stockList").innerHTML = state.stock.map((file) =>
    `<button data-path="${esc(file.path)}" aria-pressed="${file.path === state.localFile}"><b>${esc(statusFromName(file.name))}</b>${esc(stockLabel(file))}</button>`
  ).join("") || `<span class="muted">No files in responses/</span>`;
}
$("stockList").addEventListener("click", (event) => {
  const button = event.target.closest("[data-path]");
  if (button) selectFile(button.dataset.path, true);
});
function renderFileResults(files) {
  $("fileResults").innerHTML = files.map((f) => {
    const size = f.size != null ? ` · ${Math.ceil(f.size / 1024)} KB` : "";
    return `<button data-path="${esc(f.path)}" aria-pressed="${f.path === state.localFile}"><span>${esc(f.name)}</span><small>${esc(f.root)}${size}</small></button>`;
  }).join("") || `<p class="help" style="padding:8px 10px;margin:0">Nothing found. Start <span class="mono">proxy.sh --library &lt;your tamper folder&gt;</span> to search it here.</p>`;
}
async function searchFiles() {
  try {
    renderFileResults(await api(`/api/files?q=${encodeURIComponent($("fileSearch").value)}`));
  } catch (error) { toast(error.message, { error: true }); }
}
// state.presetFiles: the tamper files of one endpoint (from the Endpoints tab), shown until the user types.
function refreshFiles() { return state.presetFiles ? renderFileResults(state.presetFiles) : searchFiles(); }
let searchTimer;
$("fileSearch").addEventListener("input", () => {
  state.presetFiles = null;
  clearTimeout(searchTimer);
  searchTimer = setTimeout(searchFiles, 200);
});
$("fileResults").addEventListener("click", (event) => {
  const row = event.target.closest("[data-path]");
  if (row) { selectFile(row.dataset.path, false); refreshFiles(); }
});
$("actionChoice").addEventListener("click", (event) => { const b = event.target.closest("[data-action]"); if (b) setAction(b.dataset.action); });
$("sourceTabs").addEventListener("click", (event) => { const b = event.target.closest("[data-source]"); if (b) setSource(b.dataset.source); });
$("pauseOn").addEventListener("click", (event) => {
  const b = event.target.closest("[data-pause]");
  if (!b) return;
  state.pauseOn = b.dataset.pause;
  setPressed($("pauseOn"), "pause", state.pauseOn);
});

function validateRegex() {
  const value = $("regexInput").value.trim();
  const error = $("regexError");
  const matches = $("regexMatches");
  error.hidden = true;
  $("regexInput").removeAttribute("aria-invalid");
  matches.innerHTML = "";
  if (!value) return true;
  let pattern;
  try {
    pattern = new RegExp(value);
  } catch (exc) {
    error.innerHTML = `${icon("alert")}<span>${esc(exc.message.split(": ").pop())}</span>`;
    error.hidden = false;
    $("regexInput").setAttribute("aria-invalid", "true");
    return false;
  }
  const method = $("methodSel").value;
  const hits = [...new Set(state.recent.filter((r) => (!method || r.method === method) && pattern.test(r.url)).map((r) => r.url))];
  matches.innerHTML = hits.length
    ? `<span class="ok">Matches ${hits.length} recent request${hits.length > 1 ? "s" : ""}</span><ul>${hits.slice(0, 3).map((u) => `<li>${esc(u)}</li>`).join("")}</ul>`
    : `<span class="none">No recent request matches yet</span>`;
  return true;
}
$("regexInput").addEventListener("input", validateRegex);
$("methodSel").addEventListener("change", validateRegex);

function renderReplaces() {
  $("rwReplaces").innerHTML = state.replaces.map((r, i) => `<div class="rep">
    <input data-rep="${i}" data-key="find" placeholder="find" aria-label="Find text ${i + 1}" value="${esc(r.find)}">
    <input data-rep="${i}" data-key="replace" placeholder="replace with" aria-label="Replace with ${i + 1}" value="${esc(r.replace)}">
    <button class="icon-btn danger" data-rep-del="${i}" aria-label="Remove replace ${i + 1}">${icon("x")}</button></div>`).join("");
}
$("rwReplaces").addEventListener("input", (event) => {
  const { rep, key } = event.target.dataset;
  if (rep === undefined) return;
  state.replaces = state.replaces.map((r, i) => i === Number(rep) ? { ...r, [key]: event.target.value } : r);
});
$("rwReplaces").addEventListener("click", (event) => {
  const button = event.target.closest("[data-rep-del]");
  if (!button) return;
  state.replaces = state.replaces.filter((_, i) => i !== Number(button.dataset.repDel));
  renderReplaces();
});
$("addReplace").addEventListener("click", () => { state.replaces = [...state.replaces, { find: "", replace: "" }]; renderReplaces(); });

$("endpointInput").addEventListener("change", () => {
  const typed = $("endpointInput").value.trim();
  const match = /^(GET|POST|PUT|PATCH|DELETE)\s+(\S+)/.exec(typed);
  if (match) {
    $("methodSel").value = match[1];
    $("regexInput").value = pathToRegex(match[2]);
  } else if (typed) {
    $("regexInput").value = escapeRegex(typed);
  }
  validateRegex();
});

function rewriteHeadersToText(headers) { return Object.entries(headers || {}).map(([k, v]) => `${k}: ${v ?? ""}`).join("\n"); }
function textToRewriteHeaders(text) {
  return Object.fromEntries(textToPairs(text).map(([name, value]) => [name, value === "" ? null : value]));
}

// options.files: open Map Local on the "Tamper file" tab showing just these files (an endpoint's tampers).
function openEditor(index, preset, options = {}) {
  const rule = index !== null ? state.rules[index] : { action: "map_local", status_code: 200, ...preset };
  state.editing = index;
  $("editorTitle").textContent = index !== null ? "Edit rule" : "New rule";
  $("endpointInput").value = "";
  $("methodSel").value = (rule.methods || [])[0] || "";
  $("regexInput").value = rule.url_regex || "";
  $("noteInput").value = rule.note || "";
  $("statusInput").value = rule.status_code || 200;
  $("ctypeInput").value = rule.content_type || "application/json";
  $("delayInput").value = rule.delay_ms || 0;
  $("remoteTarget").value = rule.target || "";
  const rw = rule.rewrite || {};
  $("rwStatus").value = rw.status_code || "";
  $("rwHeaders").value = rewriteHeadersToText(rw.headers);
  $("rwReqHeaders").value = rewriteHeadersToText(rw.request_headers);
  state.replaces = (rw.body_replace || []).map((r) => ({ ...r }));
  renderReplaces();
  state.pauseOn = rule.pause_on || "response";
  setPressed($("pauseOn"), "pause", state.pauseOn);
  $("pasteName").value = "";
  $("pasteBody").value = "";
  setAction(rule.action || "map_local");
  state.presetFiles = options.files || null;
  $("fileSearch").value = "";
  $("fileSearch").placeholder = state.presetFiles
    ? `${state.presetFiles.length} tamper files for this endpoint. Type to search all files`
    : "Words in the file name, e.g. orders empty";
  const isStock = !rule.local_file || state.stock.some((f) => f.path === rule.local_file);
  setSource(state.presetFiles ? "file" : (isStock ? "stock" : "file"));
  selectFile(rule.local_file || "", false);
  validateRegex();
  $("editor").showModal();
  (rule.url_regex ? $("noteInput") : $("endpointInput")).focus();
}

async function mapLocalFile() {
  if (state.source !== "paste") return state.localFile;
  if (!$("pasteBody").value.trim()) throw new Error("Paste a body first");
  const saved = await api("/api/mock", { name: $("pasteName").value || "mock.json", content: $("pasteBody").value });
  return saved.path;
}
async function buildRule() {
  const base = state.editing !== null ? state.rules[state.editing] : { enabled: true };
  const method = $("methodSel").value;
  const { rewrite, local_file, status_code, content_type, pause_on, target, ...common } = {
    ...base, url_regex: $("regexInput").value.trim(), methods: method ? [method] : [],
    note: $("noteInput").value.trim(), action: state.action, delay_ms: Number($("delayInput").value) || 0,
  };
  if (!common.url_regex) { $("regexInput").focus(); throw new Error("URL regex is empty"); }
  if (!validateRegex()) { $("regexInput").focus(); throw new Error("Fix the URL regex first"); }
  if (state.action === "map_local") {
    const file = await mapLocalFile();
    if (!file) throw new Error("Pick a response body");
    return { ...common, local_file: file, status_code: Number($("statusInput").value) || 200,
             content_type: $("ctypeInput").value.trim() || "application/json" };
  }
  if (state.action === "rewrite") {
    const status = $("rwStatus").value;
    return { ...common, rewrite: { status_code: status ? Number(status) : null,
                                   request_headers: textToRewriteHeaders($("rwReqHeaders").value),
                                   headers: textToRewriteHeaders($("rwHeaders").value),
                                   body_replace: state.replaces.filter((r) => r.find) } };
  }
  if (state.action === "breakpoint") return { ...common, delay_ms: 0, pause_on: state.pauseOn };
  if (state.action === "map_remote") {
    const remote = $("remoteTarget").value.trim();
    if (!/^https?:\/\/[^/\s]+/.test(remote)) { $("remoteTarget").focus(); throw new Error("Target must look like https://staging.example.com"); }
    return { ...common, target: remote };
  }
  return common;
}
async function saveEditor() {
  try {
    const rule = await buildRule();
    const next = state.editing !== null
      ? state.rules.map((r, i) => i === state.editing ? rule : r)
      : [rule, ...state.rules];
    if (await saveRules(next, state.editing !== null ? "Rule updated" : "Rule added")) $("editor").close();
  } catch (error) { toast(error.message, { error: true }); }
}
$("saveBtn").addEventListener("click", saveEditor);
$("editor").addEventListener("keydown", (event) => {
  if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) { event.preventDefault(); saveEditor(); }
});
