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
    assert invite_action("join", False) == "joined"
    assert invite_action("invite", False) == "pending"  # czeka na akceptację
    assert invite_action(None, False) == "invite"
    assert invite_action("leave", False) == "invite"
    assert invite_action("leave", True) == "left"  # odrzucił/wyszedł: nie ponawiamy, ale zgłaszamy
    assert invite_action(None, True) == "skip"


def test_find_pending_tg_users_matches_normalized_username_only():
    users = [
        {"pk": 1, "attributes": {"telegramUsername": "@Jan_Kowalski"}},
        {"pk": 2, "attributes": {"telegramUsername": "@ktos_inny"}},
        {"pk": 3, "attributes": {}},
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

    async def list_users(self, include_inactive=False):
        return self.users

    before_write = None  # symulacja: admin dopisuje coś do atrybutów tuż przed zapisem bota

    async def update_attributes(self, pk, set_=None, remove=(), mutate=None):
        self.writes.append((pk, set_ or {}, list(remove)))
        for u in self.users:
            if u["pk"] == pk:
                if self.before_write:
                    self.before_write(u["attributes"])
                u["attributes"].update(set_ or {})
                for k in remove:
                    u["attributes"].pop(k, None)
                if mutate:
                    mutate(u["attributes"])


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
    return {"pk": 7, "username": "jan", "attributes": {"matrixHandles": raw}}


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
    assert attrs["matrixHandles"] == ["@a:x.y"]
    assert "niepoprawny handle: zle" in attrs["matrixStatus"]


def test_cycle_one_failure_does_not_stop_others():
    az, mx = FakeAuthentik([matrix_user("@bad:x.y @ok:x.y")]), FakeMatrix(fail={"@bad:x.y"})
    stats = run(run_matrix_cycle(az, mx, State(), ["!r"]))
    assert mx.invites == [("!r", "@ok:x.y")]
    assert stats["errors"] == 1
    assert "@bad:x.y" in az.users[0]["attributes"]["matrixStatus"]


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
    az = FakeAuthentik([tg_user(1, telegramUsername="@Jan_Kowalski")])
    text, pk = run(TelegramLogic(az, State(), []).start_reply(99, "jan_kowalski"))
    assert pk == 1 and "u1" in text


def test_start_without_request_gives_code_that_works_in_poll():
    st = State()
    az = FakeAuthentik([tg_user(1)])
    logic = TelegramLogic(az, st, [])
    text, pk = run(logic.start_reply(99, None))
    assert pk is None
    code = [w for w in text.split() if len(w) == 8 and w.isupper()][0]
    az.users[0]["attributes"]["telegramCode"] = code
    assert run(logic.poll_codes()) == 1
    attrs = az.users[0]["attributes"]
    assert attrs["telegramIDs"] == ["99"] and attrs["telegramStatus"] == "ok"
    assert "telegramCode" not in attrs


def test_bad_code_is_cleared_with_status():
    az = FakeAuthentik([tg_user(1, telegramCode="ZZZZZZZZ")])
    assert run(TelegramLogic(az, State(), []).poll_codes()) == 0
    assert az.users[0]["attributes"]["telegramStatus"] == "zły lub wygasły kod"
    assert "telegramCode" not in az.users[0]["attributes"]


def test_link_refuses_id_owned_by_other_user():
    az = FakeAuthentik([tg_user(1, telegramIDs=["99"]), tg_user(2)])
    ok, _ = run(TelegramLogic(az, State(), []).link(2, 99))
    assert not ok and "telegramIDs" not in az.users[1]["attributes"]


def test_already_linked_start_reply():
    az = FakeAuthentik([tg_user(1, telegramIDs=["99"])])
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


def test_second_telegram_account_can_be_linked_to_same_user_by_code():
    st = State()
    az = FakeAuthentik([tg_user(1, telegramIDs=["99"])])
    logic = TelegramLogic(az, st, [])
    code = st.new_tg_code(100)
    az.users[0]["attributes"]["telegramCode"] = code
    assert run(logic.poll_codes()) == 1
    attrs = az.users[0]["attributes"]
    assert attrs["telegramIDs"] == ["99", "100"]            # wiele kont na usera: dopisane, pierwsze nietknięte
    assert "telegramCode" not in attrs and attrs["telegramStatus"] == "ok"


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
    az = FakeAuthentik([tg_user(1, telegramIDs=["99"]), tg_user(2)])
    logic = TelegramLogic(az, State(), [-1])
    assert run(logic.groups_reply(99)) == (True, "")
    ok, text = run(logic.groups_reply(12345))
    assert not ok and "/start" in text
    assert run(TelegramLogic(az, State(), []).groups_reply(99))[0] is False  # brak skonfigurowanych grup


def test_user_link_is_matrix_to_user_link_and_escapes():
    assert user_link("@jan:x.pl") == '<a href="https://matrix.to/#/@jan:x.pl">@jan:x.pl</a>'
    assert "<script>" not in user_link("@a<script>:x.pl")  # nigdy surowy HTML


def test_notice_has_plain_body_and_html_user_link_with_escaped_username():
    az = FakeAuthentik([{"pk": 7, "username": "jan<b>", "attributes": {"matrixHandles": "@a:x.y"}}])
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
    assert az.users[0]["attributes"]["matrixStatus"] == "@a:x.y odrzucił zaproszenie lub wyszedł z: Pokój R"


def test_removing_handle_forgets_it_so_readding_reinvites():
    st = State()
    user = matrix_user("@a:x.y")
    az, mx = FakeAuthentik([user]), FakeMatrix()
    run(run_matrix_cycle(az, mx, st, ["!r"]))            # zaproszony
    mx.members[("!r", "@a:x.y")] = "leave"               # odrzucił
    run(run_matrix_cycle(az, mx, st, ["!r"]))
    assert len(mx.invites) == 1                          # nie ponawiamy
    user["attributes"]["matrixHandles"] = []            # user usuwa handle
    run(run_matrix_cycle(az, mx, st, ["!r"]))
    assert not st.was_invited("@a:x.y", "!r")            # pamięć wyczyszczona
    user["attributes"]["matrixHandles"] = ["@a:x.y"]    # dopisuje z powrotem
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
    assert az.users[0]["attributes"]["matrixStatus"] == "ok"  # za wcześnie
    _age_invite(st, "@a:x.y", "!r", hours=30)
    run(run_matrix_cycle(az, mx, st, ["!r"], pending_after_hours=24))
    assert az.users[0]["attributes"]["matrixStatus"] == "@a:x.y nie przyjął zaproszenia do: Pokój R (ponad dobę)"
    assert mx.invites == []  # nie ponawiamy


def test_pending_status_clears_when_invite_is_accepted():
    st = State()
    az, mx = FakeAuthentik([matrix_user("@a:x.y")]), FakeMatrix({("!r", "@a:x.y"): "invite"})
    _age_invite(st, "@a:x.y", "!r", hours=30)
    run(run_matrix_cycle(az, mx, st, ["!r"]))
    assert "nie przyjął" in az.users[0]["attributes"]["matrixStatus"]
    mx.members[("!r", "@a:x.y")] = "join"
    run(run_matrix_cycle(az, mx, st, ["!r"]))
    assert az.users[0]["attributes"]["matrixStatus"] == "ok"


def test_pending_ignored_for_invites_not_made_by_bot_and_when_disabled():
    st = State()  # bot nie zapraszał, więc brak zapisu w bazie
    az, mx = FakeAuthentik([matrix_user("@a:x.y")]), FakeMatrix({("!r", "@a:x.y"): "invite"})
    run(run_matrix_cycle(az, mx, st, ["!r"], pending_after_hours=1))
    assert az.users[0]["attributes"]["matrixStatus"] == "ok"
    _age_invite(st, "@a:x.y", "!r", hours=500)
    run(run_matrix_cycle(az, mx, st, ["!r"], pending_after_hours=0))  # 0 = wyłączone
    assert az.users[0]["attributes"]["matrixStatus"] == "ok"


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
    assert len(mx.attempts) == 1 and "502" in az.users[0]["attributes"]["matrixStatus"]
    assert "następna próba" in az.users[0]["attributes"]["matrixStatus"]
    run(run_matrix_cycle(az, mx, st, ["!r"]))   # zaraz potem: odstęp trwa, brak kolejnego żądania
    assert len(mx.attempts) == 1
    st.db.execute("UPDATE failures SET next_retry=?", (time.time() - 1,)); st.db.commit()
    del mx.errors["@a:x.y"]                      # serwer wrócił
    run(run_matrix_cycle(az, mx, st, ["!r"]))
    assert len(mx.attempts) == 2 and mx.invites == [("!r", "@a:x.y")]
    assert st.failure("@a:x.y", "!r") is None and az.users[0]["attributes"]["matrixStatus"] == "ok"


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
    assert az.users[0]["attributes"]["matrixStatus"] == "ok"
    mx.notice_error = None
    run(run_matrix_cycle(az, mx, st, ["!r"]))
    assert len(mx.invites) == 1                    # nie zapraszamy drugi raz tylko dlatego, że wiadomość padła


SETTINGS = "https://auth.sq9.wtf/if/user/#/settings"


def test_group_help_explains_start_and_grupy_dm_only_and_has_settings_link():
    text = TelegramLogic(None, State(), [], settings_url=SETTINGS).group_help("hsowyzapraszacz_bot")
    assert "@hsowyzapraszacz_bot" in text and "prywatnej" in text
    assert "/start" in text and "/grupy" in text and SETTINGS in text
    assert SETTINGS not in TelegramLogic(None, State(), []).group_help()  # bez adresu: bez linku, bez błędu


def test_dm_replies_include_settings_link_when_user_must_enter_data():
    az = FakeAuthentik([tg_user(1)])
    logic = TelegramLogic(az, State(), [-1], settings_url=SETTINGS)
    text, pk = run(logic.start_reply(99, None))
    assert pk is None and SETTINGS in text
    ok, text = run(logic.groups_reply(12345))
    assert not ok and SETTINGS in text and "/start" in text
    # połączony user nie dostaje zbędnego linku
    az2 = FakeAuthentik([tg_user(1, telegramIDs=["99"])])
    assert SETTINGS not in run(TelegramLogic(az2, State(), [], settings_url=SETTINGS).start_reply(99, "x"))[0]


def _fake_update(chat_type):
    from types import SimpleNamespace
    sent = []

    async def reply_text(text, **kw):
        sent.append(text)
    upd = SimpleNamespace(effective_chat=SimpleNamespace(type=chat_type, id=-5),
                          effective_user=SimpleNamespace(id=7, username="jan_kowalski"),
                          message=SimpleNamespace(reply_text=reply_text))
    return upd, SimpleNamespace(bot=SimpleNamespace(username="hsowyzapraszacz_bot")), sent


def test_commands_in_configured_group_get_instructions_not_silence():
    logic = TelegramLogic(None, State(), [-5], settings_url=SETTINGS)  # _fake_update używa czatu -5
    for handler in (logic.on_start, logic.on_groups):
        for chat_type in ("group", "supergroup"):
            upd, ctx, sent = _fake_update(chat_type)
            run(handler(upd, ctx))
            assert len(sent) == 1 and "prywatnej" in sent[0] and "@hsowyzapraszacz_bot" in sent[0]


def test_ptb_command_handler_matches_plain_and_addressed_forms_only_for_this_bot():
    from datetime import datetime
    from telegram import Bot, Chat, Message, MessageEntity, Update, User
    from telegram.ext import CommandHandler

    bot = Bot("123:abc")
    bot._bot_user = User(id=1, first_name="inviter", is_bot=True, username="hsowyzapraszacz_bot")

    async def noop(update, context):
        pass

    h = CommandHandler("grupy", noop)

    def update_for(text):
        msg = Message(message_id=1, date=datetime.now(), chat=Chat(id=-5, type="supergroup"), text=text,
                      entities=[MessageEntity(MessageEntity.BOT_COMMAND, 0, len(text.split()[0]))])
        msg.set_bot(bot)
        return Update(update_id=1, message=msg)

    assert h.check_update(update_for("/grupy")) is not None
    assert h.check_update(update_for("/grupy@hsowyzapraszacz_bot")) is not None
    assert h.check_update(update_for("/grupy@INNY_bot")) is None  # komenda do innego bota: nie nasza sprawa


def test_commands_in_unlisted_group_are_ignored_silently():
    logic = TelegramLogic(None, State(), [-1, -2], settings_url=SETTINGS)  # czat testowy -5 nie jest w TELEGRAM_GROUPS
    for handler in (logic.on_start, logic.on_groups):
        upd, ctx, sent = _fake_update("supergroup")
        run(handler(upd, ctx))
        assert sent == []  # zero odpowiedzi: bot dodany do losowej grupy nie zdradza, do czego służy


# ---------- matrixAccounts ----------
def test_matrix_accounts_report_state_of_each_handle():
    st = State(); st.mark_invited("@b:x.y", "!r")
    az = FakeAuthentik([matrix_user("@a:x.y @b:x.y @c:x.y @d:x.y")])
    mx = FakeMatrix({("!r", "@a:x.y"): "join", ("!r", "@b:x.y"): "invite", ("!r", "@c:x.y"): "ban"})
    run(run_matrix_cycle(az, mx, st, ["!r"]))
    acc = az.users[0]["attributes"]["matrixAccounts"]
    assert acc["@a:x.y"]["state"] == "joined"
    assert acc["@b:x.y"]["state"] == "pending" and "czeka na akceptację w: Pokój R" in acc["@b:x.y"]["detail"]
    assert acc["@c:x.y"]["state"] == "banned"
    assert acc["@d:x.y"]["state"] == "invited"  # świeżo zaproszony w tym przebiegu


def test_matrix_accounts_use_worst_state_across_rooms_and_error_detail():
    st = State(); st.mark_invited("@a:x.y", "!r2")
    az, mx = FakeAuthentik([matrix_user("@a:x.y @bad:x.y")]), FakeMatrix(
        {("!r", "@a:x.y"): "join", ("!r2", "@a:x.y"): "leave"})
    mx.fail = {"@bad:x.y"}
    run(run_matrix_cycle(az, mx, st, ["!r", "!r2"]))
    acc = az.users[0]["attributes"]["matrixAccounts"]
    assert acc["@a:x.y"]["state"] == "left"           # joined + left => left (gorszy)
    assert acc["@bad:x.y"]["state"] == "error" and "nie ma takiego usera" in acc["@bad:x.y"]["detail"]


def test_one_write_per_user_and_stable_updated_timestamp():
    st = State()
    az, mx = FakeAuthentik([matrix_user("@a:x.y")]), FakeMatrix({("!r", "@a:x.y"): "join"})
    run(run_matrix_cycle(az, mx, st, ["!r"]))
    assert len(az.writes) == 1                        # status + accounts + lista w jednym zapisie
    az.users[0]["attributes"]["matrixAccounts"]["@a:x.y"]["updated"] = 111
    run(run_matrix_cycle(az, mx, st, ["!r"]))
    assert len(az.writes) == 1                        # nic się nie zmieniło: zero zapisów
    assert az.users[0]["attributes"]["matrixAccounts"]["@a:x.y"]["updated"] == 111


def test_removed_handle_disappears_from_accounts_and_cleared_when_no_handles():
    st = State()
    az, mx = FakeAuthentik([matrix_user("@a:x.y @b:x.y")]), FakeMatrix()
    run(run_matrix_cycle(az, mx, st, ["!r"]))
    az.users[0]["attributes"]["matrixHandles"] = ["@a:x.y"]
    run(run_matrix_cycle(az, mx, st, ["!r"]))
    assert list(az.users[0]["attributes"]["matrixAccounts"]) == ["@a:x.y"]
    az.users[0]["attributes"]["matrixHandles"] = []
    run(run_matrix_cycle(az, mx, st, ["!r"]))
    assert "matrixAccounts" not in az.users[0]["attributes"] and "matrixStatus" not in az.users[0]["attributes"]


# ---------- ręczne zaproszenia (kolejka z usermanagera) ----------
def _req(handle, by="admin1", ts=1000):
    return {"handle": handle, "by": by, "ts": ts}


def test_manual_request_reinvites_despite_earlier_decline_posts_message_and_records_result():
    st = State(); st.mark_invited("@a:x.y", "!r")
    user = matrix_user("@a:x.y"); user["attributes"]["matrixInviteRequests"] = [_req("@a:x.y", "kasia")]
    az, mx = FakeAuthentik([user]), FakeMatrix({("!r", "@a:x.y"): "leave"})
    stats = run(run_matrix_cycle(az, mx, st, ["!r"]))
    assert mx.invites == [("!r", "@a:x.y")]                       # mimo „odrzucił" i pamięci
    assert mx.notices == [("!r", "Dodano @a:x.y na prośbę jan (ręcznie, przez kasia)")]
    attrs = az.users[0]["attributes"]
    assert "matrixInviteRequests" not in attrs                    # kolejka oczyszczona
    res = attrs["matrixInviteResults"]
    assert len(res) == 1 and res[0]["handle"] == "@a:x.y" and res[0]["by"] == "kasia"
    assert res[0]["result"].startswith("ok: zaproszono do 1") and res[0]["rooms"] == 1
    assert stats["manual"] == 1


def test_manual_request_for_handle_not_owned_by_user_is_rejected_without_inviting():
    user = matrix_user("@a:x.y"); user["attributes"]["matrixInviteRequests"] = [_req("@obcy:x.y")]
    az, mx = FakeAuthentik([user]), FakeMatrix()
    run(run_matrix_cycle(az, mx, State(), ["!r"]))
    assert ("!r", "@obcy:x.y") not in mx.invites and ("!r", "@obcy:x.y") not in mx.attempts
    attrs = az.users[0]["attributes"]
    assert "matrixInviteRequests" not in attrs
    assert attrs["matrixInviteResults"][0]["result"] == "error: to konto nie należy do tego usera"


def test_manual_request_ignores_failure_backoff():
    import time
    st = State(); st.record_failure("@a:x.y", "!r", "502", now=time.time())   # odstęp trwa
    user = matrix_user("@a:x.y"); user["attributes"]["matrixInviteRequests"] = [_req("@a:x.y")]
    az, mx = FakeAuthentik([user]), FakeMatrix()
    run(run_matrix_cycle(az, mx, st, ["!r"]))
    assert mx.invites == [("!r", "@a:x.y")] and st.failure("@a:x.y", "!r") is None


def test_manual_request_on_429_stays_in_queue_and_is_not_reported():
    user = matrix_user("@a:x.y"); user["attributes"]["matrixInviteRequests"] = [_req("@a:x.y")]
    az, mx = FakeAuthentik([user]), FakeMatrix()
    mx.errors["@a:x.y"] = _http_error(429, {"retry_after_ms": 60000})
    run(run_matrix_cycle(az, mx, State(), ["!r"]))
    attrs = az.users[0]["attributes"]
    assert len(attrs["matrixInviteRequests"]) == 1               # czeka na następny przebieg
    assert "matrixInviteResults" not in attrs


def test_manual_request_failure_is_consumed_with_error_result():
    user = matrix_user("@a:x.y"); user["attributes"]["matrixInviteRequests"] = [_req("@a:x.y")]
    az, mx = FakeAuthentik([user]), FakeMatrix(); mx.fail = {"@a:x.y"}
    run(run_matrix_cycle(az, mx, State(), ["!r"]))
    attrs = az.users[0]["attributes"]
    assert "matrixInviteRequests" not in attrs and attrs["matrixInviteResults"][0]["result"].startswith("error:")


def test_request_added_by_admin_during_cycle_is_not_lost():
    user = matrix_user("@a:x.y"); user["attributes"]["matrixInviteRequests"] = [_req("@a:x.y", ts=1)]
    az, mx = FakeAuthentik([user]), FakeMatrix()
    new_req = _req("@a:x.y", "admin2", ts=2)
    az.before_write = lambda attrs: attrs["matrixInviteRequests"].append(new_req)  # równoległy zapis admina
    run(run_matrix_cycle(az, mx, State(), ["!r"]))
    assert az.users[0]["attributes"]["matrixInviteRequests"] == [new_req]   # przetworzona zniknęła, nowa została


def test_manual_results_are_capped_to_last_five():
    user = matrix_user("@a:x.y")
    user["attributes"]["matrixInviteResults"] = [{"handle": "@a:x.y", "by": "x", "ts": i, "result": "ok", "rooms": 0} for i in range(5)]
    user["attributes"]["matrixInviteRequests"] = [_req("@a:x.y", ts=99)]
    az, mx = FakeAuthentik([user]), FakeMatrix({("!r", "@a:x.y"): "join"})
    run(run_matrix_cycle(az, mx, State(), ["!r"]))
    res = az.users[0]["attributes"]["matrixInviteResults"]
    assert len(res) == 5 and res[-1]["by"] == "admin1" and res[0]["ts"] == 1   # najstarszy wypadł


def test_manual_request_in_dry_run_changes_nothing():
    user = matrix_user("@a:x.y"); user["attributes"]["matrixInviteRequests"] = [_req("@a:x.y")]
    az, mx = FakeAuthentik([user]), FakeMatrix({("!r", "@a:x.y"): "leave"})
    run(run_matrix_cycle(az, mx, State(), ["!r"], dry_run=True))
    assert mx.invites == [] and az.writes == [] and len(az.users[0]["attributes"]["matrixInviteRequests"]) == 1


# ---------- migracja atrybutów (snake_case → camelCase) ----------
from bot.migrate import plan_migration, run_migration  # noqa: E402


def _apply(attrs):
    set_, remove, _ = plan_migration(attrs)
    out = dict(attrs); out.update(set_)
    for k in remove:
        out.pop(k, None)
    return out


def test_migration_merges_handles_renames_and_drops_derived():
    attrs = {
        "matrix_handles": ["@a:x.y", "@b:x.y"], "matrixHandles": "@B:x.y\n@c:x.y",
        "matrix_status": "ok", "matrix_accounts": {}, "telegram_code": "ABC", "telegram_status": "ok",
        "telegram_username": "@jan_kowalski", "telegram_id": "567621890", "telegramVerifiedId": 7,
        "telegramIDs": "288245411, 111111111", "mifareCardId": ["x"],
    }
    out = _apply(attrs)
    assert out["matrixHandles"] == ["@b:x.y", "@c:x.y", "@a:x.y"]        # nowe pierwsze, bez duplikatów
    assert "telegramUsername" not in out                                   # konto już połączone (telegram_id): wniosek spełniony
    assert out["telegramIDs"] == "288245411, 111111111, 567621890, 7"          # format (tekst) zostaje, stare znaczniki dopisane
    assert "telegramVerifiedId" not in out and "telegram_id" not in out
    assert out["mifareCardId"] == ["x"]                                   # cudzego nie ruszamy
    assert not [k for k in out if "_" in k]                               # zero starych kluczy snake_case


def test_migration_is_idempotent_and_new_username_wins_over_old():
    attrs = {"telegram_username": "@stary_user", "telegramUsername": "@nowy_user", "telegramIDs": [288245411, "111111111"]}
    out = _apply(attrs)
    assert out["telegramUsername"] == "@nowy_user"
    assert out["telegramIDs"] == [288245411, "111111111"]                 # tablicy nie przepisujemy (liczba zostaje liczbą)
    assert plan_migration(out) == ({}, [], [])                            # drugi raz: nic do roboty


def test_migration_leaves_correct_telegram_ids_untouched():
    attrs = {"telegramIDs": ["288245411", "111111111"]}
    assert plan_migration(attrs) == ({}, [], [])                          # poprawne dane: zero zmian, zero zapisów


def test_migration_drops_empty_values_saved_by_settings_flow():
    out = _apply({"matrixHandles": "", "telegramUsername": "", "telegramCode": "", "foundUsBy": ""})
    assert out == {"foundUsBy": ""}                                       # puste atrybuty bota znikają, obce zostają


def test_migration_run_updates_only_users_that_need_it_and_reports_unknown_keys():
    az = FakeAuthentik([
        {"pk": 1, "username": "stary", "attributes": {"matrix_handles": ["@a:x.y"], "dziwnyKlucz": 1}},
        {"pk": 2, "username": "nowy", "attributes": {"matrixHandles": ["@b:x.y"], "goauthentik.io/user/token-expires": True}},
    ])
    summary = run(run_migration(az))
    assert summary["users"] == 2 and summary["migrated"] == 1
    assert [w[0] for w in az.writes] == [1]                               # user 2 nietknięty (zero zapisów)
    assert az.users[0]["attributes"] == {"matrixHandles": ["@a:x.y"], "dziwnyKlucz": 1}
    assert summary["unknown"] == {"dziwnyKlucz": 1}                       # nieznane tylko zgłoszone, nie usunięte


def test_migration_dry_run_writes_nothing():
    az = FakeAuthentik([{"pk": 1, "username": "stary", "attributes": {"telegram_id": 5}}])
    summary = run(run_migration(az, dry_run=True))
    assert summary["migrated"] == 1 and az.writes == [] and az.users[0]["attributes"] == {"telegram_id": 5}


def test_real_client_include_inactive_and_mutate_applies_to_fresh_attributes():
    import json
    import httpx
    from bot.authentik import Authentik
    patched = []

    def handler(req):
        if req.method == "PATCH":
            patched.append(json.loads(req.content)); return httpx.Response(200, json={})
        if req.url.path.endswith("/users/"):
            return httpx.Response(200, json={"results": [
                {"pk": 1, "username": "a", "is_active": True}, {"pk": 2, "username": "b", "is_active": False}],
                "pagination": {"next": 0}})
        return httpx.Response(200, json={"attributes": {"obce": 1, "matrix_handles": ["@a:x.y"]}})  # świeża wersja

    az = Authentik("https://auth.test", "t", client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    assert [u["pk"] for u in run(az.list_users())] == [1]
    assert [u["pk"] for u in run(az.list_users(include_inactive=True))] == [1, 2]
    run(az.update_attributes(1, {"a": 1}, mutate=lambda attrs: attrs.pop("matrix_handles")))
    assert patched == [{"attributes": {"obce": 1, "a": 1}}]              # scalone ze świeżymi, mutate zastosowane


# ---------- Telegram: telegramIDs ----------
def test_link_adds_id_to_telegram_ids_without_touching_manual_entries_and_clears_matching_username():
    az = FakeAuthentik([tg_user(1, telegramIDs=["111", 222], telegramUsername="@Jan_Kowalski")])
    ok, _ = run(TelegramLogic(az, State(), []).link(1, 99, tg_username="jan_kowalski"))
    attrs = az.users[0]["attributes"]
    assert ok and attrs["telegramIDs"] == ["111", 222, "99"]             # cudze wpisy bez zmian (222 zostaje liczbą), nowe dopisane
    assert "telegramUsername" not in attrs                                # wniosek spełniony: nie wisi jako oczekujący
    run(TelegramLogic(az, State(), []).link(1, 99))
    assert attrs["telegramIDs"].count("99") == 1                          # bez duplikatów


def test_link_keeps_username_request_when_a_different_account_links():
    az = FakeAuthentik([tg_user(1, telegramUsername="@inne_konto")])
    run(TelegramLogic(az, State(), []).link(1, 99, tg_username="jan_kowalski"))
    assert az.users[0]["attributes"]["telegramUsername"] == "@inne_konto"


def test_link_refuses_id_registered_by_admin_for_another_user():
    az = FakeAuthentik([tg_user(1, telegramIDs=["99"]), tg_user(2)])
    ok, text = run(TelegramLogic(az, State(), []).link(2, 99))
    assert not ok and "telegramVerifiedId" not in az.users[1]["attributes"] and "innym" in text


def test_telegram_ids_are_read_in_any_stored_form():
    from bot.handles import normalize_tg_ids
    from bot.telegram_sync import add_tg_id, find_linked_user
    assert normalize_tg_ids("288245411, 111111111") == ["288245411", "111111111"]
    assert normalize_tg_ids(["1", 2], "2;3", None, "") == ["1", "2", "3"]
    users = [tg_user(1, telegramIDs="288245411, 111111111"), tg_user(2, telegramIDs=[5])]
    assert find_linked_user(users, 111111111)["pk"] == 1 and find_linked_user(users, 5)["pk"] == 2
    assert find_linked_user(users, 999) is None
    attrs = {"telegramIDs": "288245411, 111111111"}
    add_tg_id(attrs, 7)
    assert attrs["telegramIDs"] == "288245411, 111111111, 7"              # tekst zostaje tekstem (format usermanagera)


def test_admin_registered_telegram_id_counts_as_linked_for_start_and_groups():
    az = FakeAuthentik([tg_user(1, telegramIDs=["99"])])
    logic = TelegramLogic(az, State(), [-1])
    assert "już połączone" in run(logic.start_reply(99, "x"))[0]
    assert run(logic.groups_reply(99)) == (True, "")
    assert run(logic.groups_reply(12345))[0] is False


# ---------- telegramIDs: konserwatywnie (lista współdzielona z innymi narzędziami) ----------
def test_migration_never_rewrites_an_existing_array():
    for ids in (["1", "2"], [1, 2], ["1", 1, "2", "2"], []):              # liczby, duplikaty, pusta: wszystko bez zmian
        assert plan_migration({"telegramIDs": ids}) == ({}, [], [])


def test_migration_never_changes_the_format_of_telegram_ids():
    for value in ("288245411, 111111111", 288245411, "1;2", ["1", 2]):     # tekst, liczba, inne separatory, lista
        assert plan_migration({"telegramIDs": value}) == ({}, [], [])      # bez starych kluczy: zero zmian


def test_legacy_ids_are_appended_in_the_existing_format():
    set_, _, _ = plan_migration({"telegramIDs": "1, 2", "telegram_id": 3})
    assert set_ == {"telegramIDs": "1, 2, 3"}                              # tekst zostaje tekstem
    set_, _, _ = plan_migration({"telegramIDs": ["1", "2"], "telegram_id": 3})
    assert set_ == {"telegramIDs": ["1", "2", "3"]}                        # lista zostaje listą
    set_, _, _ = plan_migration({"telegramIDs": "1, 2", "telegram_id": 2})
    assert set_ == {}                                                      # już jest: bez zmian


def test_migration_appends_legacy_ids_to_existing_array_without_touching_its_elements():
    attrs = {"telegramIDs": [111, "222"], "telegram_id": "222", "telegramVerifiedId": 333}
    set_, remove, _ = plan_migration(attrs)
    assert set_ == {"telegramIDs": [111, "222", "333"]}                   # 222 już jest (porównanie jako tekst), 333 dopisane
    assert sorted(remove) == ["telegramVerifiedId", "telegram_id"]
    set_, remove, _ = plan_migration({"telegram_id": 5})                  # brak listy: powstaje z samego znacznika
    assert set_ == {"telegramIDs": ["5"]} and remove == ["telegram_id"]


def test_add_tg_id_does_not_rewrite_existing_array_elements():
    from bot.telegram_sync import add_tg_id
    attrs = {"telegramIDs": [111, "111", "222"]}
    add_tg_id(attrs, 111)                                                  # już jest (jako liczba i tekst): bez zmian
    assert attrs["telegramIDs"] == [111, "111", "222"]
    add_tg_id(attrs, 333)
    assert attrs["telegramIDs"] == [111, "111", "222", "333"]


def test_inventory_is_logged_only_with_audit_but_unknown_keys_always(caplog):
    import logging
    az = FakeAuthentik([{"pk": 1, "username": "u", "attributes": {"cardId": 1, "mifareCardId": ["x"]}}])
    with caplog.at_level(logging.INFO, logger="migrate"):
        run(run_migration(az))
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "klucz×userów" not in text and "nieznane atrybuty" in text and "cardId×1" in text
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="migrate"):
        run(run_migration(az, audit=True))
    assert "klucz×userów" in "\n".join(r.getMessage() for r in caplog.records)


def test_append_tg_id_preserves_format_and_handles_edge_cases():
    from bot.handles import append_tg_id
    assert append_tg_id("1, 2", 3) == "1, 2, 3"
    assert append_tg_id("1, 2,", 3) == "1, 2, 3"                           # końcowy przecinek nie robi pustej pozycji
    assert append_tg_id("1, 2", 2) == "1, 2"                                # już jest
    assert append_tg_id("  ", 3) == ["3"] and append_tg_id("", 3) == ["3"] and append_tg_id(None, 3) == ["3"]
    assert append_tg_id([], 3) == ["3"] and append_tg_id([1, "2"], "3") == [1, "2", "3"]
    assert append_tg_id([1, "2"], 1) == [1, "2"]                           # 1 (liczba) == "1" (tekst)
    assert append_tg_id(5, 5) == ["5"] and append_tg_id(5, 6) == ["5", "6"]
    original = ["1"]
    assert append_tg_id(original, 2) is not original and original == ["1"]  # nie mutujemy wejścia


def test_pending_username_request_is_migrated_unless_account_was_already_linked():
    set_, remove, _ = plan_migration({"telegram_username": "@czeka_na_start"})
    assert set_ == {"telegramUsername": "@czeka_na_start"} and remove == ["telegram_username"]
    set_, remove, notes = plan_migration({"telegram_username": "@polaczony", "telegram_id": 5})
    assert "telegramUsername" not in set_ and "telegram_username" in remove
    assert any("wniosek spełniony" in n for n in notes)
    set_, _, _ = plan_migration({"telegram_username": "@x", "telegram_id": ""})   # pusty znacznik nie liczy się jako połączone
    assert set_ == {"telegramUsername": "@x"}


def test_migration_reports_which_invalid_matrix_handles_were_dropped():
    _, _, notes = plan_migration({"matrix_handles": "@dobry:x.y zly literowka"})
    text = " ".join(notes)
    assert "1 kont" in text and "odrzucono niepoprawne wpisy: zly, literowka" in text
    _, _, notes = plan_migration({"matrix_handles": ""})
    assert "0 kont po scaleniu)" in " ".join(notes)                         # pusta wartość: bez wzmianki o odrzuconych
