# authentik-im-sync

User loguje się do Authentika, w ustawieniach wpisuje konta Matrix (1..n) i Telegram.
Dane zostają jako **atrybuty usera w Authentiku** — inne apki i boty mogą z nich korzystać.
Jeden bot-demon czyta atrybuty i:

- **Matrix**: zaprasza brakujące konta do pokoi z configu i pisze w pokoju
  `Dodano <handle> na prośbę <username>`.
- **Telegram**: potwierdza własność konta, zapisuje numeryczne `telegramVerifiedId`, wysyła w DM
  jednorazowe linki do grup.

Projekt jest niezależny od k0 — `docker compose up` gdziekolwiek. Wdrożenie krok po kroku (hskrk/hswro): [docs/DEPLOY.md](docs/DEPLOY.md).

## Atrybuty (kontrakt dla innych aplikacji)

| Atrybut | Pisze | Znaczenie |
|---|---|---|
| `matrixHandles` | user, potem bot normalizuje | **lista** `@x:serwer` (małe litery, bez duplikatów) |
| `matrixStatus` | bot | `ok` albo opis błędów (zły handle, ban, odmowa invite) |
| `telegramUsername` | user | wniosek, jeszcze niezweryfikowany — **nie ufać** |
| `telegramCode` | user | kod z bota, bot kasuje po użyciu |
| `telegramVerifiedId` | **tylko bot** | zweryfikowane id (liczba) — temu można ufać |
| `telegramStatus` | bot | `ok` / `zły lub wygasły kod` / konflikt |

## Jak działa weryfikacja Telegrama

Telegram sam gwarantuje, że `@username` należy do pisającego, więc **token zwykle nie jest potrzebny**:

1. User wpisuje `@jan_kowalski` w Authentiku.
2. Pisze `/start` do bota → bot pyta „konto Authentik X prosi o połączenie, to Ty?" (przycisk;
   chroni przed wpisaniem cudzego @handle) → zapis `telegramVerifiedId`.
3. Bez username (albo bez wniosku): bot odpowiada jednorazowym kodem (15 min), user wkleja go
   w polu „Kod z Telegrama" w Authentiku, bot dopasowuje przy najbliższym przebiegu (≤ `POLL_INTERVAL`).

Kierunek bot → user → Authentik jest wymuszony: Authentik nie pokaże tokenu wygenerowanego przez bota.

Grupy: bot **nie może** dodać usera do grupy po id (ograniczenie API). Wysyła jednorazowy link
(`member_limit=1`, 24 h). Bot musi być adminem grupy z prawem „Invite users".

## Uruchomienie

1. **Authentik — service account + token API**: Directory → Users → *Create Service account* (np. `im-sync`),
   potem Directory → Tokens and App passwords → *Create* z **Intent = API Token** dla tego usera
   (token z kreatora service-accounta to App password i API go odrzuca: `Token invalid/expired`).
   Nadaj mu uprawnienia `View user` i `Change user` (rola + grupa, albo
   Admin → Directory → Users → Permissions). Sprawdź: `curl -H "Authorization: Bearer $T" $URL/api/v3/core/users/`.
2. **Authentik — pola w ustawieniach**: Admin → Customization → Blueprints → import
   `blueprints/user-settings-im.yaml` (tworzy własny flow ustawień z polami na jednym ekranie),
   potem **ręcznie** Admin → System → Brands → *User settings flow* = `im-sync-user-settings-flow`
   (szczegóły i cofnięcie: [docs/DEPLOY.md](docs/DEPLOY.md), krok 2). Otwórz jako zwykły user
   `/if/user/#/settings` i sprawdź, czy są pola. Jeśli blueprint się nie zaaplikował, utwórz 3 Prompty
   z `field_key` `attributes.matrixHandles` / `attributes.telegramUsername` / `attributes.telegramCode`
   ręcznie i dopnij Prompt Stage do `default-user-settings-flow` przed etapem zapisu.
3. **Matrix**: załóż konto bota, weź access token (Element → Settings → Help & About → Access Token, albo
   `POST /_matrix/client/v3/login`). Daj botowi w pokojach power level ≥ „Invite". Pokoje mają być
   **niezaszyfrowane** (bot nie robi E2EE). ID pokoju: Element → Room settings → Advanced.
4. **Telegram** (opcjonalnie): `@BotFather` → `/newbot`; dodaj bota jako admina do grup; ID grupy
   (`-100…`) zdobędziesz np. przez `@RawDataBot`.
5. `cp .env.example .env` i `cp docker-compose.example.yml docker-compose.yml`, uzupełnij `.env`, potem
   `docker compose up -d --build`. Własny `docker-compose.yml` jest ignorowany przez git (nie koliduje z `git pull`).

Test na sucho (loguje, nic nie wysyła i nie zapisuje; Telegram wyłączony):

```
docker compose run --rm bot python -m bot.main --dry-run --once
```

## Obraz Dockera

GitHub Actions (`.github/workflows/docker.yml`) po każdym pushu na `main` uruchamia testy i buduje obraz
`linux/amd64` + `linux/arm64`, publikowany w GHCR: `ghcr.io/hackerspacekrk/im-inviter`
(tagi: `latest`, `main`, `sha-<commit>`, a po tagu `vX.Y.Z` także `X.Y.Z` i `X.Y`). Pull requesty tylko budują obraz.
Wdrożenie z gotowego obrazu: `docker compose pull && docker compose up -d` (`docker-compose.example.yml` ma `image:` z fallbackiem
`build: .`). Jeśli pierwszy `pull` zwróci „denied", pakiet jest prywatny — ustaw widoczność w Packages → Package settings.

## Testy

```
pip install -r requirements-dev.txt && pytest
```

Pokrywają logikę (normalizacja, decyzje o zaproszeniach, kody, dopasowanie) na fake'ach
Authentika i Matrixa. **Nie pokrywają** realnych API — to kroki E2E poniżej.

## Test E2E (masmiks.wtf)

1. Testowy user w Authentiku → ustawienia → wpisz handle (jeden poprawny, jeden z literówką).
2. W ≤ 60 s: poprawny dostaje invite, w pokoju jest „Dodano … na prośbę …", `matrixStatus` pokazuje
   błąd dla literówki. Drugi przebieg nic nie robi.
3. Telegram: ścieżka z @username (przycisk) i ścieżka z kodem → `telegramVerifiedId` w atrybutach usera.
4. **Bezpieczeństwo**: jako zwykły user spróbuj zapisać `attributes.telegramVerifiedId` przez API
   (`PATCH /api/v3/core/users/me/`) — nie może się udać. Jeśli się da, `telegramVerifiedId` nie jest
   godne zaufania i trzeba to zablokować po stronie Authentika.

## Znane ograniczenia

- Bot nie usuwa/nie wyrzuca kont po skasowaniu handle'a z atrybutu.
- Kto odrzucił zaproszenie albo wyszedł z pokoju, nie jest zapraszany ponownie (pamięć w SQLite), a
  `matrixStatus` mówi to wprost („… odrzucił zaproszenie lub wyszedł z: <nazwa>"). Ponowne zaproszenie:
  usuń handle z Authentika i dopisz z powrotem (bot zapomina handle'e, których nikt już nie ma wpisanych).
  Utrata `/data/state.db` oznacza ponowne zaproszenie takich osób.
- Polling co `POLL_INTERVAL` s, bez webhooków z Authentika.
