"use strict";
// Breakpoints: calls paused by a Breakpoint rule wait here until someone continues or aborts them.

const pauseState = { current: null, seen: new Set(), renderedIds: "" };

function secondsLeft(paused) { return Math.max(0, Math.round(appTimeout() - (Date.now() / 1000 - paused.paused_at))); }

function updatePaused(paused) {
  state.paused = paused;
  const fresh = paused.filter((p) => !pauseState.seen.has(p.id));
  for (const p of fresh) pauseState.seen.add(p.id);
  renderPausedBar();
  document.title = paused.length ? `⏸ ${paused.length} paused · Tamper panel` : "Tamper panel";
  if (pauseState.current && !paused.some((p) => p.id === pauseState.current.id)) {
    $("pauseDlg").close();  // it continued elsewhere or timed out
  }
  // Like Charles: a new breakpoint hit pops up on its own unless something else is open.
  if (fresh.length && !document.querySelector("dialog[open]")) openPause(fresh[0]);
}

function renderPausedBar() {
  const paused = state.paused;
  $("pausedBar").hidden = !paused.length;
  if (!paused.length) {
    pauseState.renderedIds = "";
    return;
  }
  $("pausedTitle").textContent = `${paused.length} call${paused.length > 1 ? "s" : ""} paused at a breakpoint`;
  $("pausedHint").textContent = `Your app gives up after about ${appTimeout()} s, so decide quickly.`;
  const ids = paused.map((p) => p.id).join(",");
  if (ids === pauseState.renderedIds) return tickPausedBar();
  pauseState.renderedIds = ids;
  $("pausedList").innerHTML = paused.map((p) => `<li class="paused-row">
    <span class="badge b-breakpoint">${esc(p.phase)}</span>
    <span class="method">${esc(p.method)}</span>
    <span class="path">${esc(urlPath(p.url))}</span>
    <span class="countdown" data-countdown="${esc(p.id)}"></span>
    <button class="btn small primary" data-pause-open="${esc(p.id)}">Edit</button>
    <button class="btn small" data-pause-go="${esc(p.id)}">Continue</button>
    <button class="btn small danger-btn" data-pause-abort="${esc(p.id)}">Abort</button></li>`).join("");
  tickPausedBar();
}

// Only the countdown text changes every second: rebuilding the rows would swallow a click in progress.
function tickPausedBar() {
  for (const p of state.paused) {
    const cell = $("pausedList").querySelector(`[data-countdown="${CSS.escape(p.id)}"]`);
    if (!cell) continue;
    cell.textContent = `${secondsLeft(p)} s`;
    cell.classList.toggle("late", secondsLeft(p) <= 10);
  }
}
setInterval(() => { if (state.paused.length) { tickPausedBar(); tickCountdown(); } }, 1000);

$("pausedList").addEventListener("click", (event) => {
  const button = event.target.closest("button");
  if (!button) return;
  const id = button.dataset.pauseOpen || button.dataset.pauseGo || button.dataset.pauseAbort;
  const paused = state.paused.find((p) => p.id === id);
  if (!paused) return;
  if (button.dataset.pauseOpen) return openPause(paused);
  decide(paused.id, button.dataset.pauseGo ? { action: "continue" } : { action: "abort" });
});

function tickCountdown() {
  if (!pauseState.current) return;
  const left = secondsLeft(pauseState.current);
  $("pauseCountdown").textContent = left ? `app gives up in ~${left} s` : "the app has probably given up already";
  $("pauseCountdown").classList.toggle("late", left <= 10);
}

function openPause(paused) {
  pauseState.current = paused;
  const isResponse = paused.phase === "response";
  $("pauseTitle").innerHTML = `<span class="badge b-breakpoint">Paused · ${esc(paused.phase)}</span> <span class="method">${esc(paused.method)}</span> ${esc(urlPath(paused.url))}`;
  $("pauseUrl").textContent = paused.url;
  $("pauseStatusRow").hidden = !isResponse;
  $("pauseStatus").value = isResponse ? paused.status : "";
  $("pauseHeaders").value = headersToText(paused.headers);
  const editable = !paused.binary && !paused.truncated;
  $("pauseBody").value = paused.binary ? "" : (paused.body || "");
  $("pauseBody").disabled = !editable;
  $("pauseFormat").disabled = !editable;
  $("pauseBodyNote").hidden = editable;
  $("pauseBodyNote").textContent = paused.binary ? `Binary body (${paused.size} bytes): sent unchanged.` : "Body too large to edit here: sent unchanged.";
  $("pauseRespondRow").hidden = isResponse;
  tickCountdown();
  if (!$("pauseDlg").open) $("pauseDlg").showModal();
  $(isResponse ? "pauseStatus" : "pauseHeaders").focus();
}

function fillStockSelect() {
  $("pauseStock").innerHTML = state.stock.map((f) => `<option value="${esc(f.path)}">${esc(statusFromName(f.name))} ${esc(stockLabel(f))}</option>`).join("");
}

async function decide(id, decision) {
  try {
    await api(`/api/paused/${encodeURIComponent(id)}`, decision);
    toast({ continue: "Continued", abort: "Aborted: the app gets a connection error", respond: "Answered with the ready response" }[decision.action]);
  } catch (error) {
    toast(error.message, { error: true });
  }
  state.paused = state.paused.filter((p) => p.id !== id);
  renderPausedBar();
  if (pauseState.current && pauseState.current.id === id) $("pauseDlg").close();
}

function editedDecision() {
  const paused = pauseState.current;
  const edit = { headers: textToPairs($("pauseHeaders").value) };
  if (!$("pauseBody").disabled) edit.body = $("pauseBody").value;
  if (paused.phase === "response" && $("pauseStatus").value) edit.status = Number($("pauseStatus").value);
  return { action: "continue", edit };
}

$("pauseContinue").addEventListener("click", () => decide(pauseState.current.id, editedDecision()));
$("pauseAsIs").addEventListener("click", () => decide(pauseState.current.id, { action: "continue" }));
$("pauseAbort").addEventListener("click", () => decide(pauseState.current.id, { action: "abort" }));
$("pauseRespond").addEventListener("click", () => {
  const file = $("pauseStock").value;
  decide(pauseState.current.id, { action: "respond", local_file: file, status: statusFromName(file) });
});
$("pauseFormat").addEventListener("click", () => {
  const formatted = prettyBody($("pauseBody").value);
  if (formatted === $("pauseBody").value) return toast("Body is not JSON", { error: true });
  $("pauseBody").value = formatted;
});
$("pauseDlg").addEventListener("keydown", (event) => {
  if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) { event.preventDefault(); $("pauseContinue").click(); }
});
// Esc only hides the editor: the call stays paused and remains in the bar above.
$("pauseDlg").addEventListener("close", () => { pauseState.current = null; });
