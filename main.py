from __future__ import annotations

import io
import mimetypes
import os
from pathlib import Path, PurePosixPath
from typing import Any

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload
from PIL import Image, UnidentifiedImageError


APP_DIR = Path(__file__).resolve().parent
HTML_PATH = APP_DIR / "pwa_upload.html"
MAX_IMAGE_BYTES = 2 * 1024 * 1024
DRIVE_SCOPE = ["https://www.googleapis.com/auth/drive"]
FOLDER_MIME_TYPE = "application/vnd.google-apps.folder"
IMAGE_MIME_TYPES = {"image/jpeg", "image/png", "image/webp"}

app = FastAPI(title="PWA Upload")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(HTML_PATH)


@app.get("/api/health")
def health() -> dict[str, Any]:
    credentials_path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "")
    root_folder_id = os.getenv("DRIVE_ROOT_FOLDER_ID", "")
    return {
        "ok": True,
        "drive_configured": bool(credentials_path and root_folder_id and Path(credentials_path).exists()),
        "credentials_path_set": bool(credentials_path),
        "credentials_file_exists": bool(credentials_path and Path(credentials_path).exists()),
        "drive_root_folder_id_set": bool(root_folder_id),
        "max_image_bytes": MAX_IMAGE_BYTES,
    }


@app.post("/api/upload")
def upload_files(
    files: list[UploadFile] = File(...),
    relative_paths: list[str] = Form(...),
) -> dict[str, Any]:
    if len(files) != len(relative_paths):
        raise HTTPException(status_code=400, detail="files and relative_paths counts do not match")

    uploader = DriveUploader.from_env()
    results = []

    for upload_file, relative_path in zip(files, relative_paths):
        try:
            prepared = prepare_upload(upload_file, relative_path)
            drive_file = uploader.upload_file(
                relative_path=prepared.relative_path,
                stream=prepared.stream,
                mime_type=prepared.mime_type,
            )
            results.append(
                {
                    "ok": True,
                    "relative_path": prepared.relative_path,
                    "original_name": upload_file.filename,
                    "uploaded_name": PurePosixPath(prepared.relative_path).name,
                    "uploaded_size": prepared.size,
                    "mime_type": prepared.mime_type,
                    "drive_file_id": drive_file.get("id", ""),
                    "web_view_link": drive_file.get("webViewLink", ""),
                }
            )
        except Exception as exc:
            results.append(
                {
                    "ok": False,
                    "relative_path": sanitize_relative_path(relative_path, upload_file.filename or "file"),
                    "original_name": upload_file.filename,
                    "error": str(exc),
                }
            )
        finally:
            upload_file.file.close()

    return {
        "ok": all(result["ok"] for result in results),
        "results": results,
    }


class PreparedUpload:
    def __init__(self, relative_path: str, stream: io.BytesIO, mime_type: str, size: int) -> None:
        self.relative_path = relative_path
        self.stream = stream
        self.mime_type = mime_type
        self.size = size


class DriveUploader:
    def __init__(self, credentials_path: str, root_folder_id: str) -> None:
        credentials = service_account.Credentials.from_service_account_file(
            credentials_path,
            scopes=DRIVE_SCOPE,
        )
        self.service = build("drive", "v3", credentials=credentials, cache_discovery=False)
        self.root_folder_id = root_folder_id
        self.folder_cache: dict[tuple[str, str], str] = {}

    @classmethod
    def from_env(cls) -> "DriveUploader":
        credentials_path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "")
        root_folder_id = os.getenv("DRIVE_ROOT_FOLDER_ID", "")

        if not credentials_path:
            raise HTTPException(status_code=500, detail="GOOGLE_APPLICATION_CREDENTIALS is not set")
        if not Path(credentials_path).exists():
            raise HTTPException(status_code=500, detail="GOOGLE_APPLICATION_CREDENTIALS file does not exist")
        if not root_folder_id:
            raise HTTPException(status_code=500, detail="DRIVE_ROOT_FOLDER_ID is not set")

        return cls(credentials_path, root_folder_id)

    def upload_file(self, relative_path: str, stream: io.BytesIO, mime_type: str) -> dict[str, Any]:
        parts = PurePosixPath(relative_path).parts
        if not parts:
            raise ValueError("relative path is empty")

        filename = parts[-1]
        parent_id = self.ensure_folder_path(parts[:-1])
        media = MediaIoBaseUpload(stream, mimetype=mime_type, resumable=False)
        metadata = {
            "name": filename,
            "parents": [parent_id],
        }
        return (
            self.service.files()
            .create(
                body=metadata,
                media_body=media,
                fields="id,name,webViewLink",
                supportsAllDrives=True,
            )
            .execute()
        )

    def ensure_folder_path(self, folders: tuple[str, ...]) -> str:
        parent_id = self.root_folder_id
        for folder_name in folders:
            parent_id = self.ensure_folder(parent_id, folder_name)
        return parent_id

    def ensure_folder(self, parent_id: str, folder_name: str) -> str:
        cache_key = (parent_id, folder_name)
        if cache_key in self.folder_cache:
            return self.folder_cache[cache_key]

        escaped_name = escape_drive_query_value(folder_name)
        escaped_parent = escape_drive_query_value(parent_id)
        query = (
            f"mimeType = '{FOLDER_MIME_TYPE}' and "
            f"name = '{escaped_name}' and "
            f"'{escaped_parent}' in parents and trashed = false"
        )
        existing = (
            self.service.files()
            .list(
                q=query,
                fields="files(id,name)",
                spaces="drive",
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            )
            .execute()
            .get("files", [])
        )

        if existing:
            folder_id = existing[0]["id"]
        else:
            folder = (
                self.service.files()
                .create(
                    body={
                        "name": folder_name,
                        "mimeType": FOLDER_MIME_TYPE,
                        "parents": [parent_id],
                    },
                    fields="id,name",
                    supportsAllDrives=True,
                )
                .execute()
            )
            folder_id = folder["id"]

        self.folder_cache[cache_key] = folder_id
        return folder_id


def prepare_upload(upload_file: UploadFile, relative_path: str) -> PreparedUpload:
    safe_path = sanitize_relative_path(relative_path, upload_file.filename or "file")
    content_type = upload_file.content_type or guess_mime_type(safe_path)
    upload_file.file.seek(0)

    if content_type in IMAGE_MIME_TYPES:
        try:
            return compress_image(upload_file.file, safe_path)
        except UnidentifiedImageError:
            upload_file.file.seek(0)

    data = upload_file.file.read()
    stream = io.BytesIO(data)
    stream.seek(0)
    return PreparedUpload(
        relative_path=safe_path,
        stream=stream,
        mime_type=content_type or "application/octet-stream",
        size=len(data),
    )


def compress_image(file_obj: Any, relative_path: str) -> PreparedUpload:
    with Image.open(file_obj) as image:
        if image.mode not in ("RGB", "L"):
            image = image.convert("RGB")
        elif image.mode == "L":
            image = image.convert("RGB")

        output_path = replace_extension(relative_path, ".jpg")
        best_data = b""

        for scale_index in range(14):
            scale = 0.88**scale_index
            width = max(1, round(image.width * scale))
            height = max(1, round(image.height * scale))
            candidate_image = image if scale_index == 0 else image.resize((width, height), Image.Resampling.LANCZOS)

            for quality in range(90, 39, -8):
                buffer = io.BytesIO()
                candidate_image.save(buffer, format="JPEG", optimize=True, quality=quality)
                data = buffer.getvalue()
                if not best_data or len(data) < len(best_data):
                    best_data = data
                if len(data) <= MAX_IMAGE_BYTES:
                    buffer.seek(0)
                    return PreparedUpload(output_path, buffer, "image/jpeg", len(data))

        stream = io.BytesIO(best_data)
        stream.seek(0)
        return PreparedUpload(output_path, stream, "image/jpeg", len(best_data))


def sanitize_relative_path(relative_path: str, fallback_name: str) -> str:
    raw_path = (relative_path or fallback_name).replace("\\", "/").strip()
    parts = []
    for part in PurePosixPath(raw_path).parts:
        if part in {"", ".", "/", ".."}:
            continue
        parts.append(part)

    if not parts:
        parts = [fallback_name or "file"]

    return "/".join(parts)


def replace_extension(relative_path: str, extension: str) -> str:
    path = PurePosixPath(relative_path)
    filename = path.name
    stem = filename.rsplit(".", 1)[0] if "." in filename else filename
    new_name = f"{stem}{extension}"
    if path.parent == PurePosixPath("."):
        return new_name
    return f"{path.parent.as_posix()}/{new_name}"


def guess_mime_type(path: str) -> str:
    return mimetypes.guess_type(path)[0] or "application/octet-stream"


def escape_drive_query_value(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")
