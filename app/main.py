from __future__ import annotations

import json
import mimetypes
import os
import socket
from functools import lru_cache
from pathlib import PurePosixPath
from typing import Any, Optional

import google_auth_httplib2
import httplib2
import requests
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from pydantic import BaseModel, Field


APP_DIR = os.path.dirname(os.path.abspath(__file__))
TEMPLATES_DIR = os.path.join(APP_DIR, "templates")
STATIC_DIR = os.path.join(APP_DIR, "static")
INDEX_PATH = os.path.join(TEMPLATES_DIR, "index.html")

DRIVE_SCOPE = ["https://www.googleapis.com/auth/drive"]
FOLDER_MIME_TYPE = "application/vnd.google-apps.folder"
UPLOAD_SESSION_URL = "https://www.googleapis.com/upload/drive/v3/files"
ALLOWED_REGIONS = {f"reg{i}" for i in range(1, 11)}
ALLOWED_ASSETS = {"valve", "firehydrant", "pwa_waterworks"}
ALLOWED_FILE_TYPES = {"picture_path", "drawing_path"}
GOOGLE_API_TIMEOUT_SECONDS = int(os.getenv("GOOGLE_API_TIMEOUT_SECONDS", "15"))


app = FastAPI(title="PWA Google Drive Uploader")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


class UploadSessionRequest(BaseModel):
    region: str
    branch_id: str = Field(min_length=7, max_length=7)
    asset_type: str
    file_type: str
    relative_path: str
    mime_type: Optional[str] = None
    size: int = Field(ge=0)


class TimeoutGoogleAuthRequest(GoogleAuthRequest):
    def __call__(
        self,
        url: str,
        method: str = "GET",
        body: Optional[bytes] = None,
        headers: Optional[dict[str, str]] = None,
        timeout: float = 120,
        **kwargs: Any,
    ) -> Any:
        return super().__call__(
            url,
            method=method,
            body=body,
            headers=headers,
            timeout=GOOGLE_API_TIMEOUT_SECONDS,
            **kwargs,
        )


class DriveClient:
    def __init__(self) -> None:
        self.credentials = load_credentials()
        authorized_http = google_auth_httplib2.AuthorizedHttp(
            self.credentials,
            http=httplib2.Http(timeout=GOOGLE_API_TIMEOUT_SECONDS),
        )
        self.service = build("drive", "v3", http=authorized_http, cache_discovery=False)
        self.root_folder_id = get_root_folder_id()
        self.folder_cache: dict[tuple[str, str], str] = {}

    def ensure_folder_path(self, folders: tuple[str, ...]) -> str:
        parent_id = self.root_folder_id
        for folder_name in folders:
            parent_id = self.ensure_folder(parent_id, folder_name)
        return parent_id

    def ensure_folder(self, parent_id: str, folder_name: str) -> str:
        # Folder creation is idempotent: reuse an exact parent/name match before creating.
        cache_key = (parent_id, folder_name)
        if cache_key in self.folder_cache:
            return self.folder_cache[cache_key]

        query = (
            f"mimeType = '{FOLDER_MIME_TYPE}' and "
            f"name = '{escape_drive_query_value(folder_name)}' and "
            f"'{escape_drive_query_value(parent_id)}' in parents and trashed = false"
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

    def create_upload_session(self, parent_id: str, filename: str, mime_type: str, size: int) -> str:
        self.credentials.refresh(TimeoutGoogleAuthRequest())
        headers = {
            "Authorization": f"Bearer {self.credentials.token}",
            "Content-Type": "application/json; charset=UTF-8",
            "X-Upload-Content-Type": mime_type,
            "X-Upload-Content-Length": str(size),
        }
        metadata = {
            "name": filename,
            "parents": [parent_id],
            "mimeType": mime_type,
        }
        response = requests.post(
            UPLOAD_SESSION_URL,
            params={
                "uploadType": "resumable",
                "supportsAllDrives": "true",
                "fields": "id,name,webViewLink",
            },
            headers=headers,
            json=metadata,
            timeout=(5, GOOGLE_API_TIMEOUT_SECONDS),
        )
        if response.status_code not in {200, 201}:
            raise HTTPException(
                status_code=502,
                detail=f"Google Drive rejected upload session creation: {response.status_code} {response.text}",
            )

        upload_url = response.headers.get("Location")
        if not upload_url:
            raise HTTPException(status_code=502, detail="Google Drive did not return a resumable upload URL")
        return upload_url


@app.get("/")
def index() -> FileResponse:
    return FileResponse(INDEX_PATH)


@app.get("/healthz")
def healthz() -> dict[str, bool]:
    return {"ok": True}


@app.get("/api/health")
def health() -> dict[str, Any]:
    credentials_status = inspect_credentials_config()
    root_folder_id = os.getenv("DRIVE_ROOT_FOLDER_ID", "").strip()
    return {
        "ok": True,
        "configured": credentials_status["configured"] and bool(root_folder_id),
        "credentials": credentials_status,
        "drive_root_folder_id_set": bool(root_folder_id),
        "max_image_bytes": 2 * 1024 * 1024,
        "chunk_size_bytes": 8 * 1024 * 1024,
    }


@app.post("/api/create-upload-session")
def create_upload_session(payload: UploadSessionRequest) -> dict[str, Any]:
    validate_upload_request(payload)
    safe_relative_path = sanitize_relative_path(payload.relative_path)
    path_parts = PurePosixPath(safe_relative_path).parts
    filename = path_parts[-1]
    folder_parts = path_parts[:-1]
    mime_type = payload.mime_type or guess_mime_type(filename)
    destination_folders = (
        "upload_pwa",
        payload.region,
        f"pwa_{payload.branch_id}",
        payload.asset_type,
        payload.file_type,
        *folder_parts,
    )

    try:
        drive = get_drive_client()
        parent_id = drive.ensure_folder_path(destination_folders)
        upload_url = drive.create_upload_session(parent_id, filename, mime_type, payload.size)
    except HTTPException:
        raise
    except HttpError as exc:
        raise drive_http_exception(exc) from exc
    except (TimeoutError, socket.timeout, requests.exceptions.Timeout) as exc:
        raise HTTPException(status_code=502, detail="Google Drive request timed out. Please retry.") from exc
    except requests.exceptions.RequestException as exc:
        raise HTTPException(status_code=502, detail=f"Google Drive request failed: {exc}") from exc
    destination_path = "/".join((*destination_folders, filename))

    return {
        "ok": True,
        "upload_url": upload_url,
        "destination_path": destination_path,
        "filename": filename,
        "mime_type": mime_type,
        "size": payload.size,
        "expires_in_days": 7,
    }


@lru_cache(maxsize=1)
def get_drive_client() -> DriveClient:
    return DriveClient()


def validate_upload_request(payload: UploadSessionRequest) -> None:
    if payload.region not in ALLOWED_REGIONS:
        raise HTTPException(status_code=400, detail="region must be reg1 through reg10")
    if not payload.branch_id.isdigit():
        raise HTTPException(status_code=400, detail="branch_id must be 7 digits")
    if payload.asset_type not in ALLOWED_ASSETS:
        raise HTTPException(status_code=400, detail="asset_type is not supported")
    if payload.file_type not in ALLOWED_FILE_TYPES:
        raise HTTPException(status_code=400, detail="file_type is not supported")
    if not sanitize_relative_path(payload.relative_path):
        raise HTTPException(status_code=400, detail="relative_path is required")


def load_credentials() -> service_account.Credentials:
    raw_json = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
    credentials_path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "").strip()

    try:
        if raw_json:
            info = json.loads(raw_json)
            return service_account.Credentials.from_service_account_info(info, scopes=DRIVE_SCOPE)
        if credentials_path:
            return service_account.Credentials.from_service_account_file(credentials_path, scopes=DRIVE_SCOPE)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Invalid Google service account credentials: {exc}") from exc

    raise HTTPException(status_code=500, detail="GOOGLE_SERVICE_ACCOUNT_JSON is not set")


def inspect_credentials_config() -> dict[str, Any]:
    raw_json = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
    credentials_path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "").strip()
    if raw_json:
        try:
            parsed = json.loads(raw_json)
            client_email = parsed.get("client_email", "")
            return {
                "configured": bool(client_email and parsed.get("private_key")),
                "source": "GOOGLE_SERVICE_ACCOUNT_JSON",
                "service_account_email": mask_email(client_email),
            }
        except json.JSONDecodeError as exc:
            return {"configured": False, "source": "GOOGLE_SERVICE_ACCOUNT_JSON", "error": str(exc)}
    if credentials_path:
        service_account_email = ""
        if os.path.exists(credentials_path):
            try:
                with open(credentials_path, encoding="utf-8") as credentials_file:
                    service_account_email = json.load(credentials_file).get("client_email", "")
            except (OSError, json.JSONDecodeError):
                service_account_email = ""
        return {
            "configured": os.path.exists(credentials_path),
            "source": "GOOGLE_APPLICATION_CREDENTIALS",
            "file_exists": os.path.exists(credentials_path),
            "service_account_email": mask_email(service_account_email),
        }
    return {"configured": False, "source": "missing"}


def drive_http_exception(exc: HttpError) -> HTTPException:
    try:
        status = int(getattr(exc.resp, "status", 502))
    except (TypeError, ValueError):
        status = 502
    details = parse_google_error_details(exc)
    reason = details.get("reason", "")
    message = details.get("message") or str(exc)

    if status == 403 and reason == "insufficientParentPermissions":
        return HTTPException(
            status_code=403,
            detail=(
                "Google Drive root folder permission is insufficient. Share DRIVE_ROOT_FOLDER_ID "
                "with the configured service account email as Editor, or add it to the Shared Drive."
            ),
        )

    return HTTPException(status_code=502 if status >= 500 else status, detail=f"Google Drive error: {message}")


def parse_google_error_details(exc: HttpError) -> dict[str, str]:
    try:
        content = exc.content.decode("utf-8") if isinstance(exc.content, bytes) else str(exc.content)
        payload = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}

    error = payload.get("error", {})
    errors = error.get("errors") or []
    first_error = errors[0] if errors else {}
    return {
        "message": str(first_error.get("message") or error.get("message") or ""),
        "reason": str(first_error.get("reason") or ""),
    }


def mask_email(email: str) -> str:
    if not email or "@" not in email:
        return ""
    local, domain = email.split("@", 1)
    if len(local) <= 4:
        masked_local = f"{local[:1]}***"
    else:
        masked_local = f"{local[:2]}***{local[-2:]}"
    return f"{masked_local}@{domain}"


def get_root_folder_id() -> str:
    root_folder_id = os.getenv("DRIVE_ROOT_FOLDER_ID", "").strip()
    if not root_folder_id:
        raise HTTPException(status_code=500, detail="DRIVE_ROOT_FOLDER_ID is not set")
    return root_folder_id


def sanitize_relative_path(relative_path: str) -> str:
    raw_path = (relative_path or "").replace("\\", "/").strip()
    parts = []
    for part in PurePosixPath(raw_path).parts:
        clean_part = part.strip()
        if clean_part in {"", ".", "/", ".."}:
            continue
        parts.append(clean_part)
    return "/".join(parts)


def guess_mime_type(path: str) -> str:
    return mimetypes.guess_type(path)[0] or "application/octet-stream"


def escape_drive_query_value(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")
