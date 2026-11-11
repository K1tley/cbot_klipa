"""Google Drive storage and YouTube uploads."""
from __future__ import annotations

import io
import json
import logging
import os
from pathlib import Path

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload, MediaIoBaseUpload

log = logging.getLogger(__name__)

# Google no longer allows these two scopes in one login, so there are two tokens:
#   GOOGLE_REFRESH_TOKEN  -> Drive   (drive.file = only files ClipBot itself creates)
#   YOUTUBE_REFRESH_TOKEN -> YouTube (youtube.upload)
DRIVE_SCOPES = ["https://www.googleapis.com/auth/drive.file"]
YOUTUBE_SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]
FOLDER = "application/vnd.google-apps.folder"


def _creds(refresh_env: str, scopes: list[str]) -> Credentials:
    token = os.environ.get(refresh_env)
    if not token:
        raise RuntimeError(f"GitHub secret {refresh_env} is missing")
    return Credentials(
        token=None, refresh_token=token,
        client_id=os.environ["GOOGLE_CLIENT_ID"], client_secret=os.environ["GOOGLE_CLIENT_SECRET"],
        token_uri="https://oauth2.googleapis.com/token", scopes=scopes,
    )


# ---------------------------------------------------------------- Drive
class Drive:
    def __init__(self, root_name: str) -> None:
        self.svc = build("drive", "v3", credentials=_creds("GOOGLE_REFRESH_TOKEN", DRIVE_SCOPES), cache_discovery=False)
        self.root = self.folder(root_name, None)

    def _find(self, name: str, parent: str | None, mime: str | None = None) -> str | None:
        q = [f"name = '{name.replace(chr(39), chr(92) + chr(39))}'", "trashed = false"]
        q.append(f"'{parent}' in parents" if parent else "'root' in parents")
        if mime:
            q.append(f"mimeType = '{mime}'")
        res = self.svc.files().list(q=" and ".join(q), fields="files(id)", pageSize=10).execute()
        files = res.get("files", [])
        return files[0]["id"] if files else None

    def folder(self, name: str, parent: str | None) -> str:
        fid = self._find(name, parent, FOLDER)
        if fid:
            return fid
        body = {"name": name, "mimeType": FOLDER, "parents": [parent] if parent else []}
        return self.svc.files().create(body=body, fields="id").execute()["id"]

    def folder_path(self, *names: str) -> str:
        parent = self.root
        for n in names:
            parent = self.folder(n, parent)
        return parent

    def children(self, parent: str, folders_only: bool = False) -> list[dict]:
        q = f"'{parent}' in parents and trashed = false"
        if folders_only:
            q += f" and mimeType = '{FOLDER}'"
        out, token = [], None
        while True:
            res = self.svc.files().list(q=q, fields="nextPageToken, files(id,name,mimeType)",
                                        pageSize=200, pageToken=token).execute()
            out += res.get("files", [])
            token = res.get("nextPageToken")
            if not token:
                return out

    def upload(self, path: Path, name: str, parent: str, mimetype: str = "video/mp4") -> str:
        media = MediaFileUpload(str(path), mimetype=mimetype, resumable=True, chunksize=32 * 1024 * 1024)
        existing = self._find(name, parent)
        if existing:
            return self.svc.files().update(fileId=existing, media_body=media, fields="id").execute()["id"]
        body = {"name": name, "parents": [parent]}
        return self.svc.files().create(body=body, media_body=media, fields="id").execute()["id"]

    def download(self, file_id: str, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "wb") as fh:
            dl = MediaIoBaseDownload(fh, self.svc.files().get_media(fileId=file_id), chunksize=16 * 1024 * 1024)
            done = False
            while not done:
                _, done = dl.next_chunk()
        return path

    def delete(self, file_id: str) -> None:
        # permanent delete (trash would still use storage)
        try:
            self.svc.files().delete(fileId=file_id).execute()
        except HttpError as e:
            if e.resp.status != 404:
                raise

    # --- small JSON state file (the posting queue) ---
    def read_json(self, name: str, default):
        fid = self._find(name, self.root)
        if not fid:
            return default
        buf = io.BytesIO()
        dl = MediaIoBaseDownload(buf, self.svc.files().get_media(fileId=fid))
        done = False
        while not done:
            _, done = dl.next_chunk()
        return json.loads(buf.getvalue().decode("utf-8") or "null") or default

    def write_json(self, name: str, data) -> None:
        payload = io.BytesIO(json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8"))
        media = MediaIoBaseUpload(payload, mimetype="application/json")
        fid = self._find(name, self.root)
        if fid:
            self.svc.files().update(fileId=fid, media_body=media).execute()
        else:
            self.svc.files().create(body={"name": name, "parents": [self.root]}, media_body=media).execute()


# ---------------------------------------------------------------- YouTube
class YouTube:
    def __init__(self) -> None:
        self.svc = build("youtube", "v3", credentials=_creds("YOUTUBE_REFRESH_TOKEN", YOUTUBE_SCOPES), cache_discovery=False)

    def upload(self, path: Path, title: str, description: str, tags: list[str],
               category_id: str, privacy: str, publish_at: str | None = None) -> str:
        """publish_at (RFC 3339, UTC): upload as private and let YouTube publish it at that time.
        Only works once the Google project has passed YouTube's API audit."""
        status = {"privacyStatus": privacy, "selfDeclaredMadeForKids": False}
        if publish_at:
            status.update(privacyStatus="private", publishAt=publish_at)
        body = {
            "snippet": {"title": title, "description": description, "tags": tags,
                        "categoryId": category_id},
            "status": status,
        }
        media = MediaFileUpload(str(path), mimetype="video/mp4", resumable=True, chunksize=8 * 1024 * 1024)
        req = self.svc.videos().insert(part="snippet,status", body=body, media_body=media)
        resp = None
        while resp is None:
            _, resp = req.next_chunk()
        return resp["id"]

    def set_thumbnail(self, video_id: str, path: Path) -> None:
        """Needs a phone-verified channel; failures are logged, not fatal."""
        self.svc.thumbnails().set(videoId=video_id,
                                  media_body=MediaFileUpload(str(path), mimetype="image/jpeg")).execute()
