"""Serialized PDFium access. No Tk objects may enter this module."""
from __future__ import annotations

import queue
import threading
from dataclasses import dataclass
from pathlib import Path

import pypdfium2 as pdfium
from PIL import Image

from .geometry import PageGeometry

# PDFium is process-global and cannot be called concurrently, even for different PDFs.
PDFIUM_LOCK = threading.RLock()


@dataclass(frozen=True)
class RenderRequest:
    page_index: int
    generation: int
    width: int
    height: int


@dataclass
class RenderResult:
    request: RenderRequest
    image: Image.Image | None = None
    error: str | None = None


@dataclass
class DocumentResult:
    pages: tuple[PageGeometry, ...] = ()
    error: str | None = None


class Renderer:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.results: queue.Queue[DocumentResult | RenderResult] = queue.Queue(maxsize=2)
        self.stop = threading.Event()
        self.condition = threading.Condition()
        self.requests: list[RenderRequest] = []
        self.wanted: set[RenderRequest] = set()
        self.completed: set[RenderRequest] = set()
        self.in_flight: RenderRequest | None = None
        self.thread = threading.Thread(target=self._run, name='CTkPdfiumRenderer', daemon=True)
        self.thread.start()

    def request(self, requests: list[RenderRequest]) -> None:
        with self.condition:
            self.wanted = set(requests)
            self.completed.intersection_update(self.wanted)
            self.requests = [r for r in requests if r not in self.completed and r != self.in_flight]
            self.condition.notify()

    def close(self) -> None:
        self.stop.set()
        with self.condition:
            self.requests.clear()
            self.wanted.clear()
            self.condition.notify()
        self.discard_results()

    def discard_results(self) -> None:
        while True:
            try:
                result = self.results.get_nowait()
            except queue.Empty:
                return
            if isinstance(result, RenderResult) and result.image is not None:
                result.image.close()

    def _publish(self, result: DocumentResult | RenderResult) -> None:
        while not self.stop.is_set():
            if isinstance(result, RenderResult):
                with self.condition:
                    if result.request not in self.wanted:
                        break
            try:
                self.results.put(result, timeout=0.05)
                return
            except queue.Full:
                continue
        if isinstance(result, RenderResult) and result.image is not None:
            result.image.close()

    def _run(self) -> None:
        document = None
        try:
            with PDFIUM_LOCK:
                document = pdfium.PdfDocument(str(self.path))
                document.init_forms()
                count = len(document)
            if count == 0:
                raise ValueError('The PDF does not contain any pages')
            pages = []
            for index in range(count):
                if self.stop.is_set():
                    return
                with PDFIUM_LOCK:
                    page = document[index]
                    try:
                        pages.append(PageGeometry(tuple(page.get_bbox()), page.get_rotation()))
                    finally:
                        page.close()
            self._publish(DocumentResult(tuple(pages)))
            while not self.stop.is_set():
                with self.condition:
                    self.condition.wait_for(lambda: self.requests or self.stop.is_set())
                    if self.stop.is_set():
                        return
                    request = self.requests.pop(0)
                    self.in_flight = request
                    # Mark in-flight work too, so a viewport refresh cannot duplicate it.
                    self.completed.add(request)
                try:
                    with PDFIUM_LOCK:
                        page = document[request.page_index]
                        try:
                            bitmap = page.render(
                                scale=request.width / pages[request.page_index].width,
                                fill_color=(255, 255, 255, 255),
                                prefer_bgrx=True, rev_byteorder=True,
                            )
                            try:
                                image = bitmap.to_pil().convert('RGB')
                            finally:
                                bitmap.close()
                        finally:
                            page.close()
                    self._publish(RenderResult(request, image=image))
                except Exception as exc:
                    self._publish(RenderResult(request, error=str(exc)))
                finally:
                    with self.condition:
                        if request in self.wanted:
                            self.completed.add(request)
                        self.in_flight = None
        except Exception as exc:
            self._publish(DocumentResult(error=str(exc)))
        finally:
            if document is not None:
                with PDFIUM_LOCK:
                    document.close()
            if self.stop.is_set():
                self.discard_results()
