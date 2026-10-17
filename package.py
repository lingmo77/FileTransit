"""Portable release packaging for Windows development hosts without Docker."""
from __future__ import annotations

import argparse
import hashlib
import io
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
MANIFEST = ["app", "migrations", "tests", "docker", "Dockerfile", "docker-compose.yml",
            ".dockerignore", ".gitignore", ".gitattributes", ".env.example", "alembic.ini", "pytest.ini",
            "requirements.txt", "requirements-dev.txt", "package.sh", "package.py",
            "README.md", "PROJECT.md", "DEPLOY.md", "FILES.md", "LICENSE", "AGENTS.md", "AUDIT_FIXES.md"]


def checksum(path):
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def filtered(info):
    if "__pycache__" in Path(info.name).parts or info.name.endswith((".pyc", ".pyo")):
        return None
    return info


def build(version, image=None):
    dist = ROOT / "dist"
    dist.mkdir(exist_ok=True)
    outputs = []
    for kind in (["src", "offline"] if image else ["src"]):
        target = dist / f"file-transfer-{kind}-{version}.tar.gz"
        with tarfile.open(target, "w:gz") as archive:
            for entry in MANIFEST:
                archive.add(ROOT / entry, arcname=f"file-transfer/{entry}", filter=filtered)
            if kind == "offline":
                archive.add(image, arcname="file-transfer/images/file-transfer-1.0.tar")
                data = f"{checksum(image)}  file-transfer-1.0.tar\n".encode()
                info = tarfile.TarInfo("file-transfer/images/SHA256SUMS")
                info.size = len(data)
                archive.addfile(info, io.BytesIO(data))
        outputs.append(target)
        print(target.name, target.stat().st_size, checksum(target))
    sums = dist / "SHA256SUMS"
    sums.write_text("".join(f"{checksum(path)}  {path.name}\n" for path in outputs), encoding="utf-8")
    return outputs


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", required=True)
    parser.add_argument("--image", type=Path)
    args = parser.parse_args()
    if args.image and not args.image.is_file():
        parser.error("Image archive does not exist")
    if not args.version.replace(".", "").replace("-", "").isalnum():
        parser.error("Invalid version")
    build(args.version, args.image)
