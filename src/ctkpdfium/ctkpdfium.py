from __future__ import annotations

import math
import os
import queue
import sys
import tkinter as tk
from bisect import bisect_left, bisect_right
from collections import OrderedDict
from pathlib import Path
from typing import Any, Callable

import customtkinter as ctk
from PIL import Image, ImageTk

from .geometry import Box, PageGeometry, PageSelection
from .renderer import DocumentResult, Renderer, RenderRequest

PageChangeCallback = Callable[[int, int], None]


class CTkPdfiumNavigation(ctk.CTkFrame):
    def __init__(self, viewer: CTkPdfium, *, overlay: bool = True, **kwargs: Any) -> None:
        defaults = dict(corner_radius=18, border_width=1,
                        fg_color=('gray92', 'gray16'), border_color=('gray78', 'gray28'))
        defaults.update(kwargs)
        super().__init__(viewer.get_container_frame(), **defaults)
        self._viewer = viewer
        self._page_var = tk.StringVar(self, value=str(viewer.current_page))
        self._previous_button = ctk.CTkButton(self, text='←', width=36, command=viewer.previous_page)
        self._previous_button.grid(row=0, column=0, padx=(8, 4), pady=8)
        self._page_entry = ctk.CTkEntry(self, width=56, textvariable=self._page_var, justify='center')
        self._page_entry.grid(row=0, column=1, padx=4, pady=8)
        self._page_entry.bind('<Return>', self._submit)
        self._page_entry.bind('<FocusOut>', lambda e: self._page_var.set(str(viewer.current_page)))
        self._count = ctk.CTkLabel(self, width=52, text='')
        self._count.grid(row=0, column=2, padx=4, pady=8)
        self._next_button = ctk.CTkButton(self, text='→', width=36, command=viewer.next_page)
        self._next_button.grid(row=0, column=3, padx=(4, 8), pady=8)
        viewer.add_page_change_callback(self._changed)
        self._changed(viewer.current_page, viewer.page_count)
        if overlay:
            self.place(relx=.5, rely=1, anchor='s', y=-12)

    def _submit(self, _: Any) -> None:
        try:
            self._viewer.go_to_page(int(self._page_var.get()))
        except ValueError:
            pass
        self._page_var.set(str(self._viewer.current_page))

    def _changed(self, current: int, count: int) -> None:
        self._page_var.set(str(current))
        self._count.configure(text=f'/ {count}')
        self._page_entry.configure(state='normal' if count else 'disabled')
        self._previous_button.configure(state='normal' if current > 1 else 'disabled')
        self._next_button.configure(state='normal' if current < count else 'disabled')

    def destroy(self) -> None:
        self._viewer.remove_page_change_callback(self._changed)
        super().destroy()


class CTkPdfium(ctk.CTkFrame):
    """Virtualized, fit-width PDF viewer. All public methods run on the Tk thread."""

    def __init__(self, master: Any, file: str | os.PathLike[str], *,
                 page_cache_size: int = 8, cache_memory_limit: int = 128 * 1024 * 1024,
                 width: int | None = None, height: int | None = None,
                 navigation_overlay: bool = False,
                 on_load: Callable[[CTkPdfium], None] | None = None,
                 on_error: Callable[[int | None, str], None] | None = None,
                 translate: Callable[[str], str] = lambda s: s,
                 **kwargs: Any) -> None:
        path = Path(file).expanduser()
        if not path.is_file():
            raise FileNotFoundError(path)
        self._destroyed = False
        self._ready = False
        super().__init__(master, width=width or 800, height=height or 600, **kwargs)
        self._ = translate
        self._on_load = on_load
        self._on_error = on_error
        self.page_count = 0
        self.pages: tuple[PageGeometry, ...] = ()
        self._current_page_index = -1
        self._early_page = 1
        self._page_change_callbacks: list[PageChangeCallback] = []
        self._selection_callbacks: list[Callable[[PageSelection | None], None]] = []
        self.navigation_overlay: CTkPdfiumNavigation | None = None
        self._page_cache_size = max(1, page_cache_size)
        self._memory_limit = max(1024 * 1024, cache_memory_limit)
        self._cache: OrderedDict[int, tuple[Image.Image, int]] = OrderedDict()
        self._photos: dict[int, tuple[ImageTk.PhotoImage, tuple[int, int, int]]] = {}
        self._cache_bytes = 0
        self._errors: dict[int, str] = {}
        self._items: dict[int, tuple[int, int, int]] = {}
        self._requests: dict[int, RenderRequest] = {}
        self._generation = 0
        self._page_width = 0
        self._tops: list[float] = []
        self._bottoms: list[float] = []
        self._total_height = 1.0
        self._layout_job: str | None = None
        self._refresh_job: str | None = None
        self._poll_job: str | None = None
        self._selection: PageSelection | None = None
        self._selection_image: Image.Image | None = None
        self._overlay_photo: ImageTk.PhotoImage | None = None
        self._overlay_size: tuple[int, int, int, int] | None = None
        self._drag: tuple[str, float, float, Box] | None = None
        self._wheel_remainder = 0.0
        self.grid_rowconfigure(0, weight=1)
        self.grid_columnconfigure(0, weight=1)
        self.canvas = tk.Canvas(self, highlightthickness=0, borderwidth=0, takefocus=True)
        self.canvas.grid(row=0, column=0, sticky='nsew', padx=2, pady=2)
        self._scrollbar = ctk.CTkScrollbar(self, command=self._scroll)
        self._scrollbar.grid(row=0, column=1, sticky='ns', pady=2)
        self.canvas.configure(yscrollcommand=self._scrollbar.set)
        self.canvas.bind('<Configure>', self._configure_view)
        self.canvas.bind('<MouseWheel>', self._wheel)
        self.canvas.bind('<Button-4>', lambda e: self._wheel(e, -1))
        self.canvas.bind('<Button-5>', lambda e: self._wheel(e, 1))
        self.canvas.bind('<Prior>', lambda e: self._key_page(-1))
        self.canvas.bind('<Next>', lambda e: self._key_page(1))
        self.canvas.bind('<Home>', lambda e: self._key_page(-self.page_count))
        self.canvas.bind('<End>', lambda e: self._key_page(self.page_count))
        self.canvas.bind('<ButtonPress-1>', self._press)
        self.canvas.bind('<B1-Motion>', self._motion)
        self.canvas.bind('<ButtonRelease-1>', self._release)
        self._ready = True
        self._apply_colors()
        self._status = self.canvas.create_text(20, 20, anchor='nw',
                                               text=self._('Loading PDF…'), fill='gray60')
        self._renderer = Renderer(path)
        self._poll_job = self.after(20, self._poll)
        if navigation_overlay:
            self.attach_navigation_overlay()

    @property
    def current_page(self) -> int:
        return self._current_page_index + 1

    def get_current_page(self) -> int:
        return self.current_page

    def is_page_rendered(self, page_index: int) -> bool:
        return page_index in self._cache

    def get_container_frame(self) -> CTkPdfium:
        return self

    def add_page_change_callback(self, callback: PageChangeCallback) -> None:
        if callback not in self._page_change_callbacks:
            self._page_change_callbacks.append(callback)

    def remove_page_change_callback(self, callback: PageChangeCallback) -> None:
        if callback in self._page_change_callbacks:
            self._page_change_callbacks.remove(callback)

    def attach_navigation_overlay(self, **kwargs: Any) -> CTkPdfiumNavigation:
        if self.navigation_overlay is None or not self.navigation_overlay.winfo_exists():
            self.navigation_overlay = CTkPdfiumNavigation(self, **kwargs)
        return self.navigation_overlay

    def go_to_page(self, page_number: int) -> int:
        if not self.pages:
            self._early_page = max(1, int(page_number))
            return 0
        index = min(self.page_count - 1, max(0, int(page_number) - 1))
        if not self._tops:
            self._early_page = index + 1
            self._layout()
            if not self._tops:
                return 0
        self.canvas.yview_moveto(max(0, self._tops[index] - self._padding()) / self._total_height)
        self._refresh()
        self._set_current_page(index)
        return self.current_page

    def next_page(self) -> int:
        return self.go_to_page(self.current_page + 1)

    def previous_page(self) -> int:
        return self.go_to_page(self.current_page - 1)

    def _key_page(self, delta: int) -> str:
        self.go_to_page(self.current_page + delta)
        return 'break'

    def _set_current_page(self, index: int) -> None:
        if self._current_page_index != index:
            self._current_page_index = index
            for callback in tuple(self._page_change_callbacks):
                self._call(callback, self.current_page, self.page_count)

    def _call(self, callback: Callable[..., Any], *args: Any) -> None:
        try:
            callback(*args)
        except Exception:
            self._root().report_callback_exception(*sys.exc_info())

    def _padding(self) -> float:
        return round(18 * self._get_widget_scaling())

    def _apply_colors(self) -> None:
        background = self.cget('fg_color')
        if background == 'transparent':
            background = self.cget('bg_color')
        color = self._apply_appearance_mode(background)
        self.canvas.configure(bg=color)

    def _set_appearance_mode(self, mode_string: str) -> None:
        super()._set_appearance_mode(mode_string)
        if self._ready:
            self._apply_colors()

    def _set_scaling(self, new_widget_scaling: float, new_window_scaling: float) -> None:
        super()._set_scaling(new_widget_scaling, new_window_scaling)
        if self._ready:
            self._configure_view()

    def _configure_view(self, _: Any = None) -> None:
        if self._destroyed:
            return
        if self._layout_job is not None:
            self.after_cancel(self._layout_job)
        self._layout_job = self.after(80, self._layout)

    def _layout(self) -> None:
        if self._layout_job is not None:
            self.after_cancel(self._layout_job)
        self._layout_job = None
        if self._destroyed or not self.pages:
            return
        if self.canvas.winfo_width() <= 1:
            self._configure_view()
            return
        width = max(1, int(self.canvas.winfo_width() - self._padding() * 2))
        if width == self._page_width and self._tops:
            self._total_height = max(self._bottoms[-1] + self._padding(),
                                     self._tops[-1] + self.canvas.winfo_height() - self._padding())
            self.canvas.configure(scrollregion=(0, 0, self.canvas.winfo_width(), self._total_height))
            self._refresh()
            return
        anchor_page = max(0, self._current_page_index) if self._tops else min(self.page_count - 1, self._early_page - 1)
        offset = 0.0
        if self._tops:
            offset = (self.canvas.canvasy(0) - self._tops[anchor_page]) / max(1, self._bottoms[anchor_page] - self._tops[anchor_page])
        self._page_width = width
        self._generation += 1
        self._requests.clear()
        for image, _ in self._cache.values():
            image.close()
        self._cache.clear()
        self._photos.clear()
        self._cache_bytes = 0
        self.canvas.delete('page')
        self._items.clear()
        self._tops.clear()
        self._bottoms.clear()
        top = self._padding()
        for page in self.pages:
            self._tops.append(top)
            self._bottoms.append(top + width * page.height / page.width)
            top = self._bottoms[-1] + self._padding()
        # Allow even a short last page to align above the floating navigation.
        self._total_height = max(top, self._tops[-1] + self.canvas.winfo_height() - self._padding())
        self.canvas.configure(scrollregion=(0, 0, self.canvas.winfo_width(), self._total_height))
        y = self._tops[anchor_page] + offset * (self._bottoms[anchor_page] - self._tops[anchor_page])
        self.canvas.yview_moveto(max(0, y) / self._total_height)
        self._refresh()

    def _visible_pages(self) -> list[int]:
        if not self._tops:
            return []
        top = self.canvas.canvasy(0)
        bottom = top + self.canvas.winfo_height()
        first = min(self.page_count - 1, bisect_left(self._bottoms, top))
        last = min(self.page_count, bisect_right(self._tops, bottom))
        return list(range(first, max(first + 1, last)))

    def _refresh(self) -> None:
        if self._refresh_job is not None:
            self.after_cancel(self._refresh_job)
            self._refresh_job = None
        if self._destroyed or not self._tops:
            return
        visible = self._visible_pages()
        mid = self.canvas.canvasy(0) + self.canvas.winfo_height() / 2
        current = min(visible, key=lambda i: abs((self._tops[i] + self._bottoms[i]) / 2 - mid))
        self._set_current_page(current)
        if self._destroyed:
            return
        active = set(visible)
        if visible[0] > 0:
            active.add(visible[0] - 1)
        if visible[-1] + 1 < self.page_count:
            active.add(visible[-1] + 1)
        for index in set(self._items) - active:
            for item in self._items.pop(index):
                self.canvas.delete(item)
            self._photos.pop(index, None)
        x = self._padding()
        for index in sorted(active):
            if index not in self._items:
                top, bottom = self._tops[index], self._bottoms[index]
                rect = self.canvas.create_rectangle(x, top, x + self._page_width, bottom, fill='white', outline='', tags='page')
                img = self.canvas.create_image(x, top, anchor='nw', tags='page')
                text = self.canvas.create_text(x + self._page_width / 2, (top + bottom) / 2, tags='page', fill='gray40')
                self._items[index] = rect, img, text
            _, img, label = self._items[index]
            cached = self._cache.get(index)
            if cached is not None:
                self._cache.move_to_end(index)
                self._show_image(index, img, cached[0])
                self.canvas.itemconfigure(label, text='')
            else:
                label_text = self._('Could not render page {page}').format(page=index + 1) if index in self._errors else self._('Page {page}').format(page=index + 1)
                self.canvas.itemconfigure(img, image='')
                self.canvas.itemconfigure(label, text=label_text)
        priority = sorted(active, key=lambda i: (i not in visible, abs(i - current)))
        self._requests = {}
        # Bound native bitmap memory too, before rendering exceptionally large pages.
        per_page_pixels = max(1, min(16_000_000, self._memory_limit // (8 * max(1, len(active)))))
        for index in priority:
            if index in self._cache or index in self._errors:
                continue
            height = self._bottoms[index] - self._tops[index]
            factor = min(1.0, math.sqrt(per_page_pixels / max(1, self._page_width * height)))
            self._requests[index] = RenderRequest(index, self._generation,
                                                  max(1, round(self._page_width * factor)), max(1, round(height * factor)))
        self._renderer.request(list(self._requests.values()))
        self._trim_cache(active)
        self._draw_selection()

    def _show_image(self, index: int, item: int, image: Image.Image) -> None:
        height = max(1, round(self._bottoms[index] - self._tops[index]))
        # Tall engineering drawings must not allocate a page-sized Tk image.
        # Keep a moving strip, quantized to viewport heights to avoid uploads on every wheel tick.
        viewport = max(1, self.canvas.winfo_height())
        offset = max(0, self.canvas.canvasy(0) - self._tops[index])
        start = max(0, min(height - 1, (int(offset) // viewport - 1) * viewport))
        end = min(height, start + 3 * viewport)
        key = (id(image), start, end)
        existing = self._photos.get(index)
        if existing is None or existing[1] != key:
            crop = image.crop((0, start * image.height / height, image.width, end * image.height / height))
            resized = crop.resize((self._page_width, end-start), Image.Resampling.BILINEAR)
            crop.close()
            photo = ImageTk.PhotoImage(resized, master=self.canvas)
            resized.close()
            self._photos[index] = (photo, key)
        self.canvas.coords(item, self._padding(), self._tops[index] + start)
        self.canvas.itemconfigure(item, image=self._photos[index][0])

    def _trim_cache(self, active: set[int]) -> None:
        for index in list(self._cache):
            if len(self._cache) <= self._page_cache_size and self._cache_bytes <= self._memory_limit:
                break
            if index in active:
                continue
            image, cost = self._cache.pop(index)
            image.close()
            self._cache_bytes -= cost

    def _scroll(self, *args: Any) -> None:
        self.canvas.yview(*args)
        self._schedule_refresh()

    def _schedule_refresh(self) -> None:
        # Throttle, rather than debounce: continuous scrolling still renders.
        if self._refresh_job is None and not self._destroyed:
            self._refresh_job = self.after(16, self._refresh)

    def _wheel(self, event: Any, direction: int | None = None) -> str:
        if direction is None:
            amount = -event.delta if sys.platform == 'darwin' else -event.delta / 120
        else:
            amount = direction
        self._wheel_remainder += amount * (8 if sys.platform == 'darwin' else 48) * self._get_widget_scaling()
        pixels = int(self._wheel_remainder)
        self._wheel_remainder -= pixels
        self.canvas.yview_moveto((self.canvas.canvasy(0) + pixels) / self._total_height)
        self._schedule_refresh()
        return 'break'

    def _poll(self) -> None:
        self._poll_job = None
        if self._destroyed:
            return
        # At most two PhotoImage uploads per tick; never monopolize Tk's event loop.
        changed = False
        for _ in range(2):
            try:
                result = self._renderer.results.get_nowait()
            except queue.Empty:
                break
            if isinstance(result, DocumentResult):
                if result.error is not None:
                    self.canvas.itemconfigure(self._status, text=self._('Could not open PDF'))
                    if self._on_error:
                        self._call(self._on_error, None, result.error)
                    return
                self.pages = result.pages
                self.page_count = len(result.pages)
                self.canvas.delete(self._status)
                self._layout()
                self.go_to_page(self._early_page)
                if self._on_load:
                    self._call(self._on_load, self)
                if self._destroyed:
                    return
                continue
            request = result.request
            if self._requests.get(request.page_index) != request:
                if result.image is not None:
                    result.image.close()
                continue
            if result.error is not None:
                self._errors[request.page_index] = result.error
                if self._on_error:
                    self._call(self._on_error, request.page_index, result.error)
                    if self._destroyed:
                        return
            elif result.image is not None:
                cost = result.image.width * result.image.height * 4
                old = self._cache.pop(request.page_index, None)
                if old:
                    self._cache_bytes -= old[1]
                    old[0].close()
                self._cache[request.page_index] = (result.image, cost)
                self._cache_bytes += cost
            changed = True
        if changed and not self._destroyed:
            self._refresh()
        if not self._destroyed:
            self._poll_job = self.after(20, self._poll)

    def add_selection_change_callback(self, callback: Callable[[PageSelection | None], None]) -> None:
        if callback not in self._selection_callbacks:
            self._selection_callbacks.append(callback)

    def remove_selection_change_callback(self, callback: Callable[[PageSelection | None], None]) -> None:
        if callback in self._selection_callbacks:
            self._selection_callbacks.remove(callback)

    def get_selection(self) -> PageSelection | None:
        return self._selection

    def see_selection(self) -> None:
        """Bring the selected image into the viewport without changing placement."""
        box = self._selection_canvas_box()
        if box is not None:
            y = (box[1] + box[3] - self.canvas.winfo_height()) / 2
            self.canvas.yview_moveto(max(0, y) / self._total_height)
            self._refresh()

    def set_selection(self, selection: PageSelection, image: Image.Image | None = None) -> None:
        if not 0 <= selection.page_index < self.page_count:
            raise ValueError('Page index out of range')
        page = self.pages[selection.page_index]
        if not all(math.isfinite(v) for v in selection.box):
            raise ValueError('Invalid selection bounds')
        box = page.visual_box(selection.box)
        if box[2] <= box[0] or box[3] <= box[1]:
            raise ValueError('Selection must have positive area')
        if image is not None:
            if self._selection_image:
                self._selection_image.close()
            self._selection_image = image.copy()
            self._overlay_size = None
        self._set_visual_box(selection.page_index, box)

    def clear_selection(self) -> None:
        self._selection = None
        if self._selection_image:
            self._selection_image.close()
            self._selection_image = None
        self._overlay_photo = None
        self._overlay_size = None
        self.canvas.delete('selection')
        self._notify_selection()

    def _set_visual_box(self, index: int, box: Box) -> None:
        x, y, right, bottom = box
        width, height = right - x, bottom - y
        page = self.pages[index]
        if self._selection_image:
            height = width * page.width / page.height * self._selection_image.height / self._selection_image.width
        factor = min(1, 1 / width, 1 / height)
        width, height = width * factor, height * factor
        x, y = min(1 - width, max(0, x)), min(1 - height, max(0, y))
        self._selection = PageSelection(index, page.pdf_box((x, y, x + width, y + height)))
        self._draw_selection()
        self._notify_selection()

    def _notify_selection(self) -> None:
        for callback in tuple(self._selection_callbacks):
            self._call(callback, self._selection)

    def _selection_canvas_box(self) -> Box | None:
        if self._selection is None or not self._tops:
            return None
        index = self._selection.page_index
        x, y, right, bottom = self.pages[index].visual_box(self._selection.box)
        height = self._bottoms[index] - self._tops[index]
        return (self._padding() + x * self._page_width, self._tops[index] + y * height,
                self._padding() + right * self._page_width, self._tops[index] + bottom * height)

    def _draw_selection(self) -> None:
        self.canvas.delete('selection')
        box = self._selection_canvas_box()
        if box is None or self._selection is None or self._selection.page_index not in self._items:
            return
        x, y, right, bottom = box
        if self._selection_image:
            width, height = max(1, round(right - x)), max(1, round(bottom - y))
            viewport = max(1, self.canvas.winfo_height())
            offset = max(0, self.canvas.canvasy(0) - y)
            start = max(0, min(height - 1, (int(offset) // viewport - 1) * viewport))
            end = min(height, start + 3 * viewport)
            size = width, height, start, end
            if size != self._overlay_size:
                source = self._selection_image
                crop = source.crop((0, start * source.height / height, source.width, end * source.height / height))
                resized = crop.resize((width, end-start), Image.Resampling.LANCZOS)
                crop.close()
                self._overlay_photo = ImageTk.PhotoImage(resized, master=self.canvas)
                resized.close()
                self._overlay_size = size
            self.canvas.create_image(x, y + start, image=self._overlay_photo, anchor='nw', tags='selection')
        self.canvas.create_rectangle(*box, outline='#1689cc', width=2, dash=(4, 2), tags='selection')
        radius = max(4, round(4 * self._get_widget_scaling()))
        for cx, cy in ((x, y), (right, y), (right, bottom), (x, bottom)):
            self.canvas.create_rectangle(cx-radius, cy-radius, cx+radius, cy+radius,
                                         fill='white', outline='#1689cc', tags='selection')

    def _press(self, event: Any) -> str:
        self.canvas.focus_set()
        box = self._selection_canvas_box()
        if box is None or self._selection is None:
            return 'break'
        x, y = self.canvas.canvasx(event.x), self.canvas.canvasy(event.y)
        left, top, right, bottom = box
        radius = 10 * self._get_widget_scaling()
        for name, cx, cy in (('nw', left, top), ('ne', right, top), ('se', right, bottom), ('sw', left, bottom)):
            if abs(x-cx) <= radius and abs(y-cy) <= radius:
                self._drag = name, x, y, box
                return 'break'
        if left <= x <= right and top <= y <= bottom:
            self._drag = 'move', x, y, box
        return 'break'

    def _motion(self, event: Any) -> str:
        if self._drag is None or self._selection is None:
            return 'break'
        mode, start_x, start_y, original = self._drag
        x, y = self.canvas.canvasx(event.x), self.canvas.canvasy(event.y)
        left, top, right, bottom = original
        if mode == 'move':
            left, right = left+x-start_x, right+x-start_x
            top, bottom = top+y-start_y, bottom+y-start_y
        else:
            anchor_x = right if 'w' in mode else left
            anchor_y = bottom if 'n' in mode else top
            width = max(16, (anchor_x-x) if 'w' in mode else (x-anchor_x))
            height = width * (original[3]-original[1]) / (original[2]-original[0])
            left, right = (anchor_x-width, anchor_x) if 'w' in mode else (anchor_x, anchor_x+width)
            top, bottom = (anchor_y-height, anchor_y) if 'n' in mode else (anchor_y, anchor_y+height)
        index = self._selection.page_index
        height = self._bottoms[index] - self._tops[index]
        self._set_visual_box(index, ((left-self._padding()) / self._page_width,
                                   (top-self._tops[index]) / height,
                                   (right-self._padding()) / self._page_width,
                                   (bottom-self._tops[index]) / height))
        return 'break'

    def _release(self, _: Any) -> str:
        self._drag = None
        return 'break'

    def destroy(self) -> None:
        if self._destroyed:
            return
        self._destroyed = True
        for job in (self._layout_job, self._refresh_job, self._poll_job):
            if job is not None:
                self.after_cancel(job)
        self._renderer.close()
        for image, _ in self._cache.values():
            image.close()
        self._cache.clear()
        self._photos.clear()
        if self._selection_image:
            self._selection_image.close()
        self._overlay_photo = None
        super().destroy()
