"""Portable release packaging for Windows development hosts without Docker."""
from __future__ import annotations

import argparse
import hashlib
import io
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
MANIFEST = ["app", "migrations", "tests", "docker", ".github", "Dockerfile", "docker-compose.yml", "docker-compose.aria2.yml",
            ".dockerignore", ".gitignore", ".gitattributes", ".env.example", "alembic.ini", "pytest.ini",
            "requirements.txt", "requirements-dev.txt", "package.sh", "package.py",
            "README.md", "PROJECT.md", "DEPLOY.md", "FILES.md", "LICENSE", "AGENTS.md", "AUDIT_FIXES.md",
            "OFFLINE_DOWNLOAD_PLAN.md", "RELEASE_NOTES.md", "EDITIONS.md"]


def checksum(path):
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def filtered(info):
    if "__pycache__" in Path(info.name).parts or info.name.endswith((".pyc", ".pyo")):
        return None
    return info


def build(version, image=None, aria2_image=None):
    if aria2_image and not image:
        raise ValueError("--aria2-image requires the main --image archive")
    dist = ROOT / "dist"
    dist.mkdir(exist_ok=True)
    outputs = []
    kinds = ["src"]
    if image:
        kinds.append("standard")
    if aria2_image:
        kinds.append("with-aria2")
    for kind in kinds:
        target = dist / f"file-transfer-{kind}-{version}.tar.gz"
        with tarfile.open(target, "w:gz") as archive:
            for entry in MANIFEST:
                source = "docker-compose.aria2.yml" if kind == "with-aria2" and entry == "docker-compose.yml" else entry
                archive.add(ROOT / source, arcname=f"file-transfer/{entry}", filter=filtered)
            if kind != "src":
                images = [(image, "file-transfer-1.0.tar")]
                if kind == "with-aria2":
                    images.append((aria2_image, "file-transfer-aria2-1.37.0.tar"))
                for source, name in images:
                    archive.add(source, arcname=f"file-transfer/images/{name}")
                data = "".join(f"{checksum(source)}  {name}\n" for source, name in images).encode()
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
    parser.add_argument("--aria2-image", type=Path, help="include the downloader image for fully offline deployment")
    args = parser.parse_args()
    if args.image and not args.image.is_file():
        parser.error("Image archive does not exist")
    if args.aria2_image and (not args.aria2_image.is_file() or not args.image):
        parser.error("--aria2-image must be an existing archive and requires --image")
    if not args.version.replace(".", "").replace("-", "").isalnum():
        parser.error("Invalid version")
    build(args.version, args.image, args.aria2_image)
