from __future__ import annotations

from wa_mcp.whatsapp import extract


def test_a_media_descriptor_is_json_serialisable():
    import json

    from neonize.proto.waE2E import WAWebProtobufsE2E_pb2 as E2E

    body = E2E.Message()
    body.imageMessage.mimetype = "image/jpeg"
    body.imageMessage.fileLength = 4096
    body.imageMessage.fileSHA256 = b"\x00\xff\xfe not text"

    meta = extract.media_descriptor(body)
    assert meta is not None and meta["kind"] == "image"
    json.dumps(meta)


def test_the_descriptor_key_the_web_serves_media_by():
    from pathlib import Path

    from neonize.proto.waE2E import WAWebProtobufsE2E_pb2 as E2E

    body = E2E.Message()
    body.imageMessage.mimetype = "image/jpeg"
    meta = extract.media_descriptor(body)

    source = Path("wa_mcp/web.py").read_text()
    key = next(k for k in meta if "mime" in k)
    assert f'.get("{key}")' in source, f"web.py does not read {key!r}"


def test_mime_is_sniffed_from_the_bytes_not_the_kind():
    from wa_mcp.whatsapp.client import _sniff_mime

    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 8
    jpg = b"\xff\xd8\xff\xe0" + b"\x00" * 8
    webp = b"RIFF\x00\x00\x00\x00WEBP" + b"\x00" * 4
    wav = b"RIFF\x00\x00\x00\x00WAVE" + b"\x00" * 4

    assert _sniff_mime(png, "image") == "image/png"
    assert _sniff_mime(jpg, "image") == "image/jpeg"
    assert _sniff_mime(webp, "sticker") == "image/webp"
    assert _sniff_mime(wav, "audio") == "audio/wav"
    assert _sniff_mime(b"\x00" * 16, "video") == "video/mp4"
    assert _sniff_mime(b"", "document") == "application/octet-stream"
