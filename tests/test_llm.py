"""Ad-copy generation: prompt handling, and every failure path."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.services.llm import CopyGenerator, _clean, load_prompts


class TestPromptLoading:
    def test_parses_both_blocks_from_the_provided_file(self, prompt_path):
        system, user = load_prompts(prompt_path)
        assert "one line of in-character dialogue" in system
        assert "{{CHAR_NAME}}" in user and "{{ai_prompt}}" in user

    def test_placeholders_filled(self, prompt_path):
        g = CopyGenerator(prompt_path, None, "m", 1.0, 100, enabled=False)
        out = g.build_user_prompt("Luna", "Mention the bonus.")
        assert "Luna" in out and "Mention the bonus." in out
        assert "{{" not in out

    def test_missing_file_raises(self, tmp_path):
        bad = tmp_path / "x.md"
        bad.write_text("# no code blocks")
        with pytest.raises(ValueError, match="expected 2"):
            load_prompts(bad)


class TestClean:
    @pytest.mark.parametrize("raw,expected", [
        ('  "Hello there"  ', "Hello there"),
        ("'Single quoted'", "Single quoted"),
        ("Line\nwith\nbreaks", "Line with breaks"),
        ("  spaced   out  ", "spaced out"),
        ("No quotes here", "No quotes here"),
    ])
    def test_cleanup(self, raw, expected):
        assert _clean(raw) == expected


def _gen(prompt_path, **kw) -> CopyGenerator:
    g = CopyGenerator(prompt_path, "fake-key", "m", kw.pop("timeout_s", 1.0), 100, enabled=True)
    return g


class TestGeneration:
    async def test_disabled_returns_fallback(self, prompt_path):
        g = CopyGenerator(prompt_path, None, "m", 1.0, 100, enabled=False)
        r = await g.generate("Luna", "p", "FALLBACK")
        assert r.text == "FALLBACK" and r.source == "fallback"

    async def test_success_path(self, prompt_path):
        g = _gen(prompt_path)

        class FakeMessages:
            async def create(self, **kw):
                return SimpleNamespace(
                    stop_reason="end_turn",
                    content=[SimpleNamespace(type="text", text='  "Come find me."  ')],
                )

        g._client = SimpleNamespace(messages=FakeMessages())
        r = await g.generate("Luna", "p", "FALLBACK")
        assert r.text == "Come find me."
        assert r.source == "llm"

    async def test_timeout_falls_back(self, prompt_path):
        g = _gen(prompt_path, timeout_s=0.05)

        class SlowMessages:
            async def create(self, **kw):
                await asyncio.sleep(5)

        g._client = SimpleNamespace(messages=SlowMessages())
        r = await g.generate("Luna", "p", "FALLBACK")
        assert r.text == "FALLBACK" and r.error == "timeout"

    async def test_api_error_falls_back(self, prompt_path):
        g = _gen(prompt_path)

        class BoomMessages:
            async def create(self, **kw):
                raise RuntimeError("upstream 500")

        g._client = SimpleNamespace(messages=BoomMessages())
        r = await g.generate("Luna", "p", "FALLBACK")
        assert r.text == "FALLBACK" and r.source == "fallback"

    async def test_empty_output_falls_back(self, prompt_path):
        g = _gen(prompt_path)

        class EmptyMessages:
            async def create(self, **kw):
                return SimpleNamespace(stop_reason="end_turn",
                                       content=[SimpleNamespace(type="text", text="   ")])

        g._client = SimpleNamespace(messages=EmptyMessages())
        r = await g.generate("Luna", "p", "FALLBACK")
        assert r.text == "FALLBACK" and r.error == "empty_output"

    async def test_refusal_falls_back(self, prompt_path):
        g = _gen(prompt_path)

        class RefusedMessages:
            async def create(self, **kw):
                return SimpleNamespace(stop_reason="refusal", content=[])

        g._client = SimpleNamespace(messages=RefusedMessages())
        r = await g.generate("Luna", "p", "FALLBACK")
        assert r.text == "FALLBACK" and r.error == "refusal"
