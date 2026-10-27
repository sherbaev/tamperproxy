# Profiles

One folder per app. A profile holds everything tamper-proxy needs to know about that app:

| File | What | Who writes it |
|---|---|---|
| `endpoints.txt` | the app's API endpoints, grouped by API | you |
| `base-urls.txt` | each API's base URL per environment | you |
| `mocks/` | your mock response files | you, or the panel (*Save response as mock*, *Paste body*) |
| `responses/` | optional: error bodies in your backend's own format | you |
| `rules.sample.json` | optional: rules to start from, copied to `rules.json` on the first start | you |
| `rules.json` | the active rules | the panel (or you) |
| `settings.json` | SSL proxying, Focus, Block list, No caching, latency | the panel (or you) |

```bash
cp -r profiles/example profiles/my-app
rm profiles/my-app/rules.sample.json profiles/my-app/mocks/*
./proxy.sh --profile profiles/my-app
```

Only [`example/`](example) is part of the repo. Every other folder here is git-ignored, so your
hosts, endpoints and mock data never end up in a commit. A profile can also live anywhere else:
`./proxy.sh --profile ~/work/my-app`.
