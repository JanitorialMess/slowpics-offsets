from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypedDict

from vspreview.core import VideoOutput
from vspreview.main import MainWindow


class APIEndpoints:
    BASE = "https://slow.pics"


class SlowpicsImage(TypedDict, total=False):
    uuid: str | None
    name: str
    sortOrder: int


class SlowpicsComparison(TypedDict, total=False):
    uuid: str | None
    name: str
    sortOrder: int
    images: list[SlowpicsImage]


class SlowpicsFile(TypedDict, total=False):
    url: str
    name: str
    type: str


class SlowpicsCollectionDTO(TypedDict, total=False):
    key: str | None
    name: str | None
    public: bool
    hentai: bool
    optimizeImages: bool
    removeAfter: str | int | None
    canvasMode: str | None
    imageFit: str | None
    imagePosition: str | None
    tmdbId: str | int | dict[str, Any] | None
    metaCollection: str | dict[str, Any] | None
    tags: list[str | dict[str, Any]] | None
    comparisons: list[SlowpicsComparison]
    files: list[list[SlowpicsFile]]


@dataclass
class TargetContext:
    comp_key: str | None = None
    set_key: str | None = None
    view_path: str | None = None
    post_mode: str | None = None
    collection_name: str = ""
    comparison_count: int = 0
    parse_complete: bool = False
    edit_dto: dict[str, Any] | None = None
    frame_parse_failed_indices: list[int] = field(default_factory=list)

    def reset(self) -> None:
        self.comp_key = None
        self.set_key = None
        self.view_path = None
        self.post_mode = None
        self.collection_name = ""
        self.comparison_count = 0
        self.parse_complete = False
        self.edit_dto = None
        self.frame_parse_failed_indices.clear()


@dataclass
class AppendSourcesConfiguration:
    uuid: str
    target_key: str
    post_mode: str
    edit_dto: dict[str, Any]
    base_frames: list[int]
    output_indices: list[int]
    outputs: list[VideoOutput]
    frame_offsets: dict[int, dict[int, int]]
    frame_type: bool
    cookies_path: Path
    main: MainWindow
    reference_output: VideoOutput = field(init=False)
    normalize_comparison_names: bool = False
    target_collection_name: str = ""
    generated_collection_name: str = ""
    expected_comparison_count: int = 0

    def __post_init__(self) -> None:
        self.reference_output = self.main.current_output


@dataclass
class TargetLoadWorkerConfiguration:
    uuid: str
    target_text: str
    view_path: str
    cookies_path: Path
    frame_type: bool
