"""The static dashboard: every referenced file is served, the vendored library is the pinned one.

The pages are plain HTML and ES modules with no build step. These tests parse the
files the way a browser would resolve them and fetch each one from the app.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from decimal import ROUND_HALF_UP, Decimal
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
IMPORT_RE = re.compile(
    r"""\bimport\s*(?:[\w{}*\s,]*?\bfrom\s*)?["']([^"']+)["']"""  # static import and side-effect import
    r"""|\bimport\s*\(\s*["']([^"']+)["']\s*\)"""  # dynamic import("...")
)
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
    return [("import", m.group(1) or m.group(2)) for m in IMPORT_RE.finditer(text)]


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
    assert {"/js/api.js", "/js/dom.js", "/js/table.js", "/js/format.js", "/js/routes.js", "/js/lifecycle.js",
            "/js/views/runs.js", "/js/views/overview.js", "/js/views/algo.js",
            "/js/views/compare.js"} <= seen


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
    # fetches to an absolute http(s) URL are forbidden; the root-relative "/api/..." paths that
    # `getJson` uses are the intended form (the dashboard is served at the root of its own origin)
    for path in JS_FILES:
        assert not re.search(r"""fetch\(\s*["']https?:""", path.read_text(encoding="utf-8"))


def test_router_registers_all_four_routes_and_views_exist() -> None:
    main = (STATIC / "js" / "main.js").read_text(encoding="utf-8")
    for name in VIEWS:
        assert f'"./views/{name}.js"' in main
        assert (STATIC / "js" / "views" / f"{name}.js").is_file()
    for key in ("runs", "overview", "algo", "compare", "notfound"):
        assert re.search(rf"^\s+{key}: render\w+,$", main, re.MULTILINE), key
    routes = (STATIC / "js" / "routes.js").read_text(encoding="utf-8")
    for needle in ('"overview"', '"algo"', '"compare"', '"runs"', "notfound",
                   "encodeURIComponent", "decodeURIComponent"):
        assert needle in routes


def test_main_documents_the_view_contract() -> None:
    main = (STATIC / "js" / "main.js").read_text(encoding="utf-8")
    for needle in ("VIEW CONTRACT", "render(container, params, ctx)", "ctx.signal", "ctx.isCurrent()",
                   "cleanup", "viewHeading", "HEALTH_EVENT"):
        assert needle in main


def test_dynamic_imports_are_followed() -> None:
    source = 'const m = await import("./charts/x.js"); import a from "./a.js"; import "./b.js";'
    refs = IMPORT_RE.findall(source)
    assert [a or b for a, b in refs] == ["./charts/x.js", "./a.js", "./b.js"]


def hex_luminance(color: str) -> float:
    channels = [int(color[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    linear = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def contrast(first: str, second: str) -> float:
    a, b = sorted((hex_luminance(first), hex_luminance(second)), reverse=True)
    return (a + 0.05) / (b + 0.05)


def test_control_borders_reach_three_to_one_in_both_themes() -> None:
    css = (STATIC / "app.css").read_text(encoding="utf-8")
    dark_start = css.index("prefers-color-scheme: dark")
    for theme, block in (("light", css[:dark_start]), ("dark", css[dark_start:css.index("* { box-sizing")])):
        values = dict(re.findall(r"--([\w-]+):\s*(#[0-9a-fA-F]{6})", block))
        for background in ("surface", "bg"):
            ratio = contrast(values["control-border"], values[background])
            assert ratio >= 3.0, (theme, background, ratio)


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
JS_ROOT = STATIC / "js"


def require_node() -> None:
    """Skip when node is missing, except under CI, where a missing node must fail the run."""
    if NODE is None:
        if os.environ.get("CI"):
            pytest.fail("`node` is not on PATH but CI is set: the JavaScript tests must run in CI")
        pytest.skip("no `node` on PATH: the JavaScript modules are not exercised")


@pytest.fixture(scope="module")
def node_workdir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A scratch copy of the dashboard modules that node runs as ES modules (no DOM, no network)."""
    require_node()
    work = tmp_path_factory.mktemp("node")
    (work / "package.json").write_text('{"type": "module"}', encoding="utf-8")
    shutil.copytree(JS_ROOT, work / "js")
    return work


def run_node(work: Path, name: str, source: str) -> object:
    """Run `source` (an ES module placed next to the `js` copy) and return its JSON output."""
    assert NODE is not None
    script = work / f"{name}.mjs"
    script.write_text(source, encoding="utf-8")
    done = subprocess.run([NODE, str(script)], capture_output=True, encoding="utf-8", timeout=120, cwd=work)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def test_every_javascript_module_has_valid_syntax(node_workdir: Path, tmp_path: Path) -> None:
    assert NODE is not None
    for number, path in enumerate(JS_FILES):
        copy = tmp_path / f"{number}-{path.stem}.mjs"  # checked as modules, not scripts
        copy.write_bytes(path.read_bytes())
        done = subprocess.run([NODE, "--check", str(copy)], capture_output=True, text=True,
                              timeout=60)
        assert done.returncode == 0, f"{path.name}: {done.stderr}"


def test_node_failure_policy_in_ci(monkeypatch: pytest.MonkeyPatch) -> None:
    """With CI set a missing node fails the node tests; without it they skip."""
    monkeypatch.setattr(sys.modules[__name__], "NODE", None)
    monkeypatch.setenv("CI", "true")
    with pytest.raises(pytest.fail.Exception):
        require_node()
    monkeypatch.delenv("CI")
    with pytest.raises(pytest.skip.Exception):
        require_node()


# --- routes.js ---------------------------------------------------------------------------------

ROUTE_CASES = [
    "", "#", "#/", "#/run/3", "#/run/0", "#/run/03", "#/run/3/", "#run/3", "#/nope",
    "#/run/99999999999999999999", "#/run/9007199254740993", "#/run/-3", "#/run/３",
    "#/run/3/algo/AAPL-10%2F30EMA", "#/run/3/algo/A%2FB%20%25%23%3F%26x", "#/run/3/algo/a b",
    "#/run/3/algo/a/b", "#/run/3/algo/", "#/run/3/algo", "#/run/3/algo/%E0%A4%A", "#/run/3/algo/%",
    "#/compare/3,4", "#/compare/4,3", "#/compare/3,3", "#/compare/3", "#/compare/", "#/compare/3,4,5,6,7",
    "#/compare/1,2,3,4,5,6", "#/compare/0,1", "#/compare/1,", "#/compare/1,,2", "#/compare/1,2x",
    "#/compare/01,1", "#/compare/123456789012345678,1", "#/compare/1234567890123456789,1",
    "#/compare/3,4/5", "#/compare/%E0,1",
]


def test_route_parsing_with_node(node_workdir: Path) -> None:
    out = run_node(node_workdir, "routes", (
        'import { parseRoute } from "./js/routes.js";\n'
        f"const cases = {json.dumps(ROUTE_CASES)};\n"
        "console.log(JSON.stringify(cases.map((c) => parseRoute(c))));\n"))
    assert isinstance(out, list)
    got = {case: (r["view"], r["params"]) for case, r in zip(ROUTE_CASES, out, strict=True)}
    nf = "notfound"
    assert got[""] == got["#"] == got["#/"] == ("runs", {})
    assert got["#/run/3"] == ("overview", {"runId": 3})
    for bad in ("#/run/0", "#/run/3/", "#run/3", "#/nope",
                "#/run/99999999999999999999", "#/run/9007199254740993", "#/run/-3", "#/run/３"):
        assert got[bad][0] == nf, bad
    assert got["#/run/03"] == ("overview", {"runId": 3})
    assert got["#/run/3/algo/AAPL-10%2F30EMA"] == ("algo", {"runId": 3, "idname": "AAPL-10/30EMA"})
    assert got["#/run/3/algo/A%2FB%20%25%23%3F%26x"] == ("algo", {"runId": 3, "idname": "A/B %#?&x"})
    assert got["#/run/3/algo/a b"] == ("algo", {"runId": 3, "idname": "a b"})
    for bad in ("#/run/3/algo/a/b", "#/run/3/algo/", "#/run/3/algo", "#/run/3/algo/%E0%A4%A",
                "#/run/3/algo/%"):
        assert got[bad][0] == nf, bad
        assert got[bad][1]["reason"], bad
    assert got["#/compare/3,4"] == ("compare", {"runIds": [3, 4]})
    assert got["#/compare/4,3"] == ("compare", {"runIds": [4, 3]})
    assert got["#/compare/3,4,5,6,7"] == ("compare", {"runIds": [3, 4, 5, 6, 7]})
    for bad in ("#/compare/3,3", "#/compare/3", "#/compare/", "#/compare/1,2,3,4,5,6", "#/compare/0,1",
                "#/compare/1,", "#/compare/1,,2", "#/compare/1,2x", "#/compare/01,1",
                "#/compare/1234567890123456789,1", "#/compare/3,4/5", "#/compare/%E0,1",
                "#/compare/123456789012345678,1"):  # 18 digits (the API accepts it) but not exactly
        # representable as a JS number, so the dashboard refuses it rather than fetch a different run
        assert got[bad][0] == nf, bad
        assert got[bad][1]["reason"] and got[bad][1]["path"], bad


def test_route_builders_round_trip_with_node(node_workdir: Path) -> None:
    names = ["AAPL-10/30EMA", "A/B %#?&x", "with space", "100%", "%2F", "a?b#c", "ünï", "x,y"]
    out = run_node(node_workdir, "builders", (
        'import { algoHash, compareHash, parseRoute, routeTitle, runHash, runsHash } from "./js/routes.js";\n'
        f"const names = {json.dumps(names)};\n"
        "const back = names.map((n) => parseRoute(algoHash(7, n)).params.idname);\n"
        "const hashes = names.map((n) => algoHash(7, n));\n"
        "console.log(JSON.stringify({back, hashes, run: runHash(3), runs: runsHash(),\n"
        "  cmp: compareHash([3, 4]),\n"
        "  titles: [parseRoute(''), parseRoute('#/run/3'), parseRoute('#/run/3/algo/A%2FB'),\n"
        "    parseRoute('#/compare/3,4'), parseRoute('#/zzz')].map(routeTitle)}));\n"))
    assert isinstance(out, dict)
    assert out["back"] == names
    assert all(re.fullmatch(r"#/run/7/algo/[A-Za-z0-9%._~!*'()-]+", h) for h in out["hashes"])
    assert out["run"] == "#/run/3" and out["runs"] == "#/" and out["cmp"] == "#/compare/3,4"
    assert out["titles"] == ["Runs - Stoinkulator", "Run 3 - Stoinkulator", "A/B, run 3 - Stoinkulator",
                             "Compare runs 3, 4 - Stoinkulator", "Not found - Stoinkulator"]


# --- format.js ---------------------------------------------------------------------------------


def python_percent(text: str) -> str:
    from MainStoinker.engine.cli_report import _percent

    return _percent(Decimal(text))


def test_percent_matches_the_report_command_on_every_tie(node_workdir: Path) -> None:
    # every 4-decimal fraction 0.0000 .. 1.0000, which includes all 1000 half-way values
    fractions = [f"{k / 10000:.4f}" for k in range(10001)]
    out = run_node(node_workdir, "percent", (
        'import { formatPercent } from "./js/format.js";\n'
        f"const xs = {json.dumps(fractions)};\n"
        "console.log(JSON.stringify(xs.map((x) => formatPercent(x))));\n"))
    expected = [python_percent(x) for x in fractions]
    assert out == expected
    # independent of cli_report: the same rule written out with Decimal
    for x, got in zip(fractions, out, strict=True):
        want = format((Decimal(x) * 100).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP), "f")
        assert got == want, x
    assert dict(zip(fractions, out, strict=True))["0.0015"] == "0.2"
    assert dict(zip(fractions, out, strict=True))["0.0375"] == "3.8"


def test_format_helpers_with_node(node_workdir: Path) -> None:
    out = run_node(node_workdir, "format", """
import * as f from "./js/format.js";
const out = {
  money: ["1234567.8900", "-0.0040", "0.0050", "-0.0050", "-0.0049", "-0.0000", "-999.9950", "0", "7",
    "123456789012345678.9950", "abc", null, undefined, ""].map(f.formatMoney),
  net: ["1234.5000", "-12.0000", "0.0000", "-0.0049", "999.9950", "0.0050", "-0.0050"].map(f.formatNet),
  cls: ["-3.0000", "3", "0.0000", "-0.0040", "abc", null, ""].map(f.netClass),
  percent: [null, undefined, "", "0.5", 0.5, "0", "1", "0.0015", "abc"].map((x) => f.formatPercent(x)),
  percent0: [f.formatPercent("0.0375", 0), f.formatPercent("0.0350", 0), f.formatPercent("0.0375", 2)],
  duration: [0, 0.25, 0.9994, 0.9995, 0.9996, 1, 12.34, 59.94, 59.949, 59.95, 59.99, 60, 185, 3599.4, 3599.6,
    3725, null, undefined, "", NaN, -5].map(f.formatDuration),
  utc: ["2026-11-22T17:31:13.123456+00:00", "2026-11-22T17:31:13Z", "2026-11-22 17:31:13+00:00", null, "",
    "2026-11-22T23:30:00-05:00", "2026-11-22T01:30:00+02:00", "2026-01-01T00:30:00+01:00",
    "2026-12-31T23:30:00-01:00", "2026-11-22T17:31:13+05:30", "2026-11-22T17:31:13",
    "2026-13-22T17:31:13+00:00", "2026-02-30T17:31:13+00:00", "garbage"].map(f.formatUtc),
  data: [f.formatDataTime("2024-01-02T09:30:00"), f.formatDataTime(null)],
  version: ["0123456789abcdef-dirty:d06a266cac18", "0123456789abcdef:d06a266cac18", "0123456789abcdef-dirty",
    "abc", "v1.2", null, "", "0123456789"].map(f.shortVersion),
};
console.log(JSON.stringify(out));
""")
    assert isinstance(out, dict)
    assert out["money"] == ["1,234,567.89", "0.00", "0.01", "-0.01", "0.00", "0.00", "-1,000.00", "0.00",
                            "7.00","123,456,789,012,345,679.00", "abc", "-", "-", "-"]
    assert out["net"] == ["+1,234.50", "-12.00", "0.00", "0.00", "+1,000.00", "+0.01", "-0.01"]
    assert out["cls"] == ["neg", "pos", "", "", "", "", ""]
    assert out["percent"] == ["-", "-", "-", "50.0", "50.0", "0.0", "100.0", "0.2", "abc"]
    assert out["percent0"] == ["4", "4", "3.75"]
    assert out["duration"] == [
        "0 ms", "250 ms", "999 ms", "1.0 s", "1.0 s", "1.0 s", "12.3 s", "59.9 s", "59.9 s", "1 m 00 s",
        "1 m 00 s", "1 m 00 s", "3 m 05 s", "59 m 59 s", "1 h 00 m", "1 h 02 m", "-", "-", "-", "-", "0 ms"]
    assert out["utc"] == [
        "2026-11-22 17:31:13 UTC", "2026-11-22 17:31:13 UTC", "2026-11-22 17:31:13 UTC", "-", "-",
        "2026-11-23 04:30:00 UTC", "2026-11-21 23:30:00 UTC", "2025-12-31 23:30:00 UTC",
        "2027-01-01 00:30:00 UTC", "2026-11-22 12:01:13 UTC", "2026-11-22T17:31:13",
        "2026-13-22T17:31:13+00:00", "2026-02-30T17:31:13+00:00", "garbage"]
    assert out["data"] == ["2024-01-02T09:30:00 (data time)", "-"]
    assert out["version"] == ["0123456789+dirty", "0123456789", "0123456789+dirty", "abc", "v1.2", "-", "-",
                              "0123456789"]


def test_short_version_matches_the_runs_command(node_workdir: Path) -> None:
    from MainStoinker.engine.cli_report import _short_version

    versions = ["0123456789abcdef-dirty:d06a266cac18", "0123456789abcdef:d06a266cac18", "deadbeef", "",
                "a" * 40 + "-dirty", "v1.2-dirty"]
    out = run_node(node_workdir, "version", (
        'import { shortVersion } from "./js/format.js";\n'
        f"console.log(JSON.stringify({json.dumps(versions)}.map(shortVersion)));\n"))
    assert out == [_short_version(v) for v in versions]


# --- api.js ------------------------------------------------------------------------------------


def test_get_json_errors_and_abort_with_node(node_workdir: Path) -> None:
    out = run_node(node_workdir, "api", """
import { ApiError, describeError, getJson, isAbortError } from "./js/api.js";
const calls = [];
function reply(status, body, statusText = "") {
  return async (url, init) => {
    calls.push({ url, signal: init.signal === undefined ? "none" : "signal" });
    return { ok: status >= 200 && status < 300, status, statusText, json: async () => {
      if (body === undefined) throw new SyntaxError("not json");
      return body;
    } };
  };
}
async function attempt(fetchImpl, signal) {
  globalThis.fetch = fetchImpl;
  try {
    const params = { a: 1, b: "", c: null, d: undefined, e: "x y" };
    return { ok: await getJson("/api/x", params, signal ? { signal } : undefined) };
  } catch (err) {
    return {
      name: err.name, abort: isAbortError(err), kind: err.kind, status: err.status, text: describeError(err),
    };
  }
}
const aborted = new AbortController();
aborted.abort();
const out = {
  ok: await attempt(reply(200, { runs: [] })),
  nonJson: await attempt(reply(200, undefined)),
  nullBody: await attempt(reply(200, null)),
  notFoundNoText: await attempt(reply(404, undefined, "")),
  notFoundText: await attempt(reply(404, undefined, "Not Found")),
  detail: await attempt(reply(422, { detail: "run id must be a number" }, "Unprocessable")),
  unavailable: await attempt(reply(503, { detail: "database unavailable" }, "")),
  network: await attempt(async () => { throw new TypeError("fetch failed"); }),
  abortedSignal: await attempt(async (url, init) => {
    calls.push({ url, signal: init.signal ? "signal" : "none" });
    init.signal.throwIfAborted();
    return reply(200, {})(url, init);
  }, aborted.signal),
  plain: describeError(new Error("boom")),
  calls,
};
console.log(JSON.stringify(out));
""")
    assert isinstance(out, dict)
    assert out["ok"] == {"ok": {"runs": []}}
    unexpected = "The server sent an unexpected response."
    assert out["nonJson"]["text"] == unexpected and out["nonJson"]["kind"] == "unexpected"
    assert out["nullBody"]["text"] == unexpected
    assert out["notFoundNoText"]["text"] == "Error 404: request failed"
    assert out["notFoundText"]["text"] == "Error 404: Not Found"
    assert out["detail"]["text"] == "Error 422: run id must be a number"
    assert out["unavailable"]["status"] == 503 and out["unavailable"]["kind"] == "http"
    assert out["network"]["kind"] == "network" and out["network"]["status"] == 0
    assert out["network"]["text"] == "The server could not be reached."
    assert out["abortedSignal"]["abort"] is True and out["abortedSignal"]["name"] == "AbortError"
    assert out["plain"] == "Unexpected error: boom"
    first = out["calls"][0]
    assert first["url"] == "/api/x?a=1&e=x+y" and first["signal"] == "none"
    assert out["calls"][-1]["signal"] == "signal"


# --- lifecycle.js ------------------------------------------------------------------------------


def test_view_lifecycle_with_node(node_workdir: Path) -> None:
    out = run_node(node_workdir, "lifecycle", """
import { createNavigator } from "./js/lifecycle.js";
const log = [];
const tick = () => new Promise((resolve) => setTimeout(resolve, 5));
const nav = createNavigator();
const container = { name: "c" };
const hooks = (tag) => ({
  prepare: () => log.push("prepare " + tag),
  onError: (e) => log.push("error " + tag + " " + e.message),
});

// 1. the previous view is aborted and cleaned up before the next one renders
const first = nav.run(async (c, p, ctx) => {
  ctx.signal.addEventListener("abort", () => log.push("abort A"));
  log.push("render A");
  return () => log.push("cleanup A");
}, container, {}, hooks("A"));
await first;
await nav.run(async () => { log.push("render B"); }, container, {}, hooks("B"));

// 2. a slow view that is superseded: it must see isCurrent() false, and its late cleanup runs at once
let lateSawCurrent = null;
const slow = nav.run(async (c, p, ctx) => {
  await tick(); await tick();
  lateSawCurrent = ctx.isCurrent();
  log.push("slow finished, aborted=" + ctx.signal.aborted);
  return () => log.push("cleanup slow");
}, container, {}, hooks("S"));
const fast = nav.run(async (c, p, ctx) => {
  log.push("render F " + ctx.isCurrent());
}, container, {}, hooks("F"));
const [slowResult, fastResult] = await Promise.all([slow, fast]);

// 3. errors: a rejection reaches onError once; AbortError and errors of a superseded view do not
const failed = await nav.run(async () => { throw new Error("boom"); }, container, {}, hooks("E"));
const aborted = await nav.run(async () => {
  throw new DOMException("x", "AbortError");
}, container, {}, hooks("X"));
const syncThrow = await nav.run(() => { throw new Error("sync"); }, container, {}, hooks("Y"));
const supersededFail = nav.run(async () => {
  await tick();
  throw new Error("late");
}, container, {}, hooks("Z"));
const winner = nav.run(async () => { log.push("render W"); }, container, {}, hooks("W"));
const [zr] = await Promise.all([supersededFail, winner]);

// 4. a cleanup that throws does not stop the next navigation
const origError = console.error; console.error = () => log.push("console.error");
await nav.run(async () => () => { throw new Error("cleanup fails"); }, container, {}, hooks("T"));
await nav.run(async () => { log.push("render after bad cleanup"); }, container, {}, hooks("U"));
console.error = origError;
console.log(JSON.stringify({ log, lateSawCurrent, slowResult, fastResult, failed, aborted, syncThrow, zr }));
""")
    assert isinstance(out, dict)
    log = out["log"]
    assert log[:7] == ["prepare A", "render A", "abort A", "cleanup A", "prepare B", "render B", "prepare S"]
    assert out["lateSawCurrent"] is False
    assert "slow finished, aborted=true" in log
    assert log.index("slow finished, aborted=true") < log.index("cleanup slow")
    assert "render F true" in log
    assert out["slowResult"] == {"current": False, "failed": False}
    assert out["fastResult"] == {"current": True, "failed": False}
    assert out["failed"] == {"current": True, "failed": True} and "error E boom" in log
    assert out["aborted"] == {"current": True, "failed": False}
    assert not any(e.startswith("error X") for e in log)
    assert out["syncThrow"] == {"current": True, "failed": True} and "error Y sync" in log
    assert out["zr"] == {"current": False, "failed": False} and not any(e.startswith("error Z") for e in log)
    assert log.index("console.error") < log.index("render after bad cleanup")


# --- table.js ----------------------------------------------------------------------------------


def test_table_helpers_with_node(node_workdir: Path) -> None:
    out = run_node(node_workdir, "table", """
import { compareValues, netCell, sortRows } from "./js/table.js";
const rows = [{ n: "10" }, { n: "9" }, { n: null }, { n: "100" }, { n: "9" }].map((r, i) => ({ ...r, i }));
const key = (r) => (r.n === null ? null : Number(r.n));
console.log(JSON.stringify({
  asc: sortRows(rows, key, "asc").map((r) => r.i),
  desc: sortRows(rows, key, "desc").map((r) => r.i),
  text: sortRows([{ s: "b" }, { s: "a10" }, { s: "a9" }], (r) => r.s, "asc").map((r) => r.s),
  cmp: [compareValues(1, 2) < 0, compareValues(null, 1) > 0, compareValues(1, null) < 0,
    compareValues(null, null)],
  pos: netCell("12.3400"), neg: netCell("-0.0600"), zero: netCell("0.0000"), none: netCell(null),
}));
""")
    assert isinstance(out, dict)
    assert out["asc"] == [1, 4, 0, 3, 2]  # 9, 9 (stable), 10, 100, missing last
    assert out["desc"] == [3, 0, 1, 4, 2]  # missing stays last
    assert out["text"] == ["a9", "a10", "b"]
    assert out["cmp"] == [True, True, True, 0]
    assert out["pos"] == {"content": "+12.34", "className": "pos"}
    assert out["neg"] == {"content": "-0.06", "className": "neg"}
    assert out["zero"] == {"content": "0.00", "className": ""}
    assert out["none"] == {"content": "-", "className": ""}
