"""Receipt/invoice files in the sub-account's GoHighLevel media library (scopes medias.write / medias.readonly).

Every location gets one folder (FOLDER_NAME), created on first upload; its id is cached on GhlLocation.
"""

import logging

import httpx

from ..models import GhlLocation
from .oauth import API, get_location_access_token

logger = logging.getLogger(__name__)

FOLDER_NAME = "Accounting Receipts"
VERSION = "2021-07-28"


class MediaError(Exception):
    pass


def _headers(location_id: str) -> dict:
    return {"Authorization": f"Bearer {get_location_access_token(location_id)}", "Version": VERSION, "Accept": "application/json"}


def _check(res, what: str):
    if res.status_code >= 400:
        detail = ""
        try:
            detail = str(res.json().get("message") or "")[:200]
        except Exception:  # noqa: BLE001
            pass
        hint = " (the GoHighLevel app needs the medias.write scope)" if res.status_code in (401, 403) else ""
        raise MediaError(f"GoHighLevel {what} failed ({res.status_code}){hint}. {detail}".strip())
    return res.json() if res.content else {}


def _id_of(body: dict) -> str | None:
    for src in (body, body.get("folder") or {}, body.get("data") or {}):
        for k in ("_id", "id", "fileId"):
            if isinstance(src, dict) and src.get(k):
                return str(src[k])
    return None


def _find_folder(location_id: str) -> str | None:
    for offset in range(0, 500, 100):
        res = httpx.get(
            f"{API}/medias/files",
            params={"altId": location_id, "altType": "location", "type": "folder", "query": FOLDER_NAME, "limit": 100, "offset": offset},
            headers=_headers(location_id),
            timeout=30,
        )
        files = _check(res, "folder lookup").get("files") or []
        for f in files:
            if f.get("name") == FOLDER_NAME and not f.get("parentId"):
                return f.get("_id") or f.get("id")
        if len(files) < 100:
            return None
    return None


def ensure_folder(location: GhlLocation, *, refresh: bool = False) -> str:
    if location.media_folder_id and not refresh:
        return location.media_folder_id
    folder_id = _find_folder(location.location_id)
    if not folder_id:
        res = httpx.post(
            f"{API}/medias/folder",
            json={"altId": location.location_id, "altType": "location", "name": FOLDER_NAME},
            headers=_headers(location.location_id),
            timeout=30,
        )
        folder_id = _id_of(_check(res, "folder creation"))
    if not folder_id:
        raise MediaError("GoHighLevel did not return the receipts folder id.")
    location.media_folder_id = folder_id
    location.save(update_fields=["media_folder_id"])
    return folder_id


def _upload(location: GhlLocation, folder_id: str, name: str, content: bytes, content_type: str):
    return httpx.post(
        f"{API}/medias/upload-file",
        data={"hosted": "false", "name": name, "parentId": folder_id},
        files={"file": (name, content, content_type or "application/octet-stream")},
        headers=_headers(location.location_id),
        timeout=120,
    )


def upload(location: GhlLocation, name: str, content: bytes, content_type: str) -> dict:
    """Store the file in the location's receipts folder. Returns {"file_id", "url"}."""
    folder_id = ensure_folder(location)
    res = _upload(location, folder_id, name, content, content_type)
    if res.status_code in (400, 404, 422) and location.media_folder_id:  # the folder may have been deleted in GoHighLevel
        folder_id = ensure_folder(location, refresh=True)
        res = _upload(location, folder_id, name, content, content_type)
    body = _check(res, "upload")
    file_id, url = _id_of(body), body.get("url")
    if not file_id or not url:
        raise MediaError("GoHighLevel did not return the uploaded file.")
    return {"file_id": file_id, "url": url}


def delete(location_id: str, file_id: str) -> None:
    res = httpx.delete(
        f"{API}/medias/{file_id}",
        params={"altId": location_id, "altType": "location"},
        headers=_headers(location_id),
        timeout=30,
    )
    if res.status_code == 404:
        return
    _check(res, "delete")


def fetch(url: str) -> httpx.Response:
    res = httpx.get(url, timeout=60, follow_redirects=True)
    if res.status_code >= 400:
        raise MediaError(f"File could not be loaded ({res.status_code}).")
    return res
