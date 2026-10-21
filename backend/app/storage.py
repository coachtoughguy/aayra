"""Object storage gateway (Supabase Storage in production).

Contract §6.1: the client gets a signed upload URL, uploads directly to Storage, then calls
complete-upload; the server HEAD-checks the object before accepting it.

- `StubStorage`: in-memory, for local runs and tests.
- `SupabaseStorage`: Supabase Storage REST API with the service-role key (server side only).
Select with AAYRA_STORAGE_PROVIDER=stub|supabase.
"""

import re
from dataclasses import dataclass, field
from typing import Protocol
from urllib.parse import quote
from uuid import UUID

import httpx

from app.config import get_settings

BUCKET = "lesson-content"
_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


def object_key_for(school_id: UUID, content_item_id: UUID) -> str:
    return f"schools/{school_id}/content/{content_item_id}"


def submission_prefix(school_id: UUID, student_assignment_id: UUID) -> str:
    return f"schools/{school_id}/submissions/{student_assignment_id}/"


def safe_file_name(name: str) -> str:
    cleaned = _SAFE_NAME.sub("-", name).strip("-.")[:120]
    return cleaned or "file"


@dataclass(frozen=True)
class SignedUpload:
    object_key: str
    upload_url: str
    expires_in_seconds: int


class StorageError(Exception):
    pass


class StorageGateway(Protocol):
    async def signed_upload(self, object_key: str) -> SignedUpload: ...
    async def object_size(self, object_key: str) -> int | None: ...
    async def download(self, object_key: str) -> bytes: ...
    async def signed_download_url(self, object_key: str, expires_in: int = 3600) -> str: ...


@dataclass
class StubStorage:
    """In-memory storage for local runs and tests. `put()` simulates the client's upload."""

    objects: dict[str, int] = field(default_factory=dict)
    contents: dict[str, bytes] = field(default_factory=dict)

    async def signed_upload(self, object_key: str) -> SignedUpload:
        return SignedUpload(object_key, f"stub://{BUCKET}/{object_key}?signed=1", 3600)

    async def object_size(self, object_key: str) -> int | None:
        return self.objects.get(object_key)

    async def download(self, object_key: str) -> bytes:
        if object_key not in self.objects:
            raise StorageError(f"no object at {object_key}")
        return self.contents.get(object_key, b"")

    async def signed_download_url(self, object_key: str, expires_in: int = 3600) -> str:
        return f"stub://{BUCKET}/{object_key}?download=1&expires_in={expires_in}"

    def put(self, object_key: str, size: int, content: bytes | None = None) -> None:
        self.objects[object_key] = size
        if content is not None:
            self.contents[object_key] = content


class SupabaseStorage:
    """Supabase Storage REST API (storage/v1). Uses the service-role key; never expose it to clients.
    Implemented against the documented endpoints; exercised in tests with a mocked transport."""

    def __init__(self, url: str, service_key: str, bucket: str = BUCKET, client: httpx.AsyncClient | None = None):
        self.base = url.rstrip("/") + "/storage/v1"
        self.bucket = bucket
        self.headers = {"Authorization": f"Bearer {service_key}", "apikey": service_key}
        self.client = client or httpx.AsyncClient(timeout=30)

    def _path(self, object_key: str) -> str:
        return f"{self.bucket}/{quote(object_key, safe='/')}"

    async def signed_upload(self, object_key: str) -> SignedUpload:
        r = await self.client.post(f"{self.base}/object/upload/sign/{self._path(object_key)}", headers=self.headers)
        if r.status_code >= 400:
            raise StorageError(f"signed upload failed: {r.status_code} {r.text[:200]}")
        return SignedUpload(object_key, self.base + r.json()["url"], 7200)

    async def object_size(self, object_key: str) -> int | None:
        r = await self.client.head(f"{self.base}/object/authenticated/{self._path(object_key)}", headers=self.headers)
        if r.status_code in (400, 404):
            return None
        if r.status_code >= 400:
            raise StorageError(f"head failed: {r.status_code}")
        return int(r.headers.get("content-length", "0"))

    async def download(self, object_key: str) -> bytes:
        r = await self.client.get(f"{self.base}/object/authenticated/{self._path(object_key)}", headers=self.headers)
        if r.status_code >= 400:
            raise StorageError(f"download failed: {r.status_code}")
        return r.content

    async def signed_download_url(self, object_key: str, expires_in: int = 3600) -> str:
        r = await self.client.post(
            f"{self.base}/object/sign/{self._path(object_key)}", headers=self.headers, json={"expiresIn": expires_in}
        )
        if r.status_code >= 400:
            raise StorageError(f"signed download failed: {r.status_code}")
        return self.base + r.json()["signedURL"]


_storage: StorageGateway | None = None


def get_storage() -> StorageGateway:
    global _storage
    if _storage is None:
        s = get_settings()
        if s.storage_provider == "supabase":
            if not (s.supabase_url and s.supabase_service_role_key):
                raise RuntimeError(
                    "AAYRA_SUPABASE_URL and AAYRA_SUPABASE_SERVICE_ROLE_KEY are required for Supabase storage"
                )
            _storage = SupabaseStorage(s.supabase_url, s.supabase_service_role_key, s.storage_bucket)
        else:
            _storage = StubStorage()
    return _storage


def set_storage(storage: StorageGateway | None) -> None:
    global _storage
    _storage = storage
