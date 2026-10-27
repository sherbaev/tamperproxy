---
name: tamper-proxy
description: Mock, rewrite, redirect, slow down, block or pause a mobile app's HTTP(S) calls with tamper-proxy (an open-source Charles Proxy alternative on mitmproxy) on Android or iOS, and run repeatable "mock + tap + check the screen" runs on Android. Use instead of Charles whenever a task needs a mocked/tampered API response, a specific error state (401/403/500/empty list), a changed request or response header, a slow or failing endpoint, a host routed to another backend, or proof that an app handles a given response.
---

# tamper-proxy for assistants

You drive tamper-proxy through two JSON files and two log files. The proxy re-reads the JSON files
whenever they change, so **applying a change = saving a file**. The browser panel shows the same
state to the human; never fight it, just edit the files. Paths are relative to the tamper-proxy
folder (where `proxy.sh` is). Human guide: `README.md`.

| File | What | Who writes it |
|---|---|---|
| `rules.json` (in the profile folder with `--profile`) | tamper rules, first match wins | you or the panel |
| `settings.json` (next to `rules.json`) | SSL proxying, Focus, Block list, No caching, latency | you or the panel |
| `logs/served.jsonl` | every call a rule mocked / rewrote / redirected / delayed, or the Block list stopped | the proxy |
| `logs/unmatched.jsonl` | calls that went to the real backend untouched | the proxy |

## 1. Start (or reuse) the proxy

- Running already? `lsof -nP -iTCP:8080 -sTCP:LISTEN`. Then just edit the files.
- Otherwise: `./proxy.sh --no-open` (add `--profile <dir>` for an app profile, `--library <dir>` for
  a folder of mock files, `--headless` when nobody needs the panel). First run: `bash setup.sh`.
- The device must use the proxy (`Device proxy : <ip>:8080` in the output) and trust the
  certificate. That is a human step on real devices; ask for it, don't assume it. Android:
  `./proxy.sh --adb` sets the proxy over adb (some devices, e.g. MIUI, refuse: then Wi-Fi settings).
  iOS Simulator: `./proxy.sh cert-ios-sim` once + `./proxy.sh --system-proxy`.

## 2. Find the endpoint

With a profile, `endpoints.txt` lists `METHOD /path` lines under `# <Api> · <BASE KEY>` headers and
`base-urls.txt` gives each key's URL per environment. Without one, read the URLs in
`logs/unmatched.jsonl` after the human reproduces the flow. Build `url_regex` from the path:
escape dots, `{param}` → `[^/]+`, end with `(\?|$)` so `/orders` does not also match
`/orders-summary` (unless a query parameter must match, e.g. `orders.*status=open`).

## 3. Write the rule

Keep the existing rules; insert yours where it should win (first match wins). One rule per object:

```json
{"note": "why", "url_regex": "/api/orders(\\?|$)", "methods": ["GET"],
 "action": "map_local", "local_file": "responses/500_server_error.json", "status_code": 500}
```

| Need | Rule |
|---|---|
| Fixed answer, backend not called | `"action": "map_local", "local_file": "...", "status_code": 200` |
| Same call, another backend | `"action": "map_remote", "target": "https://staging.example.com"` (a path in the target replaces the path; the query is kept) |
| Change headers / status / body | `"action": "rewrite", "rewrite": {"status_code": 401, "request_headers": {"Authorization": "Bearer expired"}, "headers": {"Cache-Control": "no-store", "X-Drop": null}, "body_replace": [{"find": "\"active\"", "replace": "\"closed\""}]}` (`null` removes a header) |
| Slow call | `"action": "delay", "delay_ms": 4000` (any rule may also carry `delay_ms`) |
| Pause for a human | `"action": "breakpoint", "pause_on": "response"` (request / response / both): **only when a person is watching the panel**; the app times out otherwise |
| Keep but switch off | `"enabled": false` |

Bodies for `map_local`: an error or empty state → `responses/<status>_*.json` (or the profile's
`responses/`); real-looking data → the human's mock folder first (copy a file into the profile's
`mocks/` and edit the copy, never the original); otherwise write a new file there. Relative
`local_file` paths start from the profile folder. Never put real customer data,
tokens or passwords in rules or mocks.

Validate before saving: the JSON parses and every `url_regex` compiles (`python3 -c "import json,re;
[re.compile(r['url_regex']) for r in json.load(open('rules.json'))]"`). A broken file is ignored and
the previous rules stay active, so a typo silently does nothing.

## 4. Settings

```json
{"ssl": {"mode": "all", "hosts": []},              // all | only | except: which HTTPS hosts are decrypted
 "focus": {"enabled": false, "hosts": []},          // Traffic list shows only these hosts
 "block": {"enabled": false, "hosts": []},          // answered 403, never reach the server
 "no_cache": false, "latency_ms": 0, "app_timeout_s": 30}
```

Host patterns: `api.example.com`, `*.example.com`, `*`. Hosts that are not decrypted pass through
untouched: rules don't apply and they don't appear in the logs. SSL changes apply to new
connections (the app may need a restart).

## 5. Verify, then report

- After the human (or `run.sh`) exercises the flow, read `logs/served.jsonl`: each record has
  `matched_regex`, `action` (`map_local`, `rewrite`, `map_remote` + `target`, `delay`, `blocked` with
  `matched_regex: null`), `status` (what the app received), `served`. No record for your rule = it
  never matched (check the regex, the method, rule order, SSL settings, or that the device uses the
  proxy at all).
- `logs/unmatched.jsonl` shows what still hit the real backend (query parameter names only).
- Report what was served with evidence (the log line, a screenshot); never claim a mock worked
  without a `served: true` record.

## 6. Repeatable checks (Android)

`./run.sh --spec <file>`: mocks + uiautomator2 steps + checks, always cleans up. Copy
`profiles/example/spec.sample.json`; schema `lib/spec.schema.json`. Passwords go through
`{"action": "input_text", "env": "LOGIN_PW"}`, never in the spec. Read
`artifacts/<name>/result.json`: `pass` needs the UI check, the traffic check and every step;
`traffic.served_count == 0` with `flow_complete: true` means interception isn't working (proxy or
certificate), not an app bug; `steps/*_FAIL.png` shows a failed step. Don't use breakpoints in specs.

## Don'ts

- Don't commit `rules.json`, `settings.json`, `mocks/`, `logs/`, private profiles (all git-ignored).
- Don't leave a device proxy, a Mac system proxy or a breakpoint behind: `./proxy.sh clear-adb-proxy`,
  Ctrl+C on `--system-proxy`, and disable breakpoint rules when you're done.
