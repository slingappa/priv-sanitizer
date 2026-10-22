from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

from priv_sanitizer.sharepoint import SharePointClient, SharePointFolder


def _client_without_resolution():
    folder = SharePointFolder(
        url="https://tenant.sharepoint.com/teams/site/Shared%20Documents/Recordings",
        drive_id="drive-id",
        item_id="folder-id",
        drive_name="Shared Documents",
    )
    with patch.object(SharePointClient, "_resolve_folder", return_value=folder):
        return SharePointClient(folder.url)


def test_sharepoint_content_urls_quote_file_names():
    client = _client_without_resolution()
    url = client._content_url(client.folder_id, "Quarter 1 & review.mp4")
    assert "/items/folder-id:/Quarter%201%20%26%20review.mp4:/content" in url


def test_sharepoint_folder_url_resolves_site_drive_and_folder():
    client = object.__new__(SharePointClient)
    client.folder_url = "https://tenant.sharepoint.com/teams/site/Shared%20Documents/Recordings"
    calls = []
    responses = iter([
        {"id": "site-id"},
        [{"id": "drive-id", "name": "Shared Documents"}],
        {"id": "recordings-id", "folder": {}},
    ])

    def fake_json(method, url, payload=None, **kwargs):
        calls.append((method, url))
        value = next(responses)
        return {"value": value} if isinstance(value, list) else value

    with patch.object(client, "_json", side_effect=fake_json):
        folder = client._resolve_folder(
            urlsplit(client.folder_url)
        )

    assert folder.drive_id == "drive-id"
    assert folder.item_id == "recordings-id"
    assert "/sites/tenant.sharepoint.com:/teams/site" in calls[0][1]
    assert "/drives/drive-id/root:/Recordings:" in calls[2][1]


def test_sharepoint_download_streams_to_atomic_destination(tmp_path):
    client = _client_without_resolution()

    class Response:
        headers = {"Content-Length": "10"}

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def read(self, size=-1):
            if not hasattr(self, "done"):
                self.done = True
                return b"0123456789"
            return b""

    progress = []
    destination = tmp_path / "downloaded.mp4"
    with patch.object(client, "_request", return_value=Response()), patch(
        "priv_sanitizer.sharepoint._progress_status",
        side_effect=lambda stage, percent, **kwargs: progress.append((stage, percent, kwargs.get("final", False))),
    ):
        client.download_file("source.mp4", destination)

    assert destination.read_bytes() == b"0123456789"
    assert progress[0] == ("staging video", 0.0, False)
    assert progress[-1] == ("staging video", 100.0, True)
    assert not list(tmp_path.glob("*.part"))
