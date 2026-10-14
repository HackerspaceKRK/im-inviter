# Integracja im-inviter ↔ usermanager

Kontrakt danych i plan zmian. Źródłem prawdy jest Authentik (atrybuty usera); `usermanager` (Go + React,
`HackerspaceKRK/usermanager`) pokazuje i edytuje te dane, a bot (`im-inviter`) je wykonuje.

## Decyzje (z rozmowy)

1. **Telegram:** wyłącznie lista `telegramIDs` (inne narzędzia ją czytają). Bot dopisuje do niej ID po weryfikacji;
   **nie ma osobnego znacznika „zweryfikowane"** ani limitu jednego konta na usera (usermanager też pozwala na wiele).
   Lista bywa zapisana jako tablica albo napis „288245411, 111111111"; bot czyta oba. Jest to lista współdzielona z innymi
   narzędziami, więc bot zmienia ją minimalnie: istniejących tablic **nie przepisuje** (liczby zostają liczbami, duplikatów
   nie usuwa), tylko dopisuje nowe ID (jako tekst) i zamienia napis na tablicę.
2. **Ręczny invite:** kolejka w atrybucie usera, wykonuje ją bot przy najbliższym przebiegu (≤ `POLL_INTERVAL`).
   `usermanager` nie dostaje żadnych sekretów Matrixa; uprawnienia pilnuje Authentik.
3. **Nazwy:** camelCase, zgodnie z konwencją hackerspace'u (`telegramIDs`, `mifareCardId`, …). Pierwotnie planowano snake_case,
   zmienione na życzenie. Stare klucze bot migruje sam (patrz „Migracja atrybutów"). `telegram_id` jest scalane do `telegramIDs`
   (celowo inna nazwa niż lista `telegramIDs`, żeby ich nie mylić).

## Kontrakt atrybutów

| Atrybut | Typ | Pisze | Znaczenie |
|---|---|---|---|
| `matrixHandles` | `string[]` | user (flow), admin (usermanager) | konta Matrix usera (`@x:serwer`) |
| `telegramUsername` | `string` | user (flow) | wniosek o weryfikację, niezweryfikowany |
| `telegramIDs` | `string[]` | admin (ręcznie) **i bot** (po weryfikacji) | numeryczne ID Telegrama, **wiele na usera**; bot tylko dopisuje, niczego nie usuwa |
| `telegramStatus` | `string` | bot | `ok` / komunikat błędu |
| `matrixStatus` | `string` | bot | `ok` / komunikat błędów (tekst zbiorczy) |
| `matrixAccounts` | `{handle: {state, detail, updated}}` | bot | stan **każdego** konta (poniżej) |
| `matrixInviteRequests` | `[{handle, by, ts}]` | **usermanager** | prośby o ręczny invite; bot przetwarza i czyści |
| `matrixInviteResults` | `[{handle, by, ts, result, rooms}]` (ostatnie 5) | bot | wynik ręcznych zaproszeń |

`matrixAccounts[handle].state` (agregat po wszystkich pokojach/przestrzeniach z konfiguracji bota, od najgorszego):
`error` > `banned` > `left` (odrzucił/wyszedł) > `pending` (czeka na akceptację) > `invited` (świeżo zaproszony) > `joined`.
`detail` to krótki opis (np. „nie przyjął zaproszenia do: Test Space (ponad dobę)"), `updated` to unix ts.

**Reguły dla piszących:**
- Atrybuty w Authentiku podmienia się **w całości** (`PATCH` zastępuje `attributes`). Każdy klient musi wczytać aktualne
  atrybuty i scalić; nigdy nie wysyłać samych „swoich" kluczy. (Obecny `EditUserPage` w `usermanager` tego nie robi i
  kasuje cudze klucze — do naprawy, patrz niżej.)
- `usermanager` zapisuje tylko: `matrixHandles`, `telegramIDs`, `matrixInviteRequests` (i swoje dotychczasowe pola).
  Reszta jest tylko do odczytu.

## Migracja atrybutów (stare nazwy → camelCase)

Bot przegląda **wszystkich** userów (także nieaktywnych) przy starcie i potem co godzinę (stary blueprint/flow może jeszcze
zapisać stare klucze). Jest idempotentna: user w nowym formacie nie jest zapisywany. Dla każdego usera:

| Stary klucz | Co robi bot |
|---|---|
| `matrix_handles` | **scala** z `matrixHandles` (nowe pierwsze, bez duplikatów, znormalizowane), stary usuwa |
| `telegram_username` | → `telegramUsername` (gdy nowy pusty), stary usuwa |
| `telegram_id`, `telegramVerifiedId` | **dopisuje** do `telegramIDs` (jeśli ID jeszcze nie ma; istniejących elementów nie zmienia), klucz usuwa |
| `matrix_status`, `matrix_accounts`, `matrix_invite_*`, `telegram_code`, `telegram_status` | **usuwa** (pochodne/jednorazowe, bot policzy od nowa) |
| puste `matrixHandles` / `telegramUsername` / `telegramCode` (`""`, `[]`) | usuwa (flow ustawień zapisuje puste stringi dla nietkniętych pól) |

Cudzych kluczy (usermanager, Authentik) **nie rusza**. W logu startowym dostajesz listę
„nieznanych atrybutów do ręcznego przejrzenia" (np. literówki albo pozostałości po innych narzędziach).
Podgląd bez zmian: `docker compose run --rm bot python -m bot.main --dry-run --once` (linie `[dry-run] migracja atrybutów usera …`).

## Zmiany w bocie (`im-inviter`)

1. **Kolejka ręcznych zaproszeń.** Dla każdego usera z `matrixInviteRequests`: dla każdego żądania, którego `handle` jest
   **na liście `matrixHandles` tego usera** (inaczej wynik `error: handle nie należy do usera` — admin nie zaprosi
   dowolnego konta przez fałszywą prośbę), zaproś do wszystkich pokoi/przestrzeni z konfiguracji **z pominięciem pamięci
   „już zapraszałem"** i odstępu po porażkach (to jest świadoma decyzja admina). `join` i `invite` (czeka) są pomijane,
   `ban` raportowany. Przy `429` żądanie **zostaje w kolejce** (bot wstrzymuje zaproszenia do końca przebiegu).
   Po przetworzeniu: usuń żądanie, dopisz wpis do `matrixInviteResults` (cap 5). Wiadomość w pokoju:
   „Dodano @x na prośbę <user> (ręcznie, przez <admin>)".
2. **`matrixAccounts`** liczone co przebieg z wyników per para; zapis tylko gdy się zmieniło.
3. **Telegram:** po weryfikacji (`/start` z przyciskiem albo kod) dopisz ID do `telegramIDs` (string, bez duplikatów,
   cudzych wpisów nie ruszaj); kolejne konta tego samego usera dopisują się do listy. Spełniony wniosek `telegramUsername`
   znika. Sprawdzenie „to ID należy już do innego usera" dotyczy `telegramIDs` innych userów.
4. Testy jednostkowe na fake'ach, jak dotąd.

## Zmiany w `usermanager`

**Backend (Go):**
- `summaryResponse` rozszerzone o statystyki integracji: `matrixLinked` (≥1 handle), `matrixHealthy` (`matrixStatus`=`ok`),
  `matrixProblems`, `telegramLinked` (niepuste `telegramIDs`), `telegramAccounts` (suma kont Telegrama),
  `telegramPending` (niepusty `telegramUsername`, czyli wniosek czeka na `/start`).
- Nowy `GET /api/local/matrix-users`: userzy z niepustym `matrixHandles` (z cache; cache i tak się unieważnia po
  zmianie `last_updated`, a zapisy bota ją zmieniają). Czysta funkcja klasyfikacji + testy Go (w repo nie ma żadnych,
  dodajemy minimalne dla liczenia).

**Frontend (React):**
- **Naprawa `EditUserPage`:** wczytane atrybuty są bazą, formularz nakłada się na nie; klucz usuwamy tylko wtedy, gdy
  user go świadomie wyczyścił. Bez tego żadna z poniższych funkcji nie przetrwa pierwszego zapisu usera.
- **Edycja usera:** sekcja „Matrix" (lista handle'i jak `telegramIDs`, walidacja formatu, status + stan każdego konta)
  i sekcja „Telegram" (edytowalna lista `telegramIDs`, wiele kont, `telegramUsername` tylko do odczytu jako „wniosek czeka", status).
- **Invitations** (dziś placeholder): nagłówek ze statystykami; tabela userów z kontami Matrix, stan każdego konta
  (odznaka z `matrixAccounts`), ostatni wynik z `matrixInviteResults`, przycisk **Invite** per konto (zapisuje żądanie
  w `matrixInviteRequests`, pokazuje „zakolejkowano", odświeża co kilka sekund aż bot przetworzy).
- **Dashboard:** karty „Matrix połączone" i „Telegram połączone" (z liczbą problemów/oczekujących).
- **Users:** kolumny „Matrix" i „Telegram" z odznakami statusu.

## Kolejność wdrożenia

1. Bot (zmiany wsteczne zgodne: nowe atrybuty są dodatkowe) → `git pull && docker compose up -d --build` na hskrk/hswro.
2. `usermanager` (branch w repo, PR przez org). Przycisk Invite działa dopiero po kroku 1.

## Weryfikacja

- Bot: testy jednostkowe (kolejka, `matrixAccounts`, `telegramIDs`, bezpieczeństwo „handle musi należeć do usera").
- `usermanager`: `go build ./... && go vet ./... && go test ./...`, `npm run build`, `npm run lint`, `npx tsc -b --noEmit`
  (to robi CI). **Wygląd UI nie jest weryfikowany wizualnie** — logowanie przez OAuth do Authentika uniemożliwia
  uruchomienie bez prawdziwej instancji; sprawdzisz go po wdrożeniu.
- Na żywo: przycisk Invite na koncie testowym → po ≤ minucie wynik w `matrixInviteResults` i stan w `matrixAccounts`.

## Poza zakresem

Usuwanie z pokoi po skasowaniu handle'a (osobna funkcja, czeka na decyzje), edycja `telegramUsername` w `usermanager`,
ręczne zaproszenia do grup Telegrama.
