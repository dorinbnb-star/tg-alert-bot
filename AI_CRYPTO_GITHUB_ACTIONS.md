# AI Crypto Trader prin GitHub Actions

Workflow: `.github/workflows/ai-crypto-trader.yml`.

## Stare curenta: dry-run automat

Workflow-ul verifica programarea la fiecare 10 minute si ruleaza scannerul numai intre `08:00` inclusiv si `22:00` exclusiv in fusul `Europe/Brussels`. Poarta de timp foloseste direct fusul local, deci trecerea CET/CEST este automata. In intervalul `22:00-07:59` nu se face checkout, nu se ruleaza testele si nu se apeleaza OKX. Atat calea programata (`schedule`), cat si cea manuala (`workflow_dispatch`) raman `--dry-run`. Nicio cale a workflow-ului nu citeste `TELEGRAM_TOKEN`/`TELEGRAM_CHAT_ID`, nu apeleaza Telegram, nu salveaza deduplicarea si nu trimite notificari. Rezultatul fiecarei scanari apare in GitHub Step Summary. Testul `tests/test_workflow.py` verifica aceste reguli la fiecare rulare activa.

Scannerul:

- foloseste endpointurile publice OKX (`https://www.okx.com/api/v5`), fara API key; Bybit raspunde HTTP 403 de pe runnerele GitHub;
- porneste cu `BTC-USDT-SWAP`, configurabil in `ai_crypto_monitor/config-v0.1.json` la `symbols`;
- foloseste context 4H, structura 1H si ultima lumanare 15m inchisa pentru trigger; lumanarile OKX cu `confirm != "1"` sunt excluse;
- scrie rezultatul fiecarei rulari (inclusiv `NO_ENTRY` si erorile) in GitHub Step Summary, nu in Telegram;
- cere sweep, revenire, confirmare, Entry, SL structural, TP structural si R:R minim;
- respinge date incomplete, vechi sau semnale expirate;
- nu trimite WATCH, startup, raport periodic sau mesaj `NO_ENTRY`;
- pastreaza `Probabilitate: NECALIBRATA` si nu deschide ordine.

## Secrete GitHub

Workflow-ul actual nu foloseste secrete. Pasii de mai jos sunt necesari numai pentru o viitoare cale de trimitere, care nu exista inca. Nu incarca `.env`. In repository-ul GitHub:

1. Deschide `Settings`.
2. Alege `Secrets and variables` -> `Actions`.
3. In `Repository secrets`, apasa `New repository secret`.
4. Creeaza exact `TELEGRAM_TOKEN` cu tokenul botului.
5. Creeaza exact `TELEGRAM_CHAT_ID` cu ID-ul chatului privat.

Valorile nu trebuie introduse in Variables, fisiere, workflow, loguri sau commit-uri.

## Dry-run manual

1. Deschide tabul `Actions`.
2. Selecteaza `AI Crypto Trader Entry Monitor`.
3. Apasa `Run workflow`.

Rularea manuala si cea programata nu citesc secretele si nu pot trimite Telegram. Un rezultat `NO_ENTRY` este succes si ramane fara notificare. O eroare tehnica (date OKX lipsa, vechi sau HTTP) face rularea rosie.

## Deduplicare

In dry-run nu se scrie si nu se salveaza starea de deduplicare. `concurrency` nu permite doua scanari simultane.

## Programare

Expresia cron `*/10 6-20 * * *` acopera reuniunea orelor UTC necesare pentru programul `08:00-21:50 Europe/Brussels` atat vara, cat si iarna. Pasul `Check Brussels scan window` aplica limita locala exacta si opreste toti pasii de lucru in afara intervalului. GitHub Actions poate porni cu intarziere sau poate sari rulari cand este aglomerat; criteriul testului de stabilitate ramane ca pauza observata intre scanari sa nu depaseasca 15 minute.
