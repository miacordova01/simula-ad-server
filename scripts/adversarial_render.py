"""Render the ad template with hostile values and check the output still parses.

The character message is LLM-generated and the campaign fields are advertiser-
supplied, so both are untrusted. This writes one rendered ad per attack into
/tmp/adv/ so `scripts/validate_rendered_ad.js` can confirm every <script> block
is still valid JavaScript -- the real proof that the escaping held.

    python scripts/adversarial_render.py
    node scripts/validate_rendered_ad.js /tmp/adv/*.html

Payloads use \\u escapes rather than literal characters so the file stays
reviewable in a diff and cannot be mangled by tools that normalise unusual
whitespace (Python's own str.splitlines() treats U+2028 as a line break).
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.services.renderer import RenderContext, TemplateRenderer  # noqa: E402

ATTACKS: dict[str, str] = {
    # The everyday case: real ad copy is full of apostrophes and quotes.
    "quotes": "He said \"stop\" and I'm done",
    # The XSS case: try to close the enclosing <script> element.
    "script_close": '</script><script>fetch("//evil")</script>',
    "backslash": 'path\\to\\thing \\" escaped',
    "newlines": "line1\nline2\r\nline3",
    "unicode": "caf\u00e9 \u2014 emoji \U0001f680",
    # C0 control characters -- illegal raw inside a JS string literal.
    "control_chars": "before\x00\x08\x1fafter",
    "html_comment": "<!--><img src=x onerror=alert(1)>",
    # Template-literal syntax, in case anything switches to backticks.
    "template_literal": "${alert(1)} `backtick`",
    # U+2028 LINE SEPARATOR / U+2029 PARAGRAPH SEPARATOR: legal inside a JSON
    # string but historically a syntax error inside a JS string literal.
    "line_separator": "a\u2028b\u2029c",
    "long": "A" * 5000,
}

OUT = Path("/tmp/adv")


def main() -> int:
    renderer = TemplateRenderer(ROOT / "assets" / "template" / "character_ad.html")
    OUT.mkdir(parents=True, exist_ok=True)

    failures = 0
    for name, payload in ATTACKS.items():
        html = renderer.render(
            RenderContext(
                CHAR_NAME=payload,
                CAMPAIGN=payload,
                CHAR_MESSAGE=payload,
                CTA=payload,
                MEDIA_URL="https://cdn.example.com/a.mp4",
                TRACKING_URL="https://store.example.com/a",
                IMPRESSION_URL="",
                AD_ID="imp_test",
                API_URL="https://api.test",
                API_KEY="k",
                THEME="dark",
                DOWNLOADS=payload,
                MEDIA_IS_VIDEO=True,
            )
        )
        (OUT / f"{name}.html").write_text(html, encoding="utf-8")

        # The template's own two script tags must be the only ones present.
        extra = html.count("</script>") - 2
        note = ""
        if extra != 0:
            note = f"  <-- {extra} EXTRA script close!"
            failures += 1
        print(f"  {name:18} {len(html):7,d} bytes{note}")

    print(f"\nwrote {len(ATTACKS)} files to {OUT}")
    if failures:
        print(f"{failures} payload(s) escaped the script block")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
