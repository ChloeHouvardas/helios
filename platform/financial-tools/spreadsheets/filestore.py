"""In-memory file store for uploaded statements and rendered sheet files.

Files are content-addressed by file_id. Limit: 50 MB per uploaded file.
"""

from __future__ import annotations

import hashlib
import io
import zipfile

from .errors import HeliosError, validation

MAX_FILE_BYTES = 50 * 1024 * 1024


def mask_account(value: str) -> str:
    """Redact to the last four digits; leaves short values alone."""
    digits = "".join(ch for ch in value if ch.isdigit())
    if len(digits) <= 4:
        return value
    return "****" + digits[-4:]


class StoredFile:
    def __init__(self, file_id: str, filename: str, content: bytes,
                 business_id: str):
        self.file_id = file_id
        self.filename = filename
        self.content = content
        self.business_id = business_id
        self.sha = hashlib.sha256(content).hexdigest()[:32]

    @property
    def size(self) -> int:
        return len(self.content)


class FileStore:
    def __init__(self) -> None:
        self._files: dict[str, StoredFile] = {}

    def put(self, filename: str, content: bytes, business_id: str) -> StoredFile:
        if len(content) > MAX_FILE_BYTES:
            raise HeliosError(
                "validation_error",
                f"file exceeds 50 MB limit ({len(content)} bytes)")
        file_id = "file_" + hashlib.sha256(
            f"{business_id}:{filename}:".encode() + content).hexdigest()[:20]
        stored = StoredFile(file_id, filename, content, business_id)
        self._files[file_id] = stored
        return stored

    def get(self, file_id: str, business_id: str) -> StoredFile:
        stored = self._files.get(file_id)
        if stored is None or stored.business_id != business_id:
            raise validation(f"unknown file_id '{file_id}'")
        return stored


def is_zip(content: bytes) -> bool:
    return zipfile.is_zipfile(io.BytesIO(content))


def is_xlsx(content: bytes) -> bool:
    if not is_zip(content):
        return False
    names = zipfile.ZipFile(io.BytesIO(content)).namelist()
    return any(n.startswith("xl/") for n in names)


def read_zip_member(content: bytes, password: str | None) -> tuple[str, bytes]:
    """Read the first member of a statement ZIP (WeChat/Alipay bills contain
    a single CSV). Raises validation_error when a password is required or
    wrong — the password itself is never echoed back."""
    try:
        zf = zipfile.ZipFile(io.BytesIO(content))
    except (zipfile.BadZipFile, ValueError) as exc:
        raise HeliosError(
            "unsupported_format",
            "file is not a readable ZIP archive") from exc
    members = [m for m in zf.namelist() if not m.endswith("/")]
    if not members:
        raise validation("ZIP archive is empty")
    member = zf.getinfo(members[0])
    if member.flag_bits & 0x1 and not password:
        raise validation("encrypted ZIP requires archive_password")
    pwd = password.encode("utf-8") if password else None
    try:
        with zf.open(member, pwd=pwd) as fh:
            return member.filename, fh.read()
    except RuntimeError as exc:
        raise validation(
            "archive_password is missing or incorrect") from exc


def decode_statement_bytes(data: bytes, filename: str = "") -> str:
    """Decode CSV/TXT content trying UTF-8 (BOM) then GB18030/GBK."""
    for encoding in ("utf-8-sig", "gb18030", "gbk"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise HeliosError("unsupported_format",
                      f"'{filename}' is not UTF-8/GBK text")


def redact_for_logs(payload):
    """Strip Authorization-like values before anything is logged."""
    if isinstance(payload, dict):
        return {
            k: ("[REDACTED]" if k.lower() in ("authorization", "api_key",
                                              "archive_password", "token",
                                              "password")
                else redact_for_logs(v))
            for k, v in payload.items()
        }
    if isinstance(payload, list):
        return [redact_for_logs(v) for v in payload]
    return payload
