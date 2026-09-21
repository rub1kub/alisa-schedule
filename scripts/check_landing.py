"""Check the public Apache routes, including URL variants used by external reviewers."""

from urllib.request import Request, urlopen


def main():
    checks = 0
    for scheme in ("http", "https"):
        for path in (
            "/",
            "/about",
            "/about/",
            "/about?from=dialogs",
            "/about/?from=dialogs",
            "/guide",
            "/guide/",
            "/guide?from=landing",
        ):
            for method in ("GET", "HEAD"):
                url = f"{scheme}://kkepik.rub1kub.ru{path}"
                request = Request(url, method=method)
                with urlopen(request, timeout=10) as response:
                    if response.status != 200 or response.headers.get_content_type() != "text/html":
                        raise RuntimeError(f"Landing unavailable: {method} {url}")
                    if response.url != url:
                        raise RuntimeError(f"Unexpected landing redirect: {method} {url}")
                    body = response.read().decode("utf-8")
                    expected = (
                        "Спроси Кэпика."
                        if path.startswith("/guide")
                        else "Расписание колледжа в Алисе"
                    )
                    if method == "GET" and expected not in body:
                        raise RuntimeError(f"Unexpected page: {url}")
                    if method == "HEAD" and body:
                        raise RuntimeError(f"HEAD returned a body: {url}")
                checks += 1
    print(f"Public landing: {checks} checks passed")


if __name__ == "__main__":
    main()
