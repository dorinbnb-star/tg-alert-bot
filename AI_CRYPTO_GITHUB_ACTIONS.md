# AI Crypto Trader prin GitHub Actions

Workflow: `.github/workflows/ai-crypto-trader.yml`.

## Stare curenta: dry-run automat

Workflow-ul este declansat la fiecare 10 minute (vezi `Programare`) si ruleaza scannerul numai intre `08:00` inclusiv si `22:00` exclusiv in fusul `Europe/Brussels`. Poarta de timp foloseste direct fusul local, deci trecerea CET/CEST este automata. In intervalul `22:00-07:59` nu se ruleaza testele si nu se apeleaza OKX; se face doar checkout pentru planificatorul `scheduler/cadence.py`. Atat calea programata (`schedule`), cat si cea manuala (`workflow_dispatch`) raman `--dry-run`. Nicio cale a workflow-ului nu citeste `TELEGRAM_TOKEN`/`TELEGRAM_CHAT_ID`, nu apeleaza Telegram, nu salveaza deduplicarea si nu trimite notificari. Rezultatul fiecarei scanari apare in GitHub Step Summary. Testul `tests/test_workflow.py` verifica aceste reguli la fiecare rulare activa.

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

### De ce `schedule` singur nu ajunge

Pe 2026-09-28, cu workflow-ul `active`, repository public si branch implicit `main`, GitHub a creat o singura rulare `schedule` (17:02:56 UTC) din aproximativ 40 de sloturi cron asteptate intre 08:46 si 18:31 UTC, iar dupa commitul 819c3b7 nu a creat rularile de la 18:40 si 18:50 UTC. Sloturile lipsa nu apar deloc in istoric (nici esuate, nici anulate, nici in coada), deci nu au fost create de planificatorul GitHub. Documentatia GitHub spune ca evenimentul `schedule` poate fi intarziat, iar unele rulari pot fi abandonate la incarcare mare, mai ales la inceputul orei. Prin urmare `schedule` nu poate garanta pauza maxima de 15 minute.

### Declansator principal: lant cu Environment wait timers, fara PAT si fara cont extern

Fiecare rulare este un "tic" cu trei joburi:

1. `wait`: singurul job care foloseste un environment. Asteapta wait timer-ul environment-ului primit de la ticul anterior (`inputs.wait_env`). In timpul asteptarii nu este ocupat niciun runner, iar timpul de asteptare nu este facturat. La pornire manuala sau din cron (fara `wait_env`) jobul este sarit.
2. `monitor`: ruleaza `scheduler/cadence.py`, care nu doarme. Planificatorul verifica ora reala in `Europe/Brussels`; numai intre `08:00` si `22:00` ruleaza testele si scannerul `--dry-run`. Apoi alege environment-ul de asteptare pentru succesor. Verifica si ca wait timer-ul chiar a fost aplicat: daca rularea porneste cu peste 30 de secunde inainte de `not_before`, ori environment-ul nu este in lista de mai jos, jobul esueaza si lantul se opreste (protectie contra buclelor rapide daca un environment lipseste sau nu are timer).
3. `next-tick`: singurul job cu `actions: write`. Porneste succesorul prin `workflow_dispatch` cu tokenul efemer al rularii (`github.token`) si reincearca de cel mult 3 ori (dupa 5, 15 si 30 de secunde) la erori de retea, 408, 429 sau 5xx. Ruleaza si daca scanarea a esuat, ca o eroare OKX sa nu rupa lantul. Anularea manuala a unei rulari opreste lantul.

Ziua: fiecare tic alege 9 sau 10 minute, astfel incat scanarea urmatoare sa cada cat mai aproape de minutele `:00`, `:10`, ..., `:50` (ultima la `21:50`). Seara: dupa scanarea de la `21:50`, succesorul asteapta intr-un environment overnight si porneste in jurul orei `08:00`, fara rulari in timpul noptii. Durata noptii este aleasa automat din fusul `Europe/Brussels`: 610 minute intr-o noapte normala (CET sau CEST), 550 in noaptea trecerii la ora de vara, 670 in noaptea trecerii la ora de iarna.

Environments de creat in `Settings` > `Environments` (nume exacte; fara secrete, fara reviewers; `Deployment branches and tags` = `No restriction`):

| Environment | Wait timer (minute) | Folosit pentru |
|---|---|---|
| `scan-wait-9m` | 9 | tic de zi, aliniere la grila de 10 minute |
| `scan-wait-10m` | 10 | tic de zi |
| `scan-overnight-550m` | 550 | noaptea trecerii la ora de vara |
| `scan-overnight-610m` | 610 | noapte normala |
| `scan-overnight-670m` | 670 | noaptea trecerii la ora de iarna |

Pornirea si recuperarea: cronul `52 5,6 * * *` (07:52 ora Bruxelles, vara si iarna) si cronul rar `7 6-20 * * *` (o data pe ora) pornesc lantul daca nu exista. `concurrency` pastreaza o singura rulare activa si cel mult una in asteptare, deci un cron care porneste in timp ce lantul este viu este anulat de urmatorul tic. Daca pornirea de dimineata ratata nu este acoperita de cron, un `Run workflow` manual pe `main` reporneste lantul.

Pe alte branchuri decat `main`, lantul continua numai pentru un numar limitat de ticuri (inputul `ticks`), folosit pentru demonstratii.

Poarta de fereastra din `scheduler/cadence.py` ramane activa in toate cazurile, deci nicio declansare nu poate produce teste, apeluri OKX sau scanari intre `22:00` si `07:59`.

### Audit al pauzelor reale

`diagnostics/scan_gap_audit.py` citeste istoricul rularilor de pe `main` prin API-ul GitHub si ia ca moment al scanarii finalizarea reusita a pasului `Dry-run scan`. Rularile cu pasul sarit de poarta de timp sau esuat nu sunt numarate. Pentru ziua locala aleasa, auditul masoara pauzele din fereastra `08:00-22:00 Europe/Brussels`, inclusiv de la `08:00` la prima scanare si de la ultima scanare pana la `22:00` (sau pana la momentul auditului). Verdictul este `FAIL` daca o pauza depaseste 15 minute sau daca exista o scanare in afara ferestrei.

Workflow-ul `.github/workflows/scan-gap-audit.yml` ruleaza auditul manual (cu data optionala) si zilnic la 21:17 UTC. Are numai permisiuni de citire si foloseste tokenul efemer `github.token`, nu secrete. Un `FAIL` face rularea rosie. Local: `python diagnostics/scan_gap_audit.py --date 2026-09-28`.
