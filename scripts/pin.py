"""Fetch the pinned OCCT source, or re-pin to a new upstream release.

The source of truth is the upstream GitHub release named in occt.pin.json, and
from that release only its tag's source archive. The binaries attached to the
release are Windows builds and are never used.

The pin records the tag, the commit that tag points at, and the sha256 of the
archive's uncompressed tar. The tar, not the .tar.gz, is hashed: GitHub does
not promise byte-stable compression, but the tar of one commit is fixed. A
fetch also checks the commit id GitHub writes into the archive's pax header, so
a re-tagged release fails even if its hash were somehow reproduced.

Usage:
  python3 scripts/pin.py fetch          download, verify and unpack into tmp/src
  python3 scripts/pin.py repin V8.0.2   point occt.pin.json at another tag
  python3 scripts/pin.py check-tag occt-8.0.1-r1   a release tag must name the pinned version

Needs Python 3.12 or newer (tarfile extraction filters).
"""

import gzip
import hashlib
import json
import re
import shutil
import sys
import tarfile
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PIN = REPO / "occt.pin.json"
SRC = REPO / "tmp" / "src"
UPSTREAM = "Open-Cascade-SAS/OCCT"


def download(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(url) as response, dest.open("wb") as out:
        shutil.copyfileobj(response, out)


def tar_sha256(archive: Path) -> str:
    digest = hashlib.sha256()
    with gzip.open(archive, "rb") as tar:
        while chunk := tar.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def archive_commit(archive: Path) -> str:
    """The commit GitHub recorded in the archive's global pax header."""
    with tarfile.open(archive) as tar:
        return tar.pax_headers.get("comment", "")


def fetch() -> None:
    pin = json.loads(PIN.read_text())
    tree = SRC / pin["root"]
    verified = SRC / f"{pin['root']}.verified"
    # The marker is written last, so a killed fetch leaves no trusted tree behind.
    if tree.is_dir() and verified.exists() and verified.read_text().strip() == pin["sha256"]:
        print(f"{pin['tag']} already unpacked and verified at {tree}")
        return
    shutil.rmtree(tree, ignore_errors=True)
    verified.unlink(missing_ok=True)
    archive = SRC / f"{pin['root']}.tar.gz"
    download(pin["archive"], archive)
    actual = tar_sha256(archive)
    if actual != pin["sha256"]:
        archive.unlink()
        sys.exit(f"{pin['archive']} unpacks to sha256 {actual}, pin says {pin['sha256']}")
    commit = archive_commit(archive)
    if commit != pin["commit"]:
        archive.unlink()
        sys.exit(f"{pin['archive']} was built from commit {commit or 'unknown'}, pin says {pin['commit']}")
    staging = SRC / f"{pin['root']}.partial"
    shutil.rmtree(staging, ignore_errors=True)
    with tarfile.open(archive) as tar:
        tar.extractall(staging, filter="data")
    (staging / pin["root"]).rename(tree)
    staging.rmdir()
    verified.write_text(pin["sha256"] + "\n")
    print(f"{pin['tag']} ({pin['commit'][:10]}) verified and unpacked to {tree}")


def repin(tag: str) -> None:
    api = f"https://api.github.com/repos/{UPSTREAM}/git/ref/tags/{tag}"
    with urllib.request.urlopen(api) as response:
        ref = json.load(response)
    if ref["object"]["type"] == "tag":  # annotated tag: the ref names the tag object
        with urllib.request.urlopen(ref["object"]["url"]) as response:
            ref = json.load(response)
    commit = ref["object"]["sha"]
    archive_url = f"https://github.com/{UPSTREAM}/archive/refs/tags/{tag}.tar.gz"
    archive = REPO / "tmp" / "repin.tar.gz"
    download(archive_url, archive)
    if archive_commit(archive) != commit:
        sys.exit(f"{tag} resolves to {commit}, but its archive was built from {archive_commit(archive)}")
    with tarfile.open(archive) as tar:
        root = tar.getnames()[0].split("/")[0]
    pin = {
        "release": f"https://github.com/{UPSTREAM}/releases/tag/{tag}",
        "tag": tag,
        "commit": commit,
        "archive": archive_url,
        "sha256": tar_sha256(archive),
        "root": root,
    }
    PIN.write_text(json.dumps(pin, indent=2) + "\n")
    print(f"pinned {tag} at {commit[:10]}, tar sha256 {pin['sha256'][:12]}")


def check_tag(tag: str) -> None:
    """A release tag is occt-<version>-r<n>, and the version is the pinned one."""
    pin = json.loads(PIN.read_text())
    version = pin["tag"].removeprefix("V")
    if not re.fullmatch(rf"occt-{re.escape(version)}-r[1-9][0-9]*", tag):
        sys.exit(f"tag {tag!r} does not match occt-{version}-r<n>, the pinned release {pin['tag']}")
    print(f"tag {tag} matches the pin {pin['tag']}")


def main() -> None:
    match sys.argv[1:]:
        case ["fetch"]:
            fetch()
        case ["repin", tag]:
            repin(tag)
        case ["check-tag", tag]:
            check_tag(tag)
        case _:
            sys.exit(__doc__)


if __name__ == "__main__":
    main()
