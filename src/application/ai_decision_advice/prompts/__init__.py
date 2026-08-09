from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path


PROMPT_PACK_EVIDENCE = "external_evidence"
PROMPT_PACK_ADVICE = "decision_advice"

_PROMPT_DIRS = {
    PROMPT_PACK_EVIDENCE: "evidence",
    PROMPT_PACK_ADVICE: "advice",
}

PROMPT_VERSION = "ai_decision_advice.prompts.v1"


@dataclass(frozen=True)
class PromptFragment:
    name: str
    text: str
    sha256: str


@dataclass(frozen=True)
class CompiledPromptPack:
    pack: str
    version: str
    prompt: str
    fragments: tuple[PromptFragment, ...]
    compiled_sha256: str


def compile_prompt_pack(pack: str, *, prompts_root: Path | None = None) -> CompiledPromptPack:
    """Compile ordered Markdown fragments into one prompt with a stable hash.

    Fragments are versioned code (docs/AI_DECISION_ADVICE_DESIGN.md 11): they
    live in this package, load in filename order, and the compiled SHA-256 is
    recorded on every model run. Dynamic inputs are passed as JSON data, never
    interpolated into the static prompt text.
    """

    pack_key = str(pack or "").strip()
    dirname = _PROMPT_DIRS.get(pack_key)
    if dirname is None:
        raise ValueError(f"unknown prompt pack: {pack}")
    root = Path(prompts_root) if prompts_root else Path(__file__).resolve().parent
    pack_dir = root / dirname
    if not pack_dir.is_dir():
        raise ValueError(f"prompt pack directory missing: {pack_dir}")
    files = sorted(pack_dir.glob("*.md"))
    if not files:
        raise ValueError(f"prompt pack has no fragments: {pack_dir}")
    fragments: list[PromptFragment] = []
    parts: list[str] = []
    for path in files:
        text = path.read_text(encoding="utf-8").strip()
        if not text:
            raise ValueError(f"empty prompt fragment: {path}")
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        fragments.append(PromptFragment(name=path.name, text=text, sha256=digest))
        parts.append(text)
    prompt = "\n\n".join(parts)
    compiled = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    return CompiledPromptPack(
        pack=pack_key,
        version=PROMPT_VERSION,
        prompt=prompt,
        fragments=tuple(fragments),
        compiled_sha256=compiled,
    )


def prompt_audit_payload(compiled: CompiledPromptPack) -> dict:
    return {
        "pack": compiled.pack,
        "version": compiled.version,
        "compiled_sha256": compiled.compiled_sha256,
        "fragments": [
            {"name": fragment.name, "sha256": fragment.sha256}
            for fragment in compiled.fragments
        ],
    }


def prompts_fingerprint(*compiled: CompiledPromptPack) -> str:
    payload = json.dumps(
        [prompt_audit_payload(item) for item in compiled],
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
