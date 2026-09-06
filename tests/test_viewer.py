import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import customtkinter as ctk
import pypdfium2 as pdfium
from PIL import Image

from ctkpdfium import CTkPdfium, PageGeometry, PageSelection
from ctkpdfium.renderer import PDFIUM_LOCK, Renderer, RenderRequest, RenderResult


class GeometryTests(unittest.TestCase):
    def test_pdfium_coordinate_agreement(self):
        with PDFIUM_LOCK:
            doc = pdfium.PdfDocument.new()
            try:
                for rotation in (0, 90, 180, 270):
                    page = doc.new_page(600, 800)
                    page.set_cropbox(30, 60, 550, 740)
                    page.set_rotation(rotation)
                    geometry = PageGeometry(tuple(page.get_bbox()), rotation)
                    bitmap = page.render(scale=1)
                    try:
                        converter = bitmap.get_posconv(page)
                        for x, y in ((0, 0), (.25, .75), (1, 1)):
                            expected = converter.to_page(round(x * bitmap.width), round(y * bitmap.height))
                            actual = geometry.to_pdf(x, y)
                            self.assertAlmostEqual(actual[0], expected[0], delta=1)
                            self.assertAlmostEqual(actual[1], expected[1], delta=1)
                            reverse = geometry.from_pdf(*actual)
                            self.assertAlmostEqual(reverse[0], x)
                            self.assertAlmostEqual(reverse[1], y)
                    finally:
                        bitmap.close()
                        page.close()
            finally:
                doc.close()


class PdfiumBinaryTests(unittest.TestCase):
    def test_native_library_is_present_and_renders(self):
        import pypdfium2_raw

        root = Path(next(iter(pypdfium2_raw.__path__)))
        binaries = [
            path for path in root.iterdir()
            if path.is_file() and (
                path.suffix.lower() in {'.so', '.dll', '.dylib'}
                or 'pdfium' in path.name.lower()
            )
        ]
        self.assertTrue(
            binaries,
            f'no PDFium binary in {root}: {[path.name for path in root.iterdir()]}',
        )
        with PDFIUM_LOCK:
            doc = pdfium.PdfDocument.new()
            try:
                page = doc.new_page(72, 72)
                bitmap = page.render(scale=1)
                try:
                    self.assertGreater(bitmap.width, 0)
                    self.assertGreater(bitmap.height, 0)
                finally:
                    bitmap.close()
                    page.close()
            finally:
                doc.close()


@unittest.skipUnless(
    os.environ.get('DISPLAY') or os.environ.get('WAYLAND_DISPLAY')
    or sys.platform in ('win32', 'darwin'),
    'Needs a display (use xvfb-run on Linux)',
)
class ViewerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = ctk.CTk()
        self.root.geometry('800x600')
        self.errors = []
        self.root.report_callback_exception = lambda *args: self.errors.append(args)
        self.viewers = []

    def tearDown(self):
        for viewer in self.viewers:
            viewer.destroy()
            viewer._renderer.thread.join(5)
            self.assertFalse(viewer._renderer.thread.is_alive())
        for job in self.root.tk.call('after', 'info'):
            self.root.after_cancel(job)
        self.root.destroy()
        self.tmp.cleanup()
        self.assertFalse(self.errors)

    def pdf(self, count):
        path = Path(self.tmp.name) / f'{count}.pdf'
        with PDFIUM_LOCK:
            doc = pdfium.PdfDocument.new()
            for index in range(count):
                page = doc.new_page(595 if index % 2 == 0 else 842, 842 if index % 2 == 0 else 595)
                page.close()
            doc.save(path)
            doc.close()
        return path

    def pump(self, predicate, timeout=5):
        deadline = time.monotonic() + timeout
        while not predicate():
            self.root.update()
            if time.monotonic() > deadline:
                self.fail('Viewer timed out')
            time.sleep(.005)
        self.root.update()

    def viewer(self, path, **kwargs):
        viewer = CTkPdfium(self.root, path, **kwargs)
        viewer.pack(fill='both', expand=True)
        self.viewers.append(viewer)
        return viewer

    def test_virtualization_benchmark_and_navigation(self):
        for count in (1, 100, 1000):
            path = self.pdf(count)
            start = time.perf_counter()
            viewer = self.viewer(path, page_cache_size=2, navigation_overlay=True)
            elapsed = time.perf_counter() - start
            self.assertLess(elapsed, .4)
            self.pump(lambda: viewer.page_count == count and bool(viewer._cache))
            self.assertLessEqual(len(viewer._items), 5)
            print(f'{count} pages: constructor {elapsed*1000:.1f} ms; active pages {len(viewer._items)}')
            viewer.go_to_page(count)
            self.pump(lambda: count-1 in viewer._cache)
            self.assertEqual(viewer.current_page, count)
            self.assertLessEqual(len(viewer._cache), max(2, len(viewer._items)))
            self.assertLessEqual(viewer._renderer.results.qsize(), 2)
            viewer.destroy()

    def test_resize_hidpi_selection_and_stale_results(self):
        viewer = self.viewer(self.pdf(100), page_cache_size=2)
        self.pump(lambda: bool(viewer._cache))
        image = Image.new('RGBA', (200, 50), (0, 0, 0, 128))
        geometry = viewer.pages[0]
        viewer.set_selection(PageSelection(0, geometry.pdf_box((.2, .2, .5, .3))), image)
        box = viewer.get_selection().box
        for size in ('900x600', '650x500', '850x650'):
            self.root.geometry(size)
            self.root.update()
        ctk.set_widget_scaling(2)
        self.pump(lambda: viewer._layout_job is None and bool(viewer._cache))
        self.assertEqual(viewer.get_selection().box, box)
        for index, (_, image_item, _) in viewer._items.items():
            if index in viewer._photos:
                self.assertEqual(viewer._photos[index][0].width(), viewer._page_width)
                self.assertAlmostEqual(viewer.canvas.coords(image_item)[0], viewer._padding())
        ctk.set_widget_scaling(1)
        image.close()

    def test_close_during_native_render_is_nonblocking(self):
        path = self.pdf(1)
        started, release = threading.Event(), threading.Event()
        original = pdfium.PdfPage.render
        def slow(page, *args, **kwargs):
            started.set()
            release.wait(3)
            return original(page, *args, **kwargs)
        with patch.object(pdfium.PdfPage, 'render', slow):
            viewer = self.viewer(path)
            self.pump(started.is_set)
            start = time.perf_counter()
            viewer.destroy()
            self.assertLess(time.perf_counter()-start, .2)
            release.set()
            viewer._renderer.thread.join(5)
            self.assertTrue(viewer._renderer.results.empty())

    def test_loading_error_callback(self):
        path = Path(self.tmp.name) / 'invalid.pdf'
        path.write_bytes(b'not a pdf')
        errors = []
        self.viewer(path, on_error=lambda *args: errors.append(args))
        self.pump(lambda: bool(errors))
        self.assertIsNone(errors[0][0])

    def test_two_viewers_do_not_share_navigation(self):
        path = self.pdf(10)
        first = self.viewer(path)
        second = self.viewer(path)
        self.pump(lambda: first._tops and second._tops)
        first.go_to_page(10)
        self.assertEqual(first.current_page, 10)
        self.assertEqual(second.current_page, 1)

    def test_render_error_is_not_retried_on_scroll_or_resize(self):
        path = self.pdf(1)
        errors = []
        with patch.object(pdfium.PdfPage, 'render', side_effect=RuntimeError('broken page')) as render:
            viewer = self.viewer(path, on_error=lambda *args: errors.append(args))
            self.pump(lambda: bool(errors))
            for _ in range(5):
                viewer.go_to_page(1)
                viewer._refresh()
            self.root.geometry('850x600')
            self.pump(lambda: viewer._layout_job is None)
            self.assertEqual(render.call_count, 1)

    def test_revisiting_inflight_page_does_not_duplicate_work(self):
        path = self.pdf(1)
        started, release = threading.Event(), threading.Event()
        original = pdfium.PdfPage.render
        def slow(page, *args, **kwargs):
            started.set()
            release.wait(3)
            return original(page, *args, **kwargs)
        renderer = Renderer(path)
        renderer.results.get(timeout=5)  # Metadata was delivered; no Tk involved.
        try:
            with patch.object(pdfium.PdfPage, 'render', side_effect=slow, autospec=True) as render:
                request = RenderRequest(0, 1, 595, 842)
                renderer.request([request])
                self.assertTrue(started.wait(3))
                renderer.request([])
                renderer.request([request])
                release.set()
                result = renderer.results.get(timeout=5)
                self.assertIsInstance(result, RenderResult)
                if result.image:
                    result.image.close()
                renderer.close()
                renderer.thread.join(5)
                self.assertEqual(render.call_count, 1)
        finally:
            release.set()
            renderer.close()
            renderer.thread.join(5)

    def test_large_page_has_bounded_tk_strips(self):
        path = Path(self.tmp.name) / 'tall.pdf'
        with PDFIUM_LOCK:
            doc = pdfium.PdfDocument.new()
            page = doc.new_page(100, 10000)
            page.close()
            doc.save(path)
            doc.close()
        viewer = self.viewer(path, cache_memory_limit=4*1024*1024)
        self.pump(lambda: bool(viewer._photos))
        self.assertLessEqual(viewer._cache_bytes, viewer._memory_limit)
        photo, _key = viewer._photos[0]
        self.assertLessEqual(photo.height(), 3*viewer.canvas.winfo_height())


if __name__ == '__main__':
    unittest.main()
