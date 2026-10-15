"""Operator-only install: uv run python scripts/install_kagi.py (requires Cargo).

Installs the verified subscriber CLI into this workspace, not a global profile.
Then run PATH="$PWD/.tools/bin:$PATH" uv run kagi-crawler with separately
provisioned KAGI_CRAWLER_ACCESS_TOKEN and KAGI_SESSION_TOKEN. No credentials
are needed for this installation.
"""
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]
subprocess.run([
    "cargo", "install", "--locked", "--git", "https://github.com/Microck/kagi-cli",
    "--rev", "cb64cf514a99e527429468f92bc9f6b633a765e4", "--root", str(ROOT / ".tools"), "kagi",
], check=True)
