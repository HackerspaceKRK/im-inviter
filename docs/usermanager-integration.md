# Integracja im-inviter ↔ usermanager

Kontrakt danych i plan zmian. Źródłem prawdy jest Authentik (atrybuty usera); `usermanager` (Go + React,
`HackerspaceKRK/usermanager`) pokazuje i edytuje te dane, a bot (`im-inviter`) je wykonuje.

## Decyzje (z rozmowy)

1. **Telegram:** bot dopisuje zweryfikowane ID do istniejącej listy `telegramIDs` (inne narzędzia ją czytają);
   `telegram_id` zostaje jako znacznik „zweryfikowane przez bota".
2. **Ręczny invite:** kolejka w atrybucie usera, wykonuje ją bot przy najbliższym przebiegu (≤ `POLL_INTERVAL`).
   `usermanager` nie dostaje żadnych sekretów Matrixa; uprawnienia pilnuje Authentik.
3. **Nazwy:** atrybuty integracji zostają w snake_case (są już na produkcji). Istniejące `telegramIDs` (camelCase) bez zmian.

## Kontrakt atrybutów

| Atrybut | Typ | Pisze | Znaczenie |
|---|---|---|---|
| `matrix_handles` | `string[]` | user (flow), admin (usermanager) | konta Matrix usera (`@x:serwer`) |
| `telegram_username` | `string` | user (flow) | wniosek o weryfikację, niezweryfikowany |
| `telegramIDs` | `string[]` | admin (ręcznie) **i bot** (zweryfikowane) | numeryczne ID Telegrama; bot tylko dopisuje, niczego nie usuwa |
| `telegram_id` | `number` | bot | zweryfikowane ID; znacznik „verified" dla wpisu w `telegramIDs` |
| `telegram_status` | `string` | bot | `ok` / komunikat błędu |
| `matrix_status` | `string` | bot | `ok` / komunikat błędów (tekst zbiorczy) |
| `matrix_accounts` | `{handle: {state, detail, updated}}` | bot | stan **każdego** konta (poniżej) |
| `matrix_invite_requests` | `[{handle, by, ts}]` | **usermanager** | prośby o ręczny invite; bot przetwarza i czyści |
| `matrix_invite_results` | `[{handle, by, ts, result, rooms}]` (ostatnie 5) | bot | wynik ręcznych zaproszeń |

`matrix_accounts[handle].state` (agregat po wszystkich pokojach/przestrzeniach z konfiguracji bota, od najgorszego):
`error` > `banned` > `left` (odrzucił/wyszedł) > `pending` (czeka na akceptację) > `invited` (świeżo zaproszony) > `joined`.
`detail` to krótki opis (np. „nie przyjął zaproszenia do: Test Space (ponad dobę)"), `updated` to unix ts.

**Reguły dla piszących:**
- Atrybuty w Authentiku podmienia się **w całości** (`PATCH` zastępuje `attributes`). Każdy klient musi wczytać aktualne
  atrybuty i scalić; nigdy nie wysyłać samych „swoich" kluczy. (Obecny `EditUserPage` w `usermanager` tego nie robi i
  kasuje cudze klucze — do naprawy, patrz niżej.)
- `usermanager` zapisuje tylko: `matrix_handles`, `telegramIDs`, `matrix_invite_requests` (i swoje dotychczasowe pola).
  Reszta jest tylko do odczytu.

## Zmiany w bocie (`im-inviter`)

1. **Kolejka ręcznych zaproszeń.** Dla każdego usera z `matrix_invite_requests`: dla każdego żądania, którego `handle` jest
   **na liście `matrix_handles` tego usera** (inaczej wynik `error: handle nie należy do usera` — admin nie zaprosi
   dowolnego konta przez fałszywą prośbę), zaproś do wszystkich pokoi/przestrzeni z konfiguracji **z pominięciem pamięci
   „już zapraszałem"** i odstępu po porażkach (to jest świadoma decyzja admina). `join` i `invite` (czeka) są pomijane,
   `ban` raportowany. Przy `429` żądanie **zostaje w kolejce** (bot wstrzymuje zaproszenia do końca przebiegu).
   Po przetworzeniu: usuń żądanie, dopisz wpis do `matrix_invite_results` (cap 5). Wiadomość w pokoju:
   „Dodano @x na prośbę <user> (ręcznie, przez <admin>)".
2. **`matrix_accounts`** liczone co przebieg z wyników per para; zapis tylko gdy się zmieniło.
3. **Telegram:** po weryfikacji (`/start` z przyciskiem albo kod) dopisz ID do `telegramIDs` (string, bez duplikatów,
   cudzych wpisów nie ruszaj). Backfill: user z `telegram_id`, którego nie ma w `telegramIDs`, dostaje wpis przy
   najbliższym przebiegu. Sprawdzenie „to ID należy już do innego usera" obejmuje także `telegramIDs` innych userów.
4. Testy jednostkowe na fake'ach, jak dotąd.

## Zmiany w `usermanager`

**Backend (Go):**
- `summaryResponse` rozszerzone o statystyki integracji: `matrixLinked` (≥1 handle), `matrixHealthy` (`matrix_status`=`ok`),
  `matrixProblems`, `telegramLinked` (zweryfikowane `telegram_id`), `telegramInList` (niepuste `telegramIDs`),
  `telegramPending` (`telegram_username` bez `telegram_id`).
- Nowy `GET /api/local/matrix-users`: userzy z niepustym `matrix_handles` (z cache; cache i tak się unieważnia po
  zmianie `last_updated`, a zapisy bota ją zmieniają). Czysta funkcja klasyfikacji + testy Go (w repo nie ma żadnych,
  dodajemy minimalne dla liczenia).

**Frontend (React):**
- **Naprawa `EditUserPage`:** wczytane atrybuty są bazą, formularz nakłada się na nie; klucz usuwamy tylko wtedy, gdy
  user go świadomie wyczyścił. Bez tego żadna z poniższych funkcji nie przetrwa pierwszego zapisu usera.
- **Edycja usera:** sekcja „Matrix" (lista handle'i jak `telegramIDs`, walidacja formatu, status + stan każdego konta)
  i sekcja „Telegram" (`telegramIDs` z odznaką „verified" dla `telegram_id`, `telegram_username` tylko do odczytu, status).
- **Invitations** (dziś placeholder): nagłówek ze statystykami; tabela userów z kontami Matrix, stan każdego konta
  (odznaka z `matrix_accounts`), ostatni wynik z `matrix_invite_results`, przycisk **Invite** per konto (zapisuje żądanie
  w `matrix_invite_requests`, pokazuje „zakolejkowano", odświeża co kilka sekund aż bot przetworzy).
- **Dashboard:** karty „Matrix połączone" i „Telegram połączone" (z liczbą problemów/oczekujących).
- **Users:** kolumny „Matrix" i „Telegram" z odznakami statusu.

## Kolejność wdrożenia

1. Bot (zmiany wsteczne zgodne: nowe atrybuty są dodatkowe) → `git pull && docker compose up -d --build` na hskrk/hswro.
2. `usermanager` (branch w repo, PR przez org). Przycisk Invite działa dopiero po kroku 1.

## Weryfikacja

- Bot: testy jednostkowe (kolejka, `matrix_accounts`, `telegramIDs`, bezpieczeństwo „handle musi należeć do usera").
- `usermanager`: `go build ./... && go vet ./... && go test ./...`, `npm run build`, `npm run lint`, `npx tsc -b --noEmit`
  (to robi CI). **Wygląd UI nie jest weryfikowany wizualnie** — logowanie przez OAuth do Authentika uniemożliwia
  uruchomienie bez prawdziwej instancji; sprawdzisz go po wdrożeniu.
- Na żywo: przycisk Invite na koncie testowym → po ≤ minucie wynik w `matrix_invite_results` i stan w `matrix_accounts`.

## Poza zakresem

Usuwanie z pokoi po skasowaniu handle'a (osobna funkcja, czeka na decyzje), edycja `telegram_username` w `usermanager`,
ręczne zaproszenia do grup Telegrama.
