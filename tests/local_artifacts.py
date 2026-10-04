"""Helpers for optional local archives and synthetic activation fixtures."""
import hashlib
import unittest
from pathlib import Path

from argonus.paths import PROJECT_ROOT


def requires_local_files(*relative_paths):
    """Skip a private integration check only when its input files are absent."""
    return unittest.skipUnless(
        all((PROJECT_ROOT / path).exists() for path in relative_paths),
        "Local archives or activation settings are not included in the public repository",
    )


def synthetic_artifact_pins(root, relative_paths):
    """Create isolated stand-ins; validators still check their real SHA256 hashes."""
    pins = {}
    for relative in relative_paths:
        path = Path(root) / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"Synthetic test artifact: {relative}\n")
        pins[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    return pins
