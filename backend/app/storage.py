"""Object storage gateway (Supabase Storage in production).

Contract §6.1: the client gets a signed upload URL, uploads directly to Storage, then calls
complete-upload; the server HEAD-checks the object before accepting it.
"""

from dataclasses import dataclass, field
from typing import Protocol
from uuid import UUID

BUCKET = "lesson-content"


def object_key_for(school_id: UUID, content_item_id: UUID) -> str:
    return f"schools/{school_id}/content/{content_item_id}"


@dataclass(frozen=True)
class SignedUpload:
    object_key: str
    upload_url: str
    expires_in_seconds: int


class StorageGateway(Protocol):
    async def signed_upload(self, object_key: str) -> SignedUpload: ...
    async def object_size(self, object_key: str) -> int | None: ...


@dataclass
class StubStorage:
    """In-memory storage for local runs and tests. `put()` simulates the client's upload."""

    objects: dict[str, int] = field(default_factory=dict)

    async def signed_upload(self, object_key: str) -> SignedUpload:
        return SignedUpload(object_key, f"stub://{BUCKET}/{object_key}?signed=1", 3600)

    async def object_size(self, object_key: str) -> int | None:
        return self.objects.get(object_key)

    def put(self, object_key: str, size: int) -> None:
        self.objects[object_key] = size


_storage: StorageGateway | None = None


def get_storage() -> StorageGateway:
    global _storage
    if _storage is None:
        _storage = StubStorage()
    return _storage


def set_storage(storage: StorageGateway | None) -> None:
    global _storage
    _storage = storage
