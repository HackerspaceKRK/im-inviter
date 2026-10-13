# Wdrożenie: hskrk i hswro

Każdy hackerspace ma **własną, niezależną instancję**: własny Authentik, homeserver, bota Telegrama
i katalog wdrożenia z własnym `.env`. Kod jest ten sam. Poniżej szablon dla jednej instancji,
potem tabela różnic.

## Różnice między instancjami (uzupełnij)

| | hskrk | hswro |
|---|---|---|
| `AUTHENTIK_URL` | `https://…` | `https://…` |
| `MATRIX_HOMESERVER` | `https://…` | `https://…` |
| konto bota Matrix | `@…:…` | `@…:…` |
| `MATRIX_ROOMS` (ID pokoi) | `!…:…` | `!…:…` |
| bot Telegrama (BotFather) | `@…bot` | `@…bot` |
| `TELEGRAM_GROUPS` | `-100…` | `-100…` |
| host z dockerem | … | … |

Szablony `.env`: `env/hskrk.env.example`, `env/hswro.env.example`.
**Nie commituj `.env`** (jest w `.gitignore`). Tokeny trzymaj tylko na hoście wdrożenia.

## 0. Wymagania na hoście

Docker + compose (`docker compose version`), dostęp do HTTPS Authentika, homeservera i
`api.telegram.org`. Bot nie wystawia żadnego portu — działa tylko wychodząco.

## 1. Authentik: service account (raz na instancję)

W panelu admina:

1. **Directory → Users → Create Service account** (nazwa `im-sync`). Token pokazany przy tworzeniu
   **zignoruj** — ma intencję *App password*, a API Authentika przyjmuje tylko intencję *API*
   (objaw: `{"detail":"Token invalid/expired"}`, HTTP 403).
2. **Directory → Tokens and App passwords → Create**: Identifier `im-sync-api`, User = `im-sync`,
   **Intent = API Token**, *Expiring* wyłączone. Potem „Copy Key" → to jest `AUTHENTIK_TOKEN`.
3. Nadaj uprawnienia **View user** i **Change user** (np. rola z tymi uprawnieniami
   przypisana do grupy, w której jest ten account).

Weryfikacja (z hosta):

```bash
curl -fsS -H "Authorization: Bearer $TOKEN" "$AUTHENTIK_URL/api/v3/core/users/me/" | python3 -m json.tool | head
```

## 2. Authentik: pola w ustawieniach usera

Blueprint tworzy **własny flow ustawień** `im-sync-user-settings-flow`: jeden ekran ze standardowymi
polami (username, name, email, locale) **i** naszymi (Matrix, Telegram), wstępnie wypełnionymi
aktualnymi wartościami. Własny flow, bo domyślne blueprinty Authentika są stosowane ponownie
i cofnęłyby dopiski w `default-user-settings-flow`.

1. Admin → **Customization → Blueprints → Create**, wgraj `blueprints/user-settings-im.yaml`
   (albo skopiuj do katalogu blueprintów serwera Authentika: `/blueprints/custom/`).
   Status blueprintu ma być **Successful**.
2. **Przełącz brand** (jednorazowo, ręcznie — blueprint go celowo nie rusza): Admin → **System → Brands** →
   brand `authentik-default` → *Edit* → **User settings flow** = `im-sync-user-settings-flow`.
   Cofnięcie = ustaw z powrotem `default-user-settings-flow`.
3. Jeśli wcześniej wdrożyłeś starszą wersję blueprintu (osobny ekran w domyślnym flow): Admin → Flows →
   `default-user-settings-flow` → *Stage Bindings* → usuń wpięcie stage'a `im-sync-prompt-stage`.
4. Jako zwykły user wejdź na `$AUTHENTIK_URL/if/user/#/settings`: w „Update your info" jest jeden ekran
   z polami „Konta Matrix", „Telegram @username" i „Kod z Telegrama". Zapisz → po ponownym wejściu
   wartości mają być na miejscu (sprawdź też w Admin → Directory → Users → user → Attributes).

Weryfikacja bez klikania (preflight sprawdza to samo): `python -m bot.preflight` uruchamia flow z brandu
i szuka naszych trzech pól.

Gdy blueprint się nie zaaplikuje: utwórz Prompty ręcznie (`field_key`: `username`, `name`, `email`,
`attributes.settings.locale` oraz `attributes.matrix_handles` (Text Area), `attributes.telegram_username`,
`attributes.telegram_code`), Prompt Stage z nimi, User Write Stage (`never_create`), flow o designation
*Stage configuration* i dopnij oba stage'e (prompt → order 20, write → order 100).
**Zapisz u nas, co dokładnie zadziałało** (sekcja „Notatki z wdrożeń" na dole).

Uwaga: flow ustawień domyślnie zabrania zmiany maila (`Not allowed to change email address.`),
więc konto serwisowe bez maila nie przejdzie go przez API — to ograniczenie konta, nie naszych pól.

## 3. Matrix: konto bota i token

1. Załóż konto bota na homeserverze (rejestracja albo admin: `register_new_matrix_user` / Synapse Admin).
2. Token (hasło wpisujesz lokalnie, nie w czacie):
   ```bash
   scripts/matrix-login.sh https://MATRIX.DOMENA @bot:DOMENA     # wypisze MATRIX_TOKEN=…
   ```
3. Dodaj bota do pokoi docelowych i daj mu **Moderator** (power level ≥ progu „Invite").
4. ID pokoju (`!abc:serwer`): Element → Ustawienia pokoju → Zaawansowane → „Wewnętrzne ID pokoju".
   Pokoje **niezaszyfrowane** (inaczej zaproszenia działają, ale wiadomość „Dodano…" nie).

### Przestrzenie (Spaces) Matrix

Bot może zapraszać do **przestrzeni** zamiast (albo obok) pojedynczych pokoi: `MATRIX_SPACES=!id:serwer`.
- Zaproszenie idzie tylko do przestrzeni. Pokoje-dzieci wpuszczają członków space same, **o ile mają regułę
  join „restricted" (Space members)** — w Elemencie: ustawienia pokoju → Bezpieczeństwo → „Dostęp do pokoju".
  Pokoje „invite only" bot musi dostać osobno w `MATRIX_ROOMS`.
- `MATRIX_NOTIFY_ROOM` = pokój, w którym bot pisze „Dodano … do przestrzeni … na prośbę …". Puste = bez wiadomości.
- Bot musi być w przestrzeni z power level ≥ „invite" (preflight sprawdza, czy to naprawdę `m.space`).
- **Nie wskazuj cudzej wspólnoty.** Bot może być w publicznych przestrzeniach (np. „Polish Hackerspaces"),
  ale nie ma tam uprawnień i nie powinien tam nikogo zapraszać.
- Testową przestrzeń z dzieckiem „restricted" zakłada: `python3 scripts/matrix-create-space.py "nazwa" "pokój"`
  (zmienne z `.env`); wypisuje `MATRIX_SPACES=…`.

## 4. Telegram (opcjonalnie)

1. `@BotFather` → `/newbot` → token. `/setprivacy` zostaw domyślne (bot działa tylko w DM).
2. Dodaj bota do grup jako **administratora z prawem „Invite users"**.
3. ID grup (`-100…`): np. dodaj tymczasowo `@RawDataBot` do grupy albo `getUpdates` botem po wiadomości w grupie.

## 5. Wdrożenie na hoście

```bash
sudo mkdir -p /opt/im-sync-hskrk && sudo chown "$USER" /opt/im-sync-hskrk   # dla hswro: im-sync-hswro
git clone <URL-repo> /opt/im-sync-hskrk && cd /opt/im-sync-hskrk
cp env/hskrk.env.example .env && chmod 600 .env && "$EDITOR" .env

# Zbuduj obraz i sprawdź całą układankę zanim cokolwiek zacznie działać:
docker compose build
docker compose run --rm bot python -m bot.preflight          # wszystko ma być "OK"

# Próba na sucho (loguje, nic nie wysyła/zapisuje):
docker compose run --rm bot python -m bot.main --dry-run --once

# Na ostro:
docker compose up -d && docker compose logs -f --tail=50
```

Katalogi `/opt/im-sync-hskrk` i `/opt/im-sync-hswro` są niezależne (osobny volume `bot-data`
z SQLite, osobna nazwa projektu compose wynika z nazwy katalogu).

## 6. Test E2E po wdrożeniu

1. Konto testowe w Authentiku → ustawienia → wpisz 1 poprawny handle Matrix i 1 z literówką.
2. W ≤ 60 s: invite przychodzi, w pokoju jest „Dodano … na prośbę …", a w atrybucie `matrix_status` jest błąd dla literówki.
   Podgląd: Admin → Directory → Users → user → Attributes.
3. Telegram: wpisz `@username` w Authentiku → `/start` do bota → „Tak, to ja" → w atrybutach pojawia się `telegram_id`, w DM linki do grup.
   Wariant bez username: `/start` → kod → wklej w polu w Authentiku → ≤ 60 s `telegram_id`.
4. **Zabezpieczenie**: jako zwykły user spróbuj zapisać `telegram_id` przez
   `curl -X PATCH -H "Authorization: Bearer <token zwykłego usera>" -d '{"attributes":{"telegram_id":1}}' $AUTHENTIK_URL/api/v3/core/users/me/` —
   ma się nie udać. Jeśli się uda, nie ufaj `telegram_id` w innych aplikacjach, dopóki tego nie zablokujesz.

## Utrzymanie

| Co | Komenda |
|---|---|
| Logi | `docker compose logs -f --tail=100` |
| Aktualizacja | `git pull && docker compose up -d --build && docker compose run --rm bot python -m bot.preflight` |
| Restart | `docker compose restart` |
| Stop | `docker compose down` (dane zostają w volume) |
| Backup stanu | `docker run --rm -v <katalog>_bot-data:/d -v "$PWD":/b alpine tar czf /b/state-backup.tgz -C /d .` |

Utrata stanu (SQLite) = bot może ponownie zaprosić osoby, które świadomie wyszły z pokoju. Poza tym nic się nie psuje.

## Typowe problemy

| Objaw | Przyczyna | Co zrobić |
|---|---|---|
| `preflight`: token Authentika 403 | Za mało uprawnień | Dodaj View/Change user |
| `matrix_status`: `403 …` przy handle | Bot bez power levelu albo user nie istnieje | `preflight` pokaże power level; sprawdź handle |
| `matrix_status`: `… ma bana w …` | Ban w pokoju | Zdejmij bana (bot nie zdejmuje sam) |
| Brak „Dodano…" w pokoju mimo invite | Pokój zaszyfrowany | Wyłącz szyfrowanie albo zaakceptuj brak wiadomości |
| Telegram: brak linków do grup | Bot nie jest adminem z „Invite users" | `preflight` wskaże grupę |
| Pola nie pojawiają się w ustawieniach | Blueprint nie zaaplikowany | Admin → Blueprints → status; ręczny wariant z kroku 2 |

## Notatki z wdrożeń

(Uzupełniaj tu po każdej instancji: wersja Authentika, czy blueprint się zaaplikował, co trzeba było poprawić.)

- Pierwszy test (auth.sq9.wtf + matrix.masmiks.wtf, 2026-10-13):
  - Token z kreatora service-accounta (App password) → `403 Token invalid/expired`. Token z
    Directory → Tokens z Intent = API Token → działa (potwierdzone).
  - Nowy service account nie ma żadnych uprawnień: `GET /core/users/me/` działa (200), ale lista
    userów → 403, dopóki nie dostanie View user (i Change user do zapisu atrybutów).
  - Authentik 2026.8.3: rola z View/Change user przypisana **bezpośrednio do usera** `im-sync`
    wystarczyła (`/core/users/me/` → `roles: ['im-sync']`, `groups: []`). Sama rola nieprzypięta do
    nikogo (`roles: []`) daje 403 mimo że istnieje. Preflight: wszystko OK; `--dry-run --once`: bot widzi userów.
  - Własny flow ustawień (`im-sync-user-settings-flow`): pola IM na jednym ekranie ze standardowymi,
    po zapisie i przeładowaniu wartości są na miejscu (potwierdzone na żywo).
  - Pułapka: wpięcie zapisu usera przed formularzem → `No Pending data.` i **zepsute ustawienia
    wszystkich userów** (brand wskazywał na flow). Dlatego: najpierw zastosuj blueprint i sprawdź
    Stage Bindings (prompt 20, write 100), dopiero potem przełącz brand. `order` jest w `attrs`
    blueprintu, nie w `identifiers` (inaczej ponowne zastosowanie robi duplikat). Preflight wykrywa ten stan.
  - E2E Matrix (2026-10-13): handle wpisany w ustawieniach → bot zamienia string na listę, zaprasza,
    pisze `Dodano @… na prośbę <user>` (m.notice), ustawia `matrix_status: ok`; drugi przebieg nic nie robi.
  - Telegram: jeszcze nie testowany.
  - Przestrzenie (2026-10-13, testowa „im-sync test" na masmiks.wtf): `MATRIX_SPACES` + `MATRIX_NOTIFY_ROOM` →
    zaproszenie tylko do space (`invite`), wiadomość w pokoju powiadomień, drugi przebieg nic nie robi.
    Nie sprawdzone: czy zaproszony po akceptacji space sam wchodzi do dziecka „restricted" (wymaga akcji zaproszonego).
- hskrk: —
- hswro: —
