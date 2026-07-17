import logging
import os
import queue

from PIL import Image

logger = logging.getLogger(__name__)

TEST_MODE = os.environ.get('TEST_MODE', '').lower() in ('1', 'true')

# TEST_MODE only: worker threads push (Image, title) here; the Tk main loop
# drains it and opens a preview window. Tk objects must only be touched from
# the main thread, hence the queue instead of building windows in-place.
_test_ui_queue: queue.Queue = queue.Queue()


def show_test_window(img: Image.Image, title: str) -> None:
    _test_ui_queue.put((img, title))


def poll(root) -> None:
    import tkinter as tk
    from PIL import ImageTk

    while True:
        try:
            img, title = _test_ui_queue.get_nowait()
        except queue.Empty:
            break
        try:
            win = tk.Toplevel(root)
            win.title(title)
            n_open = len(win.master.children) - 1
            win.geometry(f"+{100 + n_open * 30}+{100 + n_open * 30}")
            photo = ImageTk.PhotoImage(img)
            label = tk.Label(win, image=photo)
            label.image = photo  # keep a reference, else Tk garbage-collects it
            label.pack()
        except Exception as e:
            logger.error("Failed to show test preview window: %s", e)
    root.after(200, poll, root)
