"""Template rendering and escaping.

These are the highest-value tests in the suite: the values flowing into this
template are LLM-generated and advertiser-supplied, and the template injects
them into JavaScript string literals.
"""

from __future__ import annotations

import re

import pytest

from app.services.renderer import (
    RenderContext,
    TemplateRenderer,
    escape_js,
    looks_like_video,
    safe_url,
)


@pytest.fixture
def renderer(template_path):
    return TemplateRenderer(template_path)


def ctx(**over) -> RenderContext:
    base = dict(
        CHAR_NAME="Luna", CAMPAIGN="acme", CHAR_MESSAGE="hello",
        CTA="Play Free", MEDIA_URL="https://cdn.example.com/a.mp4",
        TRACKING_URL="https://store.example.com/app",
        IMPRESSION_URL="", AD_ID="imp_1", API_URL="https://api.test",
        API_KEY="k", THEME="dark", DOWNLOADS="1.2M", MEDIA_IS_VIDEO=True,
    )
    base.update(over)
    return RenderContext(**base)


def js_const(html: str, name: str) -> str | None:
    m = re.search(rf'const {name}\s*=\s*"(.*?)";', html)
    return m.group(1) if m else None


class TestEscaping:
    def test_no_placeholders_remain(self, renderer):
        out = renderer.render(ctx())
        assert not re.search(r"\{\{.*?\}\}", out)

    def test_apostrophe_in_copy_does_not_break_js(self, renderer):
        """Real ad copy is full of apostrophes -- the common case, not an attack."""
        out = renderer.render(ctx(CHAR_MESSAGE="I'm not saying it's easy"))
        assert js_const(out, "CHAR_MESSAGE") == "I'm not saying it's easy"

    def test_double_quote_is_escaped_in_js(self, renderer):
        out = renderer.render(ctx(CHAR_MESSAGE='She said "hi"'))
        assert js_const(out, "CHAR_MESSAGE") == 'She said \\"hi\\"'

    def test_script_close_cannot_escape_the_script_block(self, renderer):
        """The XSS case: a value must not be able to close <script>."""
        out = renderer.render(ctx(CHAR_MESSAGE="</script><script>alert(1)</script>"))
        # Only the template's own two script tags survive.
        assert out.count("</script>") == 2
        assert "alert(1)" in out  # present, but inert inside a string literal
        assert "\\u003c\\/script\\u003e" in out

    def test_newlines_do_not_break_js_string(self, renderer):
        out = renderer.render(ctx(CHAR_MESSAGE="line one\nline two"))
        raw = js_const(out, "CHAR_MESSAGE")
        assert raw == "line one\\nline two"

    def test_html_text_context_is_html_escaped(self, renderer):
        out = renderer.render(ctx(CHAR_NAME="<b>Luna</b>"))
        assert "<title>&lt;b&gt;Luna&lt;/b&gt; \u00b7 Sponsored</title>" in out

    def test_same_placeholder_escaped_differently_per_context(self, renderer):
        """CHAR_NAME appears in <title> (HTML) and a JS literal. Both must be safe."""
        out = renderer.render(ctx(CHAR_NAME='A"B<C'))
        assert js_const(out, "CHAR_NAME") == 'A\\"B\\u003cC'
        assert "&lt;C" in out and "A\"B" in out.split("<script>")[0]

    def test_theme_is_whitelisted(self, renderer):
        out = renderer.render(ctx(THEME='" onload="alert(1)'))
        assert 'data-theme="dark"' in out
        assert "onload=" not in out.split("<script>")[0]


class TestUrlSafety:
    @pytest.mark.parametrize("bad", [
        "javascript:alert(1)", "JaVaScRiPt:alert(1)", "vbscript:x",
        "data:text/html;base64,PHNjcmlwdD4=", "  javascript:alert(1)  ",
    ])
    def test_dangerous_schemes_rejected(self, bad):
        assert safe_url(bad) == ""

    @pytest.mark.parametrize("good", [
        "https://cdn.example.com/a.mp4", "http://example.com/x?y=1",
        "data:image/gif;base64,R0lGOD",
    ])
    def test_safe_urls_pass(self, good):
        assert safe_url(good) == good

    def test_tracking_url_scrubbed_in_render(self, renderer):
        out = renderer.render(ctx(TRACKING_URL="javascript:alert(1)"))
        assert js_const(out, "TRACKING_URL") == ""
        assert "javascript:" not in out


class TestMediaSection:
    # Match the actual element, not the bare word: the template's JavaScript
    # comments mention "<video>" when explaining playback self-healing, so a
    # substring check on "<video" is satisfied by a comment in both branches.
    def test_video_branch_selected(self, renderer):
        out = renderer.render(ctx(MEDIA_IS_VIDEO=True))
        assert "<video src=" in out
        assert "<img src=" not in out

    def test_image_branch_selected(self, renderer):
        out = renderer.render(ctx(MEDIA_IS_VIDEO=False, MEDIA_URL="https://c/x.jpg"))
        assert "<img src=" in out
        assert "<video src=" not in out

    def test_discarded_branch_placeholders_are_not_filled(self, renderer):
        """The unused branch is removed before substitution, so its
        MEDIA_URL never appears -- and never leaks into the wrong element."""
        out = renderer.render(ctx(MEDIA_IS_VIDEO=False, MEDIA_URL="https://c/x.jpg"))
        assert out.count("https://c/x.jpg") == 1

    @pytest.mark.parametrize("url,expected", [
        ("https://c/a.mp4", True), ("https://c/a.webm", True),
        ("https://c/a.MP4", True), ("https://c/a.jpg", False),
        ("https://c/a.png?x=mp4", False),  # query must not fool the sniffer
        ("", False), (None, False),
    ])
    def test_video_detection(self, url, expected):
        assert looks_like_video(url) is expected


class TestEscapeJs:
    def test_backslash(self):
        assert escape_js("a\\b") == "a\\\\b"

    def test_unicode_preserved(self):
        assert escape_js("caf\u00e9 \u2615") == "caf\u00e9 \u2615"

    def test_ampersand_and_slash_neutralised(self):
        assert escape_js("a&b/c") == "a\\u0026b\\/c"
