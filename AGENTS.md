# Agents

To mock, rewrite, redirect, slow down, block or pause an app's HTTP(S) calls with this repo (an
open-source Charles Proxy alternative), follow [SKILL.md](SKILL.md). Human guide: [README.md](README.md).

Development: `~/.cache/tamper-proxy-venv/bin/python tests/test_toolkit.py` (mitmproxy comes from
`bash setup.sh`'s venv). The panel is plain HTML/CSS/JS in `lib/panel*`, the proxy logic is the
mitmproxy addon `lib/tamper_addon.py`.
