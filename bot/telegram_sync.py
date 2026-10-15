"""Telegram: weryfikacja konta i dopisanie jego ID do listy `telegramIDs` usera w Authentiku.

Źródłem prawdy o kontach Telegrama jest wyłącznie `telegramIDs` (tablica tekstów; `usermanager` pozwala na wiele
kont na usera, bot też). Bot tylko dopisuje zweryfikowane ID; wpisów admina nie usuwa ani nie zmienia.

Dwie ścieżki (Authentik nie umie pokazać usera tokenu wygenerowanego przez bota, więc
kierunek jest: bot → user → Authentik):

A) User wpisał w Authentiku `telegramUsername`, potem pisze do bota /start. Telegram sam
   gwarantuje, że username jest jego, więc bot pyta tylko "czy to konto Authentik X to Ty?"
   (przycisk — chroni przed tym, że ktoś wpisał CUDZY @handle) i zapisuje id.
B) Brak username albo brak wniosku: bot daje jednorazowy kod (15 min). User wkleja go w polu
   `telegramCode` w Authentiku; przy najbliższym przebiegu bot dopasowuje kod i zapisuje id.

Po weryfikacji bot wysyła w DM jednorazowe linki do grup (bot nie może dodać usera do
grupy po id — to ograniczenie API Telegrama).
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any, List, Optional, Tuple

from .handles import append_tg_id, find_pending_tg_users, normalize_tg_ids, normalize_tg_username

log = logging.getLogger("telegram")


MAX_TG_RESULTS = 5  # ile ostatnich wyników ręcznych wysyłek zostaje w telegramInviteResults


def parse_tg_requests(value) -> List[dict]:
    """`telegramInviteRequests`: lista {id, by, ts}. Wpisy niepoprawnego kształtu są pomijane."""
    out: List[dict] = []
    if not isinstance(value, list):
        return out
    for item in value:
        if isinstance(item, dict) and str(item.get("id") or "").strip():
            out.append({"id": str(item["id"]).strip(), "by": str(item.get("by") or "admin"), "ts": item.get("ts")})
    return out


TAG_BACKFILL_BUDGET = 20  # par (grupa, konto) sprawdzanych w jednym przebiegu
TAG_GAP_S = 0.5  # przerwa między zapytaniami do Telegrama
TAG_RECHECK_S = 6 * 3600  # jak często sprawdzamy ponownie konto, którego nie było w grupie
VERIFIED_CACHE_TTL_S = 60  # lista zweryfikowanych ID z Authentika odświeżana najwyżej raz na tyle (nie przy każdej wiadomości)
CHAT_REVIEW_EVERY_S = 3600  # jak często powtarzamy przegląd grup (typ czatu i prawo tagowania bota)


@dataclass
class TgChats:
    """Czaty Telegrama z konfiguracji, rozdzielone rolą (dzięki temu wiadomo, co jest co):
    - `members`: grupa dla członków. Linki, komendy i **tagi** (Verified / Verified*),
    - `public`: grupa publiczna. Linki i komendy, **bez tagów**,
    - `channels`: kanały. Tylko linki (na kanałach nie ma tagów członków),
    - `legacy`: stara zmienna TELEGRAM_GROUPS: jak `public` (bez tagów), do czasu przejścia na nowe zmienne."""
    members: List[int]
    public: List[int]
    channels: List[int]
    legacy: List[int]

    @property
    def all(self) -> List[int]:
        """Wszystkie czaty (linki z /grupy, komendy na grupach), w stałej kolejności: members, public, channels, legacy."""
        return list(dict.fromkeys(self.members + self.public + self.channels + self.legacy))

    @property
    def tag(self) -> List[int]:
        return list(self.members)


def parse_tg_chats(env: Any) -> TgChats:
    """Czyta TELEGRAM_MEMBERS_GROUPS, TELEGRAM_PUBLIC_GROUPS, TELEGRAM_CHANNELS (i starą TELEGRAM_GROUPS). Ten sam czat
    w dwóch listach to błąd konfiguracji (nie zgadujemy, czy ma dostawać tagi)."""
    def ids(name: str) -> List[int]:
        out = []
        for part in (env.get(name) or "").split(","):
            part = part.strip()
            if not part:
                continue
            try:
                out.append(int(part))
            except ValueError:
                raise ValueError(f"{name}: „{part}” nie jest numerycznym ID czatu (np. -1001234567890)") from None
        return out
    members, public, channels = ids("TELEGRAM_MEMBERS_GROUPS"), ids("TELEGRAM_PUBLIC_GROUPS"), ids("TELEGRAM_CHANNELS")
    seen: dict = {}
    for role, lst in (("TELEGRAM_MEMBERS_GROUPS", members), ("TELEGRAM_PUBLIC_GROUPS", public), ("TELEGRAM_CHANNELS", channels)):
        for c in lst:
            if c in seen and seen[c] != role:
                raise ValueError(f"czat {c} jest jednocześnie w {seen[c]} i {role}: wybierz jedną rolę")
            seen[c] = role
    # stara zmienna: czat już przeniesiony do nowej listy jest pomijany (migracja bez kasowania starej od razu)
    legacy = [c for c in ids("TELEGRAM_GROUPS") if c not in seen]
    return TgChats(members=members, public=public, channels=channels, legacy=legacy)


def _tg_req_key(req: dict) -> tuple:
    return (req["id"], req["by"], req["ts"])


def _tg_err(e: Exception) -> str:
    text = str(e)
    low = text.lower()
    if "can't initiate" in low or "chat not found" in low or "forbidden" in low or "blocked" in low:
        return ("bot nie może napisać do tego konta (nie napisało jeszcze do bota /start albo go zablokowało)")
    return f"{type(e).__name__}: {text}" if text else type(e).__name__


def add_reachable_id(attrs: dict, tg_id) -> None:
    """Dopisuje ID do `telegramReachableIDs` (zawsze lista tekstów, bez duplikatów); to pole należy tylko do bota."""
    ids = normalize_tg_ids(attrs.get("telegramReachableIDs"))
    if str(tg_id) not in ids:
        attrs["telegramReachableIDs"] = ids + [str(tg_id)]


def verified_tg_ids(attrs: dict) -> set:
    """ID uznawane za zweryfikowane: każde ID z `telegramIDs`. Przypisał je admin (usermanager) albo bot po weryfikacji, więc
    uznajemy je za właściwe (decyzja 2026-10-15); brak kontaktu z botem (`telegramReachableIDs`) mówi tylko, czy bot może do
    konta napisać, a nie czy ID jest czyje."""
    return user_tg_ids(attrs)


def user_tg_ids(attrs: dict) -> set:
    """Wszystkie ID Telegrama przypisane userowi (z `telegramIDs`, w dowolnej formie zapisu)."""
    return set(normalize_tg_ids(attrs.get("telegramIDs")))


def find_linked_user(users: List[dict], tg_id: int, exclude_pk: Optional[int] = None) -> Optional[dict]:
    """User Authentika, któremu przypisano to ID Telegrama (przez bota albo ręcznie przez admina)."""
    for u in users:
        if exclude_pk is not None and u["pk"] == exclude_pk:
            continue
        if str(tg_id) in user_tg_ids(u.get("attributes") or {}):
            return u
    return None


def add_tg_id(attrs: dict, tg_id: int) -> None:
    """Dopisuje ID do `telegramIDs` ZACHOWUJĄC format zapisu (lista → lista, tekst „1, 2" → tekst „1, 2, ID").
    Istniejących wpisów nie ruszamy, nie usuwamy ani nie przepisujemy.
    Działa na ŚWIEŻYCH atrybutach (mutate w update_attributes), więc nie gubi równoległych zmian admina."""
    attrs["telegramIDs"] = append_tg_id(attrs.get("telegramIDs"), tg_id)


def clear_matching_username(attrs: dict, tg_username: Optional[str]) -> None:
    """Wniosek `telegramUsername` został spełniony (konto o tym username się połączyło) — nie zostawiamy go jako oczekującego."""
    want = normalize_tg_username(tg_username)
    if want and normalize_tg_username(attrs.get("telegramUsername")) == want:
        attrs.pop("telegramUsername", None)


def _display_name(u: Any) -> str:
    """Imię i nazwisko z obiektu użytkownika Telegrama (User z biblioteki albo surowego `getChatMember`)."""
    full = getattr(u, "full_name", None)
    if full:
        return str(full).strip()
    return " ".join(p for p in (getattr(u, "first_name", None), getattr(u, "last_name", None)) if p).strip()


class TelegramLogic:
    def __init__(self, authentik: Any, state: Any, groups: List[int], bot: Any = None,
                 settings_url: str = "", member_tag: str = "", expired_suffix: str = "", tag_groups: Optional[List[int]] = None):
        self.authentik = authentik
        # Krótki cache z Authentika: (monotonic, lista userów, ID -> tag). Grupa pisze wiele wiadomości na minutę.
        self._verified_cache: Optional[Tuple[float, list, dict]] = None
        # Grupy z TELEGRAM_GROUPS, w których tagowanie ma sens (przegląd `review_chats`); None = jeszcze nie sprawdzono
        # (wtedy próbujemy we wszystkich). Kanały nie mają członków z tagami: setChatMemberTag działa tylko w grupach.
        self.tag_chats: Optional[List[int]] = None
        self._me_id: Optional[int] = None
        self._last_review: Optional[float] = None  # monotonic czas ostatniego przeglądu
        self.expired_suffix = expired_suffix.strip()  # dopisek do tagu dla wygasłego członkostwa, np. „*”; pusty = bez
        self.member_tag = member_tag.strip()  # np. „Verified”; pusty = bot nie ustawia tagów
        self._tag_warned: set = set()  # (grupa, błąd): ten sam problem logujemy raz, nie przy każdym kontakcie
        self.state = state
        self.groups = groups  # wszystkie czaty (linki, komendy na grupach)
        # czaty, w których tagujemy (grupa members); None = wszystkie z `groups` (tylko testy, produkcja przekazuje listę)
        self.tag_groups = tag_groups
        self.bot = bot  # telegram.Bot, ustawiany po zbudowaniu Application
        self.settings_url = settings_url  # np. https://auth.example.com/if/user/#/settings

    def _settings_line(self, prefix: str = "Ustawienia w Authentiku: ") -> str:
        return f"\n{prefix}{self.settings_url}" if self.settings_url else ""

    def group_help(self, bot_username: Optional[str] = None) -> str:
        """Odpowiedź na komendę wysłaną na grupie: komendy działają tylko w prywatnej rozmowie z botem."""
        me = f"@{bot_username}" if bot_username else "mnie"
        return (
            f"Moje komendy działają tylko w prywatnej rozmowie. Napisz do {me} na priv:\n"
            "• /start — łączy Twoje konto Telegram z kontem w Authentiku (najpierw wpisz swój @username "
            "w ustawieniach Authentika), a potem wysyła Ci jednorazowe linki do grup.\n"
            "• /grupy — wysyła świeże, jednorazowe linki do grup (tylko dla kont już połączonych przez /start).\n"
            "• /verify — potwierdza Twoje konto (już dodane w Authentiku) i nadaje tag na grupach; bez linków."
            + self._settings_line()
        )
    # --- czysta decyzja dla /start (testowalna bez Telegrama) ---
    async def start_reply(self, tg_id: int, tg_username: Optional[str]) -> Tuple[str, Optional[int]]:
        """Zwraca (tekst, pk usera do potwierdzenia albo None)."""
        users = await self.authentik.list_users()
        linked = find_linked_user(users, tg_id)
        matches = find_pending_tg_users(users, tg_username) if tg_username else []
        if linked:
            # „Połączone" znaczy tylko tyle, że ID jest na liście `telegramIDs` (bot nie trzyma tego nigdzie indziej).
            # Jeśli user sam wpisał swój @username w ustawieniach (np. po tym, jak inne narzędzie skasowało mu atrybuty
            # bota), to prośba o ponowne potwierdzenie: dopisanie ID jest idempotentne, a wniosek zostaje zamknięty.
            if len(matches) == 1 and matches[0]["pk"] == linked["pk"]:
                return (
                    f"Konto Authentik „{linked['username']}” prosi o ponowne potwierdzenie połączenia z tym kontem "
                    f"Telegram (@{tg_username}). To Ty?",
                    linked["pk"],
                )
            return (
                f"To konto Telegram jest już połączone z kontem Authentik „{linked['username']}” (jego ID jest na liście "
                "telegramIDs tego konta). Żeby potwierdzić połączenie ponownie, wpisz swój @username w ustawieniach "
                "Authentika i wyślij mi /start jeszcze raz." + self._settings_line(),
                None,
            )
        if tg_username:
            if len(matches) == 1:
                u = matches[0]
                return (
                    f"Konto Authentik „{u['username']}” prosi o połączenie z tym kontem Telegram "
                    f"(@{tg_username}). To Ty?",
                    u["pk"],
                )
            if len(matches) > 1:
                return "Ten @username wpisało kilka kont Authentik — napisz do admina.", None
        code = self.state.new_tg_code(tg_id)
        return (
            "Nie widzę oczekującego wniosku dla tego konta.\n"
            "Opcja 1: wpisz swój @username w Authentiku i wyślij mi /start jeszcze raz.\n"
            f"Opcja 2 (gdy nie masz username): wklej w Authentiku w polu „Kod z Telegrama” kod "
            f"{code} (ważny 15 minut)." + self._settings_line(),
            None,
        )

    async def link(self, pk: int, tg_id: int, users: Optional[List[dict]] = None,
                   tg_username: Optional[str] = None) -> Tuple[bool, str]:
        """Dopisuje ID do `telegramIDs` usera (jeśli to ID nie należy już do kogoś innego) i czyści kod.

        User może mieć wiele kont Telegrama: kolejne dopisują się do listy. Jedno ID może należeć tylko do jednego usera."""
        users = users if users is not None else await self.authentik.list_users()
        if find_linked_user(users, tg_id, exclude_pk=pk):
            await self.authentik.update_attributes(
                pk, {"telegramStatus": "to konto Telegram jest już użyte przez innego usera"},
                remove=["telegramCode"],
            )
            return False, "To konto Telegram jest już połączone z innym użytkownikiem."

        def apply(fresh: dict) -> None:
            add_tg_id(fresh, tg_id)
            add_reachable_id(fresh, tg_id)
            clear_matching_username(fresh, tg_username)

        await self.authentik.update_attributes(
            pk, {"telegramStatus": "ok"}, remove=["telegramCode"], mutate=apply,
        )
        log.info("połączono authentik pk=%s z telegram id=%s", pk, tg_id)
        await self.tag_verified(tg_id)
        return True, "Połączone ✅"

    async def note_contact(self, tg_id: int, user: Any = None) -> None:
        """Konto napisało do bota (/start, /grupy, przycisk): zapisz ślad w `telegramReachableIDs` jego usera.

        Telegram nie pozwala botowi napisać pierwszemu, więc usermanager na podstawie tego śladu (a nie zgadywania)
        wie, że do tego konta da się wysłać linki. ID wpisane ręcznie przez admina nie mają śladu, dopóki konto
        nie napisze do bota. Błąd zapisu nie może psuć odpowiedzi użytkownikowi."""
        try:
            users = await self.authentik.list_users()
            owner = find_linked_user(users, tg_id)
            if owner is not None and str(tg_id) not in normalize_tg_ids(
                    (owner.get("attributes") or {}).get("telegramReachableIDs")):
                self._verified_cache = None  # świeżo zweryfikowany ma być od razu poznany
                await self.authentik.update_attributes(owner["pk"], mutate=lambda fresh: add_reachable_id(fresh, tg_id))
        except Exception:
            log.exception("nie udało się zapisać śladu kontaktu tg id=%s", tg_id)
            return
        if user is not None:
            await self.note_identity(tg_id, getattr(user, "username", None), _display_name(user))
        await self.tag_verified(tg_id)

    async def note_identity(self, tg_id: int, username: Optional[str], name: Optional[str]) -> None:
        """Bot zobaczył konto Telegrama (wiadomość do bota, wiadomość na grupie, członek grupy w `getChatMember`): zapisz jego
        @username i imię w `telegramAccounts` {ID: {username, name, updated}} usera, któremu to ID przypisano (do wglądu
        w usermanagerze), i scal: jeśli ten sam user ma wiszący, niepotwierdzony `telegramUsername` z TYM nickiem, wniosek jest
        spełniony i znika. Wniosek innego usera z tym nickiem zostaje nietknięty (to konflikt, nie scalenie).
        Zapis tylko przy zmianie; nieprzypisane ID są ignorowane. Błąd nie psuje obsługi wiadomości."""
        try:
            users = await self._users_cached()
            owner = find_linked_user(users, tg_id)
            if owner is None:
                return
            attrs = owner.get("attributes") or {}
            key, nick, name = str(tg_id), (username or "").lstrip("@"), (name or "").strip()
            known = attrs.get("telegramAccounts")
            known = known.get(key) if isinstance(known, dict) else None
            same = isinstance(known, dict) and known.get("username", "") == nick and known.get("name", "") == name
            pending = bool(nick) and normalize_tg_username(attrs.get("telegramUsername")) == normalize_tg_username(nick)
            if same and not pending:
                return

            def apply(fresh: dict) -> None:
                accounts = fresh.get("telegramAccounts")
                accounts = dict(accounts) if isinstance(accounts, dict) else {}
                cur = accounts.get(key)
                if not (isinstance(cur, dict) and cur.get("username", "") == nick and cur.get("name", "") == name):
                    accounts[key] = {"username": nick, "name": name, "updated": int(time.time())}
                fresh["telegramAccounts"] = accounts
                if nick:
                    clear_matching_username(fresh, nick)

            await self.authentik.update_attributes(owner["pk"], mutate=apply)
            self._verified_cache = None
            log.info("poznano konto tg id=%s: @%s (%s)%s", tg_id, nick or "-", name or "-",
                     ", wniosek telegramUsername spełniony" if pending else "")
        except Exception:
            log.exception("nie udało się zapisać danych konta tg id=%s", tg_id)

    def _tag_targets(self) -> List[int]:
        """Gdzie tagujemy: wynik przeglądu, a przed pierwszym przeglądem lista z konfiguracji (TELEGRAM_MEMBERS_GROUPS)."""
        if self.tag_chats is not None:
            return list(self.tag_chats)
        return list(self.groups if self.tag_groups is None else self.tag_groups)

    async def review_chats(self) -> None:
        """Przegląd czatów z TELEGRAM_MEMBERS_GROUPS pod kątem tagów: tylko grupy i supergrupy (na kanałach `setChatMemberTag` nie działa),
        w których bot jest adminem z prawem `can_manage_tags`. Reszta jest pomijana z jednym wpisem w logu mówiącym dlaczego.
        Linki do grup/kanałów (`/grupy`) dalej idą do wszystkich czatów z konfiguracji."""
        configured = list(self.groups if self.tag_groups is None else self.tag_groups)
        if not (self.bot and configured):
            return
        if self._me_id is None:
            self._me_id = (await self.bot.get_me()).id
        ok: List[int] = []
        for chat in configured:
            try:
                info = await self.bot.get_chat(chat)
                name = f"{getattr(info, 'title', None) or chat} ({chat})"
                if getattr(info, "type", "supergroup") not in ("group", "supergroup"):
                    log.info("przegląd czatów: %s to %s, tagi działają tylko w grupach i supergrupach: pomijam", name, info.type)
                    continue
                me = await self.bot.get_chat_member(chat, self._me_id)
                can = me.status == "administrator" and bool(
                    getattr(me, "can_manage_tags", None) or (getattr(me, "api_kwargs", None) or {}).get("can_manage_tags"))
                if not can:
                    if (chat, "rights", me.status) not in self._tag_warned:  # co godzinę ten sam komunikat byłby szumem
                        self._tag_warned.add((chat, "rights", me.status))
                        log.warning("przegląd czatów: %s: bot ma status %r bez prawa Manage tags (can_manage_tags): pomijam tagi",
                                    name, me.status)
                    continue
                ok.append(chat)
            except Exception as e:
                log.warning("przegląd czatów: nie udało się sprawdzić %s: %s", chat, e)
        self.tag_chats = ok
        log.info("przegląd czatów: tagi „%s” w %d z %d czatów z TELEGRAM_MEMBERS_GROUPS: %s", self.member_tag, len(ok), len(configured),
                 ", ".join(str(c) for c in ok) or "brak")

    async def maybe_review_chats(self) -> None:
        """Przegląd na starcie (pierwsze wywołanie) i co `CHAT_REVIEW_EVERY_S`: bot mógł dostać prawo tagowania później."""
        if not self.member_tag:
            return
        if self._last_review is not None and time.monotonic() - self._last_review < CHAT_REVIEW_EVERY_S:
            return
        self._last_review = time.monotonic()
        try:
            await self.review_chats()
        except Exception:
            log.exception("przegląd czatów nie powiódł się")

    def _tag_for(self, user: dict, now: Optional[float] = None) -> str:
        """Tag usera: `member_tag`, a z dopiskiem (`expired_suffix`), gdy konto jest nieaktywne albo jego członkostwo wygasło
        (`membershipExpirationTimestamp` w przeszłości). Brak daty = bez dopisku (nie karzemy kont bez danych)."""
        if not self.expired_suffix:
            return self.member_tag
        now = time.time() if now is None else now
        raw = (user.get("attributes") or {}).get("membershipExpirationTimestamp")
        try:
            expired = float(raw) <= now if raw not in (None, "") else False
        except (TypeError, ValueError):
            expired = False
        return self.member_tag + self.expired_suffix if (expired or user.get("is_active", True) is False) else self.member_tag

    def _tag_map(self, users: List[dict]) -> dict:
        """ID Telegrama (int) -> tag, jaki ma mieć, dla każdego ID z `telegramIDs` (patrz `verified_tg_ids`)."""
        now = time.time()
        return {int(i): self._tag_for(u, now) for u in users for i in verified_tg_ids(u.get("attributes") or {}) if i.isdigit()}

    async def _tag_in_chat(self, chat: int, tg_id: int, want: Optional[str] = None) -> str:
        """„set” (ustawiono), „has” (już ma albo ma cudzy tag, którego nie ruszamy), „skip” (nie jest zwykłym członkiem tej
        grupy), „error”. Zmieniamy tylko tag pusty albo własny (`Verified` / `Verified*`), nie ręcznie ustawiony przez admina grupy."""
        want = want or self.member_tag
        try:
            member = await self.bot.get_chat_member(chat, tg_id)
            mu = getattr(member, "user", None)  # przy okazji poznajemy @handle, bez dodatkowych zapytań
            if mu is not None and getattr(mu, "id", None) == tg_id:
                await self.note_identity(tg_id, getattr(mu, "username", None), _display_name(mu))
            # `tag` w wersji 21 biblioteki nie jest jeszcze polem obiektu: ląduje w api_kwargs
            current = getattr(member, "tag", None) or (getattr(member, "api_kwargs", None) or {}).get("tag")
            # tylko zwykli członkowie (admini mają custom_title, a spoza grupy nie ma komu nadać tagu)
            if member.status not in ("member", "restricted"):
                return "skip"
            if current == want or (current and current not in (self.member_tag, self.member_tag + self.expired_suffix)):
                return "has"
            await self.bot.do_api_request(
                "setChatMemberTag", api_kwargs={"chat_id": chat, "user_id": tg_id, "tag": want})
            log.info("tag %r ustawiony: tg id=%s grupa=%s", want, tg_id, chat)
            return "set"
        except Exception as e:
            key = (chat, str(e))
            if key not in self._tag_warned:
                self._tag_warned.add(key)
                log.warning("nie udało się ustawić tagu w grupie %s (bot musi być adminem z prawem can_manage_tags): %s",
                            chat, e)
            return "error"

    async def tag_verified(self, tg_id: int) -> int:
        """Ustawia tag (np. „Verified”, albo „Verified*” przy wygasłym członkostwie) zwykłemu członkowi każdej grupy, w której
        tagujemy, jeśli ID jest w `telegramIDs` jakiegoś usera. Zwraca liczbę grup, w których tag został właśnie ustawiony.

        Wymaga w grupie: bot jako admin z prawem `can_manage_tags`. Wołamy surowe API (`do_api_request`), bo biblioteka
        w wersji 21 nie ma jeszcze tej metody. Błąd w jednej grupie nie blokuje pozostałych ani odpowiedzi użytkownikowi."""
        if not (self.bot and self.member_tag and self._tag_targets()):
            return 0
        try:
            users = await self.authentik.list_users(include_inactive=True)  # nieaktywni też: dostają dopisek wygaśnięcia
            want = self._tag_map(users).get(int(tg_id))
            if want is None:
                return 0
        except Exception:
            log.exception("nie udało się sprawdzić, czy tg id=%s jest zweryfikowane", tg_id)
            return 0
        done = 0
        for chat in self._tag_targets():
            result = await self._tag_in_chat(chat, tg_id, want)
            self.state.set_tag_pair(chat, tg_id, result in ("set", "has"), None, want)
            done += result == "set"
        return done

    async def backfill_tags(self, budget: int = TAG_BACKFILL_BUDGET) -> int:
        """Dosypuje tagi kontom z `telegramIDs` (także połączonym, zanim tagi istniały) i poprawia te, którym zmienił się
        oczekiwany tag (wygasłe członkostwo dostaje dopisek, odnowione go traci). Po `budget` par (grupa, konto) na przebieg,
        z przerwą między zapytaniami. Załatwione pary z właściwym tagiem nie są sprawdzane drugi raz (zero zapytań);
        konto spoza grupy jest sprawdzane ponownie co kilka godzin. Zwraca liczbę sprawdzonych par."""
        chats = self._tag_targets()
        if not (self.bot and self.member_tag and chats):
            return 0
        users = await self.authentik.list_users(include_inactive=True)
        wanted = sorted(self._tag_map(users).items())
        checked, now = 0, time.time()
        for chat in chats:
            for tg_id, want in wanted:
                if checked >= budget:
                    return checked
                if not self.state.tag_pair_due(chat, tg_id, now, TAG_RECHECK_S, want):
                    continue
                result = await self._tag_in_chat(chat, tg_id, want)
                self.state.set_tag_pair(chat, tg_id, result in ("set", "has"), now, want)
                checked += 1
                if TAG_GAP_S:
                    await asyncio.sleep(TAG_GAP_S)
        return checked

    async def _users_and_tags(self) -> Tuple[list, dict]:
        now = time.monotonic()
        c = self._verified_cache
        if c and now - c[0] < VERIFIED_CACHE_TTL_S:
            return c[1], c[2]
        users = await self.authentik.list_users(include_inactive=True)
        c = (now, users, self._tag_map(users))
        self._verified_cache = c
        return c[1], c[2]

    async def _users_cached(self) -> list:
        return (await self._users_and_tags())[0]

    async def _tag_map_cached(self) -> dict:
        """`_tag_map` z krótkim cache: grupa może pisać wiele wiadomości na minutę."""
        return (await self._users_and_tags())[1]

    async def on_group_message(self, update: Any, context: Any) -> None:
        """Wiadomość na czacie z konfiguracji: poznajemy nick nadawcy (`note_identity`, we wszystkich rolach czatów), a w grupie
        members dodatkowo ustawiamy tag, jeśli nadawca ma ID w `telegramIDs`, a tej pary (grupa, konto) nie załatwiono jeszcze
        z właściwym tagiem (potem zero zapytań). Boty i wiadomości „w imieniu kanału/grupy” (anonimowi admini) pomijamy.
        Bot widzi wszystkie wiadomości tylko jako admin grupy."""
        chat, user, msg = update.effective_chat, update.effective_user, update.effective_message
        if (not self.bot or chat is None or chat.id not in self.groups or user is None or user.is_bot or msg is None
                or getattr(msg, "sender_chat", None) is not None):
            return
        await self.note_identity(user.id, getattr(user, "username", None), _display_name(user))
        if not self.member_tag or chat.id not in self._tag_targets():
            return
        try:
            want = (await self._tag_map_cached()).get(user.id)
            if want is not None and self.state.tag_pair_due(chat.id, user.id, None, TAG_RECHECK_S, want):
                result = await self._tag_in_chat(chat.id, user.id, want)
                self.state.set_tag_pair(chat.id, user.id, result in ("set", "has"), None, want)
        except Exception as e:
            key = (chat.id, "msg", str(e))
            if key not in self._tag_warned:
                self._tag_warned.add(key)
                log.warning("obsługa wiadomości na grupie %s nie powiodła się: %s", chat.id, e)

    async def verify(self, tg_id: int, user: Any = None) -> Tuple[bool, str]:
        """/verify: potwierdza konto, które admin lub bot już przypisał userowi w Authentiku (wiadomość z tego konta dowodzi,
        że je kontroluje). NIE otwiera procedury /start i nie wysyła linków (od tego są /start i /grupy): zapisuje ślad kontaktu
        (`telegramReachableIDs`) i nadaje tag na grupach. Zwraca (czy znamy konto, tekst odpowiedzi)."""
        users = await self.authentik.list_users()
        if find_linked_user(users, tg_id) is None:
            return False, ("Nie znam tego konta Telegrama w Authentiku. Wpisz swój @username w ustawieniach i wyślij /start."
                           + self._settings_line())
        await self.note_contact(tg_id, user)
        return True, "✅ Zweryfikowano."

    async def on_verify(self, update: Any, context: Any) -> None:
        """/verify działa tylko w prywatnej rozmowie; na grupie z TELEGRAM_GROUPS bot odsyła instrukcję jak przy /start."""
        if update.effective_chat.type != "private":
            await self._reply_in_group(update, context, "/verify")
            return
        u = update.effective_user
        try:
            ok, text = await self.verify(u.id, u)
        except Exception:
            log.exception("/verify tg id=%s nie powiodło się", u.id)
            ok, text = False, "Coś poszło nie tak, spróbuj za chwilę."
        log.info("/verify od tg id=%s -> %s", u.id, "ok" if ok else "nieznane konto")
        await update.message.reply_text(text)

    async def on_chat_member(self, update: Any, context: Any) -> None:
        """Ktoś wszedł do grupy (np. z linku od bota po weryfikacji): nadaj tag, jeśli jest zweryfikowany."""
        cmu = update.chat_member
        if cmu is None or cmu.chat.id not in self._tag_targets():
            return
        joined = cmu.old_chat_member.status in ("left", "kicked") and cmu.new_chat_member.status in ("member", "restricted")
        if joined:
            await self.tag_verified(cmu.new_chat_member.user.id)

    async def send_group_links(self, tg_id: int) -> int:
        """Wysyła w DM „Nazwa grupy: link" (jednorazowe, 24 h). Zwraca liczbę wysłanych linków.

        Jedna grupa, która zawiedzie (bot bez uprawnień, grupa usunięta), nie blokuje reszty;
        user dostaje listę grup, do których linku nie udało się wygenerować.
        """
        if not self.bot or not self.groups:
            return 0
        lines, failed = [], []
        for chat in self.groups:
            try:
                title = (await self.bot.get_chat(chat)).title or str(chat)
            except Exception:
                title = str(chat)
            try:
                link = await self.bot.create_chat_invite_link(
                    chat, member_limit=1, expire_date=int(time.time()) + 24 * 3600
                )
                lines.append(f"• {title}: {link.invite_link}")
            except Exception as e:
                log.error("invite link dla grupy %s: %s", chat, e)
                failed.append(title)
        text = ""
        if lines:
            text = ("Linki do grup (jednorazowe, ważne 24 h):\n" + "\n".join(lines)
                    + "\n\nNowe linki w każdej chwili: /grupy")
        if failed:
            text += ("\n\n" if text else "") + "Nie udało się wygenerować linku do: " + ", ".join(failed) + \
                    ". Napisz do admina."
        if text:
            await self.bot.send_message(tg_id, text)
        return len(lines)

    async def groups_reply(self, tg_id: int) -> Tuple[bool, str]:
        """/grupy: (czy wysłać linki, tekst gdy nie). Tylko dla połączonych userów."""
        users = await self.authentik.list_users()
        if not find_linked_user(users, tg_id):
            return False, ("To konto Telegram nie jest jeszcze połączone z Authentikiem. Wpisz swój @username "
                           "w ustawieniach Authentika, a potem wyślij mi /start." + self._settings_line())
        if not self.groups:
            return False, "Nie ma skonfigurowanych grup."
        return True, ""

    async def poll_invites(self) -> int:
        """Ręczne wysłanie linków do grup: prośby `telegramInviteRequests` (zapisuje je usermanager).

        Bot wysyła na wskazane konto DM ze świeżymi, jednorazowymi linkami (to samo co /grupy), ale tylko dla ID, które jest
        na liście `telegramIDs` TEGO usera. Wynik trafia do `telegramInviteResults`. Bot nie może pierwszy napisać do konta,
        które nigdy nie wysłało mu /start — wtedy wynik to czytelny błąd. Zwraca liczbę przetworzonych próśb."""
        if not self.bot:
            return 0
        processed = 0
        for u in await self.authentik.list_users():
            attrs = u.get("attributes") or {}
            requests = parse_tg_requests(attrs.get("telegramInviteRequests"))
            if not requests:
                continue
            owned = user_tg_ids(attrs)
            entries, done = [], set()
            for req in requests:
                n = 0
                if req["id"] not in owned:
                    result = "error: to konto Telegram nie należy do tego usera"
                elif not self.groups:
                    result = "error: brak skonfigurowanych grup"
                else:
                    try:
                        n = await self.send_group_links(int(req["id"]))
                        result = (f"ok: wysłano {n} linków do grup" if n else
                                  "error: nie udało się wygenerować żadnego linku (bot nie jest adminem grup?)")
                    except Exception as e:
                        result = "error: " + _tg_err(e)
                log.info("ręczne linki do grup dla tg id=%s (user %s, przez %s): %s", req["id"], u["username"], req["by"], result)
                entries.append({"id": req["id"], "by": req["by"], "ts": int(time.time()), "result": result, "groups": n})
                done.add(_tg_req_key(req))
                processed += 1
            prev = attrs.get("telegramInviteResults")
            prev = prev if isinstance(prev, list) else []

            def drop(fresh: dict, done: set = done) -> None:
                """Na ŚWIEŻYCH atrybutach: nie gubimy próśb dopisanych przez admina w trakcie przebiegu."""
                left = [r for r in parse_tg_requests(fresh.get("telegramInviteRequests")) if _tg_req_key(r) not in done]
                if left:
                    fresh["telegramInviteRequests"] = left
                else:
                    fresh.pop("telegramInviteRequests", None)

            await self.authentik.update_attributes(
                u["pk"], {"telegramInviteResults": (prev + entries)[-MAX_TG_RESULTS:]}, mutate=drop
            )
        return processed

    async def poll_codes(self) -> int:
        """Ścieżka B: userzy, którzy wkleili kod w Authentiku. Zwraca liczbę połączonych."""
        users = await self.authentik.list_users()
        linked = 0
        for u in users:
            attrs = u.get("attributes") or {}
            code = attrs.get("telegramCode")
            if not code:
                continue
            tg_id = self.state.consume_tg_code(str(code))
            log.info("kod od usera %s (authentik pk=%s): %s", u["username"], u["pk"],
                     f"pasuje do tg id={tg_id}" if tg_id is not None else "nieznany albo wygasły")
            if tg_id is None:
                await self.authentik.update_attributes(
                    u["pk"], {"telegramStatus": "zły lub wygasły kod"}, remove=["telegramCode"]
                )
                continue
            # Kod nie niesie username; spróbuj go poznać, żeby domknąć ewentualny wniosek `telegramUsername`.
            tg_username = None
            if self.bot:
                try:
                    tg_username = (await self.bot.get_chat(tg_id)).username
                except Exception:
                    pass
            ok, msg = await self.link(u["pk"], tg_id, users, tg_username)
            if ok:
                linked += 1
                if self.bot:
                    await self.bot.send_message(tg_id, f"Połączono z kontem Authentik „{u['username']}” ✅")
                    await self.send_group_links(tg_id)
        return linked

    # --- handlery python-telegram-bot (cienkie) ---
    async def on_start(self, update: Any, context: Any) -> None:
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup

        if update.effective_chat.type != "private":
            await self._reply_in_group(update, context, "/start")
            return
        u = update.effective_user
        await self.note_contact(u.id, u)
        text, pk = await self.start_reply(u.id, u.username)
        log.info("/start od tg id=%s username=%s -> %s", u.id, u.username,
                 f"pytanie o potwierdzenie (authentik pk={pk})" if pk is not None else "bez dopasowania")
        markup = None
        if pk is not None:
            markup = InlineKeyboardMarkup(
                [[InlineKeyboardButton("Tak, to ja", callback_data=f"link:{pk}"),
                  InlineKeyboardButton("Nie", callback_data="cancel")]]
            )
        await update.message.reply_text(text, reply_markup=markup)

    async def _reply_in_group(self, update: Any, context: Any, cmd: str) -> None:
        """Komenda na grupie (także /komenda@nazwabota): wyjaśnij, że trzeba pisać na priv.

        Tylko na grupach z TELEGRAM_GROUPS. Na grupie, do której ktoś losowo dodał bota, milczymy
        (żadnej instrukcji, żadnego śladu poza logiem), żeby nie zdradzać, do czego służy.
        """
        if update.effective_chat.id not in self.groups:
            log.debug("%s na obcej grupie %s: ignoruję", cmd, update.effective_chat.id)
            return
        log.info("%s na czacie %s (%s) od tg id=%s: odsyłam instrukcję", cmd, update.effective_chat.id,
                 update.effective_chat.type, getattr(update.effective_user, "id", None))
        await update.message.reply_text(self.group_help(getattr(context.bot, "username", None)))

    async def on_groups(self, update: Any, context: Any) -> None:
        if update.effective_chat.type != "private":
            await self._reply_in_group(update, context, "/grupy")
            return
        u = update.effective_user
        await self.note_contact(u.id, u)
        send, text = await self.groups_reply(u.id)
        log.info("/grupy od tg id=%s -> %s", u.id, "wysyłam linki" if send else "odmowa")
        if send:
            await self.send_group_links(u.id)
        else:
            await update.message.reply_text(text)

    async def on_button(self, update: Any, context: Any) -> None:
        q = update.callback_query
        await q.answer()
        if not q.data.startswith("link:"):
            await q.edit_message_text("Anulowano.")
            return
        pk = int(q.data.split(":", 1)[1])
        u = q.from_user
        await self.note_contact(u.id, u)
        log.info("potwierdzenie od tg id=%s username=%s dla authentik pk=%s", u.id, u.username, pk)
        users = await self.authentik.list_users()
        # Powtórna weryfikacja w chwili kliknięcia: wniosek nadal istnieje i pasuje do username.
        pending = [m for m in find_pending_tg_users(users, u.username or "") if m["pk"] == pk]
        if not pending:
            await q.edit_message_text("Ten wniosek już nie istnieje albo nie pasuje do Twojego konta.")
            return
        ok, msg = await self.link(pk, u.id, users, u.username)
        await q.edit_message_text(msg)
        if ok:
            await self.send_group_links(u.id)
