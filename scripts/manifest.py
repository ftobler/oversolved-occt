"""Write manifest.json next to the built artifact.

The manifest is what a consumer pins against: which upstream source went in
(the whole occt.pin.json), which toolchain built it, which commit of this repo
drove the build, and the sha256 of every file shipped beside it.

Usage: manifest.py <dist dir>
"""

import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def main() -> None:
    dist = Path(sys.argv[1])
    justfile = (REPO / "justfile").read_text()
    emsdk = re.search(r'^emsdk_version := "([^"]+)"', justfile, re.M)
    commit = os.environ.get("GITHUB_SHA") or subprocess.run(
        ["git", "-C", str(REPO), "rev-parse", "HEAD"], capture_output=True, text=True
    ).stdout.strip()
    files = {
        f.name: hashlib.sha256(f.read_bytes()).hexdigest()
        for f in sorted(dist.iterdir())
        if f.is_file() and f.name != "manifest.json"
    }
    manifest = {
        "occt": json.loads((REPO / "occt.pin.json").read_text()),
        "emsdk": emsdk.group(1) if emsdk else None,
        "build_commit": commit or None,
        "files": files,
    }
    (dist / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"manifest: {', '.join(files)}")


if __name__ == "__main__":
    main()
