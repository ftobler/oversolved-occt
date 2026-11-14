"""Fetch the pinned OCCT source, or re-pin to a new upstream release.

The source of truth is the upstream GitHub release named in occt.pin.json, and
from that release only its tag's source archive. The binaries attached to the
release are Windows builds and are never used. The pin records the tag, the
commit the tag points at, and the archive's sha256, so a re-tagged or
re-generated archive fails the fetch instead of silently changing the build.

Usage:
  python3 scripts/pin.py fetch          download, verify and unpack into tmp/src
  python3 scripts/pin.py repin V8.0.2   point occt.pin.json at another tag
"""

import hashlib
import json
import sys
import tarfile
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PIN = REPO / "occt.pin.json"
SRC = REPO / "tmp" / "src"
UPSTREAM = "Open-Cascade-SAS/OCCT"


def download(url: str, dest: Path) -> str:
    """Stream url to dest and return its sha256."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    with urllib.request.urlopen(url) as response, dest.open("wb") as out:
        while chunk := response.read(1 << 20):
            digest.update(chunk)
            out.write(chunk)
    return digest.hexdigest()


def fetch() -> None:
    pin = json.loads(PIN.read_text())
    archive = SRC / f"{pin['root']}.tar.gz"
    tree = SRC / pin["root"]
    if tree.is_dir():
        print(f"{tree} already unpacked")
        return
    actual = download(pin["archive"], archive)
    if actual != pin["sha256"]:
        archive.unlink()
        sys.exit(f"{pin['archive']} has sha256 {actual}, pin says {pin['sha256']}")
    with tarfile.open(archive) as tar:
        tar.extractall(SRC, filter="data")
    print(f"{pin['tag']} ({pin['commit'][:10]}) verified and unpacked to {tree}")


def repin(tag: str) -> None:
    api = f"https://api.github.com/repos/{UPSTREAM}/git/ref/tags/{tag}"
    with urllib.request.urlopen(api) as response:
        ref = json.load(response)
    if ref["object"]["type"] == "tag":  # annotated tag: the ref names the tag object
        with urllib.request.urlopen(ref["object"]["url"]) as response:
            ref = json.load(response)
    archive = f"https://github.com/{UPSTREAM}/archive/refs/tags/{tag}.tar.gz"
    sha256 = download(archive, REPO / "tmp" / "repin.tar.gz")
    with tarfile.open(REPO / "tmp" / "repin.tar.gz") as tar:
        root = tar.getnames()[0].split("/")[0]
    pin = {
        "release": f"https://github.com/{UPSTREAM}/releases/tag/{tag}",
        "tag": tag,
        "commit": ref["object"]["sha"],
        "archive": archive,
        "sha256": sha256,
        "root": root,
    }
    PIN.write_text(json.dumps(pin, indent=2) + "\n")
    print(f"pinned {tag} at {pin['commit'][:10]}, sha256 {sha256[:12]}")


def main() -> None:
    match sys.argv[1:]:
        case ["fetch"]:
            fetch()
        case ["repin", tag]:
            repin(tag)
        case _:
            sys.exit(__doc__)


if __name__ == "__main__":
    main()
