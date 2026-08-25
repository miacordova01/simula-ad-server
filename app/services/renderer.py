"""Renders the ad template.

Not a plain str.replace loop, because the template puts placeholders in 3
different contexts and the SAME one shows up in more than one:

    line   2  <html data-theme="{{ THEME }}">        HTML attr
    line   6  <title>{{ CHAR_NAME }} ...</title>     HTML text
    line  10  const CHAR_NAME = "{{ CHAR_NAME }}";   JS string
    line  19  const THEME     = "{{ THEME }}";       JS string

CHAR_MESSAGE is LLM copy, which is full of apostrophes. Unescaped in a JS
literal that's a syntax error and the ad renders blank; a `</script>` in there
is stored XSS on the publisher's page.

So escaping is per OCCURRENCE, not per placeholder:
  in <script>  -> JSON escaping, + < > & / so nothing closes the tag early
  in a tag     -> attr escaping
  else         -> HTML text escaping

URL placeholders also get scheme-checked so a javascript: URL can't fire.
"""

from __future__ import annotations

import html
import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

log = logging.getLogger(__name__)

_PLACEHOLDER = re.compile(r"\{\{\s*([A-Z_][A-Z0-9_]*)\s*\}\}")
_SCRIPT_BLOCK = re.compile(r"<script\b[^>]*>.*?</script\s*>", re.IGNORECASE | re.DOTALL)
_SECTION = re.compile(
    r"\{\{\s*(?P<sigil>[#^])\s*(?P<name>[A-Z_][A-Z0-9_]*)\s*\}\}"
    r"(?P<body>.*?)"
    r"\{\{\s*/\s*(?P=name)\s*\}\}",
    re.DOTALL,
)

_SAFE_URL_SCHEMES = {"http", "https"}
# Data URLs are allowed only for images, which the template uses for the
# transparent video poster.
_SAFE_DATA_PREFIX = "data:image/"


class Context:
    JS = "js"
    ATTR = "attr"
    TEXT = "text"


def safe_url(value: str | None) -> str:
    """Return the URL only if it uses a scheme that cannot execute script."""
    if not value:
        return ""
    v = value.strip()
    if v.startswith(_SAFE_DATA_PREFIX):
        return v
    try:
        parsed = urlparse(v)
    except ValueError:
        return ""
    if parsed.scheme.lower() in _SAFE_URL_SCHEMES and parsed.netloc:
        return v
    log.warning("rejected unsafe url scheme: %r", v[:80])
    return ""


def escape_js(value: str) -> str:
    """Escape for embedding inside a double-quoted JS string literal.

    `json.dumps` handles quotes, backslashes, newlines and control characters
    correctly; we strip its surrounding quotes because the template already
    supplies them. The extra replacements stop the value from terminating the
    enclosing <script> element or opening an HTML comment, both of which the
    HTML parser acts on before JavaScript ever sees the text.
    """
    encoded = json.dumps(str(value), ensure_ascii=False)[1:-1]
    return (
        encoded.replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
        .replace("/", "\\/")
    )


def escape_attr(value: str) -> str:
    return html.escape(str(value), quote=True)


def escape_text(value: str) -> str:
    return html.escape(str(value), quote=False)


_ESCAPERS = {Context.JS: escape_js, Context.ATTR: escape_attr, Context.TEXT: escape_text}


@dataclass(frozen=True)
class RenderContext:
    """Everything the template needs. Flat by design -- one place to audit."""

    CHAR_NAME: str
    CAMPAIGN: str
    CHAR_MESSAGE: str
    CTA: str
    MEDIA_URL: str
    TRACKING_URL: str
    IMPRESSION_URL: str
    AD_ID: str
    API_URL: str
    API_KEY: str
    THEME: str
    DOWNLOADS: str
    MEDIA_IS_VIDEO: bool

    def values(self) -> dict[str, str]:
        return {
            "CHAR_NAME": self.CHAR_NAME,
            "CAMPAIGN": self.CAMPAIGN,
            "CHAR_MESSAGE": self.CHAR_MESSAGE,
            "CTA": self.CTA,
            "MEDIA_URL": safe_url(self.MEDIA_URL),
            "TRACKING_URL": safe_url(self.TRACKING_URL),
            "IMPRESSION_URL": safe_url(self.IMPRESSION_URL),
            "AD_ID": self.AD_ID,
            "API_URL": self.API_URL,
            "API_KEY": self.API_KEY,
            "THEME": self.THEME if self.THEME in ("dark", "light") else "dark",
            "DOWNLOADS": self.DOWNLOADS,
        }

    def sections(self) -> dict[str, bool]:
        return {"MEDIA_IS_VIDEO": self.MEDIA_IS_VIDEO}


VIDEO_EXTENSIONS = (".mp4", ".webm", ".mov", ".m4v", ".ogv")


def looks_like_video(url: str | None) -> bool:
    """Decide the <video> vs <img> branch.

    Extension sniffing on the path only -- query strings on CDN URLs routinely
    contain the word 'mp4' in a signature or cache key, and matching those
    would put an image inside a <video> tag.
    """
    if not url:
        return False
    path = urlparse(url).path.lower()
    return path.endswith(VIDEO_EXTENSIONS)


class TemplateRenderer:
    """Loads the template once and renders it per serve."""

    def __init__(self, template_path: Path) -> None:
        self._path = template_path
        self._template = template_path.read_text(encoding="utf-8")
        self._script_spans = [m.span() for m in _SCRIPT_BLOCK.finditer(self._template)]
        log.info(
            "template loaded (%d bytes, %d script blocks)",
            len(self._template),
            len(self._script_spans),
        )

    # -- context detection ------------------------------------------
    @staticmethod
    def _context_at(text: str, index: int, script_spans: list[tuple[int, int]]) -> str:
        for start, end in script_spans:
            if start <= index < end:
                return Context.JS
        # Inside a tag if the nearest unclosed '<' precedes the nearest '>'.
        lt = text.rfind("<", 0, index)
        gt = text.rfind(">", 0, index)
        if lt > gt:
            return Context.ATTR
        return Context.TEXT

    # -- sections ---------------------------------------------------
    @staticmethod
    def _apply_sections(text: str, flags: dict[str, bool]) -> str:
        """Resolve {{#NAME}}...{{/NAME}} and {{^NAME}}...{{/NAME}}.

        Done before substitution so placeholders in the discarded branch are
        never filled -- otherwise the unused <img> would still get MEDIA_URL.
        """

        def repl(m: re.Match[str]) -> str:
            name = m.group("name")
            if name not in flags:
                return m.group(0)
            truthy = bool(flags[name])
            keep = truthy if m.group("sigil") == "#" else not truthy
            return m.group("body") if keep else ""

        prev = None
        out = text
        # Loop to handle nesting; bounded so a malformed template cannot spin.
        for _ in range(5):
            prev, out = out, _SECTION.sub(repl, out)
            if out == prev:
                break
        return out

    # -- render -----------------------------------------------------
    def render(self, ctx: RenderContext) -> str:
        values = ctx.values()
        body = self._apply_sections(self._template, ctx.sections())
        # Section removal shifts offsets, so script spans are recomputed
        # against the post-section text rather than reusing the cached ones.
        script_spans = [m.span() for m in _SCRIPT_BLOCK.finditer(body)]

        def repl(m: re.Match[str]) -> str:
            name = m.group(1)
            if name not in values:
                # Leave unknown placeholders untouched and say so loudly --
                # silently blanking them produces a broken ad that looks fine.
                log.warning("unmapped template placeholder: %s", name)
                return m.group(0)
            context = self._context_at(body, m.start(), script_spans)
            return _ESCAPERS[context](values[name])

        rendered = _PLACEHOLDER.sub(repl, body)

        leftover = _PLACEHOLDER.findall(rendered)
        if leftover:
            log.warning("template rendered with unfilled placeholders: %s", set(leftover))
        return rendered

    @property
    def placeholders(self) -> set[str]:
        return set(_PLACEHOLDER.findall(self._template))
