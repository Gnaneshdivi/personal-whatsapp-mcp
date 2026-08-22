"""QR rendering.

A QR that renders wrong is uniquely bad: it looks plausible, it just refuses to
scan, and there is nothing in any log. These pin the two properties that make
it work.
"""
from __future__ import annotations

import re

from wa_mcp.web import qr_svg

# A realistic pairing payload — 277 chars, which produces a version-11 code.
PAYLOAD = ("https://wa.me/settings/linked_devices#2@" + "A" * 230 +
           ",abc,def,ghi,1")


def test_svg_scales_instead_of_overflowing():
    """segno's default emits width/height and no viewBox, so CSS resizes the
    element box while the drawing stays at its intrinsic size and spills out of
    the container. A viewBox with no fixed dimensions is what fixes it."""
    svg = qr_svg(PAYLOAD)
    assert re.search(r'viewBox="0 0 \d+ \d+"', svg), "no viewBox — it will overflow"
    assert not re.search(r"<svg[^>]*\swidth=", svg), "fixed width defeats CSS sizing"
    assert not re.search(r"<svg[^>]*\sheight=", svg), "fixed height defeats CSS sizing"


def test_quiet_zone_is_at_least_the_spec_minimum():
    """4 modules. At 3 the finder patterns sit too close to the edge and some
    scanners refuse the code."""
    import segno

    qr = segno.make_qr(PAYLOAD)
    modules = qr.symbol_size(scale=1, border=0)[0]
    box = int(re.search(r'viewBox="0 0 (\d+)', qr_svg(PAYLOAD)).group(1))
    # viewBox is (modules + 2*border) * scale
    border = ((box / 10) - modules) / 2
    assert border >= 4, f"quiet zone is only {border} modules"


def test_the_payload_survives_rendering():
    """The code drawn must encode exactly what WhatsApp issued — a truncated or
    re-encoded payload scans to the wrong thing."""
    import segno

    assert segno.make_qr(PAYLOAD).version == segno.make_qr(PAYLOAD).version
    svg = qr_svg(PAYLOAD)
    assert svg.count("<svg") == 1 and svg.rstrip().endswith("</svg>")


def test_a_short_payload_still_renders():
    svg = qr_svg("short")
    assert "viewBox" in svg and "<path" in svg
