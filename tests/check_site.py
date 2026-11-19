#!/usr/bin/env python3
"""Static checks for the personal site. Standard library only.

Usage:
    python3 tests/check_site.py            # everyday checks
    python3 tests/check_site.py --release  # also fail on any TODO(owner)
    python3 tests/check_site.py --root DIR # check another folder (self-tests)

Exit 0 means every check passed. Each failure prints one line:
    FAIL <check>: <file>: <detail>
"""
from __future__ import annotations

import argparse
import hashlib
import re
import sys
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit, unquote

GOOGLE_FILE = "google6bf0be3df5a80368.html"
GOOGLE_SHA256 = "81a13debe7dbf46bf01b06756243905d38dc62c17b2cbf9993f0f492650ca333"

REQUIRED_FILES = [
    "index.html",
    "apps/index.html",
    "apps/tattvam/index.html",
    "apps/aangan/index.html",
    "apps/aangan/accessibility/index.html",
    "404.html",
    "assets/css/site.css",
]

LEGAL_LINKS = {
    "apps/tattvam/index.html": [
        "https://ateeshayjain.github.io/tattvam-legal/",
        "https://ateeshayjain.github.io/tattvam-legal/terms.html",
    ],
    "apps/aangan/index.html": [
        "https://ateeshayjain.github.io/aangan-site/",
        "https://ateeshayjain.github.io/aangan-site/terms.html",
    ],
}

# Pairs of (foreground, background) tokens that must reach WCAG AA 4.5:1.
CONTRAST_PAIRS = [
    ("ink", "bg"),
    ("ink-muted", "bg"),
    ("ink", "surface"),
    ("on-primary", "primary"),
    ("primary", "bg"),  # links are drawn in --primary on the page background
    ("ink-muted", "surface"),  # secondary text inside cards
    ("ink-muted", "surface-alt"),  # secondary text on tinted panels
]

VOID_TAGS = {
    "area", "base", "br", "col", "embed", "hr", "img", "input", "link",
    "meta", "param", "source", "track", "wbr",
}

# <link rel> values that are metadata only and cause no request at page load.
NON_FETCHING_RELS = {"canonical", "alternate", "me", "author", "license"}

failures: list[str] = []


def fail(check: str, file: str, detail: str) -> None:
    failures.append(f"FAIL {check}: {file}: {detail}")


# ---------------------------------------------------------------- parsing

class Page(HTMLParser):
    """Collects what the checks need from one HTML file."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.html_lang: str | None = None
        self.title_count = 0
        self.h1_count = 0
        self.metas: list[dict[str, str]] = []
        self.links: list[dict[str, str]] = []  # <link> elements
        self.refs: list[tuple[str, str, str]] = []  # (tag, attr, value)
        self.ids: set[str] = set()
        self.imgs_without_alt = 0
        self.scripts_with_src: list[str] = []
        self.inline_styles: list[str] = []
        self.empty_shots = 0
        self._stack: list[str] = []
        self._shots: list[list] = []  # [stack depth, img count]
        self._in_style = False

    def handle_starttag(self, tag, attrs):
        a = {k: (v or "") for k, v in attrs}
        if tag == "html" and "lang" in a and a["lang"].strip():
            self.html_lang = a["lang"]
        if tag == "title":
            self.title_count += 1
        if tag == "h1":
            self.h1_count += 1
        if tag == "meta":
            self.metas.append(a)
        if tag == "link":
            self.links.append(a)
        if tag == "style":
            self._in_style = True
        if "id" in a:
            self.ids.add(a["id"])
        if tag == "a" and "name" in a:
            self.ids.add(a["name"])
        for attr in ("href", "src"):
            if attr in a:
                self.refs.append((tag, attr, a[attr]))
        if "srcset" in a:
            for part in a["srcset"].split(","):
                part = part.strip()
                if part:
                    self.refs.append((tag, "srcset", part.split()[0]))
        if "style" in a:
            self.inline_styles.append(a["style"])
        if tag == "script" and a.get("src"):
            self.scripts_with_src.append(a["src"])
        if tag == "img":
            if "alt" not in dict(attrs):
                self.imgs_without_alt += 1
            for s in self._shots:
                s[1] += 1
        if tag in VOID_TAGS:
            return
        self._stack.append(tag)
        if "shots" in a.get("class", "").split():
            self._shots.append([len(self._stack), 0])

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in VOID_TAGS:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if tag == "style":
            self._in_style = False
        if tag in VOID_TAGS or tag not in self._stack:
            return
        while self._stack:
            top = self._stack.pop()
            while self._shots and self._shots[-1][0] > len(self._stack):
                if self._shots.pop()[1] == 0:
                    self.empty_shots += 1
            if top == tag:
                break

    def handle_data(self, data):
        if self._in_style:
            self.inline_styles.append(data)

    def close(self):
        super().close()
        while self._shots:
            if self._shots.pop()[1] == 0:
                self.empty_shots += 1


def parse(path: Path) -> Page:
    p = Page()
    p.feed(path.read_text(encoding="utf-8"))
    p.close()
    return p


def is_external(url: str) -> bool:
    s = urlsplit(url)
    return bool(s.scheme) or url.startswith("//")


def is_remote(url: str) -> bool:
    s = urlsplit(url)
    return s.scheme in ("http", "https") or url.startswith("//")


# ---------------------------------------------------------------- checks

def check_pages_exist(root: Path) -> None:
    for rel in REQUIRED_FILES:
        if not (root / rel).is_file():
            fail("check_pages_exist", rel, "missing")


def resolve(root: Path, page: Path, url_path: str) -> Path | None:
    """Return the file a relative or root-relative path points to, or None."""
    url_path = unquote(url_path)
    if url_path.startswith("/"):
        target = root / url_path.lstrip("/")
    elif url_path == "":
        return page
    else:
        target = page.parent / url_path
    try:
        target = target.resolve()
        target.relative_to(root.resolve())
    except ValueError:
        return None
    if target.is_file():
        return target
    if target.is_dir() and (target / "index.html").is_file():
        return target / "index.html"
    return None


def check_internal_links(root: Path, pages: dict[Path, Page]) -> None:
    for path, page in pages.items():
        rel = path.relative_to(root).as_posix()
        for tag, attr, value in page.refs:
            value = value.strip()
            if not value or is_external(value):
                continue
            parts = urlsplit(value)
            target = resolve(root, path, parts.path)
            if target is None:
                fail("check_internal_links", rel, f'{tag} {attr}="{value}" does not resolve')
                continue
            if parts.fragment:
                if target.suffix != ".html":
                    continue
                tpage = pages.get(target) or parse(target)
                if parts.fragment not in tpage.ids:
                    fail("check_internal_links", rel, f'{tag} {attr}="{value}": no id "{parts.fragment}"')


CSS_URL = re.compile(r"url\(\s*['\"]?([^'\")]+)['\"]?\s*\)", re.I)


def check_css_urls(root: Path) -> None:
    """Every local url() in a stylesheet (fonts, images) resolves to a file."""
    for css in site_files(root, "*.css"):
        text = re.sub(r"/\*.*?\*/", "", css.read_text(encoding="utf-8"), flags=re.S)
        for url in CSS_URL.findall(text):
            url = url.strip()
            if is_external(url):
                continue
            target = resolve(root, css, urlsplit(url).path)
            if target is None:
                fail("check_css_urls", css.relative_to(root).as_posix(), f'url("{url}") does not resolve')


PRELOAD_FONTS = ["/assets/fonts/fraunces-latin.woff2", "/assets/fonts/dm-sans-latin.woff2"]


def check_font_preload(root: Path, pages: dict[Path, Page]) -> None:
    """Every page preloads the two latin font files, with crossorigin."""
    for path, page in pages.items():
        rel = path.relative_to(root).as_posix()
        preloads = {
            l.get("href"): l for l in page.links
            if "preload" in l.get("rel", "").lower().split() and l.get("as") == "font"
        }
        for href in PRELOAD_FONTS:
            link = preloads.get(href)
            if link is None:
                fail("check_font_preload", rel, f"no font preload for {href}")
            elif "crossorigin" not in link or link.get("type") != "font/woff2":
                fail("check_font_preload", rel, f"preload {href} needs type=font/woff2 and crossorigin")


def check_legal_links(root: Path, pages: dict[Path, Page]) -> None:
    for rel, urls in LEGAL_LINKS.items():
        path = (root / rel).resolve()
        page = pages.get(path)
        if page is None:
            fail("check_legal_links", rel, "page missing")
            continue
        hrefs = {v for t, a, v in page.refs if t == "a" and a == "href"}
        for url in urls:
            if url not in hrefs:
                fail("check_legal_links", rel, f"no link with href exactly {url}")


def check_head(root: Path, pages: dict[Path, Page]) -> None:
    for path, page in pages.items():
        rel = path.relative_to(root).as_posix()
        if not page.html_lang:
            fail("check_head", rel, "<html> has no lang")
        if page.title_count != 1:
            fail("check_head", rel, f"expected one <title>, found {page.title_count}")
        names = {m.get("name", "").lower(): m for m in page.metas if m.get("name")}
        props = {m.get("property", "").lower(): m for m in page.metas if m.get("property")}
        for n in ("description", "viewport"):
            if not names.get(n, {}).get("content", "").strip():
                fail("check_head", rel, f'missing meta name="{n}"')
        for p in ("og:title", "og:description"):
            if not props.get(p, {}).get("content", "").strip():
                fail("check_head", rel, f'missing meta property="{p}"')
        if page.h1_count != 1:
            fail("check_head", rel, f"expected one <h1>, found {page.h1_count}")


def check_alt(root: Path, pages: dict[Path, Page]) -> None:
    for path, page in pages.items():
        if page.imgs_without_alt:
            fail("check_alt", path.relative_to(root).as_posix(),
                 f"{page.imgs_without_alt} <img> without alt")


CSS_REMOTE_IMPORT = re.compile(r"@import\s+(?:url\()?\s*['\"]?\s*(?:https?:)?//", re.I)
CSS_REMOTE_URL = re.compile(r"url\(\s*['\"]?\s*(?:https?:)?//", re.I)


def css_remote_problems(css: str) -> list[str]:
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)  # commented-out rules make no request
    out = []
    if CSS_REMOTE_IMPORT.search(css):
        out.append("remote @import")
    if CSS_REMOTE_URL.search(css):
        out.append("remote url()")
    return out


def check_no_third_party(root: Path, pages: dict[Path, Page]) -> None:
    for path, page in pages.items():
        rel = path.relative_to(root).as_posix()
        for src in page.scripts_with_src:
            if is_remote(src):
                fail("check_no_third_party", rel, f"remote script {src}")
        for link in page.links:
            href = link.get("href", "")
            rels = set(link.get("rel", "").lower().split())
            if is_remote(href) and not rels <= NON_FETCHING_RELS:
                fail("check_no_third_party", rel, f"remote <link> {href}")
        for tag, attr, value in page.refs:
            if tag in ("img", "source", "iframe", "video", "audio", "embed") and is_remote(value):
                fail("check_no_third_party", rel, f"remote {tag} {value}")
        for problem in css_remote_problems("\n".join(page.inline_styles)):
            fail("check_no_third_party", rel, f"inline CSS has {problem}")
    for css in sorted(root.rglob("*.css")):
        if ".superpowers" in css.parts:
            continue
        for problem in css_remote_problems(css.read_text(encoding="utf-8")):
            fail("check_no_third_party", css.relative_to(root).as_posix(), problem)


def check_verification_file(root: Path) -> None:
    f = root / GOOGLE_FILE
    if not f.is_file():
        fail("check_verification_file", GOOGLE_FILE, "missing")
        return
    digest = hashlib.sha256(f.read_bytes()).hexdigest()
    if digest != GOOGLE_SHA256:
        fail("check_verification_file", GOOGLE_FILE, f"sha256 changed to {digest}")


def check_no_empty_shots(root: Path, pages: dict[Path, Page]) -> None:
    for path, page in pages.items():
        if page.empty_shots:
            fail("check_no_empty_shots", path.relative_to(root).as_posix(),
                 f"{page.empty_shots} .shots element(s) without an <img>")


# ------------------------------------------------------------ contrast

def _blocks(css: str) -> list[tuple[str, str]]:
    """Top-level (prelude, body) pairs, brace-matched."""
    out, i, n = [], 0, len(css)
    while i < n:
        j = css.find("{", i)
        if j == -1:
            break
        depth, k = 1, j + 1
        while k < n and depth:
            if css[k] == "{":
                depth += 1
            elif css[k] == "}":
                depth -= 1
            k += 1
        out.append((css[i:j].strip(), css[j + 1:k - 1]))
        i = k
    return out


VAR_DECL = re.compile(r"--([\w-]+)\s*:\s*([^;]+);")
THEME_SEL = re.compile(r'\[data-theme\s*=\s*["\']?([\w-]+)["\']?\s*\]')


def parse_theme_tokens(css: str) -> dict[tuple[str, str], dict[str, str]]:
    """Map (theme, mode) -> custom properties declared for it.

    theme is "root" for :root; mode is "light" or "dark".
    """
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    found: dict[tuple[str, str], dict[str, str]] = {}

    def take(prelude: str, body: str, mode: str) -> None:
        for sel in prelude.split(","):
            sel = sel.strip()
            m = THEME_SEL.search(sel)
            if m:
                key = (m.group(1), mode)
            elif sel == ":root":
                key = ("root", mode)
            else:
                continue
            found.setdefault(key, {}).update(
                {k: v.strip() for k, v in VAR_DECL.findall(body)})

    for prelude, body in _blocks(css):
        if prelude.startswith("@media"):
            if re.search(r"prefers-color-scheme\s*:\s*dark", prelude):
                for p2, b2 in _blocks(body):
                    take(p2, b2, "dark")
            continue
        take(prelude, body, "light")
    return found


def effective_tokens(found, theme: str, mode: str) -> dict[str, str]:
    """Tokens a theme sees, applying the cascade order of the stylesheet."""
    t: dict[str, str] = {}
    t.update(found.get(("root", "light"), {}))
    if mode == "dark":
        t.update(found.get(("root", "dark"), {}))
    if theme != "root":
        t.update(found.get((theme, "light"), {}))
        if mode == "dark":
            t.update(found.get((theme, "dark"), {}))
    return t


def _hex_to_rgb(value: str) -> tuple[float, float, float] | None:
    m = re.fullmatch(r"#([0-9a-fA-F]{3}|[0-9a-fA-F]{6})", value.strip())
    if not m:
        return None
    h = m.group(1)
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    return tuple(int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))


def _luminance(rgb) -> float:
    def lin(c):
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (lin(c) for c in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast_ratio(fg: str, bg: str) -> float | None:
    a, b = _hex_to_rgb(fg), _hex_to_rgb(bg)
    if a is None or b is None:
        return None
    la, lb = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def resolve_var(tokens: dict[str, str], name: str, depth: int = 0) -> str | None:
    v = tokens.get(name)
    if v is None or depth > 10:
        return None
    m = re.fullmatch(r"var\(\s*--([\w-]+)\s*\)", v)
    return resolve_var(tokens, m.group(1), depth + 1) if m else v


def check_contrast(root: Path) -> None:
    css_path = root / "assets/css/site.css"
    rel = "assets/css/site.css"
    if not css_path.is_file():
        fail("check_contrast", rel, "missing")
        return
    found = parse_theme_tokens(css_path.read_text(encoding="utf-8"))
    if ("root", "light") not in found:
        fail("check_contrast", rel, "no :root token block")
        return
    themes = ["root"] + sorted({t for t, _ in found if t != "root"})
    for theme in themes:
        for mode in ("light", "dark"):
            if mode == "dark" and (theme, "dark") not in found and theme != "root":
                fail("check_contrast", rel, f"[data-theme={theme}] has no dark-mode block")
            tokens = effective_tokens(found, theme, mode)
            for fg, bg in CONTRAST_PAIRS:
                fv, bv = resolve_var(tokens, fg), resolve_var(tokens, bg)
                if fv is None or bv is None:
                    fail("check_contrast", rel, f"{theme}/{mode}: --{fg} or --{bg} undefined")
                    continue
                ratio = contrast_ratio(fv, bv)
                if ratio is None:
                    fail("check_contrast", rel, f"{theme}/{mode}: cannot read {fv} / {bv} as hex")
                elif ratio < 4.5:
                    fail("check_contrast", rel,
                         f"{theme}/{mode}: --{fg} {fv} on --{bg} {bv} is {ratio:.2f}:1, below 4.5")


# ---------------------------------------------------------------- release

def check_todos(root: Path, files: list[Path]) -> None:
    for f in files:
        text = f.read_text(encoding="utf-8")
        count = text.count("TODO(owner)")
        if count:
            fail("check_todos", f.relative_to(root).as_posix(), f"{count} TODO(owner) left")


# ---------------------------------------------------------------- main

def site_files(root: Path, pattern: str) -> list[Path]:
    out = []
    for f in sorted(root.rglob(pattern)):
        rel = f.relative_to(root)
        if rel.parts[0] in (".git", ".superpowers", "tests"):
            continue
        if rel.as_posix() == GOOGLE_FILE:
            continue
        out.append(f.resolve())
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--release", action="store_true", help="also fail on any TODO(owner)")
    ap.add_argument("--root", default=str(Path(__file__).resolve().parent.parent))
    args = ap.parse_args(argv)
    root = Path(args.root).resolve()
    failures.clear()

    html_files = site_files(root, "*.html")
    pages = {f: parse(f) for f in html_files}

    check_pages_exist(root)
    check_internal_links(root, pages)
    check_css_urls(root)
    check_font_preload(root, pages)
    check_legal_links(root, pages)
    check_head(root, pages)
    check_alt(root, pages)
    check_no_third_party(root, pages)
    check_verification_file(root)
    check_no_empty_shots(root, pages)
    check_contrast(root)
    if args.release:
        check_todos(root, html_files + site_files(root, "*.css"))

    for line in failures:
        print(line)
    if failures:
        print(f"{len(failures)} failure(s) in {len(html_files)} page(s)")
        return 1
    print(f"OK: {len(html_files)} page(s) checked" + (" (release)" if args.release else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
