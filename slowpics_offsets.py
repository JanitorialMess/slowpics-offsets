"""
SlowPics Offsets Plugin
"""
from __future__ import annotations

import contextlib
import json
import logging
import random
import traceback
from functools import partial
from pathlib import Path
from typing import Any
from uuid import uuid4

from PyQt6.QtCore import QKeyCombination, Qt, QThread
from PyQt6.QtGui import QKeySequence
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QFileDialog,
    QFrame,
    QInputDialog,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QWidget,
)
from PyQt6.QtWidgets import (
    QHBoxLayout as QHBox,
)
from PyQt6.QtWidgets import (
    QVBoxLayout as QVBox,
)
from vspreview.core import (
    Frame,
    HBoxLayout,
    LineEdit,
    ProgressBar,
    PushButton,
    SpinBox,
    Stretch,
    VBoxLayout,
)
from vspreview.main import MainWindow
from vspreview.plugins import AbstractPlugin, PluginConfig

__all__ = ["SlowPicsOffsetsPlugin"]

try:
    from vspreview.plugins.builtins.slowpics_comp.workers import (
        FindFramesWorker,
        FindFramesWorkerConfiguration,
        Worker,
    )
    SLOWPICS_AVAILABLE = True
except ImportError:
    SLOWPICS_AVAILABLE = False
    FindFramesWorker = None
    FindFramesWorkerConfiguration = None
    Worker = None
    WorkerConfiguration = None


class SlowPicsOffsetsPlugin(AbstractPlugin, QWidget):
    _config = PluginConfig("dev.supertouch.slowpics-offsets", "SlowPics Offsets")

    def __init__(self, main: MainWindow) -> None:
        super().__init__(main)

        self.main = main
        self.frame_offsets: dict[int, dict[int, int]] = {}
        self.selected_frames: list[int] = []
        self.current_frame_index: int = 0

        self.search_thread: QThread | None = None
        self.search_worker: FindFramesWorker | None = None
        self.upload_thread: QThread | None = None
        self.upload_worker: Worker | None = None
        self._is_generating: bool = False

    def setup_ui(self) -> None:
        super().setup_ui()

        # Offset configuration
        self.offset_header_label = QLabel("Offsets for Current Frame:")
        self.offset_header_label.setStyleSheet("font-weight: bold;")

        self.offset_container = QWidget()
        self.offset_layout = QVBox(self.offset_container)
        self.offset_layout.setContentsMargins(0, 0, 0, 0)
        self.offset_spinboxes: dict[int, SpinBox] = {}
        self.output_labels: dict[int, QLabel] = {}

        # Frame generation status
        self.gen_status_label = QLabel("No frames loaded")
        self.gen_status_label.setStyleSheet("font-style: italic; color: #888;")

        self.generate_from_slowpics_button = PushButton(
            "Generate Frames (using SlowPics settings)",
            self,
            clicked=self.generate_frames_from_slowpics
        )
        self.generate_from_slowpics_button.setEnabled(SLOWPICS_AVAILABLE)

        # Frame list
        self.frame_list_label = QLabel("Selected Frames:")

        self.frame_list = QListWidget()
        self.frame_list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.frame_list.itemClicked.connect(self.on_frame_list_clicked)
        self.frame_list.currentRowChanged.connect(self.on_frame_list_row_changed)
        self.frame_list.setMaximumHeight(150)

        self.prev_button = PushButton("◄ Prev", self, clicked=self.on_prev_clicked)
        self.frame_position_label = QLabel("No frames selected")
        self.next_button = PushButton("Next ►", self, clicked=self.on_next_clicked)

        # Status
        self.status_label = QLabel("")
        if not SLOWPICS_AVAILABLE:
            self.status_label.setText("SlowPics Comps not found!")

        # Upload controls
        self.output_url_lineedit = LineEdit("")
        self.output_url_lineedit.setReadOnly(True)
        self.output_url_lineedit.setPlaceholderText("Upload URL will appear here")

        self.copy_url_button = PushButton("📋", self, clicked=self.on_copy_url_clicked)
        self.copy_url_button.setMaximumWidth(40)

        self.upload_button = PushButton(
            "Upload",
            self,
            clicked=self.on_upload_clicked
        )
        self.upload_button.setEnabled(SLOWPICS_AVAILABLE)

        self.progress_bar = ProgressBar(self, value=0)

        self.send_to_slowpics_button = PushButton(
            "Send to SlowPics Comps Tab (no offsets)",
            self,
            clicked=self.on_send_to_slowpics_clicked
        )

        # Storage controls
        self.load_button = PushButton("Load Offsets", self, clicked=self.on_load_clicked)
        self.save_button = PushButton("Save Offsets", self, clicked=self.on_save_clicked)

        VBoxLayout(self.vlayout, [
            self.gen_status_label,
            self.generate_from_slowpics_button,

            self.get_separator(),

            self.frame_list_label,
            self.frame_list,

            HBoxLayout([
                self.prev_button,
                self.frame_position_label,
                self.next_button,
            ]),

            HBoxLayout([
                PushButton("Add Frame", self, clicked=self.on_add_frame_clicked),
                PushButton("Remove Frame", self, clicked=self.on_remove_frame_clicked),
                PushButton("Edit Frame", self, clicked=self.on_edit_frame_clicked),
            ]),

            self.get_separator(),

            self.offset_header_label,
            self.offset_container,

            self.get_separator(),

            self.upload_button,
            self.progress_bar,
            HBoxLayout([
                self.output_url_lineedit,
                self.copy_url_button,
            ]),
            self.status_label,

            self.get_separator(),

            HBoxLayout([
                self.load_button,
                self.save_button,
            ]),

            self.send_to_slowpics_button,

            Stretch()
        ])

    def init_outputs(self) -> None:
        if not self.main.outputs:
            return

        while self.offset_layout.count():
            child = self.offset_layout.takeAt(0)
            if child.widget():
                child.widget().deleteLater()

        self.offset_spinboxes.clear()
        self.output_labels.clear()

        for i, output in enumerate(self.main.outputs):
            row = QWidget()
            row_layout = QHBox(row)
            row_layout.setContentsMargins(0, 0, 0, 0)

            name_label = QLabel(output.name)
            name_label.setMinimumWidth(150)
            self.output_labels[i] = name_label

            offset_spinbox = SpinBox(None, -99999, 99999, value=0)
            offset_spinbox.setMaximumWidth(80)
            offset_spinbox.valueChanged.connect(lambda val, idx=i: self.on_offset_changed(idx, val))
            self.offset_spinboxes[i] = offset_spinbox

            row_layout.addWidget(name_label)
            row_layout.addWidget(QLabel("Offset:"))
            row_layout.addWidget(offset_spinbox)
            row_layout.addStretch()

            self.offset_layout.addWidget(row)

        self.update_highlighting()

    def update_highlighting(self) -> None:
        if not self.main.outputs:
            return

        current_idx = self.main.current_output.index
        for i, label in self.output_labels.items():
            if i == current_idx:
                label.setStyleSheet("font-weight: bold; color: #4facfe;")
            else:
                label.setStyleSheet("")

    def update_offset_controls(self) -> None:
        if not self.selected_frames:
            for spinbox in self.offset_spinboxes.values():
                spinbox.setEnabled(False)
            return

        current_frame = self.selected_frames[self.current_frame_index]
        self.offset_header_label.setText(f"Offsets for Frame {current_frame}:")

        current_offsets = self.frame_offsets.get(current_frame, {})

        for i, spinbox in self.offset_spinboxes.items():
            spinbox.blockSignals(True)
            spinbox.setValue(current_offsets.get(i, 0))
            spinbox.setEnabled(True)
            spinbox.blockSignals(False)

    def on_offset_changed(self, index: int, value: int) -> None:
        if not self.selected_frames:
            return

        current_frame = self.selected_frames[self.current_frame_index]

        if current_frame not in self.frame_offsets:
            self.frame_offsets[current_frame] = {}

        self.frame_offsets[current_frame][index] = value

        if self.main.current_output.index != index:
            self.main.switch_output(index)
        else:
            self.navigate_to_current_frame()

    def generate_frames_from_slowpics(self) -> None:
        if not SLOWPICS_AVAILABLE:
            self.status_label.setText("SlowPics not available")
            return

        if self._is_generating:
            self._set_status("Generation already in progress...")
            return

        try:
            slowpics_plugin = self.main.plugins["dev.setsugen.comp"]
            slowpics_plugin.first_load()
            slowpics = slowpics_plugin.main_tab

            manual_frames_text = slowpics.manual_frames_lineedit.text()
            manual_frames = [
                Frame(int(x.strip()))
                for x in manual_frames_text.split(",")
                if x.strip()
            ] if manual_frames_text else []

            num_random = int(slowpics.random_frames_control.value())
            seed_text = slowpics.random_seed_control.text()
            seed = int(seed_text) if seed_text else None
            dark_num = int(slowpics.random_dark_frame_edit.value())
            light_num = int(slowpics.random_light_frame_edit.value())

            picture_types = set()
            if slowpics.pic_type_button_I.isChecked():
                picture_types.add("I")
            if slowpics.pic_type_button_B.isChecked():
                picture_types.add("B")
            if slowpics.pic_type_button_P.isChecked():
                picture_types.add("P")

            samples = list(manual_frames)
            if slowpics.current_frame_checkbox.isChecked():
                samples.append(self.main.current_output.last_showed_frame)

            start_frame = int(slowpics.start_rando_frames_control.value())
            end_frame = int(slowpics.end_rando_frames_control.value())

            if not self.main.outputs:
                self._set_status("No outputs loaded", is_error=True)
                return

            lens = {out.prepared.clip.num_frames for out in self.main.outputs}
            if len(lens) != 1:
                logging.warning("Outputs don't all have the same length!")

            lens_n = min(lens)
            end_frame = min(lens_n, end_frame)

            if end_frame <= start_frame:
                self._set_status("Invalid frame range", is_error=True)
                return

            uuid = str(uuid4())
            config = FindFramesWorkerConfiguration(
                uuid,
                self.main.current_output,
                list(self.main.outputs),
                self.main,
                start_frame,
                end_frame,
                min(lens_n, end_frame - start_frame),
                dark_num,
                light_num,
                num_random,
                picture_types,
                samples
            )

            if seed is not None:
                random.seed(seed)

            if self.search_thread is not None:
                self.search_thread.quit()
                self.search_thread.wait()
                self.search_thread = None
                self.search_worker = None

            self.search_thread = QThread()
            self.search_worker = FindFramesWorker()
            self.search_worker.moveToThread(self.search_thread)

            self.search_thread.started.connect(partial(self.search_worker.run, config))
            self.search_worker.finished.connect(
                lambda uid: self.on_frames_generated(uid, config)
            )

            self.search_worker.finished.connect(self.search_thread.quit)
            self.search_thread.finished.connect(self._cleanup_search_thread)

            self.search_worker.progress_status.connect(
                lambda uid, kind, curr, total:
                    self.gen_status_label.setText(f"{kind} {curr}/{total}") if uid == uuid else None
            )

            self._is_generating = True
            self.search_thread.start()
            self.gen_status_label.setText("Generating frames...")
            self.gen_status_label.setStyleSheet("font-style: italic; color: #4facfe;")
            self.generate_from_slowpics_button.setEnabled(False)

        except KeyError:
            self._set_status("SlowPics plugin not found", is_error=True)
            self._is_generating = False
            self.generate_from_slowpics_button.setEnabled(True)
        except Exception as e:
            self._set_status(f"Error: {e}", is_error=True)
            self._is_generating = False
            self.generate_from_slowpics_button.setEnabled(True)
            traceback.print_exc()

    def _cleanup_search_thread(self) -> None:
        if self.search_thread is not None:
            with contextlib.suppress(RuntimeError):
                self.search_thread.deleteLater()
            self.search_thread = None
        if self.search_worker is not None:
            with contextlib.suppress(RuntimeError):
                self.search_worker.deleteLater()
            self.search_worker = None
        self._is_generating = False

    def on_frames_generated(self, uuid: str, config: FindFramesWorkerConfiguration) -> None:
        self.selected_frames = sorted({int(f) for f in config.samples})
        self.current_frame_index = 0

        self.update_frame_list()
        self.gen_status_label.setText(f"Loaded {len(self.selected_frames)} frames.")
        self.gen_status_label.setStyleSheet("")
        self.generate_from_slowpics_button.setEnabled(True)

        if self.selected_frames:
            self.navigate_to_current_frame()

    def update_frame_list(self) -> None:
        self.frame_list.clear()

        for i, frame in enumerate(self.selected_frames):
            item = QListWidgetItem(f"Frame {frame}")
            item.setData(Qt.ItemDataRole.UserRole, frame)
            self.frame_list.addItem(item)

        self.update_navigation_label()

    def update_navigation_label(self) -> None:
        if not self.selected_frames:
            self.frame_position_label.setText("No frames selected")
            return

        if self.current_frame_index >= len(self.selected_frames):
            self.current_frame_index = len(self.selected_frames) - 1
        if self.current_frame_index < 0:
            self.current_frame_index = 0

        current_frame = self.selected_frames[self.current_frame_index]
        self.frame_position_label.setText(
            f"Frame {self.current_frame_index + 1}/{len(self.selected_frames)}: {current_frame}"
        )
        self.frame_list.setCurrentRow(self.current_frame_index)

    def on_frame_list_clicked(self, item: QListWidgetItem) -> None:
        row = self.frame_list.row(item)
        self.current_frame_index = row
        self.navigate_to_current_frame()

    def on_frame_list_row_changed(self, row: int) -> None:
        if row >= 0 and row < len(self.selected_frames):
            self.current_frame_index = row
            self.navigate_to_current_frame()

    def on_prev_clicked(self) -> None:
        if self.selected_frames and self.current_frame_index > 0:
            self.current_frame_index -= 1
            self.navigate_to_current_frame()

    def on_next_clicked(self) -> None:
        if self.selected_frames and self.current_frame_index < len(self.selected_frames) - 1:
            self.current_frame_index += 1
            self.navigate_to_current_frame()

    def navigate_to_current_frame(self) -> None:
        if not self.selected_frames or not self.main.outputs:
            return

        if self.current_frame_index < 0 or self.current_frame_index >= len(self.selected_frames):
            return

        base_frame = self.selected_frames[self.current_frame_index]

        current_output_index = self.main.current_output.index
        current_offsets = self.frame_offsets.get(base_frame, {})
        offset = current_offsets.get(current_output_index, 0)

        target_frame = base_frame + offset
        target_frame = self._clamp_frame_to_range(target_frame)

        self.main.switch_frame(Frame(target_frame))

        self.update_navigation_label()
        self.update_offset_controls()

    def on_current_output_changed(self, index: int, prev_index: int) -> None:
        self.update_highlighting()
        if self.selected_frames:
            self.navigate_to_current_frame()

    def get_offset_adjusted_frames(self) -> list[list[int]]:
        if not self.main.outputs or not self.selected_frames:
            return []

        frames_per_output = []
        for i, output in enumerate(self.main.outputs):
            max_frame = output.total_frames - 1

            adjusted = []
            for base_frame in self.selected_frames:
                base_frame = int(base_frame)

                current_offsets = self.frame_offsets.get(base_frame, {})
                offset = int(current_offsets.get(i, 0))

                target = int(base_frame + offset)
                target = max(0, min(target, max_frame))
                adjusted.append(int(target))

            frames_per_output.append(adjusted)

        return frames_per_output

    def on_upload_clicked(self) -> None:
        if not self.selected_frames:
            self.status_label.setText("No frames to upload")
            return

        if not SLOWPICS_AVAILABLE:
            self.status_label.setText("SlowPics components not available")
            return

        if not self.main.outputs:
            self.status_label.setText("No outputs loaded")
            return

        try:
            try:
                slowpics_plugin = self.main.plugins["dev.setsugen.comp"]
                slowpics_plugin.first_load()
                slowpics = slowpics_plugin.main_tab
            except KeyError:
                self.status_label.setText("SlowPics plugin not found")
                return

            frames_per_output = self.get_offset_adjusted_frames()
            uuid = str(uuid4())

            try:
                dummy_frames = [Frame(f) for f in self.selected_frames]
                base_config = slowpics.get_slowpics_conf(uuid, dummy_frames)
            except ValueError as e:
                self.status_label.setText(str(e))
                return
            except Exception as e:
                self.status_label.setText(f"Error getting config: {e}")
                traceback.print_exc()
                return

            config = base_config._replace(frames=frames_per_output)

            self.upload_thread = QThread()
            self.upload_worker = Worker()
            self.upload_worker.moveToThread(self.upload_thread)

            self.upload_thread.started.connect(partial(self.upload_worker.run, config))

            self.upload_worker.progress_bar.connect(
                lambda uid, val: self.progress_bar.setValue(val) if uid == uuid else None
            )
            self.upload_worker.progress_status.connect(
                lambda uid, kind, curr, total: self.on_upload_progress(uid, kind, curr, total) if uid == uuid else None
            )

            self.upload_thread.start()

            self.status_label.setText("Uploading...")
            self.upload_button.setEnabled(False)

        except Exception as e:
            self.status_label.setText(f"Error: {e}")
            traceback.print_exc()

    def on_upload_progress(self, uuid: str, kind: str, curr: int | None, total: int | None) -> None:
        if kind in ("extract", "upload", "search"):
            info = f" {curr or '?'}/{total or '?'}" if curr or total else ""
            self.status_label.setText(f"{kind.capitalize()}{info}...")
        elif kind.startswith("https://"):
            self.output_url_lineedit.setText(kind)
            self.status_label.setText("Done! URL copied to clipboard.")
            self.upload_button.setEnabled(True)
            self.main.clipboard.setText(kind)
            self.main.show_message(f"Uploaded: {kind}")

    def on_copy_url_clicked(self) -> None:
        url = self.output_url_lineedit.text()
        if url:
            self.main.clipboard.setText(url)
            self.status_label.setText("URL copied!")

    def on_send_to_slowpics_clicked(self) -> None:
        if not self.selected_frames:
            self.status_label.setText("No frames to send")
            return

        try:
            slowpics = self.main.plugins["dev.setsugen.comp"]

            frames_str = ",".join(str(f) for f in self.selected_frames)
            slowpics.main_tab.manual_frames_lineedit.setText(frames_str)
            self.main.plugins_tab.setCurrentIndex(slowpics.index)

            self.status_label.setText(f"Sent {len(self.selected_frames)} frames to SlowPics")
        except KeyError:
            self.status_label.setText("SlowPics plugin not found")
        except Exception as e:
            self.status_label.setText(f"Error: {e}")

    def on_save_clicked(self) -> None:
        if not self.selected_frames:
            self.status_label.setText("No frames to save")
            return

        default_name = "offset_comp_state.json"
        if hasattr(self.main, "script_path") and self.main.script_path:
            script_name = Path(self.main.script_path).stem
            default_name = f"{script_name}_offsets.json"

        path, _ = QFileDialog.getSaveFileName(
            self, "Save State", default_name, "JSON Files (*.json)"
        )
        if not path:
            return

        try:
            if not self.main.outputs:
                self.status_label.setText("No outputs loaded")
                return

            idx_to_name = {i: out.name for i, out in enumerate(self.main.outputs)}

            export_data = {
                "version": 1,
                "selected_frames": [int(f) for f in self.selected_frames],
                "offsets": {}
            }

            for frame_num, offsets in self.frame_offsets.items():
                frame_data = {}
                for idx, offset in offsets.items():
                    if idx in idx_to_name:
                        frame_data[idx_to_name[idx]] = int(offset)
                if frame_data:
                    export_data["offsets"][str(int(frame_num))] = frame_data

            with open(path, "w", encoding="utf-8") as f:
                json.dump(export_data, f, indent=2)
                f.flush()

            self.status_label.setText(f"Saved {len(self.selected_frames)} frames to {Path(path).name}")
        except Exception as e:
            self.status_label.setText(f"Error saving: {e}")
            traceback.print_exc()

    def on_load_clicked(self) -> None:
        if not self.main.outputs:
            self.status_label.setText("No outputs loaded")
            return

        path, _ = QFileDialog.getOpenFileName(
            self, "Load State", "", "JSON Files (*.json)"
        )
        if not path:
            return

        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)

            name_to_idx = {out.name: i for i, out in enumerate(self.main.outputs)}

            self.selected_frames = []
            self.frame_offsets = {}

            if "selected_frames" in data:
                self.selected_frames = data["selected_frames"]

            offsets_data = data.get("offsets", {})
            for frame_str, offsets in offsets_data.items():
                try:
                    frame_num = int(frame_str)
                    self.frame_offsets[frame_num] = {}

                    for out_name, offset in offsets.items():
                        if out_name in name_to_idx:
                            self.frame_offsets[frame_num][name_to_idx[out_name]] = offset
                except ValueError:
                    continue

            self.selected_frames.sort()
            if hasattr(self, "frame_list"):
                self.update_frame_list()

            if self.selected_frames:
                self.current_frame_index = 0
                self.frame_list.setCurrentRow(0)
                self.navigate_to_current_frame()

            self.gen_status_label.setText(f"Loaded {len(self.selected_frames)} frames from {Path(path).name}")
            self.gen_status_label.setStyleSheet("")

        except Exception as e:
            self.gen_status_label.setText(f"Error loading: {e}")

    def on_add_frame_clicked(self) -> None:
        frame, ok = QInputDialog.getInt(
            self, "Add Frame", "Enter frame number:", 0, 0
        )
        if ok:
            original_frame = frame
            frame = self._clamp_frame_to_range(frame)

            if frame != original_frame:
                self._set_status(f"Frame {original_frame} out of range, adjusted to {frame}")

            if frame not in self.selected_frames:
                self.selected_frames.append(frame)
                self.selected_frames.sort()
                self.update_frame_list()

                try:
                    idx = self.selected_frames.index(frame)
                    self.current_frame_index = idx
                    self.frame_list.setCurrentRow(idx)
                    self.navigate_to_current_frame()
                except ValueError:
                    pass
            else:
                self.status_label.setText(f"Frame {frame} already exists")

    def on_remove_frame_clicked(self) -> None:
        if not self.selected_frames:
            return

        row = self.frame_list.currentRow()
        if row < 0:
            return

        frame = self.selected_frames[row]

        del self.selected_frames[row]

        if frame in self.frame_offsets:
            del self.frame_offsets[frame]

        if self.selected_frames:
            self.current_frame_index = min(row, len(self.selected_frames) - 1)
        else:
            self.current_frame_index = 0

        self.update_frame_list()

        if self.selected_frames:
            self.frame_list.setCurrentRow(self.current_frame_index)
            self.navigate_to_current_frame()

    def on_edit_frame_clicked(self) -> None:
        if not self.selected_frames:
            return

        row = self.frame_list.currentRow()
        if row < 0:
            return

        old_frame = self.selected_frames[row]

        new_frame, ok = QInputDialog.getInt(
            self, "Edit Frame", "Enter new frame number:", old_frame, 0
        )

        if ok and new_frame != old_frame:
            original_new_frame = new_frame
            new_frame = self._clamp_frame_to_range(new_frame)

            if new_frame != original_new_frame:
                self._set_status(f"Frame {original_new_frame} out of range, adjusted to {new_frame}")

            if new_frame in self.selected_frames:
                self.status_label.setText(f"Frame {new_frame} already exists")
                return

            self.selected_frames[row] = new_frame
            self.selected_frames.sort()

            if old_frame in self.frame_offsets:
                self.frame_offsets[new_frame] = self.frame_offsets.pop(old_frame)

            self.update_frame_list()

            try:
                idx = self.selected_frames.index(new_frame)
                self.current_frame_index = idx
                self.frame_list.setCurrentRow(idx)
                self.navigate_to_current_frame()
            except ValueError:
                pass

    def get_separator(self) -> QWidget:
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setFrameShadow(QFrame.Shadow.Sunken)
        return sep

    def add_shortcuts(self) -> None:
        self.add_shortcut(
            "offset_comp_prev_frame",
            self,
            self.on_prev_clicked,
            QKeySequence(QKeyCombination(Qt.Modifier.CTRL, Qt.Key.Key_BracketLeft).toCombined()),
            "Previous comparison frame"
        )

        self.add_shortcut(
            "offset_comp_next_frame",
            self,
            self.on_next_clicked,
            QKeySequence(QKeyCombination(Qt.Modifier.CTRL, Qt.Key.Key_BracketRight).toCombined()),
            "Next comparison frame"
        )

        super().add_shortcuts()

    def _clamp_frame_to_range(self, frame: int) -> int:
        if not self.main.outputs:
            return frame
        max_valid = max(out.total_frames - 1 for out in self.main.outputs)
        return max(0, min(frame, max_valid))

    def _set_status(self, message: str, is_error: bool = False) -> None:
        self.gen_status_label.setText(message)
        if is_error:
            self.gen_status_label.setStyleSheet("font-style: italic; color: #ff6b6b;")
        else:
            self.gen_status_label.setStyleSheet("font-style: italic; color: #888;")

    def __getstate__(self) -> dict[str, Any]:
        return super().__getstate__() | {
            "frame_offsets": self.frame_offsets,
            "selected_frames": self.selected_frames,
        }

    def __setstate__(self) -> None:
        state = self.settings.local

        if "frame_offsets" in state:
            self.frame_offsets = state["frame_offsets"]

        if "selected_frames" in state:
            self.selected_frames = state["selected_frames"]
            if hasattr(self, "frame_list"):
                self.update_frame_list()
