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
`attributes.settings.locale` oraz `attributes.matrixHandles` (Text Area), `attributes.telegramUsername`,
`attributes.telegramCode`), Prompt Stage z nimi, User Write Stage (`never_create`), flow o designation
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

### Typ wiadomości „Dodano …" (mosty)

Domyślnie `MATRIX_MSGTYPE=m.text`. Mosty mautrix **nie przekazują** `m.notice` (typ „bot"), więc wiadomość nie
dotarłaby np. na Telegram; `m.text` jest przekazywany, ale klienty Matrixa **powiadamiają** o nim (w `#hskrk` to głośniej).
Cichy wariant: `MATRIX_MSGTYPE=m.notice` plus wyjątek dla konta bota w configu mostu (`bridge_notices` / `exceptions`).

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
3. ID grup (`-100…`): np. dodaj tymczasowo `@RawDataBot` do grupy albo `getUpdates` botem po wiadomości w grupie
   (uwaga: gdy bot działa, `getUpdates` daje błąd 409 — zatrzymaj go na chwilę).
4. **Wiele grup:** `TELEGRAM_GROUPS=-100111,-100222,-100333` (po przecinku), bot musi być adminem z „Invite users"
   w każdej. Zmiana `.env` wymaga restartu bota; `python -m bot.preflight` sprawdza każdą grupę osobno.
   Po weryfikacji user dostaje w jednym DM „• Nazwa grupy: link" (jednorazowe, 24 h). Grupa, której link się nie
   wygeneruje, nie blokuje reszty — user dostaje jej nazwę na liście „Nie udało się wygenerować linku do: …".
5. **`/grupy`** (w prywatnej rozmowie z botem, tylko dla połączonych userów): świeże linki do wszystkich grup. Dzięki
   temu dodanie grupy później nie wymaga niczego od admina, a wygasły/zużyty link da się odnowić.
6. **Komendy na grupie:** `/start` i `/grupy` (także w formie `/grupy@nazwabota`) wysłane na grupie z `TELEGRAM_GROUPS` bot odpowiada
   krótką instrukcją, że komendy działają tylko w prywatnej rozmowie, co robi `/start` (łączy konto z Authentikiem) i co
   robi `/grupy` (świeże linki). W DM-ach, gdzie user ma coś wpisać (brak dopasowania przy `/start`, niepołączone konto
   przy `/grupy`), bot dokleja adres ustawień: `AUTHENTIK_URL` + `/if/user/#/settings`. Telegram w grupach z włączonym
   „privacy mode" dostarcza botowi tylko komendy zaadresowane wprost (`/grupy@nazwabota`) albo wysłane zaraz po jego
   wiadomości, więc zwykłe `/grupy` bywa w ogóle niewidoczne dla bota — to ograniczenie Telegrama (admin grupy tego nie omija
   bez wyłączenia privacy mode w BotFatherze: `/setprivacy`).
   **Na grupach spoza `TELEGRAM_GROUPS` bot milczy** (nie odpowiada instrukcją), więc dodanie go do losowej grupy
   niczego nie zdradza ani nie robi hałasu. (Nie wychodzi z niej sam; w razie potrzeby wyrzuć go ręcznie.)

## 5. Wdrożenie na hoście

```bash
sudo mkdir -p /opt/im-sync-hskrk && sudo chown "$USER" /opt/im-sync-hskrk   # dla hswro: im-sync-hswro
git clone <URL-repo> /opt/im-sync-hskrk && cd /opt/im-sync-hskrk
cp env/hskrk.env.example .env && chmod 600 .env && "$EDITOR" .env
cp docker-compose.example.yml docker-compose.yml && "$EDITOR" docker-compose.yml   # lokalna kopia, ignorowana przez git

# Zbuduj obraz i sprawdź całą układankę zanim cokolwiek zacznie działać:
docker compose build
docker compose run --rm bot python -m bot.preflight          # wszystko ma być "OK"

# Próba na sucho (loguje, nic nie wysyła/zapisuje):
docker compose run --rm bot python -m bot.main --dry-run --once

# Na ostro:
docker compose up -d && docker compose logs -f --tail=50
```

**Własny `docker-compose.yml`:** w repo jest tylko szablon `docker-compose.example.yml`, a Twoja kopia
(`docker-compose.yml`) jest w `.gitignore`, więc `git pull` jej nie rusza i nie zgłasza konfliktów. Po aktualizacji
sprawdź, czy szablon się zmienił: `diff docker-compose.example.yml docker-compose.yml`.

**Aktualizacja** (kod z gita, obraz budowany na miejscu): `git pull && docker compose up -d --build`
(albo z gotowego obrazu: `docker compose pull && docker compose up -d`).

**Jednorazowa migracja** na hoście, który już miał śledzony `docker-compose.yml` ze zmianami lokalnymi
(inaczej `git pull` zgłosi „local changes would be overwritten"):
```bash
cp docker-compose.yml ~/docker-compose.local.yml     # zabezpiecz swoją wersję
git checkout -- docker-compose.yml                   # porzuć lokalne zmiany w śledzonym pliku
git pull                                             # plik znika z repo, pojawia się docker-compose.example.yml
cp ~/docker-compose.local.yml docker-compose.yml     # przywróć swoją wersję (teraz nieśledzona)
git status --short                                   # ma być czysto
```

**Bind mount zamiast named volume:** katalog danych musi być zapisywalny dla uid 10001 (użytkownik `bot` w obrazie):
`mkdir -p dane && sudo chown -R 10001:10001 dane`. Inaczej bot kończy z `unable to open database file`.

Katalogi `/opt/im-sync-hskrk` i `/opt/im-sync-hswro` są niezależne (osobny volume `bot-data`
z SQLite, osobna nazwa projektu compose wynika z nazwy katalogu).

## 6. Test E2E po wdrożeniu

1. Konto testowe w Authentiku → ustawienia → wpisz 1 poprawny handle Matrix i 1 z literówką.
2. W ≤ 60 s: invite przychodzi, w pokoju jest „Dodano … na prośbę …", a w atrybucie `matrixStatus` jest błąd dla literówki.
   Podgląd: Admin → Directory → Users → user → Attributes.
3. Telegram: wpisz `@username` w Authentiku → `/start` do bota → „Tak, to ja" → w atrybutach pojawia się ID w `telegramIDs`, w DM linki do grup.
   Wariant bez username: `/start` → kod → wklej w polu w Authentiku → ≤ 60 s ID w `telegramIDs`.
4. **Zabezpieczenie**: jako zwykły user spróbuj zapisać `telegramIDs` przez
   `curl -X PATCH -H "Authorization: Bearer <token zwykłego usera>" -d '{"attributes":{"telegramIDs":["1"]}}' $AUTHENTIK_URL/api/v3/core/users/me/` —
   ma się nie udać. Jeśli się uda, nie ufaj `telegramIDs` w innych aplikacjach, dopóki tego nie zablokujesz.

## Migracja atrybutów na camelCase (jednorazowo na instancję)

Nazwy atrybutów zmieniły się: `matrix_handles` → `matrixHandles`, `telegram_username` → `telegramUsername`,
`telegram_id` (i krótko używane `telegramVerifiedId`) są **scalane do listy `telegramIDs`** (reszta to wartości pochodne, bot liczy je od nowa). **Dane zostają**: bot przy starcie
scala stare klucze z nowymi i usuwa stare (szczegóły i tabela w `docs/usermanager-integration.md`, „Migracja atrybutów").

Kolejność wdrożenia na instancji z istniejącymi danymi:
1. **Podgląd:** `docker compose run --rm bot python -m bot.main --dry-run --once` → w logu `[dry-run] migracja atrybutów usera …`
   dla każdego, kogo to dotyczy, oraz ewentualne ostrzeżenie „nieznane atrybuty" (klucze, których bot nie zna i nie rusza).
   Pełny inwentarz wszystkich kluczy u userów (klucz×userów, tylko do odczytu): dodaj flagę `--audit`.
2. **Zaktualizuj blueprint** (Admin → Customization → Blueprints → treść z `blueprints/user-settings-im.yaml`, *Apply*):
   pola formularza zapisują teraz `attributes.matrixHandles` / `telegramUsername` / `telegramCode`. Podpowiedzi w polach mają
   fallback na stare klucze, więc userzy nie zobaczą pustych formularzy w trakcie przejścia.
3. **Wdróż bota** (`git pull && docker compose up -d --build`). Migracja uruchamia się przy starcie i co godzinę, więc
   kolejność kroków 2 i 3 nie jest krytyczna (stare zapisy ze starego formularza zostaną dościągnięte w ciągu godziny).
4. **Sprawdź** `docker compose logs --tail=50 bot`: `migracja: userów N, zmieniono M` i brak ostrzeżeń o nieznanych atrybutach
   (albo ich lista do ręcznego przejrzenia).

## Ręczne wysłanie linków do grup Telegrama

Strona Invitations w `usermanager` ma przy każdym koncie Telegrama przycisk, który dopisuje prośbę do `telegramInviteRequests`.
Bot przy najbliższym przebiegu (≤ `POLL_INTERVAL`) wysyła na to konto **DM ze świeżymi, jednorazowymi linkami do grup** (to samo
co `/grupy`) i zapisuje wynik w `telegramInviteResults`. Warunki: ID musi być na liście `telegramIDs` TEGO usera (inaczej wynik
`error: to konto Telegram nie należy do tego usera`), a konto musi kiedyś napisać do bota `/start` (Telegram nie pozwala botowi
pisać pierwszemu; wtedy wynik to czytelny błąd, a prośba nie wisi w kolejce).

## Które konta Telegrama bot może zagadnąć

Telegram nie pozwala botowi napisać do kogoś, kto nigdy do niego nie napisał. Bot **niczego nie sprawdza na ślepo** (żadnych
zapytań o nieznane konta, które wyglądałyby na skaner/spam): gdy konto napisze do bota (`/start`, `/grupy`, przycisk) albo zostanie
połączone kodem, bot dopisuje jego ID do `telegramReachableIDs`. `usermanager` pokazuje przycisk Invite tylko przy kontach z takim
śladem. ID wpisane ręcznie przez admina nie ma śladu, dopóki to konto nie napisze do bota (np. `/grupy`); od tego momentu da się
je zaprosić. Konta połączone przez bota przed tą zmianą (mają `telegramStatus`) `usermanager` uznaje za osiągalne.

## Tag „Verified” na grupach Telegrama

Bot ustawia zwykłym członkom grup z `TELEGRAM_GROUPS` (tylko tych) tag, domyślnie `Verified` (zmiana/wyłączenie: `TELEGRAM_MEMBER_TAG`, puste =
wyłączone, max 16 znaków, bez emoji). **Zweryfikowane** = każde ID z `telegramIDs` jakiegoś usera. Uznajemy je za właściwe, niezależnie od
tego, czy dodał je admin w usermanagerze, czy bot po weryfikacji (decyzja 2026-10-15). Kontakt konta z botem (`telegramReachableIDs`) mówi tylko,
czy bot może do niego napisać (potrzebne do wysyłki linków), a nie czy ID jest czyje.

**Kiedy:** przy kontakcie z botem, przy połączeniu konta, przy wejściu do grupy (zdarzenie `chat_member`), przy pierwszej wiadomości na grupie
oraz **dosypem dla wszystkich dodanych**: w
każdym przebiegu bot sprawdza do 20 par (grupa, konto), z przerwą 0,5 s, i pomija pary już załatwione. Konto, którego nie ma w grupie, jest
sprawdzane ponownie najwcześniej po 6 h. Przy kilkuset kontach pierwszy dosyp trwa kilkanaście minut, potem dzieje się niemal nic.

**`/verify` (tylko w prywatnej rozmowie z botem).** Potwierdza konto, które admin lub bot już przypisał userowi w Authentiku: zapisuje ślad
kontaktu (`telegramReachableIDs`, czyli bot może do Ciebie pisać) i od razu nadaje tag na grupach (tag dostałoby i bez tego). Nie otwiera procedury `/start` i nie wysyła linków (od tego są `/start`
i `/grupy`); gdy bot nie zna konta, podpowiada, co zrobić (wpisać @username w ustawieniach i wysłać `/start`). Użyte na grupie z `TELEGRAM_GROUPS`
bot odsyła krótką instrukcję „pisz na priv" (jak przy `/start` i `/grupy`), niczego nie zapisuje.

**Przegląd czatów.** Przy starcie i potem co godzinę bot sprawdza każdy czat z `TELEGRAM_GROUPS`: tagi nadaje tylko w **grupach i
supergrupach, w których jest adminem z prawem Manage tags**. **Kanały** są pomijane (Telegram: `setChatMemberTag` działa tylko w grupach i
supergrupach; linki do kanałów z `/grupy` dalej działają). W logu jest po jednej linii na pominięty czat z powodem i podsumowanie
`przegląd czatów: tagi „Verified” w N z M czatów: …`. Prawo przyznane później bot zauważy przy najbliższym przeglądzie (do godziny).

**Wiadomość na grupie jako wyzwalacz.** Gdy ktoś z ID w `telegramIDs` napisze na grupie, w której tagujemy, bot nadaje mu tag (jeśli tej pary
jeszcze nie załatwiono; potem zero zapytań). Bot jako admin widzi wszystkie wiadomości. Lista ID z Authentika jest czytana najwyżej raz na minutę.

**Wymaga w każdej grupie:** bot jako administrator z prawem **Manage tags** (`can_manage_tags`), oprócz „Invite users". Bez tego przegląd
pominie tę grupę z ostrzeżeniem w logu, a reszta bota działa normalnie. Admini i właściciel grupy tagu nie dostają (mają własny „custom title").
Biblioteka `python-telegram-bot` 21 nie ma metody `setChatMemberTag`, więc bot woła ją surowo (`do_api_request`); odczyt członka idzie zwykłym
`get_chat_member`.

**Nie zdejmuje tagu:** gdy ID zniknie z `telegramIDs` albo user straci członkostwo, tag zostaje do ręcznego usunięcia (świadomie).

## Spójność z Authentikiem (nadpisane atrybuty)

Źródłem prawdy jest Authentik, a SQLite (`state.db`) to tylko pamięć bota. Znany problem: narzędzie, które zapisuje usera
i **podmienia cały obiekt atrybutów** (stary `usermanager`), kasuje wszystko, co zapisał bot, a pamięć bota zostaje.
Bot sprawdza to w każdym przebiegu:

- **Pamięć o handle'ach, których nikt już nie ma wpisanych**, jest kasowana (jak dotąd). Dodatkowo kasowana jest pamięć
  o pokojach spoza konfiguracji i o userach, których nie ma już w Authentiku.
- **Wykrywanie nadpisania:** bot zapamiętuje, że zapisał `matrixAccounts` dla usera. Jeśli ich w Authentiku nie ma, a bot
  wcześniej je zapisał, znaczy to, że ktoś je nadpisał (świadome usunięcie handle'i zostawia `matrixAccounts` do czasu
  sprzątnięcia przez bota, więc nie jest brane za nadpisanie). Wtedy bot: loguje ostrzeżenie `spójność: user … stracił
  atrybuty bota`, **czyści pamięć o jego kontach** (konta, których nie ma w pokojach, dostaną zaproszenia ponownie, bo stan
  liczy się od nowa wg prawdziwego członkostwa w Matrixie) i zostawia userowi atrybut `imInviterNote` z opisem, co się stało i
  co sprawdzić. `--dry-run` tylko loguje.
- **Telegram nie ma stanu w SQLite**: „połączone" znaczy tyle, że ID jest na liście `telegramIDs` (to pole zachowuje się po
  zapisie w `usermanager`, bo jest w jego formularzu). Jeśli user z takim ID wpisze ponownie swój `telegramUsername` w
  ustawieniach i wyśle `/start`, bot prosi o **ponowne potwierdzenie** (idempotentne, ID nie dubluje się, wniosek się zamyka).
  Wniosek innego usera o to samo ID bot odrzuca.
- Po aktualizacji bota na instancji z istniejącymi danymi nie ma fałszywych alarmów: bot zaczyna wykrywać nadpisanie dopiero
  dla userów, którym sam zapisał stan.

## Odrzucone zaproszenia i ponowne zapraszanie

- **Odrzucone zaproszenie wygląda w Matrixie jak wyjście z pokoju (`leave`).** Bot go nie ponawia (inaczej zapraszałby
  co minutę kogoś, kto powiedział „nie"), ale zapisuje w `matrixStatus` usera: „@x odrzucił zaproszenie lub wyszedł z: <nazwa>".
- **Ponowne zaproszenie:** user usuwa handle z ustawień Authentika i dopisuje z powrotem. Bot zapomina handle'e, których
  nikt już nie ma wpisanych (przy najbliższym przebiegu, ≤ `POLL_INTERVAL`), więc dopisany handle jest zapraszany od nowa.
- **Nieprzyjęte zaproszenia** (stan `invite`): zaproszenia w Matrixie nie wygasają i nie da się ich „ponowić", więc bot
  nic z nimi nie robi, ale po `MATRIX_PENDING_AFTER_HOURS` (domyślnie 24, `0` = wyłączone) wpisuje w `matrixStatus`:
  „@x nie przyjął zaproszenia do: <nazwa> (ponad dobę / ponad 3 dni)". Opis wieku jest gruby, żeby status nie zmieniał
  się co godzinę. Dotyczy tylko zaproszeń wysłanych przez bota (cudzych nie nagabuje). Po akceptacji status wraca do `ok`.
  Test na żywo: usuń i dopisz handle (nowe zaproszenie), nie przyjmuj, uruchom bota z `MATRIX_PENDING_AFTER_HOURS=0.02`
  i po ~2 minutach zobacz atrybut `matrixStatus` w Authentiku.
- Zapominanie dotyczy wyłącznie pamięci bota (SQLite). **Nikogo nie wyrzuca** z pokoi (usuwanie jest osobną, jeszcze
  niezaimplementowaną funkcją).
- Ręcznie, bez ruszania Authentika: zaproś konto z Elementa albo skasuj parę z bazy
  (`delete from invited where handle=? and room=?` w `state.db`).

## Nieudane zaproszenia, limity (429) i niedostępne serwery

- **Odstęp po porażce:** nieudane zaproszenie bot ponawia z rosnącym odstępem 5 min → 15 min → 1 h → 6 h → 24 h
  (potem co 24 h), zamiast co minutę. Moment kolejnej próby jest w `matrixStatus` („… (następna próba 10:15 UTC)"),
  a po sukcesie status wraca do `ok`. Odstęp resetuje się, gdy usuniesz handle z Authentika i dopiszesz go z powrotem.
- **`429 Too Many Requests`:** bot respektuje `retry_after_ms` z odpowiedzi i **przestaje wysyłać zaproszenia do końca
  przebiegu** (kolejne dostaną swoją kolej w następnych). Wiadomość „Dodano…" jest osobno: jej porażka (np. limit wiadomości)
  nie cofa udanego zaproszenia i nie powoduje ponownego zaproszenia.
- **Błąd serwera (np. `502`)** przy zaproszeniu konta z nieosiągalnego serwera (`@x:matrix.krg`, literówka w domenie,
  serwer wewnętrzny): bot pomija pozostałe pokoje dla tego handle'a w danym przebiegu i wpada w odstęp. Taki handle
  warto po prostu poprawić lub usunąć w Authentiku.
- **Limit zaproszeń Synapse'a jest domyślnie bardzo ostry** (`rc_invites`: pojedynczy nadawca dostaje krótki zryw, potem
  jedno zaproszenie na kilka minut, i wlicza się do niego także nieudane). Przy większej liczbie handle'i × pokoi bot będzie
  więc zapraszał partiami. Na własnym homeserverze możesz limit podnieść w `homeserver.yaml`:
  ```yaml
  rc_invites:
    per_room: {per_second: 1, burst_count: 20}
    per_user: {per_second: 0.5, burst_count: 50}
  ```
  (restart Synapse'a; to dotyczy wszystkich userów serwera, więc dobierz wartości do skali). Alternatywa: administracyjny
  „override ratelimit" dla konta bota — czy obejmuje limit zaproszeń, zależy od wersji Synapse'a, sprawdź w dokumentacji.

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
| `matrixStatus`: `403 …` przy handle | Bot bez power levelu albo user nie istnieje | `preflight` pokaże power level; sprawdź handle |
| `matrixStatus`: `… ma bana w …` | Ban w pokoju | Zdejmij bana (bot nie zdejmuje sam) |
| Brak „Dodano…" w pokoju mimo invite | Pokój zaszyfrowany | Wyłącz szyfrowanie albo zaakceptuj brak wiadomości |
| Telegram: brak linków do grup | Bot nie jest adminem z „Invite users" | `preflight` wskaże grupę |
| Pola nie pojawiają się w ustawieniach | Blueprint nie zaaplikowany | Admin → Blueprints → status; ręczny wariant z kroku 2 |
| `sqlite3.OperationalError: unable to open database file` (kontener w pętli restartów) | Katalog danych z **bind mount** (`./dane:/data`) założył Docker jako `root`, a bot działa jako uid **10001** | `sudo chown -R 10001:10001 ./dane` albo `user: "UID:GID"` w compose; named volume (`bot-data`, domyślny compose) nie ma tego problemu |

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
    pisze `Dodano @… na prośbę <user>` (m.notice), ustawia `matrixStatus: ok`; drugi przebieg nic nie robi.
  - Telegram: jeszcze nie testowany.
  - Przestrzenie (2026-10-13, testowa „im-sync test" na masmiks.wtf): `MATRIX_SPACES` + `MATRIX_NOTIFY_ROOM` →
    zaproszenie tylko do space (`invite`), wiadomość w pokoju powiadomień, drugi przebieg nic nie robi.
    Potwierdzone: po przyjęciu zaproszenia do space konto samo weszło do dziecka „restricted" (membership `join` w obu),
    bez osobnego zaproszenia do pokoju (bot zapisał tylko invite do space).
- hskrk: —
- hswro: —
