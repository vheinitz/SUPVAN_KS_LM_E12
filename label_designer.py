#!/usr/bin/env python3
"""
Graphical label designer for the Katasymbol E12 (Supvan) label printer.

Compose serialized label templates (1D Code128 or 2D QR), preview them live,
and print a series directly to the printer over BLE.

Usage:
    python3 label_designer.py
    python3 label_designer.py --address A4:93:40:02:F3:F5
"""
import sys
import json
import asyncio
from pathlib import Path

from PyQt6 import QtWidgets, QtCore, QtGui
from PIL import Image, ImageDraw

from katasymbol_e12 import E12Ble, prepare_print
from barcode_label import render_barcode_label
from qr_label import render_qr_label

APP_NAME = "Katasymbol E12 — Label Designer"
STATE_FILE = Path.home() / ".katasymbol-e12-designer.json"


def _fmt(template: str, i: int) -> str:
    """Support both '{id}' and positional/plain templates."""
    try:
        return template.format(id=i)
    except (KeyError, IndexError, ValueError):
        return template.replace("{}", str(i)).replace("{i}", str(i))


def render_label(kind: str, text: str, label_w: int, label_l: int,
                 density: int, module_px: int, text_strip: int) -> Image.Image:
    if kind == "qr":
        return render_qr_label(text, label_w, label_l, text_strip=text_strip)
    return render_barcode_label(text, label_w, label_l,
                                module_px=module_px, text_strip=text_strip)


class DesignerWindow(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.resize(900, 620)
        self._build_ui()
        self._connect_signals()
        self._load_state()
        self._refresh_preview()

    # ---- UI scaffolding -------------------------------------------------
    def _build_ui(self):
        central = QtWidgets.QWidget()
        self.setCentralWidget(central)
        root = QtWidgets.QHBoxLayout(central)

        # Left: controls
        panel = QtWidgets.QWidget()
        panel.setFixedWidth(360)
        form = QtWidgets.QFormLayout(panel)

        self.addr = QtWidgets.QLineEdit("A4:93:40:02:F3:F5")
        form.addRow("BLE address", self.addr)
        self.btn_scan = QtWidgets.QPushButton("Scan…")
        form.addRow("", self.btn_scan)

        form.addRow(QtWidgets.QLabel("<b>Series</b>"))

        self.kind = QtWidgets.QComboBox()
        self.kind.addItem("1D barcode (Code128)", "barcode")
        self.kind.addItem("2D QR code", "qr")
        form.addRow("Symbology", self.kind)

        self.template = QtWidgets.QLineEdit("S{id:04d}")
        self.template.setToolTip("Python format template with {id}, e.g. S{id:04d} or BOX-{id}")
        form.addRow("Template", self.template)

        row = QtWidgets.QHBoxLayout()
        self.from_spin = QtWidgets.QSpinBox(); self.from_spin.setRange(0, 999999); self.from_spin.setValue(1)
        self.to_spin = QtWidgets.QSpinBox(); self.to_spin.setRange(0, 999999); self.to_spin.setValue(10)
        row.addWidget(QtWidgets.QLabel("from")); row.addWidget(self.from_spin)
        row.addWidget(QtWidgets.QLabel("to")); row.addWidget(self.to_spin)
        form.addRow("Range", row)

        form.addRow(QtWidgets.QLabel("<b>Label</b>"))
        self.label_w = QtWidgets.QSpinBox(); self.label_w.setRange(1, 100); self.label_w.setValue(12)
        self.label_l = QtWidgets.QSpinBox(); self.label_l.setRange(1, 200); self.label_l.setValue(40)
        row2 = QtWidgets.QHBoxLayout()
        row2.addWidget(QtWidgets.QLabel("width mm")); row2.addWidget(self.label_w)
        row2.addWidget(QtWidgets.QLabel("length mm")); row2.addWidget(self.label_l)
        form.addRow("Size", row2)

        self.density = QtWidgets.QSpinBox(); self.density.setRange(1, 5); self.density.setValue(4)
        form.addRow("Density (1-5)", self.density)

        self.module_px = QtWidgets.QSpinBox(); self.module_px.setRange(2, 6); self.module_px.setValue(3)
        form.addRow("Barcode module px", self.module_px)

        self.text_strip = QtWidgets.QSpinBox(); self.text_strip.setRange(10, 60); self.text_strip.setValue(20)
        form.addRow("Text strip px", self.text_strip)

        form.addRow(QtWidgets.QLabel("<b>Preview</b>"))
        self.preview_value = QtWidgets.QSpinBox(); self.preview_value.setRange(0, 999999); self.preview_value.setValue(1)
        row3 = QtWidgets.QHBoxLayout()
        row3.addWidget(QtWidgets.QLabel("sample id")); row3.addWidget(self.preview_value)
        form.addRow("", row3)

        root.addWidget(panel)

        # Right: preview + actions
        right = QtWidgets.QWidget()
        rlayout = QtWidgets.QVBoxLayout(right)

        self.preview = QtWidgets.QLabel()
        self.preview.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self.preview.setMinimumSize(420, 420)
        self.preview.setStyleSheet("background:#eee; border:1px solid #999;")
        rlayout.addWidget(self.preview, stretch=1)

        self.status = QtWidgets.QLabel("Ready")
        rlayout.addWidget(self.status)

        actrow = QtWidgets.QHBoxLayout()
        self.btn_preview_png = QtWidgets.QPushButton("Save preview PNG")
        self.btn_print = QtWidgets.QPushButton("Print series")
        self.btn_print.setStyleSheet("font-weight:bold;")
        self.btn_save_tpl = QtWidgets.QPushButton("Save template")
        self.btn_load_tpl = QtWidgets.QPushButton("Load template")
        actrow.addWidget(self.btn_preview_png)
        actrow.addWidget(self.btn_save_tpl)
        actrow.addWidget(self.btn_load_tpl)
        rlayout.addLayout(actrow)
        rlayout.addWidget(self.btn_print)

        root.addWidget(right, stretch=1)

    def _connect_signals(self):
        for w in (self.kind, self.template, self.from_spin, self.to_spin,
                  self.label_w, self.label_l, self.density, self.module_px,
                  self.text_strip, self.preview_value):
            if isinstance(w, QtWidgets.QSpinBox):
                w.valueChanged.connect(self._refresh_preview)
            elif isinstance(w, QtWidgets.QComboBox):
                w.currentIndexChanged.connect(self._refresh_preview)
            elif isinstance(w, QtWidgets.QLineEdit):
                w.textChanged.connect(self._refresh_preview)
        self.btn_scan.clicked.connect(self._scan)
        self.btn_preview_png.clicked.connect(self._save_preview_png)
        self.btn_print.clicked.connect(self._print_series)
        self.btn_save_tpl.clicked.connect(self._save_template)
        self.btn_load_tpl.clicked.connect(self._load_template)

    # ---- Helpers --------------------------------------------------------
    def _current_text(self) -> str:
        return _fmt(self.template.text(), self.preview_value.value())

    def _current_image(self) -> Image.Image | None:
        try:
            return render_label(
                self.kind.currentData(), self._current_text(),
                self.label_w.value(), self.label_l.value(),
                self.density.value(), self.module_px.value(),
                self.text_strip.value())
        except Exception as e:
            self.status.setText(f"Preview error: {e}")
            return None

    def _refresh_preview(self):
        img = self._current_image()
        if img is None:
            return
        # Scale up for on-screen visibility (nearest for crisp bars).
        scale = 2
        big = img.resize((img.width * scale, img.height * scale),
                         Image.Resampling.NEAREST)
        qim = big.toqimage() if hasattr(big, "toqimage") else None
        if qim is None:
            from PIL.ImageQt import ImageQt
            qim = ImageQt(big)
        pm = QtGui.QPixmap.fromImage(qim)
        self.preview.setPixmap(pm)
        self.status.setText(
            f"{self.kind.currentData()}: {self._current_text()}  "
            f"({img.width}×{img.height}px, {self.label_w.value()}×{self.label_l.value()}mm)")

    # ---- Actions --------------------------------------------------------
    def _scan(self):
        # Run the ble scan subprocess-free via a thread to avoid blocking.
        import subprocess
        self.status.setText("Scanning for BLE printers…")
        QtWidgets.QApplication.processEvents()
        try:
            out = subprocess.run(
                [sys.executable, "katasymbol_e12.py", "scan"],
                capture_output=True, text=True, timeout=20).stdout
            for line in out.splitlines():
                if "*" in line:
                    addr = line.split()[1]
                    self.addr.setText(addr)
                    self.status.setText(f"Found: {addr}")
                    return
            self.status.setText("No Supvan printer found (is it awake?)")
        except Exception as e:
            self.status.setText(f"Scan failed: {e}")

    def _save_preview_png(self):
        img = self._current_image()
        if img is None:
            return
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "Save preview", f"label_{self._current_text()}.png", "PNG (*.png)")
        if path:
            img.save(path)
            self.status.setText(f"Saved {path}")

    def _collect_state(self) -> dict:
        return {
            "address": self.addr.text(),
            "kind": self.kind.currentData(),
            "template": self.template.text(),
            "from": self.from_spin.value(),
            "to": self.to_spin.value(),
            "label_w": self.label_w.value(),
            "label_l": self.label_l.value(),
            "density": self.density.value(),
            "module_px": self.module_px.value(),
            "text_strip": self.text_strip.value(),
        }

    def _apply_state(self, s: dict):
        self.addr.setText(s.get("address", self.addr.text()))
        self.kind.setCurrentIndex(0 if s.get("kind") == "barcode" else 1)
        self.template.setText(s.get("template", self.template.text()))
        self.from_spin.setValue(s.get("from", 1))
        self.to_spin.setValue(s.get("to", 10))
        self.label_w.setValue(s.get("label_w", 12))
        self.label_l.setValue(s.get("label_l", 40))
        self.density.setValue(s.get("density", 4))
        self.module_px.setValue(s.get("module_px", 3))
        self.text_strip.setValue(s.get("text_strip", 20))

    def _save_template(self):
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "Save template", "template.json", "JSON (*.json)")
        if path:
            Path(path).write_text(json.dumps(self._collect_state(), indent=2))
            self.status.setText(f"Saved template {path}")

    def _load_template(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "Load template", "", "JSON (*.json)")
        if path:
            self._apply_state(json.loads(Path(path).read_text()))
            self._refresh_preview()
            self.status.setText(f"Loaded {path}")

    def _save_state(self):
        STATE_FILE.write_text(json.dumps(self._collect_state(), indent=2))

    def _load_state(self):
        if STATE_FILE.exists():
            try:
                self._apply_state(json.loads(STATE_FILE.read_text()))
            except Exception:
                pass

    def closeEvent(self, ev):
        self._save_state()
        super().closeEvent(ev)

    # ---- Printing -------------------------------------------------------
    def _print_series(self):
        start, end = self.from_spin.value(), self.to_spin.value()
        if end < start:
            QtWidgets.QMessageBox.warning(self, "Range", "from > to")
            return
        addr = self.addr.text().strip()
        kind = self.kind.currentData()
        tpl = self.template.text()
        label_w, label_l = self.label_w.value(), self.label_l.value()
        density = self.density.value()
        module_px, text_strip = self.module_px.value(), self.text_strip.value()

        self.btn_print.setEnabled(False)
        self.status.setText("Printing…")

        async def run():
            async with E12Ble(addr) as printer:
                for i in range(start, end + 1):
                    code = _fmt(tpl, i)
                    img = render_label(kind, code, label_w, label_l,
                                       density, module_px, text_strip)
                    compressed, speed = prepare_print(img, density)
                    await printer.print_compressed(
                        compressed, speed, poll_completion=False,
                        ignore_ribbon_end=True, progress=lambda m: None)
                    self.status.setText(f"Printed {i}: {code}")
                    QtWidgets.QApplication.processEvents()

        def on_done(fut):
            self.btn_print.setEnabled(True)
            try:
                fut.result()
                self.status.setText(f"Series done ({start}…{end})")
            except Exception as e:
                self.status.setText(f"Error: {e}")
            QtWidgets.QMessageBox.information(self, "Print", self.status.text())

        # Run the async loop in a background thread to keep the UI responsive.
        import threading
        def _thread():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            loop.run_until_complete(run())
        t = threading.Thread(target=_thread, daemon=True)
        t.start()
        # Poll for completion (simple; the status label updates from the thread).
        def _poll():
            if t.is_alive():
                QtCore.QTimer.singleShot(200, _poll)
            else:
                self.btn_print.setEnabled(True)
        QtCore.QTimer.singleShot(200, _poll)


def main():
    app = QtWidgets.QApplication(sys.argv)
    win = DesignerWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
