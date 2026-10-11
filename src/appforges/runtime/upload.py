"""Encode the public UploadAsset JSON data field; no network request."""

import base64


def encode_upload_data(data: bytes) -> str:
    """Encode files smaller than 4 MiB as base64, never multipart."""
    if not isinstance(data, bytes):
        raise TypeError("upload data must be bytes")
    if len(data) >= 4 * 1024 * 1024:
        raise ValueError("small-file upload requires fewer than 4 MiB")
    return base64.b64encode(data).decode("ascii")
