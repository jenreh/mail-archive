#!/usr/bin/env python
"""Vendor the `uv` binary into the Tauri bundle.

Build-time only — this never runs on a user's machine. The bundled app runs its
Python backend through `uv`, and a GUI launch cannot be assumed to find one: an
app started from Finder inherits launchd's PATH (`/usr/bin:/bin:/usr/sbin:
/sbin`), which holds no developer toolchain at all, and the target Mac may not
have `uv` installed in the first place.

The official release binaries link nothing outside /usr/lib and /System, so the
copy that lands here is verified against that rule rather than rewritten the way
`vendor_falkordb.py` has to rewrite the FalkorDB module.

Usage:
    uv run python scripts/vendor_uv.py [--output DIR] [--force] [--verify-only]
"""

from __future__ import annotations

import argparse
import logging
import platform
import shutil
import sys
import tarfile
import tempfile
from pathlib import Path

from pydantic import BaseModel, ConfigDict
from vendor_falkordb import (
    VendorError,
    _codesign,
    _dependencies_of,
    download_verified,
    is_mach_o,
    offending_dependencies,
)

logger = logging.getLogger("vendor_uv")

DEFAULT_OUTPUT = Path("src-tauri/resources/uv")

UV_VERSION = "0.12.5"

#: Official uv release archives, keyed by (system, machine). Digests are the
#: published `<asset>.sha256` companions for the pinned tag.
UV_ASSETS: dict[tuple[str, str], tuple[str, str]] = {
    ("darwin", "arm64"): (
        "aarch64-apple-darwin",
        "5bb0e5fe008a773c3dbcb97ff79cd89e1241464fe9d2f986d52ad8f1b037bd62",
    ),
    ("darwin", "x86_64"): (
        "x86_64-apple-darwin",
        "b3b2137477cf96c9686ebfb71524614cec780c673fd73e59bce099aef02e70e8",
    ),
}

UV_ASSET_URL = "https://github.com/astral-sh/uv/releases/download/{version}/{asset}"

UV_FILENAME = "uv"


class UvAsset(BaseModel):
    """The uv release archive appropriate for one platform."""

    model_config = ConfigDict(frozen=True)

    triple: str
    sha256: str

    @property
    def archive_name(self) -> str:
        return f"uv-{self.triple}.tar.gz"

    @property
    def url(self) -> str:
        return UV_ASSET_URL.format(version=UV_VERSION, asset=self.archive_name)

    @property
    def member(self) -> str:
        """Where the binary sits inside the archive."""
        return f"uv-{self.triple}/{UV_FILENAME}"


def select_uv_asset(system: str, machine: str) -> UvAsset:
    """Pick the uv archive for a platform, or explain why there isn't one."""
    key = (system.lower(), machine.lower())
    if key not in UV_ASSETS:
        supported = ", ".join(f"{s}/{m}" for s, m in sorted(UV_ASSETS))
        raise VendorError(
            f"No uv {UV_VERSION} build for {system}/{machine}. Supported: {supported}"
        )
    triple, digest = UV_ASSETS[key]
    return UvAsset(triple=triple, sha256=digest)


def extract_uv(archive: Path, member: str, destination: Path) -> None:
    """Pull the single `uv` binary out of the release archive."""
    with tarfile.open(archive) as tar:
        try:
            extracted = tar.extractfile(member)
        except KeyError as exc:
            raise VendorError(f"{archive.name} has no member {member}") from exc
        if extracted is None:
            raise VendorError(f"{member} in {archive.name} is not a regular file")
        with extracted, destination.open("wb") as handle:
            shutil.copyfileobj(extracted, handle)
    destination.chmod(0o755)


def vendor(output_dir: Path, *, force: bool = False) -> Path:
    """Place a verified, self-contained `uv` in ``output_dir``."""
    output = output_dir.expanduser().resolve()
    if force and output.exists():
        logger.info("Removing %s", output)
        shutil.rmtree(output)
    output.mkdir(parents=True, exist_ok=True)

    asset = select_uv_asset(platform.system(), platform.machine())
    binary = output / UV_FILENAME

    with tempfile.TemporaryDirectory() as workdir:
        archive = Path(workdir) / asset.archive_name
        download_verified(asset.url, asset.sha256, archive)
        extract_uv(archive, asset.member, binary)

    # Nothing was rewritten, but the ad-hoc signature keeps the binary loadable
    # on arm64 after the copy, exactly as the FalkorDB runtime needs.
    if is_mach_o(binary):
        _codesign(binary)

    verify(output)
    return output


def verify(output_dir: Path) -> None:
    """Fail unless the vendored binary would work on a bare machine."""
    binary = output_dir / UV_FILENAME
    if not binary.is_file():
        raise VendorError(f"{binary} is missing — run `task tauri:vendor:uv`")
    if not is_mach_o(binary):
        return
    offenders = offending_dependencies(_dependencies_of(binary))
    if offenders:
        raise VendorError(
            f"{binary.name} still links outside the system:\n  "
            + "\n  ".join(offenders)
        )
    logger.info("uv %s verified self-contained", UV_VERSION)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help=f"where to write the binary (default: {DEFAULT_OUTPUT})",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="delete the output directory first and download again",
    )
    parser.add_argument(
        "--verify-only",
        action="store_true",
        help="only check an existing binary is self-contained",
    )
    parser.add_argument("--quiet", action="store_true", help="only report problems")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.WARNING if args.quiet else logging.INFO,
        format="%(message)s",
        stream=sys.stderr,
    )

    try:
        if args.verify_only:
            verify(args.output.expanduser().resolve())
        else:
            output = vendor(args.output, force=args.force)
            logger.info("uv ready in %s", output)
    except VendorError as exc:
        logger.error("%s", exc)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
