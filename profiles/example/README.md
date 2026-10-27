# Example profile

A working sample against two public demo APIs ([JSONPlaceholder](https://jsonplaceholder.typicode.com)
and [httpbin](https://httpbin.org)). Start it with:

```bash
./proxy.sh --profile profiles/example
```

| File | What it shows |
|---|---|
| [`endpoints.txt`](endpoints.txt) | endpoint groups: `# <Api> · <BASE KEY>`, then `METHOD  /path` (`{id}` = path parameter) |
| [`base-urls.txt`](base-urls.txt) | `KEY  environment  URL` for each group |
| [`mocks/`](mocks) | sample mock files: an empty list, a very long title, a user without an email |
| [`rules.sample.json`](rules.sample.json) | one rule per action; only the first is on (`/posts` answers `[]`) |
| [`spec.sample.json`](spec.sample.json) | a repeatable Android check for `run.sh` (needs your own app) |

On the first start `rules.sample.json` is copied to `rules.json`, and the panel saves your
changes there. `rules.json` and `settings.json` are git-ignored, so trying things never changes
the sample.
