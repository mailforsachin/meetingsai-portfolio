from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


PROMPTS_DIR = Path(__file__).resolve().parents[1] / "prompts"


@dataclass(frozen=True)
class Prompt:
    name: str
    version: str
    kind: str
    body: str


def _parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text
    fm: dict[str, str] = {}
    i = 1
    while i < len(lines):
        line = lines[i]
        if line.strip() == "---":
            i += 1
            break
        if ":" in line:
            k, v = line.split(":", 1)
            fm[k.strip()] = v.strip()
        i += 1
    return fm, "\n".join(lines[i:])


def load_prompt(name: str) -> Prompt:
    path = PROMPTS_DIR / name
    if not path.exists():
        raise FileNotFoundError(f"prompt not found: {path}")
    text = path.read_text(encoding="utf-8")
    fm, body = _parse_frontmatter(text)
    return Prompt(
        name=name,
        version=fm.get("version") or "0.0.0",
        kind=fm.get("kind") or "unknown",
        body=body,
    )


_MODE_HEADER_PREFIX = "## "


def _extract_mode_block(modes_body: str, mode: str) -> str:
    """Return the body of the `## <mode>` section, or '' if absent."""
    if not mode:
        return ""
    lines = modes_body.splitlines()
    start = None
    for idx, line in enumerate(lines):
        if line.strip().lower() == f"## {mode}".lower():
            start = idx + 1
            break
    if start is None:
        return ""
    end = len(lines)
    for j in range(start, len(lines)):
        if lines[j].startswith(_MODE_HEADER_PREFIX):
            end = j
            break
    block = "\n".join(lines[start:end]).strip()
    return block


def load_prompt_for_meeting(name: str, mode: str | None) -> Prompt:
    """Compose base prompt with the mode-specific guidance block.

    The mode is a short string (e.g. 'action_tracker').  If it is None or
    unknown, the 'general' block is used.  If the mode_guidance file is
    missing, the base prompt is returned unchanged.
    """
    base = load_prompt(name)
    try:
        modes = load_prompt("mode_guidance.md")
    except FileNotFoundError:
        return base

    chosen = (mode or "general").strip().lower()
    block = _extract_mode_block(modes.body, chosen)
    if not block and chosen != "general":
        block = _extract_mode_block(modes.body, "general")

    if not block:
        return base

    body = (
        base.body
        + "\n\n# Active mode guidance\n\n"
        + f"The meeting mode is `{chosen}`. Apply the following guidance when "
        + "deciding what is relevant, what to surface, and what to ask.\n\n"
        + block
        + "\n"
    )
    return Prompt(name=base.name, version=base.version, kind=base.kind, body=body)
