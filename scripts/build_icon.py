from __future__ import annotations

import argparse
import struct
from pathlib import Path

from PySide6.QtCore import QBuffer, QIODevice, Qt
from PySide6.QtGui import QImage, QPainter
from PySide6.QtSvg import QSvgRenderer


def render_png(renderer: QSvgRenderer, size: int) -> bytes:
    image = QImage(size, size, QImage.Format.Format_ARGB32)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    try:
        renderer.render(painter)
    finally:
        painter.end()

    buffer = QBuffer()
    if not buffer.open(QIODevice.OpenModeFlag.WriteOnly):
        raise RuntimeError("Could not open an in-memory PNG buffer.")
    try:
        if not image.save(buffer, "PNG"):
            raise RuntimeError(f"Could not render the {size}x{size} icon.")
        return bytes(buffer.data())
    finally:
        buffer.close()


def build_ico(source: Path, destination: Path) -> None:
    if not source.is_file():
        raise FileNotFoundError(f"Icon source not found: {source}")
    renderer = QSvgRenderer(str(source))
    if not renderer.isValid():
        raise ValueError(f"Invalid SVG icon: {source}")

    sizes = (256, 128, 64, 48, 32, 16)
    images = [(size, render_png(renderer, size)) for size in sizes]

    header_size = 6
    entry_size = 16
    offset = header_size + entry_size * len(images)
    directory = bytearray(struct.pack("<HHH", 0, 1, len(images)))
    payload = bytearray()

    for size, png_data in images:
        dimension = 0 if size == 256 else size
        directory.extend(struct.pack(
            "<BBBBHHII",
            dimension, dimension, 0, 0,
            1, 32, len(png_data), offset
        ))
        payload.extend(png_data)
        offset += len(png_data)

    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(directory + payload)


def main() -> int:
    parser = argparse.ArgumentParser(description="Build GitArchive's multi-size Windows ICO from SVG.")
    parser.add_argument("--source", type=Path, default=Path("assets/GitArchive.svg"))
    parser.add_argument("--output", type=Path, default=Path("build/GitArchive.ico"))
    args = parser.parse_args()
    build_ico(args.source, args.output)
    print(f"Created {args.output} ({args.output.stat().st_size:,} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
