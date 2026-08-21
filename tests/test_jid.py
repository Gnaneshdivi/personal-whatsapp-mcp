"""JID normalisation — the thing that stops one conversation becoming three."""
from __future__ import annotations

from wa_mcp.whatsapp import jid as J


def test_device_suffix_is_stripped():
    """Live data: the account's own JID is 919100828649:7@s.whatsapp.net."""
    assert J.normalise("919100828649:7@s.whatsapp.net") == "919100828649@s.whatsapp.net"
    assert J.normalise("919100828649@s.whatsapp.net") == "919100828649@s.whatsapp.net"


def test_the_same_person_from_two_devices_is_one_chat():
    a = J.normalise("919812345678:3@s.whatsapp.net")
    b = J.normalise("919812345678:11@s.whatsapp.net")
    assert a == b


def test_server_is_lowercased():
    assert J.normalise("123@S.WhatsApp.Net") == "123@s.whatsapp.net"


def test_classification():
    assert J.is_group("1234-5678@g.us")
    assert J.is_lid("207696196305131@lid")
    assert not J.is_lid("919812345678@s.whatsapp.net")
    assert J.is_ignorable("status@broadcast")
    assert J.is_ignorable("abc@newsletter")
    assert not J.is_ignorable("919812345678@s.whatsapp.net")


def test_ignorable_survives_a_device_suffix():
    assert J.is_ignorable("status:2@broadcast")


def test_phone_extraction():
    assert J.phone("919812345678:7@s.whatsapp.net") == "919812345678"
    assert J.phone("207696196305131@lid") == ""      # LIDs carry no number
    assert J.phone("1234-5678@g.us") == ""


def test_modern_all_digit_group_ids_are_not_phone_numbers():
    """120363228197508350@g.us is all digits; isdigit() alone hands it back as
    a phone number and the chat list shows 18 digits instead of a name."""
    assert J.phone("120363228197508350@g.us") == ""
    assert J.phone("919980982358-1479370608@g.us") == ""


def test_to_jid_accepts_what_a_model_would_type():
    assert J.to_jid("919812345678") == "919812345678@s.whatsapp.net"
    assert J.to_jid("+919812345678") == "919812345678@s.whatsapp.net"
    assert J.to_jid("919812345678@s.whatsapp.net") == "919812345678@s.whatsapp.net"
    assert J.to_jid("1234-5678@g.us") == "1234-5678@g.us"
    assert J.to_jid("") == ""


def test_from_obj_renders_a_protobuf_jid():
    class FakeJID:
        User = "919100828649:7"
        Server = "s.whatsapp.net"

    assert J.from_obj(FakeJID()) == "919100828649@s.whatsapp.net"
    assert J.from_obj(None) == ""


# ---------------------------------------------------------------- LID resolver

async def test_resolver_canonicalises_a_lid():
    def lookup(_lid):
        class R:
            User = "919812345678"
            Server = "s.whatsapp.net"
        return R()

    r = J.Resolver(lookup)
    assert await r.canonical("207696196305131@lid") == "919812345678@s.whatsapp.net"


async def test_resolver_caches_so_ingest_does_not_hammer_go():
    calls = []

    def lookup(lid):
        calls.append(lid)
        class R:
            User = "919812345678"
            Server = "s.whatsapp.net"
        return R()

    r = J.Resolver(lookup)
    for _ in range(5):
        await r.canonical("207696196305131@lid")
    assert len(calls) == 1


async def test_unresolvable_lid_stays_a_stable_identifier():
    """Better a coherent conversation under a LID than a lost message."""
    def lookup(_lid):
        raise RuntimeError("no mapping")

    r = J.Resolver(lookup)
    assert await r.canonical("207696196305131@lid") == "207696196305131@lid"


async def test_non_lid_never_hits_the_resolver():
    def lookup(_lid):
        raise AssertionError("should not be called")

    r = J.Resolver(lookup)
    assert await r.canonical("919812345678@s.whatsapp.net") == "919812345678@s.whatsapp.net"
