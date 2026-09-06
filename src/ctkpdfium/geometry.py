"""Page geometry independent of Tk and of live PDFium handles."""
from __future__ import annotations

from dataclasses import dataclass
from math import isfinite

Box = tuple[float, float, float, float]


@dataclass(frozen=True)
class PageSelection:
    """A zero-based page index and rectangle in unrotated PDF user space."""

    page_index: int
    box: Box


@dataclass(frozen=True)
class PageGeometry:
    box: Box
    rotation: int = 0

    def __post_init__(self) -> None:
        left, bottom, right, top = self.box
        if not all(isfinite(v) for v in self.box) or right <= left or top <= bottom:
            raise ValueError('Invalid PDF page bounds')
        if self.rotation not in (0, 90, 180, 270):
            raise ValueError('Unsupported PDF page rotation')

    @property
    def width(self) -> float:
        x0, y0, x1, y1 = self.box
        return y1 - y0 if self.rotation % 180 else x1 - x0

    @property
    def height(self) -> float:
        x0, y0, x1, y1 = self.box
        return x1 - x0 if self.rotation % 180 else y1 - y0

    def to_pdf(self, x: float, y: float) -> tuple[float, float]:
        """Convert normalized visual coordinates (top left origin) to PDF."""
        if self.rotation == 90:
            x, y = y, 1 - x
        elif self.rotation == 180:
            x, y = 1 - x, 1 - y
        elif self.rotation == 270:
            x, y = 1 - y, x
        x0, y0, x1, y1 = self.box
        return x0 + x * (x1 - x0), y1 - y * (y1 - y0)

    def from_pdf(self, x: float, y: float) -> tuple[float, float]:
        x0, y0, x1, y1 = self.box
        x, y = (x - x0) / (x1 - x0), (y1 - y) / (y1 - y0)
        if self.rotation == 90:
            return 1 - y, x
        if self.rotation == 180:
            return 1 - x, 1 - y
        if self.rotation == 270:
            return y, 1 - x
        return x, y

    def visual_box(self, box: Box) -> Box:
        points = [self.from_pdf(x, y) for x in (box[0], box[2]) for y in (box[1], box[3])]
        return self._bounds(points)

    def pdf_box(self, box: Box) -> Box:
        points = [self.to_pdf(x, y) for x in (box[0], box[2]) for y in (box[1], box[3])]
        return self._bounds(points)

    @staticmethod
    def _bounds(points: list[tuple[float, float]]) -> Box:
        xs, ys = zip(*points)
        return min(xs), min(ys), max(xs), max(ys)
