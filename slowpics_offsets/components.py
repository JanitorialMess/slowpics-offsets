from __future__ import annotations

from typing import Any

from PyQt6.QtCore import QAbstractListModel, QModelIndex, QObject, Qt, pyqtSignal
from PyQt6.QtWidgets import QAbstractItemView, QLabel, QListView, QListWidget, QSizePolicy, QWidget
from PyQt6.QtWidgets import (
    QVBoxLayout as QVBox,
)
from vspreview.core import HBoxLayout, LineEdit, PushButton


class FrameListModel(QAbstractListModel):
    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self._frames: list[int] = []

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802
        return len(self._frames)

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if not index.isValid() or not (0 <= index.row() < len(self._frames)):
            return None
        if role == Qt.ItemDataRole.DisplayRole:
            return str(self._frames[index.row()])
        return None

    def frames(self) -> list[int]:
        return list(self._frames)

    def set_frames(self, frames: list[int]) -> None:
        self.beginResetModel()
        self._frames = list(frames)
        self.endResetModel()

    def add_frame(self, frame: int) -> None:
        if frame in self._frames:
            return
        self.beginInsertRows(QModelIndex(), len(self._frames), len(self._frames))
        self._frames.append(frame)
        self.endInsertRows()

    def remove_frame(self, frame: int) -> None:
        try:
            idx = self._frames.index(frame)
            self.beginRemoveRows(QModelIndex(), idx, idx)
            self._frames.pop(idx)
            self.endRemoveRows()
        except ValueError:
            pass

    def edit_frame(self, old_frame: int, new_frame: int) -> None:
        try:
            idx = self._frames.index(old_frame)
            self._frames[idx] = new_frame
            model_idx = self.index(idx, 0)
            self.dataChanged.emit(model_idx, model_idx, [Qt.ItemDataRole.DisplayRole])
        except ValueError:
            pass

    def clear(self) -> None:
        self.beginResetModel()
        self._frames.clear()
        self.endResetModel()


class TargetLoadWidget(QWidget):
    load_requested = pyqtSignal(str)
    apply_manual_frames_requested = pyqtSignal(str)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.target_url_lineedit = LineEdit("")
        self.target_url_lineedit.setPlaceholderText("https://slow.pics/c/a1B2c3D4")

        self.load_target_button = PushButton("Load")
        self.load_target_button.setMinimumHeight(self.target_url_lineedit.sizeHint().height())

        self.target_info_label = QLabel("No target comparison loaded")
        self.target_info_label.setStyleSheet("font-style: italic; color: #888;")

        self.manual_target_frames_lineedit = LineEdit("")
        self.manual_target_frames_lineedit.setPlaceholderText("Optional frame map: 100, 250, 500")

        self.apply_manual_target_frames_button = PushButton("Apply")
        self.apply_manual_target_frames_button.setEnabled(False)
        self.apply_manual_target_frames_button.setMinimumHeight(self.manual_target_frames_lineedit.sizeHint().height())

        layout = QVBox(self)
        layout.setContentsMargins(0, 0, 0, 0)

        layout.addWidget(QLabel("Target Comparison"))
        layout.addLayout(HBoxLayout([self.target_url_lineedit, self.load_target_button]))
        layout.addWidget(self.target_info_label)
        layout.addWidget(QLabel("Frame Mapping (optional fallback)"))
        layout.addLayout(HBoxLayout([self.manual_target_frames_lineedit, self.apply_manual_target_frames_button]))

        self.load_target_button.clicked.connect(lambda: self.load_requested.emit(self.target_url_lineedit.text()))
        self.apply_manual_target_frames_button.clicked.connect(
            lambda: self.apply_manual_frames_requested.emit(self.manual_target_frames_lineedit.text())
        )

    def set_status(self, text: str, is_error: bool = False) -> None:
        self.target_info_label.setText(text)
        if is_error:
            self.target_info_label.setStyleSheet("font-style: italic; color: #ff6b6b;")
        else:
            self.target_info_label.setStyleSheet("font-style: italic; color: #888;")


class FrameSelectionWidget(QWidget):
    add_requested = pyqtSignal()
    remove_requested = pyqtSignal()
    edit_requested = pyqtSignal()
    prev_requested = pyqtSignal()
    next_requested = pyqtSignal()
    selection_changed = pyqtSignal(int)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)

        self.frame_list_label = QLabel("Selected Frames:")
        self.frame_list_label.setWordWrap(False)
        self.frame_list_label.setMaximumHeight(self.frame_list_label.sizeHint().height())
        self.frame_list_label.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)

        self.frame_list = QListView()
        self.frame_list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.frame_list.setMaximumHeight(150)

        self.prev_button = PushButton("◄ Prev")
        self.frame_position_label = QLabel("No frames selected")
        self.frame_position_label.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        self.next_button = PushButton("Next ►")

        self.add_button = PushButton("Add Frame")
        self.remove_button = PushButton("Remove Frame")
        self.edit_button = PushButton("Edit Frame")

        nav_row = HBoxLayout([self.prev_button, self.frame_position_label, self.next_button])
        nav_row.setContentsMargins(0, 0, 0, 0)
        nav_row.setSpacing(6)

        actions_row = HBoxLayout([self.add_button, self.remove_button, self.edit_button])
        actions_row.setContentsMargins(0, 0, 0, 0)
        actions_row.setSpacing(6)

        layout = QVBox(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        layout.addWidget(self.frame_list_label)
        layout.addWidget(self.frame_list)
        layout.addLayout(nav_row)
        layout.addLayout(actions_row)

        self.prev_button.clicked.connect(self.prev_requested)
        self.next_button.clicked.connect(self.next_requested)
        self.add_button.clicked.connect(self.add_requested)
        self.remove_button.clicked.connect(self.remove_requested)
        self.edit_button.clicked.connect(self.edit_requested)

    def set_model(self, model: QAbstractListModel) -> None:
        self.frame_list.setModel(model)
        self.frame_list.selectionModel().currentRowChanged.connect(
            lambda current, previous: self.selection_changed.emit(current.row())
        )

    def set_position_text(self, text: str) -> None:
        self.frame_position_label.setText(text)

    def current_row(self) -> int:
        return self.frame_list.currentIndex().row()

    def select_row(self, row: int) -> None:
        model = self.frame_list.model()
        if model:
            index = model.index(row, 0)
            self.frame_list.selectionModel().blockSignals(True)
            self.frame_list.setCurrentIndex(index)
            self.frame_list.selectionModel().blockSignals(False)


class AppendControlsWidget(QWidget):
    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)

        self.target_load_widget = TargetLoadWidget()

        self.append_outputs_list = QListWidget()
        self.append_outputs_list.setSelectionMode(QAbstractItemView.SelectionMode.MultiSelection)
        self.append_outputs_list.setMaximumHeight(120)

        layout = QVBox(self)
        layout.setContentsMargins(0, 0, 0, 0)

        layout.addWidget(self.target_load_widget)
        layout.addWidget(QLabel("Sources to Append"))
        layout.addWidget(self.append_outputs_list)
