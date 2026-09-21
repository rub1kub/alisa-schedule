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
        """<head><title>KKEPIK</title>
<link rel="stylesheet" href="{{ url_for('static', filename='css/main.css') }}"""
        """?v={{ STATIC_VERSION }}">
<script defer src="{{ url_for('static', filename='js/vendor/telegram-web-app.63.js') }}"></script>
</head><body><div id="main-content">unchanged</div></body>"""
    )
    index, fragment = integrate(original, (ROOT / "app/static/about.html").read_text())
    assert '<div id="main-content">unchanged</div>' in index
    assert index.index("telegram-web-app.63.js") < index.index("kepik-entry.js")
    assert 'media="not all"' in index
    assert "#kepik-landing h1" in fragment
    assert "html.kepik-miniapp #kepik-landing { display: none; }" in fragment
    assert "Расписание колледжа в Алисе" in fragment
    assert index.index("data-kepik-prepaint") < index.index("telegram-web-app.63.js")
    assert "html.kepik-telegram-pending #kepik-landing { display: none; }" in fragment
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
 document: {documentElement: {classList: {
 toggle: (name, value) => {selected = value}, contains: () => false, remove: () => {}}},
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


@pytest.mark.parametrize(
    "launch,expected",
    [
        ({}, False),
        ({"hash": "#tgWebAppData=query_id%3Dtest"}, True),
        ({"hash": "#/home?tgWebAppData=query_id%3Dtest"}, True),
        ({"stored": '{"tgWebAppData":"query_id=test"}'}, True),
        ({"hash": "#tgWebAppData=", "stored": '{"tgWebAppData":"old"}'}, False),
        ({"hash": "#tgWebAppData=&tgWebAppData=test"}, True),
        ({"hash": "#tgWebAppData=test&tgWebAppData="}, False),
        ({"stored": "malformed"}, False),
        ({"stored": '{"user":{"id":1}}'}, False),
        ({"storage_error": True}, False),
        ({"hash": "#tgWebAppData=test", "storage_error": True}, True),
    ],
)
def test_telegram_launch_is_hidden_before_sdk_loads(launch, expected):
    result = run_prepaint(launch)
    assert result["pendingBeforeSdk"] is expected


@pytest.mark.parametrize("sdk_available", [True, False])
def test_pending_telegram_launch_never_falls_back_to_landing(sdk_available):
    result = run_prepaint(
        {"hash": "#tgWebAppData=test", "finish": True, "sdk_available": sdk_available}
    )
    assert result["pendingBeforeSdk"] is True
    assert result["pendingAfterSdk"] is False
    assert result["miniapp"] is True
    assert result["media"] == "all"
    assert bool(result["error"]) is not sdk_available


def run_prepaint(launch):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required for frontend checks")
    harness = """const vm = require('node:vm');
const fs = require('node:fs');
const launch = JSON.parse(process.argv[1]);
const classes = new Set();
const style = {media: 'not all'};
const error = {textContent: '', style: {display: 'none'}};
const context = {
 URLSearchParams,
 window: {location: {hash: launch.hash || ''}, sessionStorage: {
  getItem: () => {if (launch.storage_error) throw Error('blocked'); return launch.stored || null}
 }},
 document: {documentElement: {classList: {
  add: name => classes.add(name), contains: name => classes.has(name),
  remove: name => classes.delete(name),
  toggle: (name, value) => value ? classes.add(name) : classes.delete(name)
 }}, getElementById: id => id === 'auth-error' ? error : style, title: 'Кэпик'}
};
vm.runInNewContext(fs.readFileSync(process.argv[2], 'utf8'), context);
const pendingBeforeSdk = classes.has('kepik-telegram-pending');
if (launch.finish) {
 if (launch.sdk_available) context.window.Telegram = {WebApp: {initData: 'test'}};
 vm.runInNewContext(fs.readFileSync(process.argv[3], 'utf8'), context);
}
process.stdout.write(JSON.stringify({pendingBeforeSdk,
 pendingAfterSdk: classes.has('kepik-telegram-pending'),
 miniapp: classes.has('kepik-miniapp'), media: style.media, error: error.textContent}));"""
    result = subprocess.run(
        [
            node,
            "-e",
            harness,
            json.dumps(launch),
            str(ROOT / "deploy/miniapp/prepaint.js"),
            str(ROOT / "deploy/miniapp/entry.js"),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)
