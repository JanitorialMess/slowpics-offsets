from __future__ import annotations

import contextlib
import json
import re
from typing import Any


def extract_json_var(html: str, var_name: str) -> dict[str, Any]:
    match = re.search(rf"var\s+{re.escape(var_name)}\s*=\s*(\{{.*?\}});", html, re.DOTALL)
    if not match:
        raise ValueError(f"Could not find `{var_name}` in page")

    payload = json.loads(match.group(1))
    if not isinstance(payload, dict):
        raise ValueError(f"`{var_name}` payload is not an object")

    return payload


def parse_comp_key(text: str) -> str | None:
    candidate = text.strip()
    if not candidate:
        return None

    match = re.search(r"slow\.pics/[cs]/([A-Za-z0-9]+)", candidate)
    if match:
        return match.group(1)

    if re.fullmatch(r"[A-Za-z0-9]+", candidate):
        return candidate

    return None


def parse_view_path(text: str) -> str | None:
    candidate = text.strip()
    if not candidate:
        return None

    match = re.search(r"slow\.pics/([cs])/([A-Za-z0-9]+)", candidate)
    if match:
        return f"/{match.group(1)}/{match.group(2)}"

    if re.fullmatch(r"[A-Za-z0-9]+", candidate):
        return f"/c/{candidate}"

    return None


def parse_frames_from_comp_names(names: list[str]) -> tuple[list[int], list[int]]:
    frames: list[int] = []
    failed_indices: list[int] = []

    for idx, name in enumerate(names):
        match = re.search(r"/\s*(\d+)\s*$", name)
        if match:
            frames.append(int(match.group(1)))
        else:
            failed_indices.append(idx)

    return frames, failed_indices


def build_append_collection_name(target_name: str, source_names: list[str], fallback_name: str) -> str:
    if not target_name:
        return fallback_name

    result_name = target_name
    for source_name in source_names:
        if re.search(rf"(^| vs ){re.escape(source_name)}($| vs )", result_name):
            continue
        result_name = f"{result_name} vs {source_name}"

    return result_name


def normalize_frame_offsets_state(raw: Any) -> dict[int, dict[int, int]]:
    if not isinstance(raw, dict):
        return {}

    normalized: dict[int, dict[int, int]] = {}
    for frame_key, offsets in raw.items():
        try:
            frame_num = int(frame_key)
        except (TypeError, ValueError):
            continue

        if not isinstance(offsets, dict):
            continue

        offset_row: dict[int, int] = {}
        for out_key, offset in offsets.items():
            try:
                out_idx = int(out_key)
                offset_row[out_idx] = int(offset)
            except (TypeError, ValueError):
                continue

        normalized[frame_num] = offset_row

    return normalized


def serialize_frame_offsets(
    offsets: dict[int, dict[int, int]], idx_to_name: dict[int, str]
) -> dict[str, dict[str, int]]:
    serialized: dict[str, dict[str, int]] = {}
    for frame_num, frame_offsets in offsets.items():
        frame_data: dict[str, int] = {}
        for idx, offset in frame_offsets.items():
            if idx in idx_to_name:
                frame_data[idx_to_name[idx]] = int(offset)
        if frame_data:
            serialized[str(int(frame_num))] = frame_data
    return serialized


def deserialize_frame_offsets(
    raw_offsets: Any, name_to_idx: dict[str, int]
) -> dict[int, dict[int, int]]:
    if not isinstance(raw_offsets, dict):
        return {}

    deserialized: dict[int, dict[int, int]] = {}
    for frame_str, offsets in raw_offsets.items():
        try:
            frame_num = int(frame_str)
        except ValueError:
            continue

        if not isinstance(offsets, dict):
            continue

        frame_data: dict[int, int] = {}
        for out_name, offset in offsets.items():
            if out_name in name_to_idx:
                with contextlib.suppress(ValueError):
                    frame_data[name_to_idx[out_name]] = int(offset)

        deserialized[frame_num] = frame_data
    return deserialized
