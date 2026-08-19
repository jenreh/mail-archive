#!/usr/bin/env python
"""Generate placeholder app icons for the Tauri bundle.

`tauri build` refuses to bundle without the icons listed in
`tauri.conf.json`. This writes a simple flat-colour set so a build works out of
the box; replace them with real artwork by running

    cargo tauri icon path/to/logo.png

which overwrites everything here from a single 1024x1024 source.

Pure stdlib: PNGs are written by hand (zlib + struct) and the .icns is built
with macOS's own `iconutil`, so there is no Pillow dependency.
"""

from __future__ import annotations

import argparse
import logging
import struct
import subprocess
import sys
import zlib
from collections.abc import Callable
from pathlib import Path

logger = logging.getLogger("make_icons")

DEFAULT_OUTPUT = Path("src-tauri/icons")

#: Sizes tauri.conf.json references directly.
PNG_SIZES = {
    "32x32.png": 32,
    "128x128.png": 128,
    "128x128@2x.png": 256,
    "icon.png": 512,
}

#: Contents of the .iconset `iconutil` compiles into icon.icns.
ICONSET_SIZES = {
    "icon_16x16.png": 16,
    "icon_16x16@2x.png": 32,
    "icon_32x32.png": 32,
    "icon_32x32@2x.png": 64,
    "icon_128x128.png": 128,
    "icon_128x128@2x.png": 256,
    "icon_256x256.png": 256,
    "icon_256x256@2x.png": 512,
    "icon_512x512.png": 512,
    "icon_512x512@2x.png": 1024,
}

#: Slate blue, matching the splash accent.
FOREGROUND = (59, 111, 212)
CORNER_RADIUS_RATIO = 0.22


def _chunk(tag: bytes, data: bytes) -> bytes:
    return (
        struct.pack(">I", len(data))
        + tag
        + data
        + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    )


def render_png(size: int) -> bytes:
    """A rounded square in the accent colour, as RGBA PNG bytes."""
    radius = size * CORNER_RADIUS_RATIO
    red, green, blue = FOREGROUND

    rows = bytearray()
    for y in range(size):
        rows.append(0)  # PNG filter type 0 (None)
        for x in range(size):
            if _inside_rounded_square(x + 0.5, y + 0.5, size, radius):
                rows.extend((red, green, blue, 255))
            else:
                rows.extend((0, 0, 0, 0))

    header = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", header)
        + _chunk(b"IDAT", zlib.compress(bytes(rows), 9))
        + _chunk(b"IEND", b"")
    )


def _inside_rounded_square(x: float, y: float, size: int, radius: float) -> bool:
    near_x = min(max(x, radius), size - radius)
    near_y = min(max(y, radius), size - radius)
    dx, dy = x - near_x, y - near_y
    return dx * dx + dy * dy <= radius * radius


def write_icons(output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    cache: dict[int, bytes] = {}

    def png(size: int) -> bytes:
        if size not in cache:
            cache[size] = render_png(size)
        return cache[size]

    for name, size in PNG_SIZES.items():
        (output_dir / name).write_bytes(png(size))
        logger.info("wrote %s", name)

    if sys.platform == "darwin":
        _write_icns(output_dir, png)


def _write_icns(output_dir: Path, png: Callable[[int], bytes]) -> None:
    iconset = output_dir / "icon.iconset"
    iconset.mkdir(exist_ok=True)
    for name, size in ICONSET_SIZES.items():
        (iconset / name).write_bytes(png(size))

    result = subprocess.run(  # noqa: S603 - fixed macOS tool, local paths only
        [
            "/usr/bin/iconutil",
            "-c",
            "icns",
            str(iconset),
            "-o",
            str(output_dir / "icon.icns"),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        msg = f"iconutil failed: {result.stderr.strip()}"
        raise RuntimeError(msg)

    for child in iconset.iterdir():
        child.unlink()
    iconset.rmdir()
    logger.info("wrote icon.icns")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stderr)
    write_icons(args.output)
    logger.info("Icons ready in %s", args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
