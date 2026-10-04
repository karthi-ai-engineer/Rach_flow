"""Draw the installer's Rflow-branded wizard images into packaging/images (BMP, as Inno Setup wants them).

The large image is on the setup's first and last pages, the small one at the top right of the others. Each comes in the
sizes Inno Setup picks from for 100% to 200% display scaling. They show the logo (sst/static/brand, from
scripts/make_brand.py) in the app's Obsidian look. Run it again after changing the logo or the colours:

  uv run python scripts/make_installer_images.py
"""
import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")  # nothing is shown; the Geist fonts come with Rflow
from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QImage, QPainter, QRadialGradient
from PySide6.QtWidgets import QApplication

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "packaging" / "images"
MARK = ROOT / "sst" / "static" / "brand" / "rflow-mark-512.png"
OBSIDIAN, TEXT, TEXT_2 = QColor("#191C22"), QColor("#ECEEF3"), QColor("#A6ACBA")
LARGE = [(164, 314), (205, 393), (246, 471), (328, 628)]  # 100%, 125%, 150%, 200%
SMALL = [55, 69, 83, 110]


def _mark(size: int) -> QImage:
    image = QImage(str(MARK))
    while image.width() >= 2 * size:  # in halves: one big smooth step would blur it
        image = image.scaled(image.width() // 2, image.height() // 2, Qt.AspectRatioMode.IgnoreAspectRatio,
                             Qt.TransformationMode.SmoothTransformation)
    return image.scaled(size, size, Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation)


def large(width: int, height: int, path: Path) -> None:
    from sst import theme
    scale = width / 164
    image = QImage(width, height, QImage.Format.Format_RGB32)
    image.fill(OBSIDIAN)
    p = QPainter(image)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
    for x, y, r, colour in ((0.38, 0.30, 120, QColor(33, 154, 254, 60)), (0.66, 0.40, 110, QColor(126, 72, 252, 52))):
        glow = QRadialGradient(QPointF(width * x, height * y), r * scale)  # the logo's blue and violet, as a faint light
        glow.setColorAt(0, colour)
        glow.setColorAt(1, QColor(colour.red(), colour.green(), colour.blue(), 0))
        p.fillRect(image.rect(), glow)
    logo = round(76 * scale)
    p.drawImage(QRectF((width - logo) / 2, 84 * scale, logo, logo), _mark(logo))
    p.setPen(TEXT)
    p.setFont(theme.font(26 * scale, 600, spacing=-0.02))
    p.drawText(QRectF(0, 172 * scale, width, 36 * scale), Qt.AlignmentFlag.AlignCenter, "Rflow")
    p.setPen(TEXT_2)
    p.setFont(theme.font(11.5 * scale, 500))
    p.drawText(QRectF(10 * scale, 210 * scale, width - 20 * scale, 40 * scale),
               Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop | Qt.TextFlag.TextWordWrap,
               "Speak anywhere,\nRflow types it.")
    p.end()
    image.save(str(path))


def small(size: int, path: Path) -> None:
    image = QImage(size, size, QImage.Format.Format_RGB32)
    image.fill(QColor("white"))  # the wizard's header is white
    p = QPainter(image)
    p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
    logo = round(size * 0.82)
    p.drawImage(QRectF((size - logo) / 2, (size - logo) / 2, logo, logo), _mark(logo))
    p.end()
    image.save(str(path))


def main() -> None:
    QApplication([])
    from sst import theme
    theme.load_fonts()
    OUT.mkdir(parents=True, exist_ok=True)
    for width, height in LARGE:
        large(width, height, OUT / f"wizard-{width}.bmp")
    for size in SMALL:
        small(size, OUT / f"wizard-small-{size}.bmp")
    print("Wrote", ", ".join(sorted(p.name for p in OUT.glob("*.bmp"))))


if __name__ == "__main__":
    main()
