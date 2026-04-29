from __future__ import annotations

import unittest

from app.main import DriveClient


class FakeDriveRequest:
    def __init__(self, response: dict) -> None:
        self.response = response

    def execute(self) -> dict:
        return self.response


class FakeDriveFiles:
    def __init__(self, existing_files: list[dict] | None = None) -> None:
        self.existing_files = existing_files or []
        self.list_calls: list[dict] = []
        self.create_calls: list[dict] = []

    def list(self, **kwargs) -> FakeDriveRequest:
        self.list_calls.append(kwargs)
        return FakeDriveRequest({"files": self.existing_files})

    def create(self, **kwargs) -> FakeDriveRequest:
        self.create_calls.append(kwargs)
        return FakeDriveRequest({"id": "created-folder-id", "name": kwargs["body"]["name"]})


class FakeDriveService:
    def __init__(self, existing_files: list[dict] | None = None) -> None:
        self.files_resource = FakeDriveFiles(existing_files)

    def files(self) -> FakeDriveFiles:
        return self.files_resource


def make_drive_client(service: FakeDriveService) -> DriveClient:
    client = DriveClient.__new__(DriveClient)
    client.service = service
    client.root_folder_id = "root-folder-id"
    client.folder_cache = {}
    return client


class DriveClientEnsureFolderTests(unittest.TestCase):
    def test_existing_folder_is_reused_without_create(self) -> None:
        service = FakeDriveService(existing_files=[{"id": "existing-folder-id", "name": "reg1"}])
        client = make_drive_client(service)

        folder_id = client.ensure_folder("parent-folder-id", "reg1")

        self.assertEqual(folder_id, "existing-folder-id")
        self.assertEqual(len(service.files_resource.list_calls), 1)
        self.assertEqual(service.files_resource.create_calls, [])

    def test_missing_folder_is_created_once(self) -> None:
        service = FakeDriveService(existing_files=[])
        client = make_drive_client(service)

        folder_id = client.ensure_folder("parent-folder-id", "picture_path")

        self.assertEqual(folder_id, "created-folder-id")
        self.assertEqual(len(service.files_resource.list_calls), 1)
        self.assertEqual(len(service.files_resource.create_calls), 1)
        self.assertEqual(
            service.files_resource.create_calls[0]["body"],
            {
                "name": "picture_path",
                "mimeType": "application/vnd.google-apps.folder",
                "parents": ["parent-folder-id"],
            },
        )

    def test_cached_folder_skips_drive_lookup_and_create(self) -> None:
        service = FakeDriveService(existing_files=[{"id": "existing-folder-id", "name": "valve"}])
        client = make_drive_client(service)

        first_folder_id = client.ensure_folder("parent-folder-id", "valve")
        second_folder_id = client.ensure_folder("parent-folder-id", "valve")

        self.assertEqual(first_folder_id, "existing-folder-id")
        self.assertEqual(second_folder_id, "existing-folder-id")
        self.assertEqual(len(service.files_resource.list_calls), 1)
        self.assertEqual(service.files_resource.create_calls, [])


if __name__ == "__main__":
    unittest.main()
