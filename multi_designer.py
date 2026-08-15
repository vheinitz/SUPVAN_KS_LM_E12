#!/usr/bin/env python3
"""
Multi-object label designer with a tree-view of objects, a properties panel,
mouse editing, and bitmap base64 embedding.

Objects: text, line, rect, image (embedded base64 or path), barcode (1D),
qrcode (2D). All objects support rotation. {id} is the series number.

Usage:
    python3 multi_designer.py
"""
import sys
import json
import base64
import asyncio
import threading
from pathlib import Path

from PyQt6 import QtWidgets, QtCore, QtGui
from PIL import Image

from katasymbol_e12 import E12Ble, prepare_print
from label_template import (
    parse_markup, render_template, substitute, RENDERERS,
)

CONFIG = Path.home() / ".katasymbol-designer.json"

# Object-type metadata for UI: (label, fields with defaults).
OBJ_SPECS = {
    "text":    {"label": "Text",    "fields": [("text", "str"), ("x", "int"), ("y", "int"),
                                               ("size", "int"), ("bold", "bool"), ("rotation", "int")]},
    "line":    {"label": "Line",    "fields": [("x1", "int"), ("y1", "int"), ("x2", "int"),
                                               ("y2", "int"), ("width", "int")]},
    "rect":    {"label": "Rect",    "fields": [("x", "int"), ("y", "int"), ("w", "int"),
                                               ("h", "int"), ("width", "int"), ("fill", "bool")]},
    "image":   {"label": "Image",   "fields": [("data", "b64"), ("path", "path"), ("x", "int"),
                                               ("y", "int"), ("w", "int"), ("h", "int"),
                                               ("mode", "str"), ("rotation", "int")]},
    "barcode": {"label": "Barcode", "fields": [("data", "str"), ("x", "int"), ("y", "int"),
                                               ("module_px", "int"), ("height", "int"),
                                               ("text", "str"), ("rotation", "int")]},
    "qrcode":  {"label": "QR code", "fields": [("data", "str"), ("x", "int"), ("y", "int"),
                                               ("module_px", "int"), ("border", "int"),
                                               ("rotation", "int")]},
}

EXAMPLE = """text "S{id:04d}" x=6 y=4 size=18 bold
line x1=0 y1=36 x2=96 y2=36 width=1
barcode data="S{id:04d}" x=2 y=40 module_px=3 height=88
"""


class PreviewWidget(QtWidgets.QLabel):
    object_moved = QtCore.pyqtSignal(int, str, int)   # index, axis, delta px

    def __init__(self):
        super().__init__()
        self.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self.setMinimumSize(440, 440)
        self.setStyleSheet("background:#f0f0f0; border:1px solid #999;")
        self._scale = 2
        self._canvas_rect = QtCore.QRect()
        self._drag_index = None
        self._drag_last = None
        self._hit_fn = lambda x, y: -1

    def set_preview(self, pil_img: Image.Image):
        big = pil_img.resize((pil_img.width * self._scale, pil_img.height * self._scale),
                             Image.Resampling.NEAREST)
        from PIL.ImageQt import ImageQt
        self.setPixmap(QtGui.QPixmap.fromImage(ImageQt(big.convert("RGBA"))))
        cw, ch = pil_img.width * self._scale, pil_img.height * self._scale
        self._canvas_rect = QtCore.QRect((self.width() - cw) // 2,
                                         (self.height() - ch) // 2, cw, ch)

    def _to_px(self, pos):
        r = self._canvas_rect
        return ((pos.x() - r.x()) // self._scale, (pos.y() - r.y()) // self._scale)

    def mousePressEvent(self, ev):
        if ev.button() == QtCore.Qt.MouseButton.LeftButton:
            x, y = self._to_px(ev.pos())
            idx = self._hit_fn(x, y)
            if idx >= 0:
                self._drag_index = idx
                self._drag_last = (x, y)
        super().mousePressEvent(ev)

    def mouseMoveEvent(self, ev):
        if self._drag_index is not None and self._drag_last:
            x, y = self._to_px(ev.pos())
            dx, dy = x - self._drag_last[0], y - self._drag_last[1]
            if dx or dy:
                self.object_moved.emit(self._drag_index, "x", dx)
                self.object_moved.emit(self._drag_index, "y", dy)
            self._drag_last = (x, y)
        super().mouseMoveEvent(ev)

    def mouseReleaseEvent(self, ev):
        self._drag_index = None; self._drag_last = None
        super().mouseReleaseEvent(ev)


class DesignerWindow(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Katasymbol E12 — Multi-object Designer (tree + properties)")
        self.resize(1200, 720)
        self._template_objects = []
        self._boxes = []
        self._build_ui()
        self._connect_signals()
        self._load_config()
        self._set_example()
        self.refresh_all()

    # ---- UI ----
    def _build_ui(self):
        central = QtWidgets.QWidget(); self.setCentralWidget(central)
        root = QtWidgets.QHBoxLayout(central)

        # Left: address, series, tree, add-buttons
        left = QtWidgets.QWidget(); left.setFixedWidth(320)
        lv = QtWidgets.QVBoxLayout(left)

        ar = QtWidgets.QHBoxLayout()
        self.addr = QtWidgets.QLineEdit("A4:93:40:02:F3:F5")
        self.btn_scan = QtWidgets.QPushButton("Scan")
        ar.addWidget(QtWidgets.QLabel("BLE")); ar.addWidget(self.addr); ar.addWidget(self.btn_scan)
        lv.addLayout(ar)

        sr = QtWidgets.QHBoxLayout()
        self.from_spin = QtWidgets.QSpinBox(); self.from_spin.setRange(0, 999999); self.from_spin.setValue(1)
        self.to_spin = QtWidgets.QSpinBox(); self.to_spin.setRange(0, 999999); self.to_spin.setValue(10)
        self.sample_spin = QtWidgets.QSpinBox(); self.sample_spin.setRange(0, 999999); self.sample_spin.setValue(1)
        sr.addWidget(QtWidgets.QLabel("from")); sr.addWidget(self.from_spin)
        sr.addWidget(QtWidgets.QLabel("to")); sr.addWidget(self.to_spin)
        sr.addWidget(QtWidgets.QLabel("prev")); sr.addWidget(self.sample_spin)
        lv.addLayout(sr)

        lv.addWidget(QtWidgets.QLabel("<b>Objects</b>"))
        self.tree = QtWidgets.QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.SingleSelection)
        self.tree.setDragDropMode(QtWidgets.QAbstractItemView.DragDropMode.InternalMove)
        lv.addWidget(self.tree, stretch=1)

        addrow = QtWidgets.QGridLayout()
        self.btn_add = {}
        for i, (key, spec) in enumerate(OBJ_SPECS.items()):
            b = QtWidgets.QPushButton(spec["label"])
            self.btn_add[key] = b
            addrow.addWidget(b, i // 2, i % 2)
        lv.addLayout(addrow)

        rmrow = QtWidgets.QHBoxLayout()
        self.btn_del = QtWidgets.QPushButton("Delete selected")
        self.btn_up = QtWidgets.QPushButton("▲")
        self.btn_down = QtWidgets.QPushButton("▼")
        rmrow.addWidget(self.btn_del); rmrow.addWidget(self.btn_up); rmrow.addWidget(self.btn_down)
        lv.addLayout(rmrow)

        lv.addWidget(QtWidgets.QLabel("<b>Properties</b>"))
        self.props = QtWidgets.QFormLayout()

        ctrlrow = QtWidgets.QHBoxLayout()
        self.btn_save = QtWidgets.QPushButton("Save template")
        self.btn_load = QtWidgets.QPushButton("Load template")
        ctrlrow.addWidget(self.btn_save); ctrlrow.addWidget(self.btn_load)
        lv.addLayout(ctrlrow)
        lv.addLayout(self.props)

        root.addWidget(left)

        # Center: preview
        self.preview = PreviewWidget()
        root.addWidget(self.preview, stretch=1)

        # Right: markup (secondary view)
        right = QtWidgets.QWidget(); right.setFixedWidth(320)
        rv = QtWidgets.QVBoxLayout(right)
        rv.addWidget(QtWidgets.QLabel("<b>Markup (auto-synced)</b>"))
        self.markup = QtWidgets.QPlainTextEdit()
        self.markup.setFont(QtGui.QFont("Monospace", 9))
        rv.addWidget(self.markup, stretch=1)

        self.density = QtWidgets.QSpinBox(); self.density.setRange(1,5); self.density.setValue(4)
        dr = QtWidgets.QHBoxLayout()
        dr.addWidget(QtWidgets.QLabel("Density")); dr.addWidget(self.density); dr.addStretch()
        rv.addLayout(dr)

        self.btn_preview_png = QtWidgets.QPushButton("Save preview PNG")
        self.btn_import_img = QtWidgets.QPushButton("Import bitmap → base64")
        self.btn_print = QtWidgets.QPushButton("Print series")
        self.btn_print.setStyleSheet("font-weight:bold; padding:6px;")
        rv.addWidget(self.btn_import_img)
        rv.addWidget(self.btn_preview_png)
        rv.addWidget(self.btn_print)

        self.status = QtWidgets.QLabel("Ready")
        rv.addWidget(self.status)
        root.addWidget(right)

    def _connect_signals(self):
        self.tree.currentItemChanged.connect(self._on_tree_select)
        self.tree.model().rowsMoved.connect(lambda *_: self._sync_from_tree_order())
        for key, b in self.btn_add.items():
            b.clicked.connect(lambda _, k=key: self._add_object(k))
        self.btn_del.clicked.connect(self._delete_selected)
        self.btn_up.clicked.connect(lambda: self._move_selected(-1))
        self.btn_down.clicked.connect(lambda: self._move_selected(+1))
        self.preview.object_moved.connect(self._on_object_moved)
        self.sample_spin.valueChanged.connect(self.refresh_all)
        self.markup.textChanged.connect(self._on_markup_edited)
        self.btn_scan.clicked.connect(self._scan)
        self.btn_print.clicked.connect(self._print_series)
        self.btn_preview_png.clicked.connect(self._save_preview)
        self.btn_import_img.clicked.connect(self._import_bitmap)
        self.btn_save.clicked.connect(self._save_template)
        self.btn_load.clicked.connect(self._load_template)

    # ---- model / render ----
    def _set_example(self):
        self._template_objects = parse_markup(EXAMPLE)

    def _current_id(self):
        return self.sample_spin.value()

    def refresh_all(self):
        self._rebuild_tree()
        self._refresh_preview()
        self._sync_markup()

    def _refresh_preview(self):
        objs = [substitute(o, self._current_id()) for o in self._template_objects]
        img = render_template(objs)
        self.preview.set_preview(img)
        self._compute_boxes()
        count = len(self._template_objects)

    def _compute_boxes(self):
        boxes = []
        for o in self._template_objects:
            k = o.get("type")
            if k == "text":
                boxes.append((o.get("x",0), o.get("y",0),
                              o.get("x",0)+max(10, len(str(o.get('text','')))*o.get('size',16)),
                              o.get("y",0)+o.get('size',16)))
            elif k == "barcode":
                h = o.get("height", 60)
                boxes.append((o.get("x",0), o.get("y",0), o.get("x",0)+h, o.get("y",0)+h))
            elif k == "qrcode":
                s = o.get("module_px",3)*30
                boxes.append((o.get("x",0), o.get("y",0), o.get("x",0)+s, o.get("y",0)+s))
            elif k == "line":
                boxes.append((o.get("x1",0), o.get("y1",0), o.get("x2",96), o.get("y2",0)))
            elif k == "rect":
                boxes.append((o.get("x",0), o.get("y",0), o.get("x",0)+o.get("w",10), o.get("y",0)+o.get("h",10)))
            elif k == "image":
                boxes.append((o.get("x",0), o.get("y",0), o.get("x",0)+(o.get("w",40) or 40), o.get("y",0)+(o.get("h",40) or 40)))
            else:
                boxes.append((0,0,10,10))
        self._boxes = boxes
        self.preview._hit_fn = self._hit_test

    def _hit_test(self, px, py):
        for i in range(len(self._boxes)-1, -1, -1):
            x0,y0,x1,y1 = self._boxes[i]
            x0,x1 = min(x0,x1), max(x0,x1)
            y0,y1 = min(y0,y1), max(y0,y1)
            if x0<=px<=x1 and y0<=py<=y1:
                return i
        return -1

    # ---- tree view ----
    def _rebuild_tree(self):
        self.tree.blockSignals(True)
        self.tree.clear()
        for i, o in enumerate(self._template_objects):
            k = o.get("type")
            label = OBJ_SPECS[k]["label"]
            primary = "data" if k in ("barcode","qrcode") else "text"
            val = o.get(primary, "")
            val = str(val)[:24]
            name = f"{label}: {val}" if val else label
            item = QtWidgets.QTreeWidgetItem([name])
            item.setData(0, QtCore.Qt.ItemDataRole.UserRole, i)
            self.tree.addTopLevelItem(item)
        self.tree.blockSignals(False)

    def _on_tree_select(self, cur, prev):
        if cur is None:
            return
        idx = cur.data(0, QtCore.Qt.ItemDataRole.UserRole)
        self._current_object_index = idx
        self._build_props(idx)

    def _sync_from_tree_order(self):
        # reorder _template_objects to match tree top-level order
        order = []
        for i in range(self.tree.topLevelItemCount()):
            order.append(self.tree.topLevelItem(i).data(0, QtCore.Qt.ItemDataRole.UserRole))
        if sorted(order) == list(range(len(self._template_objects))):
            self._template_objects = [self._template_objects[i] for i in order]
            self._refresh_preview(); self._sync_markup()

    def _build_props(self, idx):
        # clear old props
        while self.props.rowCount():
            self.props.removeRow(0)
        if not (0 <= idx < len(self._template_objects)):
            return
        o = self._template_objects[idx]
        k = o.get("type")
        for field, ftype in OBJ_SPECS[k]["fields"]:
            val = o.get(field, "")
            if ftype == "bool":
                w = QtWidgets.QCheckBox(); w.setChecked(bool(val))
                w.toggled.connect(lambda ch, f=field: self._set_prop(idx, f, ch))
            elif ftype == "b64":
                w = QtWidgets.QLabel("[embedded]" if val else "[none]")
            elif ftype == "path":
                w = QtWidgets.QLineEdit(str(val))
                w.textChanged.connect(lambda t, f=field: self._set_prop(idx, f, t))
            elif ftype == "int":
                w = QtWidgets.QSpinBox(); w.setRange(-10000, 100000); w.setValue(int(val or 0))
                w.valueChanged.connect(lambda v, f=field: self._set_prop(idx, f, v))
            else:
                w = QtWidgets.QLineEdit(str(val))
                w.textChanged.connect(lambda t, f=field: self._set_prop(idx, f, t))
            self.props.addRow(field, w)

    def _set_prop(self, idx, field, val):
        if not (0 <= idx < len(self._template_objects)):
            return
        self._template_objects[idx][field] = val
        self._refresh_preview(); self._sync_markup()

    # ---- add / delete / reorder ----
    def _add_object(self, kind):
        defaults = {
            "text":    {"type":"text", "text":"TEXT", "x":20, "y":150, "size":20},
            "line":    {"type":"line", "x1":0, "y1":30, "x2":96, "y2":30, "width":1},
            "rect":    {"type":"rect", "x":10, "y":10, "w":30, "h":20, "width":1, "fill":False},
            "image":   {"type":"image", "x":10, "y":10, "w":40, "mode":"fit"},
            "barcode": {"type":"barcode", "data":"S{id:04d}", "x":2, "y":40, "module_px":3, "height":88},
            "qrcode":  {"type":"qrcode", "data":"SAMPLE", "x":20, "y":20, "module_px":3, "border":3},
        }
        self._template_objects.append(dict(defaults[kind]))
        self.refresh_all()

    def _delete_selected(self):
        idx = getattr(self, "_current_object_index", -1)
        if 0 <= idx < len(self._template_objects):
            del self._template_objects[idx]
            self.refresh_all()

    def _move_selected(self, delta):
        idx = getattr(self, "_current_object_index", -1)
        j = idx + delta
        if 0 <= idx < len(self._template_objects) and 0 <= j < len(self._template_objects):
            self._template_objects[idx], self._template_objects[j] = \
                self._template_objects[j], self._template_objects[idx]
            self.refresh_all()

    def _on_object_moved(self, idx, axis, delta):
        if not (0 <= idx < len(self._template_objects)):
            return
        o = self._template_objects[idx]
        k = o.get("type")
        if k == "line":
            key = {"x":"x1","y":"y1"}.get(axis, "x1")
            o[key] = max(0, o.get(key,0)+delta)
            if axis == "x": o["x2"] = o.get("x2",96)+delta
            else: o["y2"] = o.get("y2",0)+delta
        else:
            key = {"x":"x","y":"y"}.get(axis, "x")
            o[key] = max(0, o.get(key,0)+delta)
        self._refresh_preview()
        self._sync_markup()

    # ---- markup sync ----
    def _sync_markup(self):
        lines = []
        for o in self._template_objects:
            k = o.get("type")
            primary = "data" if k in ("barcode","qrcode") else "text"
            parts = [k]
            pv = o.get(primary)
            if pv is not None:
                parts.append(f'{primary}={_q(pv)}')
            for key, val in o.items():
                if key in ("type", primary):
                    continue
                parts.append(f"{key}={_q(val)}")
            lines.append(" ".join(parts))
        self.markup.blockSignals(True)
        self.markup.setPlainText("\n".join(lines))
        self.markup.blockSignals(False)

    def _on_markup_edited(self):
        # dedupe loop guard
        if getattr(self, "_syncing", False):
            return
        self._syncing = True
        try:
            self._template_objects = parse_markup(self.markup.toPlainText())
            self._rebuild_tree()
            self._refresh_preview()
        except Exception as e:
            self.status.setText(f"Markup error: {e}")
        finally:
            self._syncing = False

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
                    self.status.setText(f"Found {line.split()[1]}"); return
            self.status.setText("No Supvan printer found (is it awake?)")
        except Exception as e:
            self.status.setText(f"Scan failed: {e}")

    def _import_bitmap(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "Import bitmap", "", "Images (*.png *.jpg *.bmp)")
        if not path:
            return
        data = base64.b64encode(Path(path).read_bytes()).decode()
        # add an image object with embedded data
        self._template_objects.append({
            "type": "image", "data": data, "x": 10, "y": 10,
            "w": 40, "mode": "fit"})
        self.refresh_all()
        self.status.setText(f"Imported {Path(path).name} as embedded image")

    def _save_preview(self):
        objs = [substitute(o, self._current_id()) for o in self._template_objects]
        img = render_template(objs)
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "Save preview", "label.png", "PNG (*.png)")
        if path:
            img.save(path); self.status.setText(f"Saved {path}")

    def _collect(self):
        return {"address": self.addr.text(),
                "objects": self._template_objects,
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
        if not path:
            return
        s = json.loads(Path(path).read_text())
        self.addr.setText(s.get("address", self.addr.text()))
        self._template_objects = s.get("objects", [])
        self.from_spin.setValue(s.get("from", 1))
        self.to_spin.setValue(s.get("to", 10))
        self.density.setValue(s.get("density", 4))
        self.refresh_all(); self.status.setText(f"Loaded {path}")

    def _save_config(self):
        CONFIG.write_text(json.dumps(self._collect(), indent=2))

    def _load_config(self):
        if CONFIG.exists():
            try:
                s = json.loads(CONFIG.read_text())
                self.addr.setText(s.get("address", self.addr.text()))
                self._template_objects = s.get("objects", [])
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
        objs = self._template_objects
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
            loop = asyncio.new_event_loop(); asyncio.set_event_loop(loop)
            try:
                loop.run_until_complete(_run())
                self.status.setText(f"Series done ({start}…{end})")
            except Exception as e:
                self.status.setText(f"Error: {e}")

        t = threading.Thread(target=run, daemon=True); t.start()
        def _poll():
            if t.is_alive(): QtCore.QTimer.singleShot(200, _poll)
            else: self.btn_print.setEnabled(True)
        QtCore.QTimer.singleShot(200, _poll)


def _q(v):
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
