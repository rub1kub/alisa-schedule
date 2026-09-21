"""Root screen selection must not use unverified user metadata as a login."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from scripts.prepare_root_landing import integrate

ROOT = Path(__file__).resolve().parents[1]


def test_integration_preserves_app_and_scopes_landing():
    original = (
        """<title>KKEPIK</title>
<link rel="stylesheet" href="{{ url_for('static', filename='css/main.css') }}"""
        """?v={{ STATIC_VERSION }}">
<script defer src="{{ url_for('static', filename='js/vendor/telegram-web-app.63.js') }}"></script>
<body><div id="main-content">unchanged</div></body>"""
    )
    index, fragment = integrate(original, (ROOT / "app/static/about.html").read_text())
    assert '<div id="main-content">unchanged</div>' in index
    assert index.index("telegram-web-app.63.js") < index.index("kepik-entry.js")
    assert 'media="not all"' in index
    assert "#kepik-landing h1" in fragment
    assert "html.kepik-miniapp #kepik-landing { display: none; }" in fragment
    assert "Кэпик — мой проект" in fragment
    with pytest.raises(ValueError):
        integrate(index, (ROOT / "app/static/about.html").read_text())


@pytest.mark.parametrize(
    "telegram,expected",
    [
        (None, False),
        ({"WebApp": {"initData": ""}}, False),
        ({"WebApp": {"initDataUnsafe": {"user": {"id": 1}}}}, False),
        ({"WebApp": {"initData": "   "}}, False),
        ({"WebApp": {"initData": "invalid-test-data"}}, True),
    ],
)
def test_entry_only_selects_screen(telegram, expected):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required for frontend checks")
    harness = """const vm = require('node:vm');
const fs = require('node:fs');
const style = {media: 'not all'};
let selected = false;
const context = {
 window: {Telegram: JSON.parse(process.argv[1])},
 document: {documentElement: {classList: {toggle: (name, value) => {selected = value}}},
 getElementById: () => style, title: 'Кэпик'}
};
vm.runInNewContext(fs.readFileSync(process.argv[2], 'utf8'), context);
process.stdout.write(JSON.stringify({selected, media: style.media}));"""
    result = subprocess.run(
        [node, "-e", harness, json.dumps(telegram), str(ROOT / "deploy/miniapp/entry.js")],
        check=True,
        capture_output=True,
        text=True,
    )
    assert json.loads(result.stdout) == {
        "selected": expected,
        "media": "all" if expected else "not all",
    }
