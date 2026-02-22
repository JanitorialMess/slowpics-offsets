"""
SlowPics Offsets Plugin
"""
from __future__ import annotations

import contextlib
import json
import logging
import random
import re
import traceback
from functools import partial
from pathlib import Path
from typing import Any
from uuid import uuid4

from PyQt6.QtCore import QKeyCombination, Qt, QThread
from PyQt6.QtGui import QKeySequence
from PyQt6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QFrame,
    QInputDialog,
    QLabel,
    QListWidgetItem,
    QMessageBox,
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

from .components import AppendControlsWidget, FrameListModel, FrameSelectionWidget
from .models import (
    AppendSourcesConfiguration,
    TargetContext,
    TargetLoadWorkerConfiguration,
)
from .utils import (
    build_append_collection_name,
    deserialize_frame_offsets,
    normalize_frame_offsets_state,
    parse_comp_key,
    parse_frames_from_comp_names,
    parse_view_path,
    serialize_frame_offsets,
)
from .workers import AppendSourcesWorker, TargetLoadWorker

__all__ = ["SlowPicsOffsetsPlugin"]

try:
    from vspreview.plugins.builtins.slowpics_comp.utils import (
        get_frame_time,
        get_slowpic_headers,
        get_slowpic_upload_headers,
    )
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
    get_frame_time = None
    get_slowpic_headers = None
    get_slowpic_upload_headers = None


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
        self.append_thread: QThread | None = None
        self.append_worker: AppendSourcesWorker | None = None
        self.load_target_thread: QThread | None = None
        self.load_target_worker: TargetLoadWorker | None = None
        self._is_generating: bool = False

        self.target_context = TargetContext()
        self.append_frame_map_source: str = "none"
        self.append_frame_map_change_reason: str = ""
        self.upload_mode: str = "new"

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

        self.frames_model = FrameListModel(self)
        self.frame_selection_widget = FrameSelectionWidget(self)
        self.frame_selection_widget.set_model(self.frames_model)
        self.frame_selection_widget.setMaximumHeight(self.frame_selection_widget.sizeHint().height())

        self.frame_selection_widget.prev_requested.connect(self.on_prev_clicked)
        self.frame_selection_widget.next_requested.connect(self.on_next_clicked)
        self.frame_selection_widget.add_requested.connect(self.on_add_frame_clicked)
        self.frame_selection_widget.remove_requested.connect(self.on_remove_frame_clicked)
        self.frame_selection_widget.edit_requested.connect(self.on_edit_frame_clicked)
        self.frame_selection_widget.selection_changed.connect(self.on_frame_list_row_changed)

        self.frames_model.dataChanged.connect(lambda *args: self.update_navigation_label())
        self.frames_model.modelReset.connect(self.update_navigation_label)
        self.frames_model.rowsInserted.connect(lambda *args: self.update_navigation_label())
        self.frames_model.rowsRemoved.connect(lambda *args: self.update_navigation_label())

        self.status_label = QLabel("Ready.")
        if not SLOWPICS_AVAILABLE:
            self.status_label.setText("SlowPics components not available.")

        self.output_url_lineedit = LineEdit("")
        self.output_url_lineedit.setReadOnly(True)
        self.output_url_lineedit.setPlaceholderText("Created comparison URL will appear here")

        self.copy_url_button = PushButton("📋", self, clicked=self.on_copy_url_clicked)
        self.copy_url_button.setFixedSize(40, self.output_url_lineedit.sizeHint().height())

        self.primary_upload_button = PushButton("Upload", self, clicked=self.on_primary_upload_clicked)
        self.primary_upload_button.setEnabled(SLOWPICS_AVAILABLE)

        self.progress_bar = ProgressBar(self, value=0)

        self.send_to_slowpics_button = PushButton(
            "Send Frame List to SlowPics Comps Tab", self, clicked=self.on_send_to_slowpics_clicked
        )

        self.append_controls_widget = AppendControlsWidget(self)
        self.upload_mode_combobox = QComboBox(self)
        self.upload_mode_combobox.addItems(["New Comparison", "Append to Existing"])
        self.upload_mode_combobox.currentIndexChanged.connect(self.on_upload_mode_changed)

        self.append_controls_widget.target_load_widget.load_requested.connect(
            lambda url: self.on_load_target_comp_clicked()
        )
        self.append_controls_widget.target_load_widget.apply_manual_frames_requested.connect(
            self.on_apply_manual_target_frames_clicked
        )
        self.append_controls_widget.append_outputs_list.itemSelectionChanged.connect(
            self._update_append_controls
        )

        # Storage controls
        self.load_button = PushButton("Load Offsets", self, clicked=self.on_load_clicked)
        self.save_button = PushButton("Save Offsets", self, clicked=self.on_save_clicked)

        VBoxLayout(self.vlayout, [
            self.gen_status_label,
            self.generate_from_slowpics_button,

            self.get_separator(),

            self.frame_selection_widget,

            self.get_separator(),

            self.offset_header_label,
            self.offset_container,

            self.get_separator(),

            HBoxLayout([
                QLabel("Mode:"),
                self.upload_mode_combobox,
            ]),
            self.append_controls_widget,
            self.primary_upload_button,
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

        self.upload_mode_combobox.setCurrentIndex(0)
        self._update_append_controls()

    def init_outputs(self) -> None:
        if not self.main.outputs:
            if hasattr(self, "append_controls_widget"):
                self.append_controls_widget.append_outputs_list.clear()
            if hasattr(self, "primary_upload_button"):
                self._update_append_controls()
            return

        while self.offset_layout.count():
            child = self.offset_layout.takeAt(0)
            if child.widget():
                child.widget().deleteLater()

        self.offset_spinboxes.clear()
        self.output_labels.clear()
        self.append_controls_widget.append_outputs_list.clear()

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

            list_item = QListWidgetItem(output.name)
            list_item.setData(Qt.ItemDataRole.UserRole, i)
            self.append_controls_widget.append_outputs_list.addItem(list_item)
            list_item.setSelected(i == self.main.current_output.index)

        self.update_highlighting()
        self._update_append_controls()

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
        selected_frames = self.frames_model.frames()
        if not selected_frames:
            for spinbox in self.offset_spinboxes.values():
                spinbox.setEnabled(False)
            return

        current_frame = selected_frames[self.current_frame_index]
        self.offset_header_label.setText(f"Offsets for Frame {current_frame}:")

        current_offsets = self._get_offsets_for_frame(current_frame)

        for i, spinbox in self.offset_spinboxes.items():
            spinbox.blockSignals(True)
            spinbox.setValue(current_offsets.get(i, 0))
            spinbox.setEnabled(True)
            spinbox.blockSignals(False)

    def _set_append_status(self, message: str, is_error: bool = False, is_ready: bool = False) -> None:
        self.status_label.setText(message)
        if is_error:
            self.status_label.setStyleSheet("font-style: italic; color: #ff6b6b;")
            return
        if is_ready:
            self.status_label.setStyleSheet("font-style: italic; color: #4facfe;")
            return
        self.status_label.setStyleSheet("")

    def _reset_target_context(self, info_label: str) -> None:
        self.target_context.reset()
        self.append_frame_map_source = "none"
        self.append_frame_map_change_reason = ""
        self.append_controls_widget.target_load_widget.set_status(info_label)

    def _cleanup_thread_pair(self, thread_attr: str, worker_attr: str) -> None:
        thread = getattr(self, thread_attr, None)
        if thread is not None:
            with contextlib.suppress(RuntimeError):
                thread.deleteLater()
            setattr(self, thread_attr, None)

        worker = getattr(self, worker_attr, None)
        if worker is not None:
            with contextlib.suppress(RuntimeError):
                worker.deleteLater()
            setattr(self, worker_attr, None)

    def _handle_upload_progress_common(
        self,
        kind: str,
        curr: int | None,
        total: int | None,
        *,
        show_message: bool
    ) -> bool:
        if kind in ("extract", "upload", "search"):
            info = f" {curr or '?'}/{total or '?'}" if curr or total else ""
            self._set_append_status(f"{kind.capitalize()}{info}...")
            return True

        if kind.startswith("https://"):
            self.output_url_lineedit.setText(kind)
            self._set_append_status("Upload complete. URL copied to clipboard.", is_ready=True)
            self.main.clipboard.setText(kind)
            if show_message:
                self.main.show_message(f"Uploaded: {kind}")
            return True

        if kind.startswith("Error:"):
            self._set_append_status(kind, is_error=True)
            return True

        return False

    def on_upload_mode_changed(self, index: int) -> None:
        self.upload_mode = "new" if index == 0 else "append"
        self._update_append_controls()

    def on_primary_upload_clicked(self) -> None:
        if self.upload_mode == "append":
            self.on_append_sources_clicked()
            return
        self.on_upload_clicked()

    def _append_target_loaded(self) -> bool:
        return bool(
            self.target_context.set_key
            and self.target_context.edit_dto is not None
            and self.target_context.comparison_count > 0
        )

    def _get_slowpics_tab(self) -> Any:
        try:
            slowpics_plugin = self.main.plugins["dev.setsugen.comp"]
            slowpics_plugin.first_load()
            return slowpics_plugin.main_tab
        except KeyError as exc:
            raise RuntimeError("SlowPics plugin not found") from exc

    def _sanitize_slowpics_tmdb_data(self, slowpics: Any) -> None:
        tmdb_data = getattr(slowpics, "tmdb_data", None)
        if not isinstance(tmdb_data, dict):
            return

        for key, value in list(tmdb_data.items()):
            if not isinstance(value, dict):
                tmdb_data[key] = {}

    def _safe_slowpics_keyword_replace(self, slowpics: Any, keyword: str) -> str | None:
        try:
            replacement = slowpics._get_replace_option(keyword)
        except Exception:
            if keyword == "{video_nodes}":
                return " vs ".join(output.name for output in self.main.outputs)
            return ""

        if replacement is None:
            return None

        return str(replacement)

    def _generate_collection_name_from_slowpics(self, slowpics: Any | None = None) -> str:
        if slowpics is None:
            slowpics = self._get_slowpics_tab()

        with contextlib.suppress(Exception):
            slowpics._do_tmdb_id_request()

        self._sanitize_slowpics_tmdb_data(slowpics)

        collection_name = str(slowpics.collection_name_lineedit.text()).strip()
        for match in set(re.findall(r"\{[a-z0-9_-]+\}", collection_name, flags=re.IGNORECASE)):
            replacement = self._safe_slowpics_keyword_replace(slowpics, match)
            if replacement is not None:
                collection_name = collection_name.replace(match, replacement)

        if not collection_name:
            collection_name = str(slowpics.settings.DEFAULT_COLLECTION_NAME or "").strip()

        if not collection_name:
            raise ValueError("You have to put a collection name!")
        if len(collection_name) <= 1:
            raise ValueError("Your collection name is too short!")

        if not getattr(self.main, "script_path", None):
            raise ValueError("Could not resolve script name for collection naming.")

        try:
            return collection_name.format(script_name=self.main.script_path.stem)
        except Exception as exc:
            raise ValueError(f"Invalid collection name template: {exc}") from exc

    def _mark_external_frame_map(self, reason: str = "local frame list changed") -> None:
        if self._append_target_loaded():
            self.append_frame_map_source = "external"
            self.target_context.parse_complete = False
            self.append_frame_map_change_reason = reason

    def _append_readiness(self) -> tuple[bool, str]:
        if not SLOWPICS_AVAILABLE:
            return False, "SlowPics components not available."
        if self.append_thread is not None:
            return False, "Clone append in progress..."
        if self.upload_thread is not None:
            return False, "Upload in progress..."
        if not self.main.outputs:
            return False, "Load local sources first."
        if not self._append_target_loaded():
            return False, "Load target comparison."

        selected_frames = self.frames_model.frames()
        if not selected_frames:
            return False, "Provide frame map for target comparisons."
        if self.append_frame_map_source not in {"target", "manual"}:
            reason = self.append_frame_map_change_reason or "local edit"
            return (
                False,
                (
                    f"Frame map mismatch ({len(selected_frames)}/"
                    f"{self.target_context.comparison_count}, {reason}). "
                    "Reload or press Apply."
                ),
            )
        if len(selected_frames) != self.target_context.comparison_count:
            return (
                False,
                f"Frame map rows {len(selected_frames)}/{self.target_context.comparison_count}.",
            )
        if not self._selected_append_output_indices():
            return False, "Select at least one source."
        return True, "Ready to upload."

    def _update_append_controls(self) -> None:
        if not hasattr(self, "primary_upload_button") or not hasattr(self, "append_controls_widget"):
            return

        append_mode = self.upload_mode == "append"
        self.append_controls_widget.setVisible(append_mode)

        any_upload_in_progress = self.append_thread is not None or self.upload_thread is not None
        self.upload_mode_combobox.setEnabled(not any_upload_in_progress)

        if not append_mode:
            selected_frames = self.frames_model.frames()
            ready_new_upload = (
                SLOWPICS_AVAILABLE
                and not any_upload_in_progress
                and bool(self.main.outputs)
                and bool(selected_frames)
            )
            self.primary_upload_button.setEnabled(ready_new_upload)
            if not any_upload_in_progress:
                if not SLOWPICS_AVAILABLE:
                    self._set_append_status("SlowPics components not available.")
                elif not self.main.outputs:
                    self._set_append_status("No outputs loaded.")
                elif not selected_frames:
                    self._set_append_status("No frames selected for upload.")
                else:
                    self._set_append_status("Ready to upload.", is_ready=True)
            return

        target_loaded = self._append_target_loaded()
        outputs_ready = bool(self.main.outputs)

        tlw = self.append_controls_widget.target_load_widget
        tlw.load_target_button.setEnabled(SLOWPICS_AVAILABLE and not any_upload_in_progress)
        tlw.apply_manual_target_frames_button.setEnabled(target_loaded and not any_upload_in_progress)

        list_enabled = target_loaded and outputs_ready and not any_upload_in_progress
        self.append_controls_widget.append_outputs_list.setEnabled(list_enabled)

        ready, reason = self._append_readiness()
        self.primary_upload_button.setEnabled(ready)
        if not any_upload_in_progress:
            self._set_append_status(reason, is_ready=ready)

    def on_offset_changed(self, index: int, value: int) -> None:
        selected_frames = self.frames_model.frames()
        if not selected_frames:
            return

        current_frame = selected_frames[self.current_frame_index]

        if not isinstance(self.frame_offsets.get(current_frame), dict):
            self.frame_offsets[current_frame] = {}

        self.frame_offsets[current_frame][index] = value

        if self.main.current_output.index != index:
            self.main.switch_output(index)
        else:
            self.navigate_to_current_frame()

    def _build_search_config_from_slowpics(
        self, slowpics: Any, uuid: str
    ) -> tuple[FindFramesWorkerConfiguration, int | None]:
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
            raise ValueError("No outputs loaded")

        lens = {out.prepared.clip.num_frames for out in self.main.outputs}
        if len(lens) != 1:
            logging.warning("Outputs don't all have the same length!")

        lens_n = min(lens)
        end_frame = min(lens_n, end_frame)

        if end_frame <= start_frame:
            raise ValueError("Invalid frame range")

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
        return config, seed

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

            uuid = str(uuid4())
            try:
                config, seed = self._build_search_config_from_slowpics(slowpics, uuid)
            except ValueError as e:
                self._set_status(str(e), is_error=True)
                return

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
        self._cleanup_thread_pair("search_thread", "search_worker")
        self._is_generating = False

    def on_frames_generated(self, uuid: str, config: FindFramesWorkerConfiguration) -> None:
        self.frames_model.set_frames(sorted({int(f) for f in config.samples}))
        self.current_frame_index = 0
        self._mark_external_frame_map("generated frame list")

        self.gen_status_label.setText(f"Loaded {self.frames_model.rowCount()} frames.")
        self.gen_status_label.setStyleSheet("")
        self.generate_from_slowpics_button.setEnabled(True)

        if self.frames_model.frames():
            self.navigate_to_current_frame()
        self._update_append_controls()

    def update_navigation_label(self) -> None:
        selected_frames = self.frames_model.frames()
        if not selected_frames:
            self.frame_selection_widget.set_position_text("No frames selected")
            return

        if self.current_frame_index >= len(selected_frames):
            self.current_frame_index = len(selected_frames) - 1
        if self.current_frame_index < 0:
            self.current_frame_index = 0

        current_frame = selected_frames[self.current_frame_index]
        self.frame_selection_widget.set_position_text(
            f"Frame {self.current_frame_index + 1}/{len(selected_frames)}: {current_frame}"
        )
        self.frame_selection_widget.select_row(self.current_frame_index)

    def on_frame_list_row_changed(self, row: int) -> None:
        if 0 <= row < self.frames_model.rowCount():
            self.current_frame_index = row
            self.navigate_to_current_frame()

    def on_prev_clicked(self) -> None:
        if self.frames_model.rowCount() > 0 and self.current_frame_index > 0:
            self.current_frame_index -= 1
            self.navigate_to_current_frame()

    def on_next_clicked(self) -> None:
        if self.frames_model.rowCount() > 0 and self.current_frame_index < self.frames_model.rowCount() - 1:
            self.current_frame_index += 1
            self.navigate_to_current_frame()

    def navigate_to_current_frame(self) -> None:
        selected_frames = self.frames_model.frames()
        if not selected_frames or not self.main.outputs:
            return

        if self.current_frame_index < 0 or self.current_frame_index >= len(selected_frames):
            return

        base_frame = selected_frames[self.current_frame_index]

        current_output_index = self.main.current_output.index
        current_offsets = self._get_offsets_for_frame(base_frame)
        offset = current_offsets.get(current_output_index, 0)

        target_frame = base_frame + offset
        target_frame = self._clamp_frame_to_range(target_frame)

        self.main.switch_frame(Frame(target_frame))

        self.update_navigation_label()
        self.update_offset_controls()

    def on_current_output_changed(self, index: int, prev_index: int) -> None:
        self.update_highlighting()
        if self.frames_model.frames():
            self.navigate_to_current_frame()

    def get_offset_adjusted_frames(self) -> list[list[int]]:
        selected_frames = self.frames_model.frames()
        if not self.main.outputs or not selected_frames:
            return []

        frames_per_output = []
        for i, output in enumerate(self.main.outputs):
            max_frame = output.total_frames - 1

            adjusted = []
            for base_frame in selected_frames:
                base_frame = int(base_frame)

                current_offsets = self._get_offsets_for_frame(base_frame)
                offset = int(current_offsets.get(i, 0))

                target = int(base_frame + offset)
                target = max(0, min(target, max_frame))
                adjusted.append(int(target))

            frames_per_output.append(adjusted)

        return frames_per_output

    def _selected_append_output_indices(self) -> list[int]:
        return [
            int(item.data(Qt.ItemDataRole.UserRole))
            for item in self.append_controls_widget.append_outputs_list.selectedItems()
        ]

    def _load_target_dto(self) -> None:
        pass

    def on_load_target_comp_clicked(self) -> None:
        if not SLOWPICS_AVAILABLE:
            self._set_append_status("SlowPics components not available.", is_error=True)
            self._update_append_controls()
            return

        tlw = self.append_controls_widget.target_load_widget
        target_text = tlw.target_url_lineedit.text().strip()
        view_path = parse_view_path(target_text)
        if not view_path:
            self._set_append_status("Invalid target URL/key.", is_error=True)
            self._update_append_controls()
            return

        self.load_target_thread = QThread()
        self.load_target_worker = TargetLoadWorker()
        self.load_target_worker.moveToThread(self.load_target_thread)

        try:
            slowpics = self._get_slowpics_tab()
        except RuntimeError as exc:
            self._set_append_status(str(exc), is_error=True)
            self._update_append_controls()
            return
        cookies_path = Path(str(slowpics.settings.cookies_path))
        frame_type = bool(slowpics.settings.frame_type_enabled)

        conf = TargetLoadWorkerConfiguration(
            uuid=str(uuid4()),
            target_text=target_text,
            view_path=view_path,
            cookies_path=cookies_path,
            frame_type=frame_type
        )

        self.load_target_thread.started.connect(partial(self.load_target_worker.run, conf))
        self.load_target_worker.finished.connect(self.on_target_load_finished)
        self.load_target_worker.error.connect(self.on_target_load_error)
        self.load_target_thread.finished.connect(self._cleanup_load_target_thread)

        self._set_append_status("Fetching target comparison data...", is_ready=False)
        self.load_target_thread.start()

    def _cleanup_load_target_thread(self) -> None:
        self._cleanup_thread_pair("load_target_thread", "load_target_worker")
        self._update_append_controls()

    def on_target_load_error(self, uuid: str, msg: str) -> None:
        self._set_append_status(msg, is_error=True)

        if self.load_target_thread is not None:
            self.load_target_thread.quit()

    def on_target_load_finished(self, uuid: str, result: dict[str, Any]) -> None:
        if self.load_target_thread is not None:
            self.load_target_thread.quit()

        collection = result["collection"]
        set_key = result["set_key"]
        edit_dto = result["edit_dto"]
        post_mode = result["post_mode"]

        tlw = self.append_controls_widget.target_load_widget
        target_text = tlw.target_url_lineedit.text().strip()
        view_path = parse_view_path(target_text)

        comparisons = collection.get("comparisons", [])
        if not isinstance(comparisons, list) or not comparisons:
            self._set_append_status("Target comparison has no rows/frames.", is_error=True)
            self._update_append_controls()
            return

        comp_names: list[str] = []
        invalid_rows: list[int] = []
        for row, comp in enumerate(comparisons):
            if not isinstance(comp, dict):
                comp_names.append("")
                invalid_rows.append(row)
                continue
            comp_names.append(str(comp.get("name", "")))

        parsed_frames, failed = parse_frames_from_comp_names(comp_names)
        if invalid_rows:
            failed = sorted(set(failed + invalid_rows))

        parsed_comp_key = parse_comp_key(target_text)
        self.target_context.comp_key = parsed_comp_key
        self.target_context.set_key = set_key
        self.target_context.view_path = view_path
        self.target_context.post_mode = post_mode
        resolved_collection_name = str(collection.get("name", "")).strip()
        if not resolved_collection_name and isinstance(edit_dto, dict):
            resolved_collection_name = str(edit_dto.get("name", "")).strip()
        self.target_context.collection_name = resolved_collection_name
        self.target_context.comparison_count = len(comparisons)
        self.target_context.edit_dto = edit_dto
        self.target_context.frame_parse_failed_indices = failed

        if failed:
            self.target_context.parse_complete = False
            self.append_frame_map_source = "none"
            self.frames_model.clear()
            self.current_frame_index = 0

            failed_preview = ", ".join(str(i + 1) for i in failed[:8])
            suffix = "..." if len(failed) > 8 else ""
            self.append_controls_widget.target_load_widget.set_status(
                f"Loaded target ({len(comparisons)} rows). "
                f"Unparsed rows: {failed_preview}{suffix}"
            )
            self._set_append_status(
                "Manual frame map required.",
                is_error=True
            )
            self._update_append_controls()
            return

        self.target_context.parse_complete = True
        self.append_frame_map_source = "target"
        self.frames_model.set_frames(parsed_frames)
        self.current_frame_index = 0
        if self.frames_model.frames():
            self.frame_selection_widget.select_row(0)
            self.navigate_to_current_frame()

        self.append_controls_widget.target_load_widget.set_status(
            f"Loaded target ({len(comparisons)} rows). Frame map parsed."
        )
        self._set_append_status("Target loaded and frame map parsed.", is_ready=True)
        self._update_append_controls()

    def on_apply_manual_target_frames_clicked(self, explicit_raw: str = "") -> None:
        if not self.target_context.comp_key:
            self._set_append_status("Load a target comparison first.", is_error=True)
            return

        tlw = self.append_controls_widget.target_load_widget
        raw = explicit_raw.strip() if explicit_raw else tlw.manual_target_frames_lineedit.text().strip()
        if not raw:
            self._set_append_status("Manual frame input is empty.", is_error=True)
            return

        try:
            manual_frames = [int(x.strip()) for x in raw.split(",") if x.strip()]
        except ValueError:
            self._set_append_status("Manual frames must be comma-separated integers.", is_error=True)
            return

        if len(manual_frames) != self.target_context.comparison_count:
            self._set_append_status(
                f"Manual frame count mismatch: got {len(manual_frames)}, "
                f"expected {self.target_context.comparison_count}.",
                is_error=True
            )
            return

        self.frames_model.set_frames(manual_frames)
        self.current_frame_index = 0
        self.target_context.parse_complete = False
        self.target_context.frame_parse_failed_indices = []
        self.append_frame_map_source = "manual"

        if self.frames_model.frames():
            self.frame_selection_widget.select_row(0)
            self.navigate_to_current_frame()

        self._set_append_status(
            f"Manual frame map applied ({len(manual_frames)} rows).",
            is_ready=True
        )
        self._update_append_controls()

    def _cleanup_append_thread(self) -> None:
        self._cleanup_thread_pair("append_thread", "append_worker")
        self._update_append_controls()

    def _cleanup_upload_thread(self) -> None:
        self._cleanup_thread_pair("upload_thread", "upload_worker")
        self._update_append_controls()

    def on_append_progress(self, uuid: str, kind: str, curr: int | None, total: int | None) -> None:
        if self._handle_upload_progress_common(kind, curr, total, show_message=False):
            return

        self._set_append_status(kind)

    def on_append_sources_clicked(self) -> None:
        ready, reason = self._append_readiness()
        if not ready:
            self._set_append_status(reason, is_error=True)
            return

        output_indices = self._selected_append_output_indices()

        try:
            slowpics = self._get_slowpics_tab()
        except RuntimeError as exc:
            self._set_append_status(str(exc), is_error=True)
            return

        cookies_path = Path(str(slowpics.settings.cookies_path))
        frame_type = bool(slowpics.settings.frame_type_enabled)
        post_mode = self.target_context.post_mode or "clone"

        try:
            target_name = str(self.target_context.collection_name or "").strip()
            if not target_name and isinstance(self.target_context.edit_dto, dict):
                target_name = str(self.target_context.edit_dto.get("name", "")).strip()
            selected_names = [
                str(self.main.outputs[idx].name).strip()
                for idx in output_indices
                if 0 <= idx < len(self.main.outputs) and str(self.main.outputs[idx].name).strip()
            ]
            fallback_name = target_name if target_name else self._generate_collection_name_from_slowpics()
            generated_collection_name = build_append_collection_name(target_name, selected_names, fallback_name)
        except Exception as exc:
            self._set_append_status(str(exc), is_error=True)
            return

        if frame_type:
            has_nonstandard_existing_names = False
            if not isinstance(self.target_context.edit_dto, dict):
                self._set_append_status("Target payload is invalid.", is_error=True)
                return

            comparisons = self.target_context.edit_dto.get("comparisons", [])
            if isinstance(comparisons, list):
                for comparison in comparisons:
                    if not isinstance(comparison, dict):
                        continue
                    images = comparison.get("images", [])
                    if not isinstance(images, list):
                        continue
                    for image in images:
                        if not isinstance(image, dict):
                            continue
                        image_name = str(image.get("name", ""))
                        if not re.match(r"^\([IBP?]\)\s+.+", image_name):
                            has_nonstandard_existing_names = True
                            break
                    if has_nonstandard_existing_names:
                        break

            if has_nonstandard_existing_names:
                QMessageBox.information(
                    self,
                    "Existing Name Normalization",
                    (
                        "Some existing source names do not follow the built-in `(I/P/B) Name` pattern.\n\n"
                        "Existing source names will be kept unchanged because this plugin does not have enough "
                        "source metadata to safely normalize old columns."
                    ),
                )

        normalize_comparison_names = True
        if not self.target_context.parse_complete:
            answer = QMessageBox.question(
                self,
                "Auto Normalize Names?",
                (
                    "Frame numbers were manually provided, so normalization may not match original naming intent.\n\n"
                    "Apply built-in style comparison names anyway?"
                ),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Yes
            )
            if answer == QMessageBox.StandardButton.Cancel:
                self._set_append_status("Clone append canceled.")
                return
            normalize_comparison_names = answer == QMessageBox.StandardButton.Yes

        if self.append_thread is not None:
            self._set_append_status("Clone append already in progress.", is_error=True)
            return

        config = AppendSourcesConfiguration(
            uuid=str(uuid4()),
            target_key=self.target_context.set_key,
            post_mode=post_mode,
            edit_dto=self.target_context.edit_dto,
            base_frames=[int(f) for f in self.frames_model.frames()],
            output_indices=output_indices,
            outputs=[self.main.outputs[i] for i in output_indices],
            frame_offsets=normalize_frame_offsets_state(self.frame_offsets),
            frame_type=frame_type,
            cookies_path=cookies_path,
            main=self.main,
            normalize_comparison_names=normalize_comparison_names,
            target_collection_name=self.target_context.collection_name,
            generated_collection_name=generated_collection_name,
            expected_comparison_count=self.target_context.comparison_count,
        )

        self.append_thread = QThread()
        self.append_worker = AppendSourcesWorker()
        self.append_worker.moveToThread(self.append_thread)

        self.append_thread.started.connect(partial(self.append_worker.run, config))
        self.append_worker.progress_bar.connect(
            lambda uid, val: self.progress_bar.setValue(val) if uid == config.uuid else None
        )
        self.append_worker.progress_status.connect(
            lambda uid, kind, curr, total:
                self.on_append_progress(uid, kind, curr, total) if uid == config.uuid else None
        )
        self.append_worker.finished.connect(self.append_thread.quit)
        self.append_thread.finished.connect(self._cleanup_append_thread)

        self._update_append_controls()
        self.progress_bar.setValue(0)
        self._set_append_status("Uploading...")
        self.append_thread.start()

    def on_upload_clicked(self) -> None:
        if self.upload_thread is not None or self.append_thread is not None:
            self._set_append_status("Upload already in progress.", is_error=True)
            return

        selected_frames = self.frames_model.frames()
        if not selected_frames:
            self._set_append_status("No frames selected for upload.", is_error=True)
            return

        if not SLOWPICS_AVAILABLE:
            self._set_append_status("SlowPics components not available.", is_error=True)
            return

        if not self.main.outputs:
            self._set_append_status("No outputs loaded.", is_error=True)
            return

        try:
            try:
                slowpics = self._get_slowpics_tab()
            except RuntimeError as exc:
                self._set_append_status(str(exc), is_error=True)
                return

            frames_per_output = self.get_offset_adjusted_frames()
            uuid = str(uuid4())

            try:
                generated_collection_name = self._generate_collection_name_from_slowpics(slowpics)
            except Exception as e:
                self._set_append_status(f"Error generating name: {e}", is_error=True)
                return

            original_collection_text = str(slowpics.collection_name_lineedit.text())
            try:
                slowpics.collection_name_lineedit.setText(generated_collection_name)
                dummy_frames = [Frame(f) for f in selected_frames]
                base_config = slowpics.get_slowpics_conf(uuid, dummy_frames)
            except ValueError as e:
                self._set_append_status(str(e), is_error=True)
                return
            except Exception as e:
                logging.exception("Error building SlowPics upload config")
                self._set_append_status(f"Error getting config: {e}", is_error=True)
                traceback.print_exc()
                return
            finally:
                slowpics.collection_name_lineedit.setText(original_collection_text)

            config = base_config._replace(frames=frames_per_output)

            self.upload_thread = QThread()
            self.upload_worker = Worker()
            self.upload_worker.moveToThread(self.upload_thread)

            self.upload_thread.started.connect(partial(self.upload_worker.run, config))
            self.upload_worker.finished.connect(self.upload_thread.quit)
            self.upload_thread.finished.connect(self._cleanup_upload_thread)

            self.upload_worker.progress_bar.connect(
                lambda uid, val: self.progress_bar.setValue(val) if uid == uuid else None
            )
            self.upload_worker.progress_status.connect(
                lambda uid, kind, curr, total: self.on_upload_progress(uid, kind, curr, total) if uid == uuid else None
            )

            self.progress_bar.setValue(0)
            self.upload_thread.start()

            self._update_append_controls()
            self._set_append_status("Uploading...")

        except Exception as e:
            logging.exception("Upload flow failed")
            self._set_append_status(f"Error: {e}", is_error=True)
            traceback.print_exc()

    def on_upload_progress(self, uuid: str, kind: str, curr: int | None, total: int | None) -> None:
        self._handle_upload_progress_common(kind, curr, total, show_message=True)

    def on_copy_url_clicked(self) -> None:
        url = self.output_url_lineedit.text()
        if url:
            self.main.clipboard.setText(url)
            self.status_label.setText("URL copied!")

    def on_send_to_slowpics_clicked(self) -> None:
        selected_frames = self.frames_model.frames()
        if not selected_frames:
            self.status_label.setText("No frames to send")
            return

        try:
            slowpics = self.main.plugins["dev.setsugen.comp"]

            frames_str = ",".join(str(f) for f in selected_frames)
            slowpics.main_tab.manual_frames_lineedit.setText(frames_str)
            self.main.plugins_tab.setCurrentIndex(slowpics.index)

            self.status_label.setText(f"Sent {len(selected_frames)} frames to SlowPics")
        except KeyError:
            self.status_label.setText("SlowPics plugin not found")
        except Exception as e:
            self.status_label.setText(f"Error: {e}")

    def on_save_clicked(self) -> None:
        selected_frames = self.frames_model.frames()
        if not selected_frames:
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
                "selected_frames": [int(f) for f in selected_frames],
                "offsets": serialize_frame_offsets(self.frame_offsets, idx_to_name)
            }

            with open(path, "w", encoding="utf-8") as f:
                json.dump(export_data, f, indent=2)
                f.flush()

            self.status_label.setText(f"Saved {len(selected_frames)} frames to {Path(path).name}")
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

            if "selected_frames" in data:
                self.frames_model.set_frames(sorted(data["selected_frames"]))

            name_to_idx = {out.name: i for i, out in enumerate(self.main.outputs)}
            self.frame_offsets = deserialize_frame_offsets(data.get("offsets", {}), name_to_idx)

            self._mark_external_frame_map("loaded frame list from file")

            selected_frames = self.frames_model.frames()
            if selected_frames:
                self.current_frame_index = 0
                self.frame_selection_widget.select_row(0)
                self.navigate_to_current_frame()

            self.gen_status_label.setText(f"Loaded {len(selected_frames)} frames from {Path(path).name}")
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

            selected_frames = self.frames_model.frames()
            if frame not in selected_frames:
                new_frames = [*selected_frames, frame]
                self.frames_model.set_frames(sorted(new_frames))
                self._mark_external_frame_map("added frame")

                try:
                    idx = self.frames_model.frames().index(frame)
                    self.current_frame_index = idx
                    self.frame_selection_widget.select_row(idx)
                    self.navigate_to_current_frame()
                except ValueError:
                    pass
            else:
                self.status_label.setText(f"Frame {frame} already exists")

    def on_remove_frame_clicked(self) -> None:
        selected_frames = self.frames_model.frames()
        if not selected_frames:
            return

        row = self.frame_selection_widget.current_row()
        if row < 0:
            return

        frame = selected_frames[row]

        new_frames = list(selected_frames)
        del new_frames[row]
        self.frames_model.set_frames(new_frames)
        self._mark_external_frame_map("removed frame")

        if frame in self.frame_offsets:
            del self.frame_offsets[frame]

        selected_frames = self.frames_model.frames()
        if selected_frames:
            self.current_frame_index = min(row, len(selected_frames) - 1)
        else:
            self.current_frame_index = 0

        if selected_frames:
            self.frame_selection_widget.select_row(self.current_frame_index)
            self.navigate_to_current_frame()

    def on_edit_frame_clicked(self) -> None:
        selected_frames = self.frames_model.frames()
        if not selected_frames:
            return

        row = self.frame_selection_widget.current_row()
        if row < 0:
            return

        old_frame = selected_frames[row]

        new_frame, ok = QInputDialog.getInt(
            self, "Edit Frame", "Enter new frame number:", old_frame, 0
        )

        if ok and new_frame != old_frame:
            original_new_frame = new_frame
            new_frame = self._clamp_frame_to_range(new_frame)

            if new_frame != original_new_frame:
                self._set_status(f"Frame {original_new_frame} out of range, adjusted to {new_frame}")

            if new_frame in selected_frames:
                self.status_label.setText(f"Frame {new_frame} already exists")
                return

            new_frames = list(selected_frames)
            new_frames[row] = new_frame
            self.frames_model.set_frames(sorted(new_frames))
            self._mark_external_frame_map("edited frame")

            if old_frame in self.frame_offsets:
                self.frame_offsets[new_frame] = self.frame_offsets.pop(old_frame)

            selected_frames = self.frames_model.frames()
            try:
                idx = selected_frames.index(new_frame)
                self.current_frame_index = idx
                self.frame_selection_widget.select_row(idx)
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

    def _get_offsets_for_frame(self, frame: int) -> dict[int, int]:
        raw = self.frame_offsets.get(int(frame))
        if not isinstance(raw, dict):
            return {}
        return raw

    def _set_status(self, message: str, is_error: bool = False) -> None:
        self.gen_status_label.setText(message)
        if is_error:
            self.gen_status_label.setStyleSheet("font-style: italic; color: #ff6b6b;")
        else:
            self.gen_status_label.setStyleSheet("font-style: italic; color: #888;")

    def __getstate__(self) -> dict[str, Any]:
        return super().__getstate__() | {
            "frame_offsets": self.frame_offsets,
            "selected_frames": self.frames_model.frames(),
        }

    def __setstate__(self) -> None:
        state = self.settings.local

        if "frame_offsets" in state:
            self.frame_offsets = normalize_frame_offsets_state(state["frame_offsets"])

        if "selected_frames" in state:
            self.frames_model.set_frames(state["selected_frames"])

        # Always default to "New Comparison" when the plugin/session is restored.
        self.upload_mode = "new"
        if hasattr(self, "upload_mode_combobox"):
            self.upload_mode_combobox.blockSignals(True)
            self.upload_mode_combobox.setCurrentIndex(0)
            self.upload_mode_combobox.blockSignals(False)
        if hasattr(self, "append_controls_widget"):
            self._update_append_controls()
