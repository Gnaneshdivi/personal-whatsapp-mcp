from __future__ import annotations

import re

from wa_mcp.web import qr_svg

PAYLOAD = ("https://wa.me/settings/linked_devices#2@" + "A" * 230 +
           ",abc,def,ghi,1")


def test_svg_scales_instead_of_overflowing():
    svg = qr_svg(PAYLOAD)
    assert re.search(r'viewBox="0 0 \d+ \d+"', svg), "no viewBox — it will overflow"
    assert not re.search(r"<svg[^>]*\swidth=", svg), "fixed width defeats CSS sizing"
    assert not re.search(r"<svg[^>]*\sheight=", svg), "fixed height defeats CSS sizing"


def test_quiet_zone_is_at_least_the_spec_minimum():
    import segno

    qr = segno.make_qr(PAYLOAD)
    modules = qr.symbol_size(scale=1, border=0)[0]
    box = int(re.search(r'viewBox="0 0 (\d+)', qr_svg(PAYLOAD)).group(1))
    border = ((box / 10) - modules) / 2
    assert border >= 4, f"quiet zone is only {border} modules"


def test_the_payload_survives_rendering():
    import segno

    assert segno.make_qr(PAYLOAD).version == segno.make_qr(PAYLOAD).version
    svg = qr_svg(PAYLOAD)
    assert svg.count("<svg") == 1 and svg.rstrip().endswith("</svg>")


def test_a_short_payload_still_renders():
    svg = qr_svg("short")
    assert "viewBox" in svg and "<path" in svg


def test_the_outgoing_bubble_class_is_not_shared_with_the_header():
    import pathlib
    import re

    from wa_mcp.ui import CSS

    bare = re.findall(r"^\.me\{", CSS, re.M)
    assert not bare, "a bare .me rule also styles every outgoing message bubble"

    body = pathlib.Path("wa_mcp/ui.py").read_text()
    assert 'class="me"' not in body, \
        "only message bubbles may carry the `me` class"


def test_every_class_the_page_renders_has_a_rule():
    import pathlib
    import re

    from wa_mcp.ui import CSS

    body = pathlib.Path("wa_mcp/ui.py").read_text()
    rendered = set(re.findall(r'class="([a-z][a-z0-9 _-]*)"', body))
    names = {c for group in rendered for c in group.split()}
    ignore = {"sp", "wrap", "open", "hide", "sel", "on", "warn", "off"}
    missing = sorted(n for n in names - ignore
                     if not re.search(r"[.\s]%s[\s{.,:]" % re.escape(n), CSS))
    assert not missing, f"rendered but unstyled: {missing}"
