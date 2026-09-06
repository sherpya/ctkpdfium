"""Executable smoke test for a standalone build (no external PDF or token needed)."""
import tempfile
import time
from pathlib import Path

import customtkinter as ctk
import pypdfium2 as pdfium

from ctkpdfium import CTkPdfium
from ctkpdfium.renderer import PDFIUM_LOCK


def main():
    with tempfile.TemporaryDirectory(prefix='ctkpdfium-smoke-') as temporary:
        path = Path(temporary) / 'sample.pdf'
        with PDFIUM_LOCK:
            document = pdfium.PdfDocument.new()
            page = document.new_page(595, 842)
            page.close()
            document.save(path)
            document.close()
        root = ctk.CTk()
        root.geometry('800x600')
        errors = []
        root.report_callback_exception = lambda *args: errors.append(args)
        viewer = CTkPdfium(root, path, on_error=lambda *args: errors.append(args))
        viewer.pack(fill='both', expand=True)
        try:
            deadline = time.monotonic() + 10
            while not viewer.is_page_rendered(0):
                root.update()
                if errors or time.monotonic() > deadline:
                    raise RuntimeError(errors or 'PDFium rendering timed out')
                time.sleep(.01)
            print('Standalone PDFium render OK:', viewer.page_count, 'page(s)')
        finally:
            viewer.destroy()
            viewer._renderer.thread.join(5)
            root.destroy()


if __name__ == '__main__':
    main()
