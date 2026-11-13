from __future__ import annotations

import logging

import sounddevice as sd

log = logging.getLogger("meetingsai.device")

# The canonical 2-channel capture endpoint exposed by VB-CABLE.
# On most Windows systems this is "CABLE Output (VB-Audio Virtual Cable)" with in=2.
STRICT_NAME = "CABLE Output (VB-Audio Virtual Cable)"
STRICT_CHANNELS = 2

# Loose fallback if strict match is not found.
LOOSE_SUBSTRING = "CABLE Output"


def _list_inputs() -> list[tuple[int, str, int]]:
    out = []
    for i, d in enumerate(sd.query_devices()):
        ch = int(d.get("max_input_channels", 0))
        if ch > 0:
            out.append((i, str(d.get("name", "")), ch))
    return out


def find_input_device(name_substring: str | None = None) -> int:
    """Return the index of the VB-CABLE capture endpoint.

    Prefers the strict 2-channel endpoint; falls back to any input whose
    name contains 'CABLE Output'. Logs all candidate matches.
    """
    inputs = _list_inputs()
    matches = [(i, n, c) for i, n, c in inputs if LOOSE_SUBSTRING in n]
    if not matches:
        lines = "\n".join(f"  [{i}] {n} (in={c})" for i, n, c in inputs)
        raise RuntimeError(
            f"no input device containing '{LOOSE_SUBSTRING}' found. "
            f"Available inputs:\n{lines}"
        )

    log.info("VB-CABLE candidate matches:")
    for i, n, c in matches:
        log.info("  [%d] %s (in=%d)", i, n, c)

    # Prefer the strict match
    for i, n, c in matches:
        if n.strip() == STRICT_NAME and c == STRICT_CHANNELS:
            log.info("selected strict match: [%d] %s (in=%d)", i, n, c)
            return i

    # Fallback: first match with exactly 2 channels
    for i, n, c in matches:
        if c == 2:
            log.warning("strict match not found; using 2-channel fallback: [%d] %s", i, n)
            return i

    # Final fallback: first candidate
    i, n, c = matches[0]
    log.warning("using first loose match: [%d] %s (in=%d)", i, n, c)
    return i
