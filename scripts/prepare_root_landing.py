"""Generate a narrow template integration, preserving the original Mini App markup."""

import argparse
import re
from pathlib import Path


def integrate(original: str, landing: str) -> tuple[str, str]:
    if 'id="kepik-landing"' in original or "kepik_landing.html" in original:
        raise ValueError("Template already integrated; use its original backup")
    prepaint = (Path(__file__).resolve().parents[1] / "deploy/miniapp/prepaint.js").read_text()
    css = re.search(r"<style>(.*?)</style>", landing, re.S).group(1)

    def scope(match):
        selector = match.group(1)
        if selector.strip().startswith("@"):
            return selector + "{"
        selectors = []
        for part in selector.strip().split(","):
            part = part.strip()
            selectors.append(
                "#kepik-landing" if part in (":root", "body") else "#kepik-landing " + part
            )
        return "\n" + ", ".join(selectors) + " {"

    css = re.sub(r"([^{}]+)\{", scope, css)
    content = re.search(r"<body>(.*?)</body>", landing, re.S).group(1)
    fragment = (
        "<style>\n"
        + css
        + """
html:not(.kepik-miniapp) body { margin: 0; padding: 0; display: block; background: #111; }
#kepik-miniapp { display: none; }
html.kepik-miniapp #kepik-miniapp { display: contents; }
html.kepik-miniapp #kepik-landing { display: none; }
html.kepik-telegram-pending #kepik-landing { display: none; }
</style>
<section id="kepik-landing" aria-label="Кэпик — расписание колледжа">
"""
        + content
        + "\n</section>\n"
    )
    assert original.count("<head>") == original.count("</title>") == 1
    result = original.replace(
        "</title>",
        "</title>\n    <script data-kepik-prepaint>\n" + prepaint + "</script>",
    )
    result = result.replace(
        "<title>KKEPIK</title>",
        "<title>Кэпик — расписание колледжа</title>\n"
        '    <meta name="description" content="Кэпик — расписание Краснодарского колледжа '
        'электронного приборостроения. Telegram-бот и навык Алисы от rub1kub.">',
    )
    stylesheet = (
        "<link rel=\"stylesheet\" href=\"{{ url_for('static', filename='css/main.css') }}"
        '?v={{ STATIC_VERSION }}">'
    )
    assert result.count(stylesheet) == 1
    result = result.replace(
        stylesheet, stylesheet.replace("<link ", '<link id="kepik-miniapp-style" media="not all" ')
    )
    sdk = (
        "<script defer src=\"{{ url_for('static', filename='js/vendor/telegram-web-app.63.js') }}"
        '"></script>'
    )
    assert result.count(sdk) == 1
    result = result.replace(
        sdk, sdk + '\n    <script defer src="/static/js/kepik-entry.js?v=20260921-2"></script>'
    )
    assert result.count("<body>") == result.count("</body>") == 1
    result = result.replace(
        "<body>", '<body>\n    {% include "kepik_landing.html" %}\n    <div id="kepik-miniapp">'
    )
    result = result.replace("</body>", "    </div>\n</body>")
    return result, fragment


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("original", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    index, fragment = integrate(
        args.original.read_text(), (root / "app/static/about.html").read_text()
    )
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "index.html").write_text(index)
    (args.output / "kepik_landing.html").write_text(fragment)
    (args.output / "kepik-entry.js").write_text((root / "deploy/miniapp/entry.js").read_text())


if __name__ == "__main__":
    main()
