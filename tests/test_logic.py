import asyncio

from bot.handles import (
    pending_age_label,
    find_pending_tg_users, invite_action, normalize_matrix_handles, normalize_tg_username,
)
from bot.matrix_sync import run_matrix_cycle, user_link
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
    assert invite_action("invite", False) == "pending"  # czeka na akceptację
    assert invite_action(None, False) == "invite"
    assert invite_action("leave", False) == "invite"
    assert invite_action("leave", True) == "left"  # odrzucił/wyszedł: nie ponawiamy, ale zgłaszamy
    assert invite_action(None, True) == "skip"


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
        self.invites, self.notices, self.htmls = [], [], []
        self.attempts, self.errors, self.notice_error = [], {}, None

    async def membership(self, room, mxid):
        return self.members.get((room, mxid))

    async def invite(self, room, mxid):
        self.attempts.append((room, mxid))
        if mxid in self.errors:
            raise self.errors[mxid]
        if mxid in self.fail:
            raise RuntimeError("403 nie ma takiego usera")
        self.invites.append((room, mxid))
        self.members[(room, mxid)] = "invite"

    async def notice(self, room, text, html=None):
        if self.notice_error:
            raise self.notice_error
        self.notices.append((room, text))
        self.htmls.append(html)

    async def room_name(self, room):
        return {"!space": "Test Space", "!r": "Pokój R"}.get(room)


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


class FakeTgBot:
    def __init__(self, titles=None, broken=()):
        self.titles, self.broken = titles or {}, set(broken)
        self.sent = []

    async def get_chat(self, chat):
        from types import SimpleNamespace
        return SimpleNamespace(title=self.titles.get(chat))

    async def create_chat_invite_link(self, chat, member_limit=None, expire_date=None):
        from types import SimpleNamespace
        if chat in self.broken:
            raise RuntimeError("Not enough rights")
        assert member_limit == 1 and expire_date
        return SimpleNamespace(invite_link=f"https://t.me/+link{chat}")

    async def send_message(self, tg_id, text):
        self.sent.append((tg_id, text))


def test_group_links_have_titles_and_one_message():
    bot = FakeTgBot({-1: "Główna", -2: "Offtop"})
    n = run(TelegramLogic(None, State(), [-1, -2], bot).send_group_links(99))
    assert n == 2 and len(bot.sent) == 1
    text = bot.sent[0][1]
    assert "• Główna: https://t.me/+link-1" in text and "• Offtop: https://t.me/+link-2" in text
    assert "/grupy" in text


def test_group_links_one_failure_does_not_block_others_and_is_reported():
    bot = FakeTgBot({-1: "Główna", -2: "Zepsuta"}, broken=[-2])
    n = run(TelegramLogic(None, State(), [-1, -2], bot).send_group_links(99))
    assert n == 1
    text = bot.sent[0][1]
    assert "Główna" in text and "Nie udało się wygenerować linku do: Zepsuta" in text


def test_group_links_title_falls_back_to_id():
    bot = FakeTgBot()  # get_chat zwraca title=None
    run(TelegramLogic(None, State(), [-5], bot).send_group_links(1))
    assert "• -5: " in bot.sent[0][1]


def test_groups_command_only_for_linked_users():
    az = FakeAuthentik([tg_user(1, telegram_id=99), tg_user(2)])
    logic = TelegramLogic(az, State(), [-1])
    assert run(logic.groups_reply(99)) == (True, "")
    ok, text = run(logic.groups_reply(12345))
    assert not ok and "/start" in text
    assert run(TelegramLogic(az, State(), []).groups_reply(99))[0] is False  # brak skonfigurowanych grup


def test_user_link_is_matrix_to_user_link_and_escapes():
    assert user_link("@jan:x.pl") == '<a href="https://matrix.to/#/@jan:x.pl">@jan:x.pl</a>'
    assert "<script>" not in user_link("@a<script>:x.pl")  # nigdy surowy HTML


def test_notice_has_plain_body_and_html_user_link_with_escaped_username():
    az = FakeAuthentik([{"pk": 7, "username": "jan<b>", "attributes": {"matrix_handles": "@a:x.y"}}])
    mx = FakeMatrix()
    run(run_matrix_cycle(az, mx, State(), ["!r"]))
    assert mx.notices == [("!r", "Dodano @a:x.y na prośbę jan<b>")]  # body: czysty tekst
    html = mx.htmls[0]
    assert 'href="https://matrix.to/#/@a:x.y"' in html and "jan&lt;b&gt;" in html and "<b>" not in html


def test_declined_invite_is_reported_in_status_with_room_name_and_not_reinvited():
    st = State(); st.mark_invited("@a:x.y", "!r")
    az, mx = FakeAuthentik([matrix_user("@a:x.y")]), FakeMatrix({("!r", "@a:x.y"): "leave"})
    run(run_matrix_cycle(az, mx, st, ["!r"]))
    assert mx.invites == []
    assert az.users[0]["attributes"]["matrix_status"] == "@a:x.y odrzucił zaproszenie lub wyszedł z: Pokój R"


def test_removing_handle_forgets_it_so_readding_reinvites():
    st = State()
    user = matrix_user("@a:x.y")
    az, mx = FakeAuthentik([user]), FakeMatrix()
    run(run_matrix_cycle(az, mx, st, ["!r"]))            # zaproszony
    mx.members[("!r", "@a:x.y")] = "leave"               # odrzucił
    run(run_matrix_cycle(az, mx, st, ["!r"]))
    assert len(mx.invites) == 1                          # nie ponawiamy
    user["attributes"]["matrix_handles"] = []            # user usuwa handle
    run(run_matrix_cycle(az, mx, st, ["!r"]))
    assert not st.was_invited("@a:x.y", "!r")            # pamięć wyczyszczona
    user["attributes"]["matrix_handles"] = ["@a:x.y"]    # dopisuje z powrotem
    run(run_matrix_cycle(az, mx, st, ["!r"]))
    assert len(mx.invites) == 2                          # zaproszony ponownie


def test_forget_keeps_handles_still_listed_by_someone_and_dry_run_does_not_forget():
    st = State(); st.mark_invited("@a:x.y", "!r"); st.mark_invited("@b:x.y", "!r")
    az, mx = FakeAuthentik([matrix_user("@a:x.y")]), FakeMatrix({("!r", "@a:x.y"): "join"})
    run(run_matrix_cycle(az, mx, st, ["!r"], dry_run=True))
    assert st.was_invited("@b:x.y", "!r")                # dry-run niczego nie kasuje
    run(run_matrix_cycle(az, mx, st, ["!r"]))
    assert st.was_invited("@a:x.y", "!r") and not st.was_invited("@b:x.y", "!r")


def _age_invite(st, handle, room, hours):
    st.mark_invited(handle, room)
    st.db.execute("UPDATE invited SET ts=? WHERE handle=? AND room=?",
                  (int(__import__("time").time() - hours * 3600), handle, room))
    st.db.commit()


def test_pending_age_label_is_coarse():
    assert pending_age_label(30 * 60) == "ponad 1 h"
    assert pending_age_label(5 * 3600) == "ponad 5 h"
    assert pending_age_label(30 * 3600) == "ponad dobę"
    assert pending_age_label(3 * 86400 + 100) == "ponad 3 dni"


def test_pending_invite_reported_only_after_threshold():
    st = State()
    az, mx = FakeAuthentik([matrix_user("@a:x.y")]), FakeMatrix({("!r", "@a:x.y"): "invite"})
    _age_invite(st, "@a:x.y", "!r", hours=2)
    run(run_matrix_cycle(az, mx, st, ["!r"], pending_after_hours=24))
    assert az.users[0]["attributes"]["matrix_status"] == "ok"  # za wcześnie
    _age_invite(st, "@a:x.y", "!r", hours=30)
    run(run_matrix_cycle(az, mx, st, ["!r"], pending_after_hours=24))
    assert az.users[0]["attributes"]["matrix_status"] == "@a:x.y nie przyjął zaproszenia do: Pokój R (ponad dobę)"
    assert mx.invites == []  # nie ponawiamy


def test_pending_status_clears_when_invite_is_accepted():
    st = State()
    az, mx = FakeAuthentik([matrix_user("@a:x.y")]), FakeMatrix({("!r", "@a:x.y"): "invite"})
    _age_invite(st, "@a:x.y", "!r", hours=30)
    run(run_matrix_cycle(az, mx, st, ["!r"]))
    assert "nie przyjął" in az.users[0]["attributes"]["matrix_status"]
    mx.members[("!r", "@a:x.y")] = "join"
    run(run_matrix_cycle(az, mx, st, ["!r"]))
    assert az.users[0]["attributes"]["matrix_status"] == "ok"


def test_pending_ignored_for_invites_not_made_by_bot_and_when_disabled():
    st = State()  # bot nie zapraszał, więc brak zapisu w bazie
    az, mx = FakeAuthentik([matrix_user("@a:x.y")]), FakeMatrix({("!r", "@a:x.y"): "invite"})
    run(run_matrix_cycle(az, mx, st, ["!r"], pending_after_hours=1))
    assert az.users[0]["attributes"]["matrix_status"] == "ok"
    _age_invite(st, "@a:x.y", "!r", hours=500)
    run(run_matrix_cycle(az, mx, st, ["!r"], pending_after_hours=0))  # 0 = wyłączone
    assert az.users[0]["attributes"]["matrix_status"] == "ok"


def test_matrix_resolves_aliases_once_and_uses_room_id_in_api_calls():
    import httpx
    from bot.matrix_sync import Matrix
    calls = []

    def handler(req):
        calls.append((req.method, req.url.path))
        if "/directory/room/" in req.url.path:
            return httpx.Response(200, json={"room_id": "!abc:hackerspace.pl"})
        if req.url.path.endswith("/invite"):
            return httpx.Response(200, json={})
        return httpx.Response(404, json={})

    mx = Matrix("https://hs.test", "t", client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    assert run(mx.membership("#hskrk:hackerspace.pl", "@a:x.y")) is None
    run(mx.invite("#hskrk:hackerspace.pl", "@a:x.y"))
    run(mx.invite("!raw:x.y", "@a:x.y"))  # ID bez zmian, bez zapytania o alias
    directory = [c for c in calls if "/directory/room/" in c[1]]
    assert len(directory) == 1  # alias rozwiązany raz (cache)
    assert all("%23" not in c[1] and "#" not in c[1] for c in calls if "/rooms/" in c[1])  # w ścieżce ID, nie alias
    assert any("!abc" in c[1] or "%21abc" in c[1] for c in calls if c[1].endswith("/invite"))


def test_not_in_room_error_has_actionable_message():
    import httpx
    from bot.matrix_sync import _err
    req = httpx.Request("POST", "https://x")
    resp = httpx.Response(403, json={"errcode": "M_FORBIDDEN", "error": "User @b:x not in room !r:y, and room previews are disabled"}, request=req)
    assert "bot nie jest w tym pokoju" in _err(httpx.HTTPStatusError("e", request=req, response=resp))


def test_message_type_is_configurable_and_defaults_to_m_text():
    import json
    import httpx
    from bot.matrix_sync import Matrix
    sent = []

    def handler(req):
        sent.append(json.loads(req.content))
        return httpx.Response(200, json={"event_id": "$e"})

    def mk(**kw):
        return Matrix("https://hs.test", "t", client=httpx.AsyncClient(transport=httpx.MockTransport(handler)), **kw)

    run(mk().notice("!r:x.y", "Dodano @a:x.y na prośbę jan", "<b>x</b>"))
    run(mk(msgtype="m.notice").notice("!r:x.y", "cicho"))
    assert sent[0]["msgtype"] == "m.text" and sent[0]["format"] == "org.matrix.custom.html"
    assert sent[1]["msgtype"] == "m.notice"


def _http_error(status, body=None, headers=None):
    import httpx
    req = httpx.Request("POST", "https://hs.test/invite")
    return httpx.HTTPStatusError("e", request=req, response=httpx.Response(status, json=body or {}, headers=headers, request=req))


def test_failed_invite_backs_off_then_retries_and_clears_failure_on_success():
    import time
    st = State()
    az, mx = FakeAuthentik([matrix_user("@a:x.y")]), FakeMatrix()
    mx.errors["@a:x.y"] = _http_error(502)
    run(run_matrix_cycle(az, mx, st, ["!r"]))
    assert len(mx.attempts) == 1 and "502" in az.users[0]["attributes"]["matrix_status"]
    assert "następna próba" in az.users[0]["attributes"]["matrix_status"]
    run(run_matrix_cycle(az, mx, st, ["!r"]))   # zaraz potem: odstęp trwa, brak kolejnego żądania
    assert len(mx.attempts) == 1
    st.db.execute("UPDATE failures SET next_retry=?", (time.time() - 1,)); st.db.commit()
    del mx.errors["@a:x.y"]                      # serwer wrócił
    run(run_matrix_cycle(az, mx, st, ["!r"]))
    assert len(mx.attempts) == 2 and mx.invites == [("!r", "@a:x.y")]
    assert st.failure("@a:x.y", "!r") is None and az.users[0]["attributes"]["matrix_status"] == "ok"


def test_backoff_grows_and_is_capped():
    st = State()
    delays = []
    for i in range(7):
        nxt = st.record_failure("@a:x.y", "!r", "e", now=1000)
        delays.append(int(nxt - 1000))
    assert delays == [300, 900, 3600, 21600, 86400, 86400, 86400]


def test_429_halts_remaining_invites_in_cycle_and_respects_retry_after():
    import time
    st = State()
    az = FakeAuthentik([matrix_user("@a:x.y\n@b:x.y")])
    mx = FakeMatrix()
    mx.errors["@a:x.y"] = _http_error(429, {"errcode": "M_LIMIT_EXCEEDED", "retry_after_ms": 600000})
    run(run_matrix_cycle(az, mx, st, ["!r"]))
    assert mx.attempts == [("!r", "@a:x.y")]      # b nawet nie próbowany w tym przebiegu
    assert st.failure("@a:x.y", "!r")[1] - time.time() >= 590  # retry_after 10 min > bazowy 5 min
    del mx.errors["@a:x.y"]
    st.db.execute("UPDATE failures SET next_retry=0"); st.db.commit()
    run(run_matrix_cycle(az, mx, st, ["!r"]))     # po upływie odstępu: oba dostają zaproszenie
    assert sorted(h for _, h in mx.invites) == ["@a:x.y", "@b:x.y"]


def test_server_error_skips_remaining_rooms_for_that_handle():
    az, mx = FakeAuthentik([matrix_user("@a:x.y")]), FakeMatrix()
    mx.errors["@a:x.y"] = _http_error(502)
    run(run_matrix_cycle(az, mx, State(), ["!r", "!r2", "!r3"]))
    assert len(mx.attempts) == 1                   # 1 żądanie zamiast 3


def test_notice_failure_does_not_undo_or_retry_the_invite():
    st = State()
    az, mx = FakeAuthentik([matrix_user("@a:x.y")]), FakeMatrix()
    mx.notice_error = _http_error(429, {"retry_after_ms": 1000})
    run(run_matrix_cycle(az, mx, st, ["!r"]))
    assert mx.invites == [("!r", "@a:x.y")] and st.was_invited("@a:x.y", "!r")
    assert az.users[0]["attributes"]["matrix_status"] == "ok"
    mx.notice_error = None
    run(run_matrix_cycle(az, mx, st, ["!r"]))
    assert len(mx.invites) == 1                    # nie zapraszamy drugi raz tylko dlatego, że wiadomość padła
