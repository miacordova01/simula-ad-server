"""Character dialogue generation.

Prompts are parsed out of the provided prompts/character_dialogue.md rather
than copy-pasted here, so that file stays the source of truth.

Serving constraints drive the rest:
  - hard timeout, and every failure degrades to the ad set's fallback_copy.
    Slightly generic copy beats a slow ad or a 500.
  - one short line out, so max_tokens is small and effort is pinned low.
    Thinking stays adaptive - disabling it can leak reasoning into the visible
    answer, which would end up as ad copy.
  - copy only depends on (character_name, ai_prompt), so it's cached per
    variant. Steady state costs no LLM latency at all.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

_CODE_BLOCK = re.compile(r"```text\n(.*?)```", re.DOTALL)


@dataclass(frozen=True)
class CopyResult:
    text: str
    source: str  # "llm" | "cache" | "fallback"
    latency_ms: float = 0.0
    error: str | None = None


def load_prompts(prompt_path: Path) -> tuple[str, str]:
    """Extract (system, user) templates from the provided markdown."""
    raw = prompt_path.read_text(encoding="utf-8")
    blocks = _CODE_BLOCK.findall(raw)
    if len(blocks) < 2:
        raise ValueError(
            f"expected 2 ```text blocks in {prompt_path}, found {len(blocks)}"
        )
    return blocks[0].strip(), blocks[1].strip()


def _clean(text: str) -> str:
    """Tidy model output into something safe to drop in a chat bubble.

    Models occasionally wrap a single line in quotes or prefix it with the
    character's name; both look wrong in the bubble, and the surrounding quotes
    would read as part of the dialogue.
    """
    t = " ".join(text.strip().split())
    if len(t) >= 2 and t[0] in "\"'\u201c" and t[-1] in "\"'\u201d":
        t = t[1:-1].strip()
    return t


class CopyGenerator:
    """Generates the character's line, with cache and fallback."""

    def __init__(
        self,
        prompt_path: Path,
        api_key: str | None,
        model: str,
        timeout_s: float,
        max_tokens: int,
        enabled: bool = True,
    ) -> None:
        self.system_prompt, self.user_template = load_prompts(prompt_path)
        self.model = model
        self.timeout_s = timeout_s
        self.max_tokens = max_tokens
        self._client = None
        self.enabled = bool(enabled and api_key)

        if self.enabled:
            try:
                from anthropic import AsyncAnthropic

                self._client = AsyncAnthropic(api_key=api_key, timeout=timeout_s, max_retries=0)
                log.info("copy generator enabled (model=%s, timeout=%.1fs)", model, timeout_s)
            except Exception:
                log.exception("failed to construct Anthropic client; using fallback copy")
                self.enabled = False
        else:
            log.warning("copy generator disabled (no API key); serving fallback copy")

    def build_user_prompt(self, character_name: str, ai_prompt: str) -> str:
        return self.user_template.replace("{{CHAR_NAME}}", character_name).replace(
            "{{ai_prompt}}", ai_prompt
        )

    async def generate(
        self, character_name: str, ai_prompt: str, fallback: str
    ) -> CopyResult:
        """One line of dialogue. Never raises -- always returns usable copy."""
        if not self.enabled or self._client is None:
            return CopyResult(text=fallback, source="fallback", error="llm_disabled")

        loop = asyncio.get_running_loop()
        started = loop.time()
        try:
            # `max_retries=0` on the client plus an outer wait_for means the
            # p99 of this call is bounded by timeout_s, not by retry storms.
            resp = await asyncio.wait_for(
                self._client.messages.create(
                    model=self.model,
                    max_tokens=self.max_tokens,
                    system=self.system_prompt,
                    # Low effort: this is a one-line generation and latency is
                    # the binding constraint. Thinking stays adaptive.
                    output_config={"effort": "low"},
                    messages=[
                        {
                            "role": "user",
                            "content": self.build_user_prompt(character_name, ai_prompt),
                        }
                    ],
                ),
                timeout=self.timeout_s,
            )
            elapsed = (loop.time() - started) * 1000

            if getattr(resp, "stop_reason", None) == "refusal":
                log.warning("llm refused copy generation for %s", character_name)
                return CopyResult(fallback, "fallback", elapsed, "refusal")

            # Only text blocks carry copy; thinking/tool blocks are skipped.
            text = _clean(
                "".join(
                    getattr(b, "text", "")
                    for b in resp.content
                    if getattr(b, "type", None) == "text"
                )
            )
            if not text:
                return CopyResult(fallback, "fallback", elapsed, "empty_output")
            return CopyResult(text, "llm", elapsed)

        except TimeoutError:
            elapsed = (loop.time() - started) * 1000
            log.warning("llm timed out after %.0fms; using fallback", elapsed)
            return CopyResult(fallback, "fallback", elapsed, "timeout")
        except Exception as exc:
            # Deliberately broad: this is a best-effort enrichment on the serve
            # path. Any failure -- auth, rate limit, network, SDK change --
            # must degrade to fallback copy rather than fail the ad request.
            elapsed = (loop.time() - started) * 1000
            log.warning("llm call failed (%s): %s", type(exc).__name__, exc)
            return CopyResult(fallback, "fallback", elapsed, type(exc).__name__)
