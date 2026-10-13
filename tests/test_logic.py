import asyncio

from bot.handles import (
    find_pending_tg_users, invite_action, normalize_matrix_handles, normalize_tg_username,
)
from bot.matrix_sync import run_matrix_cycle
from bot.state import State
from bot.telegram_sync import TelegramLogic


def run(coro):
    return asyncio.run(coro)


# ---------- handles ----------
def test_normalize_matrix_handles_text_and_dupes():
    valid, invalid = normalize_matrix_handles("@Jan:masmiks.wtf\nanna:masmiks.wtf, @jan:masmiks.wtf; zle")
    assert valid == ["@jan:masmiks.wtf", "@anna:masmiks.wtf"]
    assert invalid == ["zle"]


def test_normalize_matrix_handles_list_and_none():
    assert normalize_matrix_handles(["@a:b.c"]) == (["@a:b.c"], [])
    assert normalize_matrix_handles(None) == ([], [])


def test_normalize_tg_username():
    assert normalize_tg_username("@Jan_Kowalski") == "jan_kowalski"
    assert normalize_tg_username("https://t.me/Jan_Kowalski") == "jan_kowalski"
    assert normalize_tg_username("abc") is None  # za krótki
    assert normalize_tg_username("dwa slowa") is None


def test_invite_action():
    assert invite_action("ban", False) == "banned"
    assert invite_action("join", False) == "skip"
    assert invite_action("invite", False) == "skip"
    assert invite_action(None, False) == "invite"
    assert invite_action("leave", False) == "invite"
    assert invite_action("leave", True) == "skip"  # świadomie wyszedł


def test_find_pending_tg_users_skips_linked():
    users = [
        {"pk": 1, "attributes": {"telegram_username": "@Jan_Kowalski"}},
        {"pk": 2, "attributes": {"telegram_username": "@jan_kowalski", "telegram_id": 5}},
    ]
    assert [u["pk"] for u in find_pending_tg_users(users, "jan_kowalski")] == [1]


# ---------- state ----------
def test_tg_code_single_use_and_ttl():
    s = State()
    code = s.new_tg_code(42, now=1000)
    assert s.consume_tg_code(code.lower(), now=1100) == 42
    assert s.consume_tg_code(code, now=1100) is None  # jednorazowy
    code = s.new_tg_code(42, now=1000)
    assert s.consume_tg_code(code, now=1000 + 16 * 60) is None  # wygasł


# ---------- fakes ----------
class FakeAuthentik:
    def __init__(self, users):
        self.users = users
        self.writes = []

    async def list_users(self):
        return self.users

    async def update_attributes(self, pk, set_=None, remove=()):
        self.writes.append((pk, set_ or {}, list(remove)))
        for u in self.users:
            if u["pk"] == pk:
                u["attributes"].update(set_ or {})
                for k in remove:
                    u["attributes"].pop(k, None)


class FakeMatrix:
    def __init__(self, membership=None, fail=()):
        self.members = membership or {}  # (room, mxid) -> membership
        self.fail = set(fail)
        self.invites, self.notices = [], []

    async def membership(self, room, mxid):
        return self.members.get((room, mxid))

    async def invite(self, room, mxid):
        if mxid in self.fail:
            raise RuntimeError("403 nie ma takiego usera")
        self.invites.append((room, mxid))
        self.members[(room, mxid)] = "invite"

    async def notice(self, room, text):
        self.notices.append((room, text))

    async def room_name(self, room):
        return {"!space": "Test Space"}.get(room)


def matrix_user(raw):
    return {"pk": 7, "username": "jan", "attributes": {"matrix_handles": raw}}


# ---------- matrix cycle ----------
def test_cycle_invites_missing_and_posts_message_once():
    az, mx, st = FakeAuthentik([matrix_user("@a:x.y\n@b:x.y")]), FakeMatrix({("!r", "@a:x.y"): "join"}), State()
    run(run_matrix_cycle(az, mx, st, ["!r"]))
    assert mx.invites == [("!r", "@b:x.y")]
    assert mx.notices == [("!r", "Dodano @b:x.y na prośbę jan")]
    # drugi przebieg nic nie robi
    run(run_matrix_cycle(az, mx, st, ["!r"]))
    assert len(mx.invites) == 1 and len(mx.notices) == 1


def test_cycle_normalizes_string_to_list_and_sets_status():
    az, mx = FakeAuthentik([matrix_user("@A:x.y zle")]), FakeMatrix()
    run(run_matrix_cycle(az, mx, State(), ["!r"]))
    attrs = az.users[0]["attributes"]
    assert attrs["matrix_handles"] == ["@a:x.y"]
    assert "niepoprawny handle: zle" in attrs["matrix_status"]


def test_cycle_one_failure_does_not_stop_others():
    az, mx = FakeAuthentik([matrix_user("@bad:x.y @ok:x.y")]), FakeMatrix(fail={"@bad:x.y"})
    stats = run(run_matrix_cycle(az, mx, State(), ["!r"]))
    assert mx.invites == [("!r", "@ok:x.y")]
    assert stats["errors"] == 1
    assert "@bad:x.y" in az.users[0]["attributes"]["matrix_status"]


def test_cycle_left_user_not_reinvited_and_dry_run_is_silent():
    st = State()
    st.mark_invited("@a:x.y", "!r")
    az, mx = FakeAuthentik([matrix_user(["@a:x.y"])]), FakeMatrix({("!r", "@a:x.y"): "leave"})
    run(run_matrix_cycle(az, mx, st, ["!r"]))
    assert mx.invites == []
    az2, mx2 = FakeAuthentik([matrix_user("@new:x.y")]), FakeMatrix()
    run(run_matrix_cycle(az2, mx2, State(), ["!r"], dry_run=True))
    assert mx2.invites == [] and az2.writes == []


# ---------- telegram ----------
def tg_user(pk, **attrs):
    return {"pk": pk, "username": f"u{pk}", "attributes": attrs}


def test_start_with_matching_username_asks_confirmation():
    az = FakeAuthentik([tg_user(1, telegram_username="@Jan_Kowalski")])
    text, pk = run(TelegramLogic(az, State(), []).start_reply(99, "jan_kowalski"))
    assert pk == 1 and "u1" in text


def test_start_without_request_gives_code_that_works_in_poll():
    st = State()
    az = FakeAuthentik([tg_user(1)])
    logic = TelegramLogic(az, st, [])
    text, pk = run(logic.start_reply(99, None))
    assert pk is None
    code = [w for w in text.split() if len(w) == 8 and w.isupper()][0]
    az.users[0]["attributes"]["telegram_code"] = code
    assert run(logic.poll_codes()) == 1
    attrs = az.users[0]["attributes"]
    assert attrs["telegram_id"] == 99 and attrs["telegram_status"] == "ok"
    assert "telegram_code" not in attrs


def test_bad_code_is_cleared_with_status():
    az = FakeAuthentik([tg_user(1, telegram_code="ZZZZZZZZ")])
    assert run(TelegramLogic(az, State(), []).poll_codes()) == 0
    assert az.users[0]["attributes"]["telegram_status"] == "zły lub wygasły kod"
    assert "telegram_code" not in az.users[0]["attributes"]


def test_link_refuses_id_owned_by_other_user():
    az = FakeAuthentik([tg_user(1, telegram_id=99), tg_user(2)])
    ok, _ = run(TelegramLogic(az, State(), []).link(2, 99))
    assert not ok and "telegram_id" not in az.users[1]["attributes"]


def test_already_linked_start_reply():
    az = FakeAuthentik([tg_user(1, telegram_id=99)])
    text, pk = run(TelegramLogic(az, State(), []).start_reply(99, "x"))
    assert pk is None and "już połączone" in text


def test_space_invite_has_no_message_in_space_but_in_notify_room():
    az, mx, st = FakeAuthentik([matrix_user("@a:x.y")]), FakeMatrix(), State()
    run(run_matrix_cycle(az, mx, st, [], spaces=["!space"], notify_room="!notify"))
    assert mx.invites == [("!space", "@a:x.y")]
    assert mx.notices == [("!notify", "Dodano @a:x.y do przestrzeni Test Space na prośbę jan")]
    run(run_matrix_cycle(az, mx, st, [], spaces=["!space"], notify_room="!notify"))
    assert len(mx.invites) == 1 and len(mx.notices) == 1  # idempotentnie


def test_space_without_notify_room_is_silent_and_room_still_gets_message():
    az, mx = FakeAuthentik([matrix_user("@a:x.y")]), FakeMatrix()
    run(run_matrix_cycle(az, mx, State(), ["!r"], spaces=["!space"]))
    assert sorted(mx.invites) == [("!r", "@a:x.y"), ("!space", "@a:x.y")]
    assert mx.notices == [("!r", "Dodano @a:x.y na prośbę jan")]


def test_code_from_already_linked_user_gets_status_and_is_cleared():
    az = FakeAuthentik([tg_user(1, telegram_id=99, telegram_code="ABCDEFGH")])
    assert run(TelegramLogic(az, State(), []).poll_codes()) == 0
    attrs = az.users[0]["attributes"]
    assert attrs["telegram_id"] == 99  # nie ruszone
    assert "telegram_code" not in attrs and "już połączony" in attrs["telegram_status"]


def test_new_start_does_not_invalidate_previous_codes_but_is_capped():
    s = State()
    first = s.new_tg_code(42, now=1000)
    second = s.new_tg_code(42, now=1010)  # drugi /start
    assert s.consume_tg_code(first, now=1020) == 42  # pierwszy nadal działa
    assert s.consume_tg_code(second, now=1020) == 42
    codes = [s.new_tg_code(7, now=2000 + i) for i in range(8)]
    ok = [c for c in codes if s.consume_tg_code(c, now=2100) == 7]
    assert len(ok) == 5 and ok == codes[-5:]  # zostaje 5 najnowszych
