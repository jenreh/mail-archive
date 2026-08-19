import hashlib
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import vendor_falkordb as vendor  # noqa: E402

# Real `otool -L` output for the official module, which references Homebrew
# OpenSSL through the `opt` symlink.
MODULE_OTOOL = """\
src-tauri/resources/falkordb/falkordb.so:
\t@rpath/falkordb.so (compatibility version 0.0.0, current version 0.0.0)
\t/opt/homebrew/opt/openssl@3/lib/libssl.3.dylib (compatibility version 3.0.0, current version 3.0.0)
\t/System/Library/Frameworks/CoreFoundation.framework/Versions/A/CoreFoundation (compatibility version 150.0.0, current version 3502.1.255)
\t/opt/homebrew/opt/openssl@3/lib/libcrypto.3.dylib (compatibility version 3.0.0, current version 3.0.0)
\t/usr/lib/libc++.1.dylib (compatibility version 1.0.0, current version 1900.180.0)
\t/usr/lib/libSystem.B.dylib (compatibility version 1.0.0, current version 1356.0.0)
"""

# libssl points at a VERSION-PINNED Cellar path, not the `opt` symlink the
# module uses. Hardcoding either form would miss the other.
LIBSSL_OTOOL = """\
/opt/homebrew/opt/openssl@3/lib/libssl.3.dylib:
\t/opt/homebrew/opt/openssl@3/lib/libssl.3.dylib (compatibility version 3.0.0, current version 3.0.0)
\t/opt/homebrew/Cellar/openssl@3/3.6.3/lib/libcrypto.3.dylib (compatibility version 3.0.0, current version 3.0.0)
\t/usr/lib/libSystem.B.dylib (compatibility version 1.0.0, current version 1356.0.0)
"""

RELINKED_OTOOL = """\
falkordb.so:
\t@loader_path/falkordb.so (compatibility version 0.0.0, current version 0.0.0)
\t@loader_path/libssl.3.dylib (compatibility version 3.0.0, current version 3.0.0)
\t@loader_path/libcrypto.3.dylib (compatibility version 3.0.0, current version 3.0.0)
\t/usr/lib/libSystem.B.dylib (compatibility version 1.0.0, current version 1356.0.0)
"""


class TestSelectModuleAsset:
    def test_picks_the_macos_arm64_build(self) -> None:
        asset = vendor.select_module_asset("Darwin", "arm64")

        assert asset.name == "falkordb-macos-arm64v8.so"
        assert asset.url.endswith(
            f"/v{vendor.FALKORDB_VERSION}/falkordb-macos-arm64v8.so"
        )

    @pytest.mark.parametrize(
        ("system", "machine", "expected"),
        [
            ("Linux", "x86_64", "falkordb-x64.so"),
            ("Linux", "aarch64", "falkordb-arm64v8.so"),
        ],
    )
    def test_picks_the_linux_builds(self, system, machine, expected) -> None:
        assert vendor.select_module_asset(system, machine).name == expected

    def test_unsupported_platform_lists_what_is_supported(self) -> None:
        with pytest.raises(vendor.VendorError) as excinfo:
            vendor.select_module_asset("Windows", "AMD64")

        message = str(excinfo.value)
        assert "Windows/AMD64" in message
        assert "darwin/arm64" in message

    def test_every_pinned_digest_is_a_sha256(self) -> None:
        for name, digest in vendor.FALKORDB_ASSETS.values():
            assert len(digest) == 64, name
            assert set(digest) <= set("0123456789abcdef"), name


class TestParseOtoolOutput:
    def test_skips_the_header_line_and_strips_versions(self) -> None:
        assert vendor.parse_otool_output(MODULE_OTOOL) == [
            "@rpath/falkordb.so",
            "/opt/homebrew/opt/openssl@3/lib/libssl.3.dylib",
            "/System/Library/Frameworks/CoreFoundation.framework/Versions/A/CoreFoundation",
            "/opt/homebrew/opt/openssl@3/lib/libcrypto.3.dylib",
            "/usr/lib/libc++.1.dylib",
            "/usr/lib/libSystem.B.dylib",
        ]

    def test_handles_the_cellar_versioned_libcrypto_path(self) -> None:
        dependencies = vendor.parse_otool_output(LIBSSL_OTOOL)

        assert "/opt/homebrew/Cellar/openssl@3/3.6.3/lib/libcrypto.3.dylib" in (
            dependencies
        )

    def test_empty_output_yields_nothing(self) -> None:
        assert vendor.parse_otool_output("") == []
        assert vendor.parse_otool_output("only-a-header:\n") == []


class TestOffendingDependencies:
    def test_flags_homebrew_paths(self) -> None:
        offending = vendor.offending_dependencies(
            vendor.parse_otool_output(MODULE_OTOOL)
        )

        assert offending == [
            "@rpath/falkordb.so",
            "/opt/homebrew/opt/openssl@3/lib/libssl.3.dylib",
            "/opt/homebrew/opt/openssl@3/lib/libcrypto.3.dylib",
        ]

    def test_a_relinked_binary_has_nothing_to_flag(self) -> None:
        assert (
            vendor.offending_dependencies(vendor.parse_otool_output(RELINKED_OTOOL))
            == []
        )

    def test_system_and_usr_lib_are_allowed(self) -> None:
        assert (
            vendor.offending_dependencies(
                ["/usr/lib/libSystem.B.dylib", "/System/Library/Frameworks/Foo"]
            )
            == []
        )


class TestDownloadVerified:
    def test_downloads_and_keeps_a_matching_payload(self, tmp_path, httpserver) -> None:
        payload = b"pretend this is a mach-o"
        digest = hashlib.sha256(payload).hexdigest()
        httpserver.expect_request("/module.so").respond_with_data(payload)
        destination = tmp_path / "falkordb.so"

        vendor.download_verified(httpserver.url_for("/module.so"), digest, destination)

        assert destination.read_bytes() == payload

    def test_a_digest_mismatch_rejects_the_download(self, tmp_path, httpserver) -> None:
        httpserver.expect_request("/module.so").respond_with_data(b"tampered")
        destination = tmp_path / "falkordb.so"

        with pytest.raises(vendor.VendorError, match="Digest mismatch"):
            vendor.download_verified(
                httpserver.url_for("/module.so"), "0" * 64, destination
            )

        assert not destination.exists()
        assert list(tmp_path.glob("*.part")) == []

    def test_an_already_correct_file_is_not_re_downloaded(
        self, tmp_path, httpserver
    ) -> None:
        payload = b"already here"
        digest = hashlib.sha256(payload).hexdigest()
        destination = tmp_path / "falkordb.so"
        destination.write_bytes(payload)
        # No expectation registered: a request would fail the assertion below.

        vendor.download_verified(httpserver.url_for("/never"), digest, destination)

        assert destination.read_bytes() == payload
        httpserver.check_assertions()

    def test_a_stale_file_is_replaced(self, tmp_path, httpserver) -> None:
        payload = b"the new one"
        digest = hashlib.sha256(payload).hexdigest()
        httpserver.expect_request("/module.so").respond_with_data(payload)
        destination = tmp_path / "falkordb.so"
        destination.write_bytes(b"the old one")

        vendor.download_verified(httpserver.url_for("/module.so"), digest, destination)

        assert destination.read_bytes() == payload

    def test_a_failed_request_reports_the_url(self, tmp_path, httpserver) -> None:
        httpserver.expect_request("/missing").respond_with_data("nope", status=404)
        destination = tmp_path / "falkordb.so"

        with pytest.raises(vendor.VendorError, match="Failed to download"):
            vendor.download_verified(
                httpserver.url_for("/missing"), "0" * 64, destination
            )

        assert list(tmp_path.glob("*.part")) == []


def _fake_bundle(directory: Path, payload: bytes = b"not a binary") -> Path:
    """A bundle with every required file present and executable."""
    for name in (
        vendor.MODULE_FILENAME,
        vendor.REDIS_SERVER_FILENAME,
        *vendor.OPENSSL_DYLIBS,
    ):
        target = directory / name
        target.write_bytes(payload)
        target.chmod(0o755)
    return directory


class TestVerify:
    def test_missing_files_are_named(self, tmp_path) -> None:
        with pytest.raises(vendor.VendorError, match="Missing from"):
            vendor.verify(tmp_path)

    def test_a_module_without_the_execute_bit_is_rejected(self, tmp_path) -> None:
        """The regression that let a broken bundle pass: redis-server dlopens
        the module and rejects it unless it is executable."""
        _fake_bundle(tmp_path)
        (tmp_path / vendor.MODULE_FILENAME).chmod(0o644)

        with pytest.raises(vendor.VendorError, match="Not executable"):
            vendor.verify(tmp_path)

    @pytest.mark.skipif(sys.platform != "darwin", reason="macOS Mach-O checks")
    def test_a_non_mach_o_payload_is_rejected(self, tmp_path) -> None:
        _fake_bundle(tmp_path, payload=b"not a binary")

        with pytest.raises(vendor.VendorError, match="not a Mach-O binary"):
            vendor.verify(tmp_path)


class TestHelpers:
    def test_sha256_of_matches_hashlib(self, tmp_path) -> None:
        target = tmp_path / "payload"
        target.write_bytes(b"abc")

        assert vendor.sha256_of(target) == hashlib.sha256(b"abc").hexdigest()

    def test_is_mach_o_detects_the_magic(self, tmp_path) -> None:
        macho = tmp_path / "macho"
        macho.write_bytes(b"\xcf\xfa\xed\xfe" + b"\x00" * 16)
        plain = tmp_path / "plain"
        plain.write_bytes(b"#!/bin/sh\n")

        assert vendor.is_mach_o(macho) is True
        assert vendor.is_mach_o(plain) is False
