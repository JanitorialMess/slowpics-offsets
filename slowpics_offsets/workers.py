from __future__ import annotations

import json
import logging
import re
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

from PyQt6.QtCore import QObject, pyqtSignal
from requests import RequestException, Response, Session
from requests.utils import cookiejar_from_dict
from requests_toolbelt import MultipartEncoder
from vstools import vs

try:
    from vspreview.plugins.builtins.slowpics_comp.utils import (
        get_frame_time,
    )
except ImportError:
    get_frame_time = None

from .models import (
    APIEndpoints,
    AppendSourcesConfiguration,
    SlowpicsCollectionDTO,
    SlowpicsComparison,
    SlowpicsImage,
    TargetLoadWorkerConfiguration,
)
from .utils import (
    extract_json_var,
    get_append_slowpic_headers,
    get_append_slowpic_upload_headers,
)

RETRY_DELAYS = (5.0, 10.0, 20.0)
RETRYABLE_STATUS_CODES = {408, 429, 500, 502, 503, 504}


class TargetLoadWorker(QObject):
    finished = pyqtSignal(str, dict)
    error = pyqtSignal(str, str)

    def run(self, conf: TargetLoadWorkerConfiguration) -> None:
        try:
            with Session() as sess:
                response = sess.get(
                    f"{APIEndpoints.BASE}{conf.view_path}",
                    headers=get_append_slowpic_headers(sess),
                    timeout=60
                )
            if response.status_code != 200:
                self.error.emit(conf.uuid, f"Target failed ({response.status_code})")
                return

            collection = extract_json_var(response.text, "collection")

            target_text = conf.target_text
            set_key = str(collection.get("key", "")).strip()
            if not set_key:
                match = re.search(r"slow\.pics/[cs]/([A-Za-z0-9]+)", target_text.strip())
                if match:
                    parsed_fallback = match.group(1)
                elif re.fullmatch(r"[A-Za-z0-9]+", target_text.strip()):
                    parsed_fallback = target_text.strip()
                else:
                    parsed_fallback = None
                if not parsed_fallback:
                    self.error.emit(conf.uuid, "Target data missing.")
                    return
                set_key = parsed_fallback

            with Session() as sess:
                if conf.cookies_path.is_file():
                    sess.cookies.update(cookiejar_from_dict(json.loads(conf.cookies_path.read_text(encoding="utf-8"))))

                clone_response = sess.get(
                    f"{APIEndpoints.BASE}/c/{set_key}/clone",
                    headers=get_append_slowpic_headers(sess),
                    timeout=60
                )
                if clone_response.status_code == 200:
                    edit_dto = extract_json_var(clone_response.text, "collectionDTO")
                    post_mode = "clone"
                elif clone_response.status_code in (401, 403):
                    msg = "Clone denied."
                    self.error.emit(conf.uuid, msg)
                    return
                else:
                    self.error.emit(conf.uuid, f"Clone failed ({clone_response.status_code})")
                    return

            result = {
                "collection": collection,
                "set_key": set_key,
                "edit_dto": edit_dto,
                "post_mode": post_mode,
            }
            self.finished.emit(conf.uuid, result)
        except Exception as exc:
            logging.warning("TargetLoadWorker failed: %s", exc)
            self.error.emit(conf.uuid, "Target failed: network.")


class AppendSourcesWorker(QObject):
    progress_bar = pyqtSignal(str, int)
    progress_status = pyqtSignal(str, str, int, int)
    finished = pyqtSignal(str)

    def __init__(self) -> None:
        super().__init__()
        self.is_finished = False

    def _progress_update(self, value: int, endvalue: int, *, uuid: str) -> None:
        if endvalue <= 0:
            self.progress_bar.emit(uuid, 0)
            return
        self.progress_bar.emit(uuid, int(100 * value / endvalue))

    def _frame_type_from_vsframe(self, frame: vs.VideoFrame) -> str:
        try:
            raw = frame.props.get("_PictType", b"?")
            decoded = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else str(raw)
            return decoded.strip()[:1] or "?"
        except Exception:
            return "?"

    def _emit_retry_status(self, uuid: str, label: str, retry_index: int) -> None:
        self.progress_status.emit(uuid, f"Retry:{label} Retry {retry_index}/{len(RETRY_DELAYS)}...", 0, 0)

    def _failure_message_from_status(self, status_code: int) -> str:
        if status_code == 429:
            return "Upload failed: rate limit."
        if status_code in (401, 403):
            return "Upload failed: auth."
        if status_code == 404:
            return "Upload failed: not found."
        if status_code in RETRYABLE_STATUS_CODES or 500 <= status_code <= 599:
            return "Upload failed: server."
        return "Upload failed."

    def _request_with_retries(
        self,
        sess: Session,
        method: str,
        url: str,
        *,
        uuid: str,
        headers: dict[str, str],
        timeout: int,
        context: str,
        data: bytes | None = None,
        allow_image_complete: bool = False,
    ) -> Response:
        for retry_index, delay in enumerate(RETRY_DELAYS + (None,), start=1):
            try:
                response = sess.request(
                    method,
                    url,
                    headers=headers,
                    timeout=timeout,
                    data=data,
                )
            except RequestException as exc:
                if delay is not None:
                    logging.warning("%s network retry %s/%s: %s", context, retry_index, len(RETRY_DELAYS), exc)
                    self._emit_retry_status(uuid, "Network error.", retry_index)
                    time.sleep(delay)
                    continue
                logging.warning("%s failed with network error: %s", context, exc)
                raise RuntimeError("Upload failed: network.") from exc

            if allow_image_complete and response.status_code == 400 and response.headers.get("X-Error-Message") == "IMAGE_IS_COMPLETE":
                return response

            if 200 <= response.status_code < 300:
                return response

            api_error = self._extract_api_error_message(response.text)
            if delay is not None and response.status_code in RETRYABLE_STATUS_CODES:
                retry_label = "Rate limited." if response.status_code == 429 else "Server busy."
                logging.warning(
                    "%s retry %s/%s after HTTP %s%s",
                    context,
                    retry_index,
                    len(RETRY_DELAYS),
                    response.status_code,
                    f": {api_error}" if api_error else "",
                )
                self._emit_retry_status(uuid, retry_label, retry_index)
                time.sleep(delay)
                continue

            logging.warning(
                "%s failed with status %s%s",
                context,
                response.status_code,
                f": {api_error}" if api_error else "",
            )
            raise RuntimeError(self._failure_message_from_status(response.status_code))

        raise RuntimeError("Upload failed.")

    def _upload_single_image(
        self,
        sess: Session,
        collection_uuid: str,
        image_uuid: str,
        image_path: Path,
        uuid: str,
        browser_id: str,
        *,
        file_name: str | None = None,
        mime_type: str = "image/png"
    ) -> None:
        upload_info = MultipartEncoder({
            "collectionUuid": collection_uuid,
            "imageUuid": image_uuid,
            "file": (file_name or image_path.name, image_path.read_bytes(), mime_type),
            "browserId": browser_id,
        }, str(uuid4()))

        self._request_with_retries(
            sess,
            "POST",
            f"{APIEndpoints.BASE}/upload/image/{image_uuid}",
            uuid=uuid,
            headers=get_append_slowpic_upload_headers(upload_info.len, upload_info.content_type, sess),
            timeout=120,
            context=f"Append upload `{image_uuid}`",
            data=upload_info.to_string(),
            allow_image_complete=True,
        )

    def _download_image_to_path(self, sess: Session, image_url: str, out_path: Path, *, uuid: str) -> None:
        response = self._request_with_retries(
            sess,
            "GET",
            image_url,
            uuid=uuid,
            headers=get_append_slowpic_headers(sess),
            timeout=120,
            context=f"Existing image fetch `{image_url}`",
        )
        out_path.write_bytes(response.content)

    def _extract_api_error_message(self, response_text: str) -> str | None:
        try:
            payload = json.loads(response_text)
        except Exception:
            return None

        if not isinstance(payload, dict):
            return None

        error = payload.get("error")
        message = payload.get("message")
        if error and message:
            return f"{error}: {message}"
        if message:
            return str(message)
        if error:
            return str(error)
        return None

    def _extract_frames(
        self, conf: AppendSourcesConfiguration, tempdir: Path
    ) -> tuple[int, list[list[Path]], list[list[str]]]:
        extracted = 0
        total_extract = len(conf.base_frames) * len(conf.outputs)
        extracted_paths: list[list[Path]] = []
        per_output_image_names: list[list[str]] = []

        for list_idx, output in enumerate(conf.outputs):
            original_output_idx = conf.output_indices[list_idx]
            clip = output.prepare_vs_output(output.source.clip, True)
            max_frame = output.total_frames - 1

            output_paths: list[Path] = []
            output_names: list[str] = []

            for frame_idx, base_frame in enumerate(conf.base_frames):
                self.progress_status.emit(conf.uuid, "extract", extracted + 1, total_extract)

                frame_offsets = conf.frame_offsets.get(base_frame)
                if not isinstance(frame_offsets, dict):
                    frame_offsets = {}

                offset = int(frame_offsets.get(original_output_idx, 0))
                target_frame = max(0, min(int(base_frame + offset), max_frame))

                frame_path = tempdir / f"output_{list_idx}_cmp_{frame_idx}_{target_frame}.png"
                with clip.get_frame(target_frame) as frame:
                    ptype = self._frame_type_from_vsframe(frame)
                    image_name = f"({ptype}) {output.name}" if conf.frame_type else output.name
                    qimage = output.frame_to_qimage(frame)
                    if not qimage.save(str(frame_path), "PNG", 100):
                        raise RuntimeError(f"Failed to save frame image to `{frame_path}`")

                output_paths.append(frame_path)
                output_names.append(image_name)
                extracted += 1
                self._progress_update(extracted, total_extract + total_extract, uuid=conf.uuid)

            extracted_paths.append(output_paths)
            per_output_image_names.append(output_names)

        return total_extract, extracted_paths, per_output_image_names

    def _prepare_dto(
        self,
        conf: AppendSourcesConfiguration,
        per_output_image_names: list[list[str]]
    ) -> tuple[SlowpicsCollectionDTO, list[tuple[int, int, str, str, str]]]:

        dto = cast(SlowpicsCollectionDTO, json.loads(json.dumps(conf.edit_dto)))
        comparisons = dto.get("comparisons")
        if not isinstance(comparisons, list) or len(comparisons) != conf.expected_comparison_count:
            count = len(comparisons) if isinstance(comparisons, list) else 0
            raise ValueError(
                f"Target edit payload mismatch: expected {conf.expected_comparison_count} comparisons, got {count}"
            )

        normalized_comparisons: list[SlowpicsComparison] = []
        for row, comparison in enumerate(comparisons):
            if not isinstance(comparison, dict):
                raise ValueError(f"Invalid clone payload: comparison row `{row}` is not an object")

            images_raw = comparison.get("images", [])
            normalized_images: list[SlowpicsImage] = []
            for col, image in enumerate(images_raw):
                if isinstance(image, dict):
                    normalized_images.append(cast(SlowpicsImage, image))
                elif image is None:
                    normalized_images.append({"uuid": None, "name": "", "sortOrder": col})
                else:
                    raise ValueError(f"Invalid clone payload: row `{row}`, col `{col}`")

            comparison["images"] = normalized_images
            normalized_comparisons.append(comparison)

        comparisons = normalized_comparisons
        dto["comparisons"] = comparisons

        image_counts = [len(comp.get("images", [])) for comp in comparisons]
        if not image_counts or min(image_counts) != max(image_counts):
            raise ValueError("Target comparison has inconsistent source column counts")

        existing_image_cols = image_counts[0]
        existing_files = dto.get("files")
        if not isinstance(existing_files, list) or len(existing_files) != conf.expected_comparison_count:
            raise ValueError(f"Invalid clone files matrix: expected {conf.expected_comparison_count} rows")

        existing_upload_slots: list[tuple[int, int, str, str, str]] = []
        for row, file_row in enumerate(existing_files):
            if not isinstance(file_row, list) or len(file_row) < existing_image_cols:
                raise ValueError(f"Invalid clone files row `{row}`")

            for col in range(existing_image_cols):
                cell = file_row[col]
                if not isinstance(cell, dict):
                    continue
                file_url = str(cell.get("url", "")).strip()
                if not file_url:
                    continue
                file_name = str(cell.get("name", "")).strip() or f"existing_{row}_{col}"
                file_mime = str(cell.get("type", "")).strip() or "application/octet-stream"
                existing_upload_slots.append((row, col, file_url, file_name, file_mime))

        max_frame = max(conf.base_frames) if conf.base_frames else 0
        if conf.normalize_comparison_names:
            for row, base_frame in enumerate(conf.base_frames):
                frame_name = get_frame_time(conf.main, conf.reference_output, base_frame, max_frame)
                comparisons[row]["name"] = frame_name

        for row, comparison in enumerate(comparisons):
            images = comparison.get("images", [])
            if comparison.get("sortOrder") is None:
                comparison["sortOrder"] = row

            for out_pos in range(len(conf.outputs)):
                images.append({
                    "uuid": None,
                    "name": per_output_image_names[out_pos][row],
                    "sortOrder": len(images),
                })
        return dto, existing_upload_slots

    def _build_multipart_fields(
        self, conf: AppendSourcesConfiguration, dto: SlowpicsCollectionDTO, browser_id: str
    ) -> dict[str, str]:
        fields: dict[str, str] = {}
        if conf.post_mode == "edit":
            fields["key"] = str(dto.get("key") or conf.target_key)
        fields["collectionName"] = str(
            conf.generated_collection_name
            or conf.target_collection_name
            or dto.get("name")
            or f"Comp {conf.target_key}"
        )
        fields["browserId"] = browser_id

        fields["public"] = str(bool(dto.get("public", False))).lower()
        fields["hentai"] = str(bool(dto.get("hentai", False))).lower()
        fields["optimizeImages"] = str(bool(dto.get("optimizeImages", True))).lower()

        for simple_key in ("removeAfter", "canvasMode", "imageFit", "imagePosition"):
            value = dto.get(simple_key)
            if value is not None and value != "":
                fields[simple_key] = str(value)

        tmdb = dto.get("tmdbId")
        if isinstance(tmdb, dict):
            if tmdb_value := tmdb.get("value"):
                fields["tmdbId"] = str(tmdb_value)
        elif tmdb:
            fields["tmdbId"] = str(tmdb)

        meta_collection = dto.get("metaCollection")
        if isinstance(meta_collection, dict):
            if meta_value := meta_collection.get("value"):
                fields["metaCollection"] = str(meta_value)
        elif meta_collection:
            fields["metaCollection"] = str(meta_collection)

        tags = dto.get("tags")
        if isinstance(tags, list):
            for tag_idx, tag in enumerate(tags):
                tag_value = tag.get("value") if isinstance(tag, dict) else tag
                if tag_value:
                    fields[f"tags[{tag_idx}]"] = str(tag_value)

        for row, comparison in enumerate(dto.get("comparisons", [])):
            if comp_uuid := comparison.get("uuid"):
                fields[f"comparisons[{row}].uuid"] = str(comp_uuid)
            fields[f"comparisons[{row}].name"] = str(comparison.get("name", ""))
            if (comp_sort := comparison.get("sortOrder")) is not None:
                fields[f"comparisons[{row}].sortOrder"] = str(comp_sort)

            for col, image in enumerate(comparison.get("images", [])):
                if img_uuid := image.get("uuid"):
                    fields[f"comparisons[{row}].images[{col}].uuid"] = str(img_uuid)
                fields[f"comparisons[{row}].images[{col}].name"] = str(image.get("name", ""))
                if (img_sort := image.get("sortOrder")) is not None:
                    fields[f"comparisons[{row}].images[{col}].sortOrder"] = str(img_sort)

        return fields

    def _upload_images(
        self,
        conf: AppendSourcesConfiguration,
        sess: Session,
        tempdir: Path,
        browser_id: str,
        edit_json: dict[str, Any],
        existing_upload_slots: list[tuple[int, int, str, str, str]],
        extracted_paths: list[list[Path]],
        existing_image_cols: int,
        total_extract: int,
        extracted: int
    ) -> None:
        collection_uuid = edit_json.get("collectionUuid")
        image_uuid_matrix = edit_json.get("images")
        if not collection_uuid or not isinstance(image_uuid_matrix, list):
            raise ValueError("Edit response missing `collectionUuid` or `images`")

        uploaded = 0
        total_upload = len(existing_upload_slots) + total_extract
        total_progress = total_extract + total_upload

        self._progress_update(extracted, total_progress, uuid=conf.uuid)
        for row in range(len(conf.base_frames)):
            if row >= len(image_uuid_matrix):
                raise ValueError(f"Edit response missing comparison row `{row}`")
            matrix_row = image_uuid_matrix[row]
            if not isinstance(matrix_row, list):
                raise ValueError(f"Edit response images row `{row}` is invalid")

            for col in range(existing_image_cols):
                if col >= len(matrix_row):
                    raise ValueError(f"Edit response missing UUID for comparison {row + 1}, existing column {col + 1}")
                image_uuid_raw = matrix_row[col]
                image_uuid = str(image_uuid_raw).strip() if image_uuid_raw is not None else ""
                if not image_uuid:
                    raise ValueError(f"Edit response missing UUID for comparison {row + 1}, existing column {col + 1}")

                slot = next((s for s in existing_upload_slots if s[0] == row and s[1] == col), None)
                if not slot:
                    continue

                _, _, file_url, file_name, file_mime = slot
                image_path = tempdir / f"existing_{row}_{col}.bin"

                self.progress_status.emit(conf.uuid, "upload", uploaded + 1, total_upload)
                self._download_image_to_path(sess, file_url, image_path, uuid=conf.uuid)
                self._upload_single_image(
                    sess, str(collection_uuid), image_uuid, image_path, conf.uuid, browser_id,
                    file_name=file_name, mime_type=file_mime
                )
                uploaded += 1
                self._progress_update(total_extract + uploaded, total_progress, uuid=conf.uuid)

            for list_idx in range(len(conf.outputs)):
                matrix_col = existing_image_cols + list_idx
                if matrix_col >= len(matrix_row):
                    raise ValueError(f"Edit response missing UUID for comparison {row + 1}, new column {list_idx + 1}")

                image_uuid_raw = matrix_row[matrix_col]
                image_uuid = str(image_uuid_raw).strip() if image_uuid_raw is not None else ""
                if not image_uuid:
                    raise ValueError(f"Edit response missing UUID for new column {list_idx + 1}")

                new_image_path = extracted_paths[list_idx][row]

                self.progress_status.emit(conf.uuid, "upload", uploaded + 1, total_upload)
                self._upload_single_image(
                    sess, str(collection_uuid), image_uuid, new_image_path, conf.uuid, browser_id
                )
                uploaded += 1
                self._progress_update(total_extract + uploaded, total_progress, uuid=conf.uuid)

    def run(self, conf: AppendSourcesConfiguration) -> None:
        tempdir = Path(tempfile.mkdtemp(prefix="spo_append_"))
        browser_id = str(uuid4())

        try:
            if len(conf.base_frames) != conf.expected_comparison_count:
                raise ValueError(
                    f"Frame count mismatch: got {len(conf.base_frames)} frames "
                    f"but target has {conf.expected_comparison_count} comparisons."
                )

            total_extract, extracted_paths, per_output_image_names = self._extract_frames(conf, tempdir)
            dto, existing_upload_slots = self._prepare_dto(conf, per_output_image_names)
            fields = self._build_multipart_fields(conf, dto, browser_id)

            with Session() as sess:
                if conf.cookies_path.is_file():
                    sess.cookies.update(cookiejar_from_dict(json.loads(conf.cookies_path.read_text(encoding="utf-8"))))

                _ = self._request_with_retries(
                    sess,
                    "GET",
                    f"{APIEndpoints.BASE}/comparison",
                    uuid=conf.uuid,
                    headers=get_append_slowpic_headers(sess),
                    timeout=45,
                    context="Append bootstrap",
                )

                form = MultipartEncoder(fields, str(uuid4()))
                post_url = (
                    f"{APIEndpoints.BASE}/c/{conf.target_key}/edit"
                    if conf.post_mode == "edit"
                    else f"{APIEndpoints.BASE}/upload/comparison"
                )
                mode_label = "Append edit" if conf.post_mode == "edit" else "Append clone"
                edit_response = self._request_with_retries(
                    sess,
                    "POST",
                    post_url,
                    uuid=conf.uuid,
                    headers=get_append_slowpic_upload_headers(form.len, form.content_type, sess),
                    timeout=180,
                    context=mode_label,
                    data=form.to_string(),
                )

                edit_json = edit_response.json()
                if not isinstance(edit_json, dict):
                    raise ValueError("Clone response payload is not an object")

                fallback_comps = [cast(SlowpicsComparison, {})]
                existing_image_cols = len(
                    dto.get("comparisons", fallback_comps)[0].get("images", [])
                ) - len(conf.outputs)

                self._upload_images(
                    conf, sess, tempdir, browser_id, edit_json,
                    existing_upload_slots, extracted_paths,
                    existing_image_cols, total_extract, total_extract
                )

            result_key = str(edit_json.get("key") or conf.target_key)
            self.progress_status.emit(conf.uuid, f"{APIEndpoints.BASE}/c/{result_key}", 0, 0)
        except Exception as exc:
            logging.exception("AppendSourcesWorker failed")
            message = str(exc).strip() or "Upload failed."
            self.progress_status.emit(conf.uuid, f"Error: {message}", 0, 0)
        finally:
            shutil.rmtree(tempdir, ignore_errors=True)
            self.finished.emit(conf.uuid)
