#!/usr/bin/env python
"""Vendor a self-contained FalkorDB runtime into the Tauri bundle.

Build-time only — this never runs on a user's machine. It produces a directory
holding everything a local FalkorDB needs:

    redis-server        built from the pinned Redis source (no TLS, so it links
                        nothing but system libraries)
    falkordb.so         the pinned official FalkorDB module
    libssl.3.dylib      macOS only: the module links Homebrew OpenSSL at
    libcrypto.3.dylib   absolute paths, so both are copied in and the load
                        commands are repointed at @loader_path

On macOS every rewritten Mach-O is re-signed: editing a Mach-O invalidates its
signature and arm64 macOS refuses to load unsigned code. The run finishes by
re-reading the load commands and failing if anything still points outside
@loader_path, /usr/lib or /System — without that check a bundle that only works
on a machine with Homebrew looks perfectly healthy here.

Usage:
    uv run python scripts/vendor_falkordb.py [--output DIR] [--force]
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path

from pydantic import BaseModel, ConfigDict

logger = logging.getLogger("vendor_falkordb")

DEFAULT_OUTPUT = Path("src-tauri/resources/falkordb")

FALKORDB_VERSION = "4.20.3"
REDIS_VERSION = "8.10.1"

#: Official FalkorDB release assets, keyed by (system, machine).
#: Digests come from the GitHub release metadata for the pinned tag.
FALKORDB_ASSETS: dict[tuple[str, str], tuple[str, str]] = {
    ("darwin", "arm64"): (
        "falkordb-macos-arm64v8.so",
        "efed85e0ec863b354b45b7bdbd698a2150c3adc65910011b6917b7d32ff52412",
    ),
    ("linux", "aarch64"): (
        "falkordb-arm64v8.so",
        "d84ccdcdce076469e7838828139ac768cf8457b96d9ab7f5ea7e26f0ff7f0071",
    ),
    ("linux", "x86_64"): (
        "falkordb-x64.so",
        "f7765d4898ccebb771c31eebce1b23e0102a5ae0c4e0c2fecf4d88e00e51f1d6",
    ),
}

FALKORDB_ASSET_URL = (
    "https://github.com/FalkorDB/FalkorDB/releases/download/v{version}/{asset}"
)

#: Redis source tarball. URL and digest match the Homebrew formula's pin, which
#: is an independently maintained check on the same artifact.
REDIS_SOURCE_URL = "https://download.redis.io/releases/redis-{version}.tar.gz"
REDIS_SOURCE_SHA256 = "60166c95ab7aedaa9dfe516de685be0a4dd87be95ded59ba429df14c13f1b663"

MODULE_FILENAME = "falkordb.so"
REDIS_SERVER_FILENAME = "redis-server"
OPENSSL_DYLIBS = ("libssl.3.dylib", "libcrypto.3.dylib")

#: Load-command prefixes a self-contained bundle is allowed to reference.
ALLOWED_DEPENDENCY_PREFIXES = ("@loader_path/", "/usr/lib/", "/System/")

_DOWNLOAD_CHUNK = 1 << 20
_MACH_O_MAGIC = {b"\xcf\xfa\xed\xfe", b"\xce\xfa\xed\xfe", b"\xca\xfe\xba\xbe"}


class VendorError(RuntimeError):
    """A vendoring step failed in a way the caller has to fix."""


class ModuleAsset(BaseModel):
    """The FalkorDB release asset appropriate for one platform."""

    model_config = ConfigDict(frozen=True)

    name: str
    sha256: str

    @property
    def url(self) -> str:
        return FALKORDB_ASSET_URL.format(version=FALKORDB_VERSION, asset=self.name)


# --------------------------------------------------------------------------
# Pure helpers (unit-tested)
# --------------------------------------------------------------------------


def select_module_asset(system: str, machine: str) -> ModuleAsset:
    """Pick the FalkorDB asset for a platform, or explain why there isn't one."""
    key = (system.lower(), machine.lower())
    if key not in FALKORDB_ASSETS:
        supported = ", ".join(f"{s}/{m}" for s, m in sorted(FALKORDB_ASSETS))
        raise VendorError(
            f"No FalkorDB {FALKORDB_VERSION} build for {system}/{machine}. "
            f"Supported: {supported}"
        )
    name, digest = FALKORDB_ASSETS[key]
    return ModuleAsset(name=name, sha256=digest)


def parse_otool_output(text: str) -> list[str]:
    """Extract the dependency paths from ``otool -L`` output.

    The first line is the file being inspected and every following indented
    line is ``<path> (compatibility version ..., current version ...)``.
    """
    dependencies: list[str] = []
    for raw in text.splitlines()[1:]:
        line = raw.strip()
        if not line:
            continue
        path, _, _ = line.partition(" (")
        path = path.strip()
        if path:
            dependencies.append(path)
    return dependencies


def offending_dependencies(dependencies: list[str]) -> list[str]:
    """Return the dependencies that would break on a machine without Homebrew."""
    return [
        dep for dep in dependencies if not dep.startswith(ALLOWED_DEPENDENCY_PREFIXES)
    ]


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(_DOWNLOAD_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def is_mach_o(path: Path) -> bool:
    with path.open("rb") as handle:
        return handle.read(4) in _MACH_O_MAGIC


# --------------------------------------------------------------------------
# Side-effecting steps
# --------------------------------------------------------------------------


def download_verified(url: str, expected_sha256: str, destination: Path) -> None:
    """Download to ``destination``, verifying the digest before it lands.

    The download goes to a sibling temp file and is renamed only after the
    digest matches, so an interrupted or tampered run never leaves a file that
    later looks valid.
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() and sha256_of(destination) == expected_sha256:
        logger.info("%s already present and verified", destination.name)
        return

    logger.info("Downloading %s", url)
    with tempfile.NamedTemporaryFile(
        dir=destination.parent, delete=False, suffix=".part"
    ) as tmp:
        temp_path = Path(tmp.name)
        request = urllib.request.Request(  # noqa: S310 - pinned https URLs only
            url, headers={"User-Agent": "mail-archive-vendor"}
        )
        try:
            with urllib.request.urlopen(request) as response:  # noqa: S310
                shutil.copyfileobj(response, tmp, _DOWNLOAD_CHUNK)
        except Exception as exc:
            temp_path.unlink(missing_ok=True)
            raise VendorError(f"Failed to download {url}: {exc}") from exc

    actual = sha256_of(temp_path)
    if actual != expected_sha256:
        temp_path.unlink(missing_ok=True)
        raise VendorError(
            f"Digest mismatch for {url}\n  expected {expected_sha256}\n"
            f"  actual   {actual}"
        )
    temp_path.replace(destination)
    logger.info("Verified %s (%s)", destination.name, expected_sha256[:12])


def fetch_module(output_dir: Path) -> Path:
    asset = select_module_asset(platform.system(), platform.machine())
    destination = output_dir / MODULE_FILENAME
    download_verified(asset.url, asset.sha256, destination)
    # redis-server refuses to load a module without the execute bit
    # ("failed to load: It does not have execute permissions"), and the
    # download lands via a 0600 temp file.
    destination.chmod(0o755)
    if sys.platform == "darwin":
        # Downloaded code carries com.apple.quarantine; Gatekeeper then blocks
        # dlopen and redis-server dies with an unhelpful "can't open shared
        # object file" message.
        _run(["/usr/bin/xattr", "-c", str(destination)], "clear quarantine")
    return destination


def copy_openssl(output_dir: Path) -> list[Path]:
    """Copy the Homebrew OpenSSL dylibs the FalkorDB module links against."""
    prefix = _brew_prefix("openssl@3")
    copied: list[Path] = []
    for name in OPENSSL_DYLIBS:
        source = prefix / "lib" / name
        if not source.is_file():
            raise VendorError(f"{source} not found — run `brew install openssl@3`")
        target = output_dir / name
        shutil.copy2(source, target)
        target.chmod(0o755)
        copied.append(target)
        logger.info("Copied %s", name)
    return copied


def build_redis_server(output_dir: Path) -> Path:
    """Build redis-server from the pinned source and copy it into the bundle."""
    destination = output_dir / REDIS_SERVER_FILENAME
    with tempfile.TemporaryDirectory(prefix="redis-build-") as workdir:
        work = Path(workdir)
        tarball = work / f"redis-{REDIS_VERSION}.tar.gz"
        download_verified(
            REDIS_SOURCE_URL.format(version=REDIS_VERSION),
            REDIS_SOURCE_SHA256,
            tarball,
        )

        logger.info("Extracting %s", tarball.name)
        with tarfile.open(tarball) as archive:
            archive.extractall(work, filter="data")
        source_dir = work / f"redis-{REDIS_VERSION}"
        if not source_dir.is_dir():
            raise VendorError(f"Unexpected tarball layout: {source_dir} missing")

        jobs = str(os.cpu_count() or 2)
        logger.info("Building redis-server (make -j%s, BUILD_TLS=no)", jobs)
        # BUILD_TLS=no keeps OpenSSL out of redis-server entirely, so the only
        # binary needing the vendored dylibs is the FalkorDB module.
        _run(
            ["make", "-j", jobs, "BUILD_TLS=no", "MALLOC=libc", "redis-server"],
            "build redis-server",
            cwd=source_dir / "src",
        )

        built = source_dir / "src" / REDIS_SERVER_FILENAME
        if not built.is_file():
            raise VendorError(f"Build finished but {built} is missing")
        shutil.copy2(built, destination)
    destination.chmod(0o755)
    logger.info("Built %s", REDIS_SERVER_FILENAME)
    return destination


def relink_macos(output_dir: Path) -> None:
    """Repoint absolute Homebrew paths at ``@loader_path`` and re-sign."""
    module = output_dir / MODULE_FILENAME
    libssl = output_dir / OPENSSL_DYLIBS[0]
    libcrypto = output_dir / OPENSSL_DYLIBS[1]

    _set_install_name(module, f"@loader_path/{MODULE_FILENAME}")
    for name in OPENSSL_DYLIBS:
        _set_install_name(output_dir / name, f"@loader_path/{name}")

    # The real paths differ between binaries: the module references the `opt`
    # symlink while libssl references a version-pinned Cellar path. Discover
    # them rather than hardcoding either form.
    for binary in (module, libssl, libcrypto):
        for dependency in offending_dependencies(_dependencies_of(binary)):
            name = Path(dependency).name
            if name not in OPENSSL_DYLIBS:
                raise VendorError(
                    f"{binary.name} depends on {dependency}, which is not "
                    "vendored. Update the vendoring script before shipping."
                )
            _run(
                [
                    "/usr/bin/install_name_tool",
                    "-change",
                    dependency,
                    f"@loader_path/{name}",
                    str(binary),
                ],
                f"repoint {binary.name} -> {name}",
            )

    for binary in (module, libssl, libcrypto, output_dir / REDIS_SERVER_FILENAME):
        _codesign(binary)


def verify(output_dir: Path) -> None:
    """Fail unless the bundle can load on a machine without Homebrew."""
    required = [MODULE_FILENAME, REDIS_SERVER_FILENAME]
    if sys.platform == "darwin":
        required += list(OPENSSL_DYLIBS)

    missing = [name for name in required if not (output_dir / name).is_file()]
    if missing:
        raise VendorError(f"Missing from {output_dir}: {', '.join(missing)}")

    # redis-server dlopens the module and requires the execute bit on it as
    # well as on itself; without this check the bundle looks fine and the
    # server aborts at startup.
    not_executable = [
        name
        for name in (MODULE_FILENAME, REDIS_SERVER_FILENAME)
        if not os.access(output_dir / name, os.X_OK)
    ]
    if not_executable:
        raise VendorError(
            f"Not executable in {output_dir}: {', '.join(not_executable)}"
        )

    if sys.platform != "darwin":
        logger.info("Verified %s", output_dir)
        return

    problems: list[str] = []
    for name in required:
        binary = output_dir / name
        if not is_mach_o(binary):
            problems.append(f"{name} is not a Mach-O binary")
            continue
        problems.extend(
            f"{name} still depends on {dependency}"
            for dependency in offending_dependencies(_dependencies_of(binary))
        )
        try:
            _run(["/usr/bin/codesign", "--verify", str(binary)], "verify signature")
        except VendorError:
            problems.append(f"{name} has an invalid signature")

    if problems:
        raise VendorError("Bundle is not self-contained:\n  " + "\n  ".join(problems))
    logger.info("Verified %s is self-contained", output_dir)


def vendor(output_dir: Path, *, force: bool = False) -> Path:
    output_dir = output_dir.expanduser().resolve()
    if force and output_dir.exists():
        logger.info("Removing %s", output_dir)
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    fetch_module(output_dir)
    if sys.platform == "darwin":
        copy_openssl(output_dir)
    build_redis_server(output_dir)
    if sys.platform == "darwin":
        relink_macos(output_dir)
    verify(output_dir)
    return output_dir


# --------------------------------------------------------------------------
# Process helpers
# --------------------------------------------------------------------------


def _run(
    command: list[str], what: str, cwd: Path | None = None
) -> subprocess.CompletedProcess[str]:
    logger.debug("$ %s", " ".join(command))
    # Fixed command lists built from pinned tool paths and local file paths.
    result = subprocess.run(  # noqa: S603
        command, cwd=cwd, capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        raise VendorError(
            f"Failed to {what} (exit {result.returncode}):\n"
            f"{result.stderr.strip() or result.stdout.strip()}"
        )
    return result


def _dependencies_of(binary: Path) -> list[str]:
    result = _run(["/usr/bin/otool", "-L", str(binary)], f"inspect {binary.name}")
    dependencies = parse_otool_output(result.stdout)
    # The first entry of a dylib is its own install name, not a dependency.
    return [dep for dep in dependencies if Path(dep).name != binary.name]


def _set_install_name(binary: Path, install_name: str) -> None:
    _run(
        ["/usr/bin/install_name_tool", "-id", install_name, str(binary)],
        f"set install name of {binary.name}",
    )


def _codesign(binary: Path) -> None:
    _run(
        ["/usr/bin/codesign", "--force", "--sign", "-", str(binary)],
        f"sign {binary.name}",
    )


def _brew_prefix(formula: str) -> Path:
    brew = shutil.which("brew")
    if brew is None:
        raise VendorError(
            "Homebrew is required on the build machine to supply OpenSSL "
            "(the target machine needs nothing)."
        )
    result = _run([brew, "--prefix", formula], f"locate {formula}")
    return Path(result.stdout.strip())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help=f"where to write the runtime (default: {DEFAULT_OUTPUT})",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="delete the output directory first and rebuild everything",
    )
    parser.add_argument(
        "--verify-only",
        action="store_true",
        help="only check an existing bundle is self-contained",
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
            logger.info("FalkorDB runtime ready in %s", output)
    except VendorError as exc:
        logger.error("%s", exc)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
