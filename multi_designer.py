#!/usr/bin/env python3
"""
Multi-object label designer (markup + live preview + mouse) for the
Katasymbol E12.

Combine text, lines, rectangles, bitmap images, 1D barcodes and 2D QR codes
into a serialized label template. Edit the markup on the left, see a live
preview on the right, drag objects with the mouse, and print the series.

Markup grammar (one object per line, `#` for comments):

    text   "S{id:04d}" x=8  y=280 size=20 bold
    line   x1=0 y1=10 x2=96 y2=10 width=2
    rect   x=2 y=2 w=20 h=20 width=1           # or fill=1
    image  path=/tmp/logo.png x=4 y=4 w=40 mode=fit
    barcode data="S{id:04d}" x=2 y=20 module_px=3 height=70
    qrcode data="SAMPLE-2024-001" x=8 y=30 module_px=3

The bare first word is the primary field (text/data). All positions are in
printer pixels (96 = 12 mm wide, 320 = 40 mm long). {id} is the series number.

Mouse: click a rendered object to select it; drag to move its x/y (or x1/y1
for lines). The markup updates to match.
"""
import sys
import json
import asyncio
import threading
from pathlib import Path

from PyQt6 import QtWidgets, QtCore, QtGui
from PIL import Image

from katasymbol_e12 import E12Ble, prepare_print
from label_template import (
    parse_markup, render_template, substitute, RENDERERS,
)

APP_NAME = "Katasymbol E12 — Multi-object Designer"
CONFIG = Path.home() / ".katasymbol-designer.json"

EXAMPLE = """# Sample-tube label: text on top, barcode below
text "S{id:04d}" x=6 y=4 size=18 bold
barcode data="S{id:04d}" x=2 y=40 module_px=3 height=88
line x1=0 y1=36 x2=96 y2=36 width=1
"""


class PreviewWidget(QtWidgets.QLabel):
    """Rendered preview with mouse drag-to-move support."""
    object_moved = QtCore.pyqtSignal(int, str, int)  # index, axis key, delta px

    def __init__(self):
        super().__init__()
        self.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self.setMinimumSize(440, 440)
        self.setStyleSheet("background:#f0f0f0; border:1px solid #999;")
        self._scale = 2
        self._img_size = (96, 320)
        self._canvas_rect = QtCore.QRect()
        self._drag_index = None
        self._drag_axis = None
        self._drag_last = None

    def set_preview(self, pil_img: Image.Image):
        self._img_size = pil_img.size
        big = pil_img.resize((pil_img.width * self._scale,
                              pil_img.height * self._scale),
                             Image.Resampling.NEAREST)
        from PIL.ImageQt import ImageQt
        qim = ImageQt(big.convert("RGBA"))
        self.setPixmap(QtGui.QPixmap.fromImage(qim))
        # compute canvas rect on the label widget for hit-testing
        cw = pil_img.width * self._scale
        ch = pil_img.height * self._scale
        ox = (self.width() - cw) // 2
        oy = (self.height() - ch) // 2
        self._canvas_rect = QtCore.QRect(ox, oy, cw, ch)

    def _to_px(self, pos) -> tuple[int, int]:
        r = self._canvas_rect
        if not r.width() or not r.height():
            return (-1, -1)
        x = (pos.x() - r.x()) // self._scale
        y = (pos.y() - r.y()) // self._scale
        return (x, y)

    def mousePressEvent(self, ev):
        if ev.button() == QtCore.Qt.MouseButton.LeftButton:
            x, y = self._to_px(ev.pos())
            idx = self.hit_test(x, y)
            if idx >= 0:
                self._drag_index = idx
                self._drag_last = (x, y)
        super().mousePressEvent(ev)

    def mouseMoveEvent(self, ev):
        if self._drag_index is not None:
            x, y = self._to_px(ev.pos())
            if self._drag_last:
                dx = x - self._drag_last[0]
                dy = y - self._drag_last[1]
                if dx or dy:
                    self.object_moved.emit(self._drag_index, "x", dx)
                    self.object_moved.emit(self._drag_index, "y", dy)
                self._drag_last = (x, y)
        super().mouseMoveEvent(ev)

    def mouseReleaseEvent(self, ev):
        self._drag_index = None
        self._drag_last = None
        super().mouseReleaseEvent(ev)

    def hit_test(self, px, py) -> int:
        # Overridden by the window, which knows the object bounding boxes.
        return getattr(self, "_hit_test_fn", lambda x, y: -1)(px, py)


class DesignerWindow(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.resize(1000, 680)
        self._objects = []
        self._template_objects = []
        self._boxes = []       # per-object bounding boxes in canvas px
        self._build_ui()
        self._hook_preview_hit_test()
        self._connect_signals()
        self._load_config()
        self.refresh_preview()

    # ---- UI ----
    def _build_ui(self):
        central = QtWidgets.QWidget()
        self.setCentralWidget(central)
        root = QtWidgets.QHBoxLayout(central)

        # left: address + series + markup
        left = QtWidgets.QWidget(); left.setFixedWidth(430)
        lform = QtWidgets.QVBoxLayout(left)

        addr_row = QtWidgets.QHBoxLayout()
        self.addr = QtWidgets.QLineEdit("A4:93:40:02:F3:F5")
        self.btn_scan = QtWidgets.QPushButton("Scan")
        addr_row.addWidget(QtWidgets.QLabel("BLE")); addr_row.addWidget(self.addr); addr_row.addWidget(self.btn_scan)
        lform.addLayout(addr_row)

        series_row = QtWidgets.QHBoxLayout()
        self.from_spin = QtWidgets.QSpinBox(); self.from_spin.setRange(0, 999999); self.from_spin.setValue(1)
        self.to_spin = QtWidgets.QSpinBox(); self.to_spin.setRange(0, 999999); self.to_spin.setValue(10)
        self.sample_spin = QtWidgets.QSpinBox(); self.sample_spin.setRange(0, 999999); self.sample_spin.setValue(1)
        series_row.addWidget(QtWidgets.QLabel("from")); series_row.addWidget(self.from_spin)
        series_row.addWidget(QtWidgets.QLabel("to")); series_row.addWidget(self.to_spin)
        series_row.addWidget(QtWidgets.QLabel("preview id")); series_row.addWidget(self.sample_spin)
        lform.addLayout(series_row)

        lform.addWidget(QtWidgets.QLabel("Template (one object per line):"))
        self.markup = QtWidgets.QPlainTextEdit()
        self.markup.setPlainText(EXAMPLE)
        self.markup.setFont(QtGui.QFont("Monospace", 10))
        lform.addWidget(self.markup, stretch=1)

        self.lbl_error = QtWidgets.QLabel("")
        self.lbl_error.setWordWrap(True)
        self.lbl_error.setStyleSheet("color:#a00;")
        lform.addWidget(self.lbl_error)

        btn_row = QtWidgets.QHBoxLayout()
        self.btn_add_text = QtWidgets.QPushButton("+Text")
        self.btn_add_bc = QtWidgets.QPushButton("+Barcode")
        self.btn_add_qr = QtWidgets.QPushButton("+QR")
        self.btn_add_line = QtWidgets.QPushButton("+Line")
        self.btn_add_rect = QtWidgets.QPushButton("+Rect")
        for b in (self.btn_add_text, self.btn_add_bc, self.btn_add_qr,
                  self.btn_add_line, self.btn_add_rect):
            btn_row.addWidget(b)
        lform.addLayout(btn_row)

        row2 = QtWidgets.QHBoxLayout()
        self.btn_save = QtWidgets.QPushButton("Save template")
        self.btn_load = QtWidgets.QPushButton("Load template")
        row2.addWidget(self.btn_save); row2.addWidget(self.btn_load)
        lform.addLayout(row2)

        root.addWidget(left)

        # right: preview + actions
        right = QtWidgets.QWidget()
        rlayout = QtWidgets.QVBoxLayout(right)
        self.preview = PreviewWidget()
        rlayout.addWidget(self.preview, stretch=1)

        self.density = QtWidgets.QSpinBox(); self.density.setRange(1,5); self.density.setValue(4)
        drow = QtWidgets.QHBoxLayout()
        drow.addWidget(QtWidgets.QLabel("Density")); drow.addWidget(self.density)
        drow.addStretch()
        self.status = QtWidgets.QLabel("Ready")
        drow.addWidget(self.status)
        rlayout.addLayout(drow)

        self.btn_preview_png = QtWidgets.QPushButton("Save preview PNG")
        self.btn_print = QtWidgets.QPushButton("Print series")
        self.btn_print.setStyleSheet("font-weight:bold; padding:6px;")
        rlayout.addWidget(self.btn_preview_png)
        rlayout.addWidget(self.btn_print)
        root.addWidget(right, stretch=1)

    def _hook_preview_hit_test(self):
        self.preview._hit_test_fn = self._hit_test

    def _connect_signals(self):
        self.markup.textChanged.connect(self.refresh_preview)
        self.sample_spin.valueChanged.connect(self.refresh_preview)
        self.preview.object_moved.connect(self._on_object_moved)
        self.btn_scan.clicked.connect(self._scan)
        self.btn_print.clicked.connect(self._print_series)
        self.btn_preview_png.clicked.connect(self._save_preview)
        self.btn_save.clicked.connect(self._save_template)
        self.btn_load.clicked.connect(self._load_template)
        for btn, kind in ((self.btn_add_text, "text"),
                          (self.btn_add_bc, "barcode"),
                          (self.btn_add_qr, "qrcode"),
                          (self.btn_add_line, "line"),
                          (self.btn_add_rect, "rect")):
            btn.clicked.connect(lambda _, k=kind: self._add_object(k))

    # ---- template / rendering ----
    def _current_id(self):
        return self.sample_spin.value()

    def refresh_preview(self):
        try:
            self._template_objects = parse_markup(self.markup.toPlainText())
            self._objects = [substitute(o, self._current_id())
                             for o in self._template_objects]
            img = render_template(self._objects)
            self.preview.set_preview(img)
            self._compute_boxes()
            self.lbl_error.setText("")
            self.status.setText(
                f"{len(self._objects)} object(s), preview id {self._current_id()}")
        except Exception as e:
            self.lbl_error.setText(f"Error: {e}")

    def _compute_boxes(self):
        """Estimate bounding boxes for each object (for hit-testing)."""
        boxes = []
        for o in self._objects:
            k = o.get("type")
            if k == "text":
                boxes.append((o.get("x", 0), o.get("y", 0),
                              o.get("x", 0) + max(10, len(str(o.get('text','')))*o.get('size',16)),
                              o.get("y", 0) + o.get('size', 16)))
            elif k in ("barcode", "qrcode"):
                h = o.get("height", 60) if k == "barcode" else o.get("module_px",3)*30
                boxes.append((o.get("x",0), o.get("y",0), o.get("x",0)+h, o.get("y",0)+h))
            elif k == "line":
                boxes.append((o.get("x1",0), o.get("y1",0), o.get("x2",96), o.get("y2",0)))
            elif k == "rect":
                boxes.append((o.get("x",0), o.get("y",0), o.get("x",0)+o.get("w",10), o.get("y",0)+o.get("h",10)))
            elif k == "image":
                boxes.append((o.get("x",0), o.get("y",0), o.get("x",0)+(o.get("w",40) or 40), o.get("y",0)+(o.get("h",40) or 40)))
            else:
                boxes.append((0,0,10,10))
        self._boxes = boxes

    def _hit_test(self, px, py):
        # return topmost object whose box contains (px, py)
        for i in range(len(self._boxes) - 1, -1, -1):
            x0, y0, x1, y1 = self._boxes[i]
            x0, x1 = min(x0, x1), max(x0, x1)
            y0, y1 = min(y0, y1), max(y0, y1)
            if x0 <= px <= x1 and y0 <= py <= y1:
                return i
        return -1

    def _on_object_moved(self, idx, axis, delta):
        # Update the TEMPLATE objects (un-substituted) so {id} is preserved.
        if not (0 <= idx < len(self._template_objects)):
            return
        o = self._template_objects[idx]
        k = o.get("type")
        if k == "line":
            key = {"x": "x1", "y": "y1"}.get(axis, "x1")
        else:
            key = {"x": "x", "y": "y"}.get(axis, "x")
        o[key] = max(0, o.get(key, 0) + delta)
        if k == "line" and axis == "x":
            o["x2"] = o.get("x2", 96) + delta
        elif k == "line" and axis == "y":
            o["y2"] = o.get("y2", 0) + delta
        self._write_markup_from_template()

    def _write_markup_from_template(self):
        lines = []
        for o in self._template_objects:
            k = o.get("type")
            parts = []
            primary = "data" if k in ("barcode", "qrcode") else "text"
            prim_val = o.get(primary)
            if prim_val is not None:
                if k in ("barcode", "qrcode"):
                    parts.append(f"{k} data={_q(prim_val)}")
                else:
                    parts.append(f'{k} {_q(prim_val)}')
            else:
                parts.append(k)
            for key, val in o.items():
                if key in ("type", primary):
                    continue
                parts.append(f"{key}={_q(val)}")
            lines.append(" ".join(parts))
        self.markup.blockSignals(True)
        self.markup.setPlainText("\n".join(lines))
        self.markup.blockSignals(False)
        self.refresh_preview()

    def _add_object(self, kind):
        cur = self.markup.toPlainText().rstrip("\n")
        defaults = {
            "text": 'text "TEXT" x=20 y=150 size=20',
            "barcode": 'barcode data="S{id:04d}" x=2 y=40 module_px=3 height=80',
            "qrcode": 'qrcode data="SAMPLE" x=20 y=20 module_px=3',
            "line": "line x1=0 y1=30 x2=96 y2=30 width=1",
            "rect": "rect x=10 y=10 w=30 h=20 width=1",
        }
        self.markup.setPlainText((cur + "\n" if cur else "") + defaults[kind])
        self.refresh_preview()

    # ---- actions ----
    def _scan(self):
        import subprocess
        self.status.setText("Scanning…"); QtWidgets.QApplication.processEvents()
        try:
            out = subprocess.run([sys.executable, "katasymbol_e12.py", "scan"],
                                 capture_output=True, text=True, timeout=20).stdout
            for line in out.splitlines():
                if "*" in line:
                    self.addr.setText(line.split()[1])
                    self.status.setText(f"Found {line.split()[1]}")
                    return
            self.status.setText("No Supvan printer found (is it awake?)")
        except Exception as e:
            self.status.setText(f"Scan failed: {e}")

    def _save_preview(self):
        img = render_template(self._objects)
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "Save preview", "label.png", "PNG (*.png)")
        if path:
            img.save(path); self.status.setText(f"Saved {path}")

    def _collect(self):
        return {"address": self.addr.text(), "markup": self.markup.toPlainText(),
                "from": self.from_spin.value(), "to": self.to_spin.value(),
                "density": self.density.value()}

    def _save_template(self):
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "Save template", "template.json", "JSON (*.json)")
        if path:
            Path(path).write_text(json.dumps(self._collect(), indent=2))
            self.status.setText(f"Saved {path}")

    def _load_template(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "Load template", "", "JSON (*.json)")
        if path:
            s = json.loads(Path(path).read_text())
            self.addr.setText(s.get("address", self.addr.text()))
            self.markup.setPlainText(s.get("markup", EXAMPLE))
            self.from_spin.setValue(s.get("from", 1))
            self.to_spin.setValue(s.get("to", 10))
            self.density.setValue(s.get("density", 4))
            self.refresh_preview(); self.status.setText(f"Loaded {path}")

    def _save_config(self):
        CONFIG.write_text(json.dumps(self._collect(), indent=2))

    def _load_config(self):
        if CONFIG.exists():
            try:
                s = json.loads(CONFIG.read_text())
                self.addr.setText(s.get("address", self.addr.text()))
                self.markup.setPlainText(s.get("markup", EXAMPLE))
                self.from_spin.setValue(s.get("from", 1))
                self.to_spin.setValue(s.get("to", 10))
                self.density.setValue(s.get("density", 4))
            except Exception:
                pass

    def closeEvent(self, ev):
        self._save_config(); super().closeEvent(ev)

    def _print_series(self):
        start, end = self.from_spin.value(), self.to_spin.value()
        if end < start:
            QtWidgets.QMessageBox.warning(self, "Range", "from > to"); return
        addr = self.addr.text().strip()
        objs = parse_markup(self.markup.toPlainText())
        density = self.density.value()
        self.btn_print.setEnabled(False)
        self.status.setText("Printing…")

        def run():
            async def _run():
                async with E12Ble(addr) as printer:
                    for i in range(start, end + 1):
                        o = [substitute(x, i) for x in objs]
                        img = render_template(o)
                        compressed, speed = prepare_print(img, density)
                        await printer.print_compressed(
                            compressed, speed, poll_completion=False,
                            ignore_ribbon_end=True, progress=lambda m: None)
                        self.status.setText(f"Printed {i}")
                        QtWidgets.QApplication.processEvents()
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                loop.run_until_complete(_run())
                self.status.setText(f"Series done ({start}…{end})")
            except Exception as e:
                self.status.setText(f"Error: {e}")

        t = threading.Thread(target=run, daemon=True); t.start()
        def _poll():
            if t.is_alive():
                QtCore.QTimer.singleShot(200, _poll)
            else:
                self.btn_print.setEnabled(True)
        QtCore.QTimer.singleShot(200, _poll)


def _q(v):
    """Quote a value for markup output."""
    if isinstance(v, str):
        return f'"{v}"'
    if isinstance(v, bool):
        return "1" if v else "0"
    return str(v)


def main():
    app = QtWidgets.QApplication(sys.argv)
    win = DesignerWindow(); win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
