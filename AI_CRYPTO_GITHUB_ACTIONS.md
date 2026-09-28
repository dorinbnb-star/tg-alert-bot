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

### Declansator principal: lant auto-programat, fara PAT si fara cont extern

Fiecare rulare este un "tic":

1. `scheduler/cadence.py` asteapta slotul primit de la ticul anterior (minutele `:00`, `:10`, ..., `:50`; maximum 11 minute), apoi verifica fereastra `08:00-22:00 Europe/Brussels` dupa ceasul real.
2. Numai in fereastra ruleaza testele si scannerul `--dry-run`.
3. Jobul separat `next-tick` (singurul cu `actions: write`) porneste urmatorul tic prin `workflow_dispatch`, cu tokenul efemer al rularii (`github.token`). GitHub permite ca `GITHUB_TOKEN` sa creeze rulari noi prin `workflow_dispatch`. Jobul ruleaza si daca scanarea a esuat, ca o eroare OKX sa nu rupa lantul.
4. Dupa scanarea de la `21:50`, urmatorul slot (`22:00`) este in afara ferestrei, deci lantul se opreste.

Pornirea si repornirea: cronul `52 5,6 * * *` porneste lantul la `07:52` ora Bruxelles (vara si iarna); primul tic asteapta pana la `08:00`. Cronul `5,15,25,35,45,55 6-20 * * *` reporneste lantul daca s-a rupt. `concurrency` pastreaza o singura rulare activa si cel mult una in asteptare, deci lanturile duplicate se reduc singure la unul.

Risc ramas: pornirea de dimineata si repornirea dupa o rupere depind de `schedule`, care pe 2026-09-28 a livrat 1 din ~40 de sloturi. Daca niciun cron nu porneste intre 07:52 si 08:15, prima scanare a zilei intarzie si auditul arata `FAIL`. O pornire manuala (`Run workflow` pe `main`) reporneste imediat lantul.

Pe alte branchuri decat `main`, lantul continua numai pentru un numar limitat de ticuri (inputul `ticks`), folosit pentru teste.

Cost si limite: repository-ul este public, deci minutele GitHub-hosted sunt gratuite. In fereastra activa, un runner asteapta pana la ~10 minute pe tic (aproximativ 14 ore de runner pe zi). Aceasta este o incarcare continua a unui runner GitHub; conditiile GitHub Actions interzic activitatile care pun pe servere o sarcina disproportionata fata de beneficiu, deci exista un risc de politica pe care proprietarul il accepta la merge.

### Alternativa cu incarcare minima: cron extern cu PAT

Daca riscul de mai sus nu este acceptat, un serviciu cron extern gratuit (de exemplu cron-job.org) poate trimite la fiecare 10 minute `POST https://api.github.com/repos/dorinbnb-star/tg-alert-bot/actions/workflows/ai-crypto-trader.yml/dispatches` cu corpul `{"ref":"main"}`, antetele `Accept: application/vnd.github+json`, `X-GitHub-Api-Version: 2022-11-28` si un token fine-grained limitat la acest repository, cu permisiunea `Actions: Read and write`. Necesita un cont extern si un token persistent.

Poarta de fereastra din `scheduler/cadence.py` ramane activa in toate cazurile, deci nicio declansare nu poate produce scanari intre `22:00` si `07:59`.

### Audit al pauzelor reale

`diagnostics/scan_gap_audit.py` citeste istoricul rularilor de pe `main` prin API-ul GitHub si ia ca moment al scanarii finalizarea reusita a pasului `Dry-run scan`. Rularile cu pasul sarit de poarta de timp sau esuat nu sunt numarate. Pentru ziua locala aleasa, auditul masoara pauzele din fereastra `08:00-22:00 Europe/Brussels`, inclusiv de la `08:00` la prima scanare si de la ultima scanare pana la `22:00` (sau pana la momentul auditului). Verdictul este `FAIL` daca o pauza depaseste 15 minute sau daca exista o scanare in afara ferestrei.

Workflow-ul `.github/workflows/scan-gap-audit.yml` ruleaza auditul manual (cu data optionala) si zilnic la 21:17 UTC. Are numai permisiuni de citire si foloseste tokenul efemer `github.token`, nu secrete. Un `FAIL` face rularea rosie. Local: `python diagnostics/scan_gap_audit.py --date 2026-09-28`.
