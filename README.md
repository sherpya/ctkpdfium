# CTkPdfium

![Python 3.11+](https://img.shields.io/badge/python-3.11%20|%203.12%20|%203.13%20|%203.14-blue)
![License: MIT](https://img.shields.io/badge/license-MIT-green)
![Platform: Linux | macOS | Windows](https://img.shields.io/badge/platform-Linux%20|%20macOS%20|%20Windows-lightgrey)

An asynchronous PDF viewer for CustomTkinter, powered by PDFium. Pages fit the
available width and scroll continuously. Only visible pages and their immediate
neighbours have canvas items; opening a large document does not create thousands
of Tk widgets.

Requires Python 3.11+, Tk, CustomTkinter, Pillow and pypdfium2. PDFium binaries are
provided by the pypdfium2 wheel on supported platforms. This package is pure
Python; installing it on Linux, macOS or Windows pulls the matching pypdfium2
wheel.

## Install and run

From PyPI:

```sh
pip install ctkpdfium
```

From this directory:

```sh
uv sync
uv run main.py /path/to/document.pdf
```

To install the local library in another project:

```sh
uv add --editable /path/to/ctkpdfium
```

There is no requirement to change CustomTkinter's scaling. The viewer follows the
application's widget scaling and theme, and also works at 2× scaling.

## Minimal example

```python
import customtkinter as ctk
from ctkpdfium import CTkPdfium

root = ctk.CTk()
root.geometry('900x700')
viewer = CTkPdfium(
    root,
    file='document.pdf',
    navigation_overlay=True,
    on_load=lambda viewer: print(viewer.page_count, 'pages'),
    on_error=lambda page_index, message: print(page_index, message),
)
viewer.pack(fill='both', expand=True)
root.mainloop()
```

Opening the document, discovering page geometry and rendering happen in a worker.
`page_count` and `current_page` are zero until the document is loaded. A missing
file raises `FileNotFoundError` immediately. Invalid, empty or password-protected
PDFs report a document error asynchronously. `on_load(viewer)` and
`on_error(page_index, message)` run on the Tk thread; the error page is `None` for
an opening error, otherwise a zero-based index. Errors are shown in the viewer
and are not retried indefinitely.

## Navigation and configuration

- `current_page`, `get_current_page()`: current **one-based** page number.
- `go_to_page(number)`, `next_page()`, `previous_page()`: return the resulting page
  number. Requests are clamped to the document range; an early `go_to_page()` is
  applied when the layout is ready.
- `add_page_change_callback(callback)` / `remove_page_change_callback(callback)`:
  callbacks receive `(current_page, page_count)` on the Tk thread.
- `attach_navigation_overlay(**kwargs)`: create or return the floating navigation.
- `CTkPdfiumNavigation(viewer, overlay=False)`: create navigation for the caller
  to position inside the viewer. `get_container_frame()` returns the viewer frame.
- `page_cache_size=8`: maximum cached pages, except pages active in the viewport
  and their neighbours, which remain pinned to avoid repeated rendering.
- `cache_memory_limit=128 * 1024 * 1024`: decoded bitmap cache budget in bytes.
  Native render resolution is limited by this budget and a 16-million-pixel cap.
  Active images take priority; Tk display strips, in-flight bitmaps and PDFium's
  document memory are additional memory, not part of this cache budget.
- `translate=callable`: translate the English UI messages (for example gettext).
- Standard CTkFrame styling, `width` and `height` are accepted.

Mouse wheel events are local to each viewer. Page Up/Down and Home/End work when
the canvas has keyboard focus. Resizing preserves the reading position. Tall
pages are displayed using bounded-height image strips, not enormous Tk images.

## Image placement

The viewer does not sign or modify PDFs. Its optional selection API is intended
for applications that need to place an image accurately on a PDF page.

```python
from PIL import Image
from ctkpdfium import PageSelection

# Run after on_load. Coordinates are normalized to the displayed page:
# (0, 0) is its top left, (1, 1) its bottom right.
page = viewer.pages[0]
with Image.open('signature.png') as image:
    viewer.set_selection(
        PageSelection(0, page.pdf_box((0.15, 0.70, 0.45, 0.80))),
        image=image,
    )
viewer.add_selection_change_callback(lambda selection: print(selection))
```

`PageSelection(page_index, box)` uses a **zero-based** page index. `box` is
`(left, bottom, right, top)` in the PDF's **unrotated user space**, including the
page origin; user units normally correspond to 1/72 inch. It is not a rectangle
in screen pixels. `PageGeometry` handles page rotation and the intersection of
MediaBox and CropBox. Its `to_pdf` / `from_pdf` methods convert points, and
`pdf_box` / `visual_box` convert rectangles.

The image is copied and displayed upright as viewed on screen. Drag it to move,
or drag a corner to resize with fixed aspect ratio. Placement is clamped to the
page. To move it to another page, call `set_selection` with that page's geometry.
An application that writes a PDF appearance must also compensate for the page's
rotation when embedding the image; coordinate conversion alone does not rotate
its content.

`get_selection()` returns the selection or `None`; `clear_selection()` clears it.
`see_selection()` scrolls it into view; `is_page_rendered(page_index)` reports
whether a page is currently available in the render cache.
Selection callbacks receive this same value. Use
`remove_selection_change_callback()` to detach a callback. Without an image,
the same API displays a rectangle. Only one selection is supported.

## Lifecycle and limitations

Call all viewer methods on the Tk thread. PDFium handles belong to the renderer
and are closed there, including when the viewer is destroyed during a render.
`destroy()` cancels callbacks and pending work without waiting for native work.
Do not use private PDFium handles from the UI. Viewer workers share a lock since
PDFium cannot be called concurrently, even for different documents. Other code
using PDFium in the same process must coordinate with
`ctkpdfium.renderer.PDFIUM_LOCK` as well.

The viewer is fit-width only: no text selection, search, password entry, document
editing or arbitrary zoom. Very large pages may have reduced preview resolution.
The public navigation methods from the earlier implementation remain available;
the widget now derives from CTkFrame, not CTkScrollableFrame, and private
scrollable-frame attributes are no longer available.

## PyPI publishing configuration

The release workflow builds one sdist and one `py3-none-any` wheel, then
installs that wheel on Linux, Windows and macOS (CPython 3.11–3.14). Those
jobs pull pypdfium2's platform-specific PDFium binary, check that it loads, and
run the test suite (with `xvfb-run` on Linux).

Configure a Trusted Publisher for the `ctkpdfium` project using owner
`sherpya`, repository `ctkpdfium`, workflow `release.yml` and GitHub
environment `ctkpdfium`. For a project that does not exist yet, register a
[pending publisher](https://pypi.org/manage/account/publishing/).

The workflow checks metadata with `twine check --strict` before upload and
skips already uploaded files when retrying a partial release. PyPI files are
immutable: increment `VERSION` for changed code or metadata. Release metadata
(description, summary) is taken from the first file uploaded for that version.

```sh
python -m pip install --upgrade build twine
python -m build
python -m twine check --strict dist/*
```

## Checks

```sh
uv run python -m unittest discover -s tests -v
uv run mypy src/ctkpdfium
# Linux without a graphical session:
xvfb-run -a uv run python -m unittest discover -s tests -v
```

Tests create synthetic PDFs; no private documents or signing token are needed.
They cover 1/100/1,000-page documents, PDFium coordinate agreement on rotated and
cropped pages, HiDPI, rapid resize, independent viewers, loading errors, and
closing during rendering. On the development Linux machine, constructor times
were approximately 83 ms for 100 pages and 127 ms for 1,000 pages (versus roughly
one second for 100 pages in the earlier implementation). These are constructor
measurements, not total render times, and vary by system.

### Standalone Linux smoke test

The probe creates a temporary PDF, opens a window, verifies that PDFium renders
the page, and exits. It requires development headers matching the Python
interpreter used to compile it. The Linux check was also run successfully with
Nuitka 4.2 and Python 3.14; normal development tests use Python 3.13.

From this directory:

```sh
uv run --with nuitka --with zstandard python -m nuitka \
  --standalone --static-libpython=no --enable-plugin=tk-inter \
  --include-package=ctkpdfium --include-package=pypdfium2 \
  --include-package=pypdfium2_raw --output-dir=build/smoke \
  tests/packaging_probe.py
xvfb-run -a build/smoke/packaging_probe.dist/packaging_probe.bin
```

The successful run prints `Standalone PDFium render OK: 1 page(s)`. Check that
`packaging_probe.dist/pypdfium2_raw/libpdfium.so` is present. This tests the
packaged viewer and its native library; it does not sign with a smart card.

License: MIT. Copyright (c) Gianluigi Tiesi <sherpya@gmail.com>.
