import io
import sys
import tarfile
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import vendor_uv as vendor  # noqa: E402
from vendor_falkordb import VendorError  # noqa: E402


class TestSelectUvAsset:
    def test_picks_the_arm64_mac_build(self) -> None:
        asset = vendor.select_uv_asset("Darwin", "arm64")

        assert asset.triple == "aarch64-apple-darwin"
        assert asset.archive_name == "uv-aarch64-apple-darwin.tar.gz"
        assert asset.member == "uv-aarch64-apple-darwin/uv"
        assert asset.url.endswith(
            f"/download/{vendor.UV_VERSION}/uv-aarch64-apple-darwin.tar.gz"
        )

    def test_picks_the_intel_mac_build(self) -> None:
        assert (
            vendor.select_uv_asset("Darwin", "x86_64").triple == "x86_64-apple-darwin"
        )

    def test_unsupported_platform_names_what_is_supported(self) -> None:
        with pytest.raises(VendorError, match="darwin/arm64"):
            vendor.select_uv_asset("Windows", "AMD64")

    def test_digests_are_full_length_sha256(self) -> None:
        for _, digest in vendor.UV_ASSETS.values():
            assert len(digest) == 64
            assert set(digest) <= set("0123456789abcdef")


class TestExtractUv:
    @staticmethod
    def _archive(path: Path, members: dict[str, bytes]) -> Path:
        with tarfile.open(path, "w:gz") as tar:
            for name, payload in members.items():
                info = tarfile.TarInfo(name)
                info.size = len(payload)
                tar.addfile(info, io.BytesIO(payload))
        return path

    def test_writes_the_binary_and_makes_it_executable(self, tmp_path: Path) -> None:
        archive = self._archive(
            tmp_path / "uv.tar.gz", {"uv-triple/uv": b"binary", "uv-triple/uvx": b"x"}
        )
        destination = tmp_path / "uv"

        vendor.extract_uv(archive, "uv-triple/uv", destination)

        assert destination.read_bytes() == b"binary"
        assert destination.stat().st_mode & 0o111


class TestVerify:
    def test_missing_binary_is_an_error(self, tmp_path: Path) -> None:
        with pytest.raises(VendorError, match="missing"):
            vendor.verify(tmp_path)

    def test_a_non_macho_file_passes_untouched(self, tmp_path: Path) -> None:
        (tmp_path / vendor.UV_FILENAME).write_bytes(b"#!/bin/sh\n")

        vendor.verify(tmp_path)
