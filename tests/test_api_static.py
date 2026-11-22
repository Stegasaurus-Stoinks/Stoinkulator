"""The static dashboard: every referenced file is served, the vendored library is the pinned one.

The pages are plain HTML and ES modules with no build step. These tests parse the
files the way a browser would resolve them and fetch each one from the app.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
from importlib.resources import files
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

import pytest

from tests.api_support import make_client

STATIC = Path(str(files("MainStoinker.api") / "static"))
VENDOR = "vendor/lightweight-charts/lightweight-charts.standalone.production.js"
VENDOR_SHA256 = "338a625ece52c5b85fb026e222dad5d0f365f745060afbfae96bda91636142e9"
VENDOR_SIZE = 198_600
ATTRIBUTION_URL = "https://www.tradingview.com/"

ATTR_RE = re.compile(r"""\b(src|href)\s*=\s*["']([^"']*)["']""", re.IGNORECASE)
IMPORT_RE = re.compile(r"""\bimport\s*(?:[\w{}*\s,]*?\bfrom\s*)?["']([^"']+)["']""")
JS_FILES = sorted(p for p in STATIC.rglob("*.js") if "vendor" not in p.relative_to(STATIC).parts)
VIEWS = ("overview", "algo", "compare", "runs")


@pytest.fixture(scope="module")
def empty_db(tmp_path_factory: pytest.TempPathFactory) -> Path:
    from MainStoinker.engine.store import SqliteStore

    path = tmp_path_factory.mktemp("static") / "empty.db"
    with SqliteStore(path, env={}):
        pass
    return path


def page_references(page: Path) -> list[tuple[str, str]]:
    """(kind, target) of every src/href attribute (HTML) or import (JS) in `page`."""
    text = page.read_text(encoding="utf-8")
    if page.suffix == ".html":
        return [(m.group(1).lower(), m.group(2)) for m in ATTR_RE.finditer(text)]
    return [("import", m.group(1)) for m in IMPORT_RE.finditer(text)]


def served_path(page: Path, target: str) -> str:
    """The URL path a browser requests for `target` found in `page` (relative targets only)."""
    base = PurePosixPath("/" + page.relative_to(STATIC).as_posix()).parent
    parts: list[str] = []
    for part in (base / urlsplit(target).path).parts[1:]:
        if part == "..":
            parts.pop()
        elif part != ".":
            parts.append(part)
    return "/" + "/".join(parts)


def all_pages() -> list[Path]:
    return [STATIC / "index.html", *JS_FILES]


def external(target: str) -> bool:
    return bool(re.match(r"^(?:[a-z][a-z0-9+.-]*:)?//|^[a-z][a-z0-9+.-]*:", target, re.IGNORECASE))


def test_index_served_at_root(empty_db: Path) -> None:
    client = make_client(empty_db)
    for target in ("/", "/index.html"):
        response = client.get(target)
        assert response.status_code == 200
        assert "text/html" in response.headers["content-type"]
        assert 'id="app"' in response.text and "Stoinkulator" in response.text


def test_every_referenced_static_file_is_served(empty_db: Path) -> None:
    client = make_client(empty_db)
    seen: set[str] = set()
    for page in all_pages():
        refs = page_references(page)
        for kind, target in refs:
            if external(target):
                continue
            assert target.startswith(("./", "../")) or page.suffix == ".html", (page, target)
            url = served_path(page, target)
            seen.add(url)
            response = client.get(url)
            assert response.status_code == 200, (page.name, kind, target, url)
            content_type = response.headers["content-type"]
            if url.endswith((".js", ".mjs")):
                assert content_type.startswith("text/javascript"), (url, content_type)
            elif url.endswith(".css"):
                assert content_type.startswith("text/css"), (url, content_type)
    assert {"/app.css", "/js/main.js", "/" + VENDOR} <= seen
    assert {"/js/api.js", "/js/views/runs.js", "/js/views/overview.js",
            "/js/views/algo.js", "/js/views/compare.js"} <= seen


def test_javascript_is_served_as_javascript(empty_db: Path) -> None:
    client = make_client(empty_db)
    found = list(STATIC.rglob("*.js"))
    assert len(found) >= 8
    for path in found:
        url = "/" + path.relative_to(STATIC).as_posix()
        response = client.get(url)
        assert response.status_code == 200, url
        assert response.headers["content-type"].startswith("text/javascript"), url


def test_vendor_file_matches_recorded_sha256() -> None:
    data = (STATIC / VENDOR).read_bytes()
    assert len(data) == VENDOR_SIZE
    assert hashlib.sha256(data).hexdigest() == VENDOR_SHA256
    folder = STATIC / "vendor" / "lightweight-charts"
    notes = (folder / "SOURCE.md").read_text(encoding="utf-8")
    for needed in (VENDOR_SHA256, "5.3.0", "https://registry.npmjs.org/lightweight-charts/-/"
                   "lightweight-charts-5.3.0.tgz", "198,600", "Apache"):
        assert needed in notes
    assert "Apache License" in (folder / "LICENSE").read_text(encoding="utf-8")


def test_attribution_link_in_footer() -> None:
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    footer = re.search(r"<footer\b.*?</footer>", html, re.DOTALL | re.IGNORECASE)
    assert footer is not None
    link = re.search(r"<a\b[^>]*\bhref=\"" + re.escape(ATTRIBUTION_URL) + r"\"[^>]*>([^<]*)</a>",
                     footer.group(0))
    assert link is not None
    assert "Charts by TradingView Lightweight Charts" in link.group(1)


def test_no_external_urls_loaded() -> None:
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    for page in all_pages():
        for kind, target in page_references(page):
            if external(target):
                # the one allowed absolute URL: the attribution link in the page footer
                assert page.name == "index.html" and kind == "href" and target == ATTRIBUTION_URL
    for match in ATTR_RE.finditer(html):
        if match.group(1).lower() == "src":
            assert not external(match.group(2)), match.group(2)
    for path in JS_FILES:
        text = path.read_text(encoding="utf-8")
        assert not re.search(r"https?://", text, re.IGNORECASE), path.name
        assert "@import" not in text
    css = (STATIC / "app.css").read_text(encoding="utf-8")
    assert not re.search(r"https?://|@import|url\(", css, re.IGNORECASE)
    # root-relative fetches would escape a prefix and are not used either
    for path in JS_FILES:
        assert not re.search(r"""fetch\(\s*["']https?:""", path.read_text(encoding="utf-8"))


def test_router_registers_all_four_routes_and_views_exist() -> None:
    main = (STATIC / "js" / "main.js").read_text(encoding="utf-8")
    for name in VIEWS:
        assert f'"./views/{name}.js"' in main
        assert (STATIC / "js" / "views" / f"{name}.js").is_file()
    for needle in ('"overview"', '"algo"', '"compare"', '"runs"', "notfound",
                   "encodeURIComponent", "decodeURIComponent"):
        assert needle in main


def test_no_inner_html_with_data() -> None:
    forbidden = ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write")
    assert JS_FILES
    for path in JS_FILES:
        text = path.read_text(encoding="utf-8")
        for word in forbidden:
            assert word not in text, f"{path.name} uses {word}"


def test_index_loads_vendor_library_before_the_module() -> None:
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    assert html.index(VENDOR) < html.index("js/main.js")
    assert re.search(r'<script src="' + re.escape(VENDOR) + '"></script>', html)
    assert re.search(r'<script type="module" src="js/main\.js"></script>', html)


def test_css_has_dark_mode_and_focus_styles() -> None:
    css = (STATIC / "app.css").read_text(encoding="utf-8")
    assert "prefers-color-scheme: dark" in css
    assert ":focus-visible" in css
    assert "tabular-nums" in css
    body = css.split("* { box-sizing", 1)[1]  # everything after the custom properties
    assert not re.search(r"(?<![\w-])#[0-9a-fA-F]{3,8}\b", body)


NODE = shutil.which("node")


@pytest.mark.skipif(NODE is None, reason="no `node` on PATH: JavaScript syntax is not checked")
def test_every_javascript_module_has_valid_syntax(tmp_path: Path) -> None:
    assert NODE is not None
    for number, path in enumerate(JS_FILES):
        copy = tmp_path / f"{number}-{path.stem}.mjs"  # checked as modules, not scripts
        copy.write_bytes(path.read_bytes())
        done = subprocess.run([NODE, "--check", str(copy)], capture_output=True, text=True,
                              timeout=60)
        assert done.returncode == 0, f"{path.name}: {done.stderr}"


@pytest.mark.skipif(NODE is None, reason="no `node` on PATH: format.js is not exercised")
def test_format_helpers_with_node(tmp_path: Path) -> None:
    assert NODE is not None
    module = tmp_path / "format.mjs"
    module.write_bytes((STATIC / "js" / "format.js").read_bytes())
    script = tmp_path / "check.mjs"
    script.write_text(
        'import * as f from "./format.mjs";\n'
        "const out = [\n"
        '  f.formatMoney("1234567.8900"), f.formatMoney("-0.0040"), f.formatMoney("0.0050"),\n'
        '  f.formatNet("1234.5000"), f.formatNet("-12.0000"), f.formatNet("0.0000"),\n'
        '  f.formatNet("-0.0049"), f.formatNet("999.9950"), f.netClass("-3.0000"),\n'
        "  f.formatPercent(0.5), f.formatPercent(null),\n"
        "  f.formatDuration(0.25), f.formatDuration(12.34), f.formatDuration(185), f.formatDuration(3725),\n"
        "  f.formatDuration(null),\n"
        '  f.formatUtc("2026-11-22T17:31:13.123456+00:00"), f.formatUtc(null),\n'
        '  f.formatDataTime("2024-01-02T09:30:00"),\n'
        '  f.shortVersion("0123456789abcdef-dirty:d06a266cac18"), f.shortVersion("v1.2"),\n'
        "  f.shortVersion(null),\n"
        "];\n"
        "console.log(JSON.stringify(out));\n",
        encoding="utf-8")
    done = subprocess.run([NODE, str(script)], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout) == [
        "1,234,567.89", "0.00", "0.01",
        "+1,234.50", "-12.00", "0.00",
        "0.00", "+1,000.00", "neg",
        "50.0%", "-",
        "250 ms", "12.3 s", "3 m 05 s", "1 h 02 m",
        "-",
        "2026-11-22 17:31:13 UTC", "-",
        "2024-01-02T09:30:00 (data time)",
        "01234567-dirty", "v1.2", "-",
    ]
