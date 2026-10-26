"""Operator-only install: uv run python scripts/install_kagi.py (requires Cargo).

Installs the verified subscriber CLI into this workspace, not a global profile.
Then run PATH="$PWD/.tools/bin:$PATH" uv run vresto with separately
provisioned VRESTO_ACCESS_TOKEN and KAGI_SESSION_TOKEN. No credentials
are needed for this installation.
"""
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]
subprocess.run([
    "cargo", "install", "--locked", "--git", "https://github.com/Microck/kagi-cli",
    "--rev", "fe20aa2cde1155e226dbaa313680193652efca7b", "--root", str(ROOT / ".tools"), "kagi",
], check=True)
