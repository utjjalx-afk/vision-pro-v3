"""Audit tracked text for accidental public-repository disclosures.

This bounded pattern check complements review; it is not a complete secret detector.
"""

import re
import subprocess
import sys
from pathlib import Path


def main() -> int:
    root = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parents[1]
    paths = subprocess.check_output(["git", "ls-files", "-z"], cwd=root).decode().split("\0")
    patterns = {
        "access token": r"(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})",
        "private key": r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----",
        "personal absolute path": r"(?:[A-Za-z]:[\\/](?:Users|Documents)[\\/]|/(?:home|Users)/\w)",
        "private URL": r"https?://(?:192\.168\.|10\.\d+\.|172\.(?:1[6-9]|2\d|3[01])\.|[^/\s]+\.internal\b)",
        "URL credential": r"[a-z]+://[^\s/:]+:[^\s/@]+@",
    }
    findings = []
    for name in filter(None, paths):
        if name.startswith((".venv/", ".git/", "data/", "logs/")):
            findings.append((name, "unexpected tracked private/generated file"))
        content = (root / name).read_text(encoding="utf-8")
        for label, pattern in patterns.items():
            if re.search(pattern, content):
                findings.append((name, label))
        if name == ".env.example":
            for line in content.splitlines():
                if not line or line.startswith("#"):
                    continue
                key, value = line.split("=", 1)
                allowed = "false" if key.startswith("VISION_") else ""
                if value != allowed:
                    findings.append((name, "non-placeholder environment value"))
    for filename, reason in findings:
        print(f"{filename}: {reason}")
    if findings:
        return 1
    print(f"Public content audit passed for {len(list(filter(None, paths)))} tracked files.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
