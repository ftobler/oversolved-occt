"""Write manifest.json next to the built artifact.

The manifest is what a consumer pins against: which upstream source went in
(the whole occt.pin.json), which compiler built it, which commit of this repo
drove the build, and the sha256 of every file shipped beside it.

The compiler version is asked of `emcc` itself and must match the pinned emsdk
version, so a stale or moved emsdk cannot ship under the pin's name.

Usage: manifest.py <dist dir> <pinned emsdk version>   (emcc on PATH)
"""

import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def emcc_version(pinned: str) -> str:
    """The first line of `emcc --version`, checked against the pin."""
    line = subprocess.run(["emcc", "--version"], capture_output=True, text=True,
                          check=True).stdout.splitlines()[0]
    found = re.search(r"\) (\d+\.\d+\.\d+)", line)
    if not found or found.group(1) != pinned:
        sys.exit(f"emcc reports {line!r}, but emsdk {pinned} is pinned")
    return line


def build_commit() -> str:
    """The commit that drove the build. A pull request builds a merge commit, so
    CI passes the head commit in BUILD_COMMIT instead."""
    if commit := os.environ.get("BUILD_COMMIT"):
        return commit
    run = subprocess.run(["git", "-C", str(REPO), "rev-parse", "HEAD"],
                         capture_output=True, text=True, check=False)
    if run.returncode != 0:
        sys.exit("no build commit: set BUILD_COMMIT or build inside a git checkout with a commit")
    status = subprocess.run(["git", "-C", str(REPO), "status", "--porcelain"],
                            capture_output=True, text=True, check=False).stdout
    # A local build of uncommitted work must not claim to be the commit.
    return run.stdout.strip() + ("-dirty" if status.strip() else "")


def main() -> None:
    dist, pinned = Path(sys.argv[1]), sys.argv[2]
    files = {
        f.name: hashlib.sha256(f.read_bytes()).hexdigest()
        for f in sorted(dist.iterdir())
        if f.is_file() and f.name != "manifest.json"
    }
    manifest = {
        "occt": json.loads((REPO / "occt.pin.json").read_text()),
        "emcc": emcc_version(pinned),
        "build_commit": build_commit(),
        "files": files,
    }
    (dist / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"manifest: {', '.join(files)}")


if __name__ == "__main__":
    main()
