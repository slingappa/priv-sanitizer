"""Microsoft Graph transfers for optional SharePoint-backed batches.

The normal sanitizer remains filesystem-only.  This module is used only when
``--sharepoint-dir`` is supplied; it resolves a SharePoint folder URL once,
downloads source files directly to local scratch, and uploads completed
deliveries to a safe ``outputs_sanitized`` child folder.
"""

from __future__ import annotations

import json
import os
import subprocess
import base64
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, unquote, urlsplit
from urllib.request import Request, urlopen

from .media import ToolError, _progress_status


GRAPH_API_BASE = "https://graph.microsoft.com/v1.0"
GRAPH_APP_ID = "8b7fb4aa-7e9c-4f5b-b016-d2ca428eb4d3"
UPLOAD_CHUNK_SIZE = 32 * 320 * 1024  # 10 MiB; Graph requires 320 KiB multiples.
TRANSFER_CHUNK_SIZE = 4 * 1024 * 1024


def _graph_auth_error(status: int) -> ToolError:
    if status == 401:
        return ToolError(
            "The Microsoft Graph token for https://graph.microsoft.com/v1.0 "
            "appears expired or invalid. Refresh it in the Secrets Manager at "
            "http://go/secretsmanager/ and rerun the SharePoint request."
        )
    if status == 403:
        return ToolError(
            "The Microsoft Graph token for https://graph.microsoft.com/v1.0 "
            "is present but lacks the required SharePoint delegated scopes. "
            "Update it in the Secrets Manager at http://go/secretsmanager/ "
            "and rerun the SharePoint request."
        )
    return ToolError(f"Microsoft Graph request failed with HTTP {status}")


def _token() -> str:
    """Retrieve a fresh delegated token without placing it in the environment."""
    try:
        result = subprocess.run(
            ["secrets", "get", "msgraph", "--url", GRAPH_APP_ID],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        raise ToolError(
            "No Microsoft Graph token was found for https://graph.microsoft.com/v1.0. "
            "Configure it in the Secrets Manager at http://go/secretsmanager/ "
            "and rerun the SharePoint request."
        ) from exc
    value = result.stdout.strip()
    if result.returncode != 0 or not value:
        raise ToolError(
            "No Microsoft Graph token was found for https://graph.microsoft.com/v1.0. "
            "Configure or refresh it in the Secrets Manager at "
            "http://go/secretsmanager/ and rerun the SharePoint request."
        )
    return value


def _path_url(parts: list[str]) -> str:
    return "/".join(quote(part, safe="") for part in parts)


def _share_id(url: str) -> str:
    """Encode a SharePoint sharing URL for Graph's ``/shares`` endpoint."""
    encoded = base64.urlsafe_b64encode(url.encode("utf-8")).decode("ascii").rstrip("=")
    return f"u!{encoded}"


@dataclass(frozen=True)
class SharePointFolder:
    url: str
    drive_id: str
    item_id: str
    drive_name: str


class SharePointClient:
    """Resolve a folder URL and stream files through Microsoft Graph."""

    def __init__(self, folder_url: str) -> None:
        parsed = urlsplit(folder_url)
        if (
            parsed.scheme.lower() != "https"
            or not parsed.netloc
            or not parsed.path
        ):
            raise ToolError("--sharepoint-dir must be an HTTPS SharePoint folder URL")
        self.folder_url = folder_url
        if parsed.path.lower().startswith("/:f:/"):
            self._folder = self._resolve_share_link(folder_url)
        else:
            if parsed.query or parsed.fragment:
                raise ToolError("--sharepoint-dir must be a folder URL, not a SharePoint page URL")
            self.folder_url = folder_url.rstrip("/")
            self._folder = self._resolve_folder(parsed)
        self._upload_folder_id: str | None = None

    @property
    def drive_id(self) -> str:
        return self._folder.drive_id

    @property
    def folder_id(self) -> str:
        return self._folder.item_id

    def _request(
        self,
        method: str,
        url: str,
        *,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
        timeout: float = 120.0,
        authenticated: bool = True,
    ):
        request_headers = dict(headers or {})
        if authenticated:
            request_headers["Authorization"] = f"Bearer {_token()}"
        request = Request(url, data=body, headers=request_headers, method=method)
        try:
            return urlopen(request, timeout=timeout)
        except HTTPError as exc:
            if exc.code in {401, 403}:
                raise _graph_auth_error(exc.code) from exc
            detail = ""
            try:
                detail = exc.read(512).decode("utf-8", errors="replace")
            except OSError:
                pass
            raise ToolError(
                f"Microsoft Graph request failed with HTTP {exc.code}"
                + (f": {detail[:200]}" if detail else "")
            ) from exc
        except URLError as exc:
            raise ToolError(f"Microsoft Graph request failed: {exc.reason}") from exc

    def _json(
        self,
        method: str,
        url: str,
        payload: dict[str, Any] | None = None,
        *,
        authenticated: bool = True,
    ) -> dict[str, Any]:
        body = None
        headers = {"Accept": "application/json"}
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        with self._request(method, url, body=body, headers=headers, authenticated=authenticated) as response:
            raw = response.read()
        try:
            value = json.loads(raw.decode("utf-8")) if raw else {}
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ToolError("Microsoft Graph returned invalid JSON") from exc
        if not isinstance(value, dict):
            raise ToolError("Microsoft Graph returned an unexpected response")
        return value

    def _resolve_folder(self, parsed) -> SharePointFolder:
        segments = [unquote(part) for part in parsed.path.split("/") if part]
        if len(segments) < 3 or segments[0].lower() not in {"sites", "teams"}:
            raise ToolError(
                "--sharepoint-dir must include a SharePoint site and document-library path, "
                "for example https://tenant.sharepoint.com/teams/site/Shared%20Documents/folder"
            )
        site_path = f"/{segments[0]}/{segments[1]}"
        library_name = segments[2]
        folder_parts = segments[3:]
        site_url = f"{GRAPH_API_BASE}/sites/{quote(parsed.netloc, safe='')}:{quote(site_path, safe='/')}"
        site = self._json("GET", site_url)
        site_id = site.get("id")
        if not site_id:
            raise ToolError("Microsoft Graph did not return a site id for --sharepoint-dir")

        drives_url = f"{GRAPH_API_BASE}/sites/{quote(str(site_id), safe='')}/drives"
        drives = self._paged_values(drives_url)
        drive = next(
            (item for item in drives if str(item.get("name", "")).casefold() == library_name.casefold()),
            None,
        )
        if not drive or not drive.get("id"):
            raise ToolError(f"SharePoint document library not found: {library_name}")
        drive_id = str(drive["id"])
        if folder_parts:
            folder_url = (
                f"{GRAPH_API_BASE}/drives/{quote(drive_id, safe='')}/root:/"
                f"{_path_url(folder_parts)}:"
            )
            item = self._json("GET", folder_url)
        else:
            item = self._json("GET", f"{GRAPH_API_BASE}/drives/{quote(drive_id, safe='')}/root")
        if not item.get("id"):
            raise ToolError("Microsoft Graph did not return a folder id for --sharepoint-dir")
        if "folder" not in item:
            raise ToolError("--sharepoint-dir does not resolve to a SharePoint folder")
        return SharePointFolder(
            url=self.folder_url,
            drive_id=drive_id,
            item_id=str(item["id"]),
            drive_name=str(drive.get("name", library_name)),
        )

    def _resolve_share_link(self, url: str) -> SharePointFolder:
        share_url = f"{GRAPH_API_BASE}/shares/{quote(_share_id(url), safe='!')}/driveItem"
        payload = self._json("GET", share_url)
        item = payload.get("remoteItem")
        if not isinstance(item, dict):
            item = payload
        parent = item.get("parentReference") or payload.get("parentReference") or {}
        drive_id = parent.get("driveId")
        item_id = item.get("id")
        if not drive_id or not item_id:
            raise ToolError("Microsoft Graph did not return a drive/folder id for the SharePoint sharing link")
        if "folder" not in item and "folder" not in payload:
            raise ToolError("--sharepoint-dir sharing link does not resolve to a SharePoint folder")
        return SharePointFolder(
            url=url,
            drive_id=str(drive_id),
            item_id=str(item_id),
            drive_name=str(parent.get("driveName") or "SharePoint"),
        )

    def _paged_values(self, url: str) -> list[dict[str, Any]]:
        values: list[dict[str, Any]] = []
        next_url: str | None = url
        while next_url:
            payload = self._json("GET", next_url)
            page = payload.get("value", [])
            if isinstance(page, list):
                values.extend(item for item in page if isinstance(item, dict))
            next_url = payload.get("@odata.nextLink")
        return values

    def _content_url(self, parent_id: str, name: str) -> str:
        return (
            f"{GRAPH_API_BASE}/drives/{quote(self.drive_id, safe='')}/items/"
            f"{quote(parent_id, safe='')}:/{quote(name, safe='')}:/content"
        )

    def download_file(self, name: str, destination: Path, *, stage: str = "staging video") -> None:
        """Stream a remote source file into a local atomic destination."""
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(f".{destination.name}.part")
        temporary.unlink(missing_ok=True)
        try:
            with self._request("GET", self._content_url(self.folder_id, name), timeout=300.0) as response:
                total_header = response.headers.get("Content-Length")
                try:
                    total = max(0, int(total_header)) if total_header else 0
                except ValueError:
                    total = 0
                copied = 0
                _progress_status(stage, 0.0)
                with temporary.open("wb") as output:
                    while True:
                        chunk = response.read(TRANSFER_CHUNK_SIZE)
                        if not chunk:
                            break
                        output.write(chunk)
                        copied += len(chunk)
                        if total:
                            percent = min(100.0, copied * 100.0 / total)
                            if percent < 100.0:
                                _progress_status(stage, percent)
                _progress_status(stage, 100.0, final=True)
            os.replace(temporary, destination)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise

    def _ensure_upload_folder(self) -> str:
        if self._upload_folder_id:
            return self._upload_folder_id
        name = "outputs_sanitized"
        path_url = (
            f"{GRAPH_API_BASE}/drives/{quote(self.drive_id, safe='')}/items/"
            f"{quote(self.folder_id, safe='')}:/{quote(name, safe='')}:"
        )
        try:
            item = self._json("GET", path_url)
        except ToolError as exc:
            if "HTTP 404" not in str(exc):
                raise
            item = self._json(
                "POST",
                f"{GRAPH_API_BASE}/drives/{quote(self.drive_id, safe='')}/items/"
                f"{quote(self.folder_id, safe='')}/children",
                {
                    "name": name,
                    "folder": {},
                    "@microsoft.graph.conflictBehavior": "fail",
                },
            )
        if not item.get("id"):
            raise ToolError("Microsoft Graph did not return the SharePoint output folder id")
        self._upload_folder_id = str(item["id"])
        return self._upload_folder_id

    def upload_file(self, source: Path, name: str | None = None) -> None:
        """Upload a completed delivery using a resumable Graph upload session."""
        source = source.expanduser().resolve()
        filename = name or source.name
        parent_id = self._ensure_upload_folder()
        session_url = (
            f"{GRAPH_API_BASE}/drives/{quote(self.drive_id, safe='')}/items/"
            f"{quote(parent_id, safe='')}:/{quote(filename, safe='')}:/createUploadSession"
        )
        session = self._json(
            "POST",
            session_url,
            {"item": {"@microsoft.graph.conflictBehavior": "replace"}},
        )
        upload_url = session.get("uploadUrl")
        if not upload_url:
            raise ToolError("Microsoft Graph did not return an upload URL")

        total = source.stat().st_size
        copied = 0
        _progress_status("uploading output", 0.0)
        with source.open("rb") as input_stream:
            if total == 0:
                request = Request(
                    str(upload_url), data=b"", headers={"Content-Length": "0"}, method="PUT"
                )
                try:
                    with urlopen(request, timeout=300.0):
                        pass
                except HTTPError as exc:
                    raise _graph_auth_error(exc.code) if exc.code in {401, 403} else ToolError(
                        f"SharePoint upload failed with HTTP {exc.code}"
                    ) from exc
            else:
                while copied < total:
                    chunk = input_stream.read(UPLOAD_CHUNK_SIZE)
                    if not chunk:
                        break
                    end = copied + len(chunk) - 1
                    headers = {
                        "Content-Length": str(len(chunk)),
                        "Content-Range": f"bytes {copied}-{end}/{total}",
                        "Content-Type": "application/octet-stream",
                    }
                    request = Request(str(upload_url), data=chunk, headers=headers, method="PUT")
                    try:
                        with urlopen(request, timeout=600.0):
                            pass
                    except HTTPError as exc:
                        raise _graph_auth_error(exc.code) if exc.code in {401, 403} else ToolError(
                            f"SharePoint upload failed with HTTP {exc.code}"
                        ) from exc
                    copied = end + 1
                    percent = min(100.0, copied * 100.0 / total)
                    if percent < 100.0:
                        _progress_status("uploading output", percent)
        _progress_status("uploading output", 100.0, final=True)
