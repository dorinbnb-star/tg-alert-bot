# AI Crypto Trader prin GitHub Actions

Workflow: `.github/workflows/ai-crypto-trader.yml`.

## Stare curenta: alerte Telegram pentru ENTRY confirmat si rezultatul teoretic

Workflow-ul este declansat la fiecare 10 minute (vezi `Programare`) si ruleaza scannerul numai intre `08:00` inclusiv si `22:00` exclusiv in fusul `Europe/Brussels`. In intervalul `22:00-07:59` nu se ruleaza testele si nu se apeleaza OKX; se face doar checkout pentru planificatorul `scheduler/cadence.py`.

Doua cai, care se exclud reciproc:

- **Live** (pasul `Live scan`): numai pe `main` si numai pentru ticurile pornite de `schedule` sau de lantul insusi (`github-actions[bot]`). Este singurul pas care citeste secretele `TELEGRAM_TOKEN` si `TELEGRAM_CHAT_ID` si singurul care ruleaza `--send`. Trimite numai pentru un entry complet confirmat sau pentru rezultatul teoretic TP/SL/timeout al unei alerte acceptate anterior. Pentru `NO_ENTRY`, setup neconfirmat, semnal expirat sau duplicat nu trimite nimic. Nu exista mesaje WATCH, startup, heartbeat, periodice sau de eroare.
- **Dry-run** (pasul `Dry-run scan`): orice `Run workflow` manual, orice re-run pornit de o persoana si orice alt branch. Nu vede secretele si nu poate trimite.

Rezultatul fiecarei scanari apare in GitHub Step Summary. Testele `tests/test_workflow.py` si `tests/test_alert_path.py` verifica aceste reguli la fiecare tic activ.

Scannerul:

- foloseste endpointurile publice OKX (`https://www.okx.com/api/v5`), fara API key; Bybit raspunde HTTP 403 de pe runnerele GitHub;
- scaneaza 18 contracte perpetue USDT active pe OKX, configurate in `ai_crypto_monitor/config-v0.1.json` la `symbols`;
- foloseste context 4H, structura 1H si ultima lumanare 15m inchisa pentru trigger; lumanarile OKX cu `confirm != "1"` sunt excluse;
- scrie rezultatul fiecarei rulari (inclusiv `NO_ENTRY` si erorile) in GitHub Step Summary, nu in Telegram;
- cere sweep, revenire, confirmare, Entry, SL structural, TP structural si R:R minim;
- respinge date incomplete sau vechi; un setup confirmat care a expirat (peste `entry_valid_seconds` = 420 s de la inchiderea lumanarii de confirmare) sau al carui pret a deviat peste 0,15% este sarit silentios (`ENTRY_SKIPPED`);
- nu executa ordine si nu apeleaza niciun endpoint de ordine; foloseste doar endpointuri publice de piata OKX.

Formatul alertei (exemplu sintetic din teste, nu semnal real):

```
🟢 AAVE LONG

Entry teoretic: 179.12
Preț verificat: 178.95
SL: 177.72
TP: 182.75
R:R: 3.09
Valabil până la: 20:07
Status: PENDING
```

Pentru SHORT se foloseste marcajul rosu, iar simbolul este afisat fara sufixul `-USDT-SWAP`. R:R afisat este calculat fata de `Pret verificat`; R:R folosit de strategie ramane separat in diagnostic drept `strategy_rr`. BIAS, SETUP, TRIGGER, Pro, Contra si eticheta `NECALIBRATA` raman in JSONL si Step Summary, nu in Telegram. Scorul de confluenta nu este tratat ca probabilitate.

Precizia tuturor preturilor afisate vine din `tickSz`, citit o singura data pe rulare din endpointul public `/api/v5/public/instruments?instType=SWAP`. Valorile interne nu sunt rotunjite. Daca endpointul de instrumente esueaza, scanarea continua cu un fallback de sase cifre semnificative.

## Urmarirea rezultatului teoretic

O alerta acceptata de Telegram deschide o urmarire teoretica la `Pret verificat`; botul nu presupune ca utilizatorul a executat tranzactia. La fiecare tic live sunt citite high/low-urile lumanarilor complet inchise dupa ultima verificare. Pentru LONG, low la SL inchide cu `-1R`, iar high la TP inchide cu R:R urmarit; pentru SHORT regulile sunt inversate. Daca TP si SL apar in aceeasi lumanare, rezultatul este SL.

OKX ofera lumanari publice `1s` pentru SWAP. Trackerul ignora orice lumanare care a inceput inaintea alertei: foloseste 1s pana la primul minut complet, 1m pana la urmatoarea granita 15m si apoi 15m. Ramane o limita inevitabila mai mica de o secunda intre verificarea pretului si prima lumanare 1s completa; acea fractiune nu este atribuita retroactiv. La final se folosesc din nou 1m/1s, astfel incat timeout-ul sa ramana la 72 de ore de la acceptarea alertei.

Timeout-ul este parametrul operational `outcome_timeout_hours` din `config-v0.1.json`, nu prag de strategie. Daca TP si SL nu sunt atinse in 72 ore, rezultatul foloseste tickerul curent si R calculat fata de pretul verificat. Mesajele de rezultat sunt:

```
✅ AAVE LONG: TP HIT (+2.59R)
❌ AAVE LONG: SL HIT (−1R)
⏱ AAVE LONG: ÎNCHIS LA TIMEOUT (+0.34R la prețul curent 179.73)
```

Noaptea nu ruleaza scannerul. Atingerile dintre `22:00` si `08:00` sunt recuperate cronologic din lumanarile istorice, iar mesajul rezultatului pleaca la primul tic activ de dupa `08:00`.

Starea `ai_crypto_monitor/state/alert-outcomes.json` este separata de `dedup.json` si de diagnostic. Contine alertele deschise si tombstone-uri pentru rezultatele deja trimise. Cache-ul are o cheie noua la fiecare rulare; artifactul `open-alert-state`, pastrat 30 de zile, este rezerva. Daca lipseste cache-ul, workflow-ul recupereaza ultimul artifact de pe acelasi branch. Pentru aceasta operatie numai jobul `monitor` are permisiunea suplimentara `actions: read`; nu exista alta permisiune noua. Daca lipsesc si cache-ul si artifactul, starea porneste goala, scrie `TRACKING_STATE_LOST` in diagnostic si nu reconstruieste sau retrimite rezultate vechi.

Starea unui rezultat este mutata in `resolved` numai dupa ce Telegram accepta mesajul. La refuz Telegram, alerta ramane deschisa pentru reincercare. Exista acelasi risc rezidual ca la dedup: o cadere a runnerului dupa acceptarea Telegram, dar inainte de salvarea cache-ului/artifactului, poate permite o repetare.

## Secrete GitHub

Calea live citeste exact doua secrete de repository: `TELEGRAM_TOKEN` si `TELEGRAM_CHAT_ID`. Daca lipseste oricare, pasul `Live scan` esueaza inainte de orice apel de retea (fail closed) si nu trimite nimic; valorile nu sunt afisate niciodata. Inainte de o trimitere, scannerul verifica si ca botul are username-ul din `expected_bot_username` si ca destinatia este un chat privat.

Secretele se creeaza in `Settings` > `Secrets and variables` > `Actions` > `Repository secrets`, cu numele exacte de mai sus. Valorile nu se pun in Variables, fisiere, workflow, loguri sau commit-uri.

## Dry-run manual

`Actions` > `AI Crypto Trader Entry Monitor` > `Run workflow`. Rularea manuala foloseste pasul `Dry-run scan`, nu vede secretele si nu poate trimite Telegram. Pe `main`, rularea manuala porneste si succesorul lantului, iar ticurile urmatoare (pornite de `github-actions[bot]`) folosesc calea live. Un rezultat `NO_ENTRY` este succes si ramane fara notificare. O eroare tehnica (date OKX lipsa, vechi sau HTTP) face rularea rosie.

## Deduplicare

Semnatura unui semnal include simbolul, directia, pivotul 1H, lumanarea de sweep si lumanarea 15m de confirmare. Pe calea live, starea `ai_crypto_monitor/state/dedup.json` este restaurata din cache-ul GitHub Actions (`ai-crypto-dedup-<branch>-*`) si salvata intr-un cache nou numai dupa ce Telegram a acceptat alerta (`alert_sent=true`). Acelasi setup confirmat nu poate alerta de doua ori. Risc rezidual: daca Telegram accepta alerta, dar salvarea cache-ului esueaza, un tic ulterior din fereastra de 420 s ar putea repeta alerta; esecul se vede in log. In dry-run nu se scrie si nu se salveaza starea. `concurrency` (grupul `ai-crypto-trader-entry-monitor-${{ github.ref }}`) nu permite doua scanari simultane pe acelasi branch.

## Diagnostic de strategie (experiment de 7 zile)

Diagnosticul este separat de `dedup.json` si nu schimba decizia, alerta sau configuratia strategiei. Decizia se calculeaza intai fara trace; o a doua evaluare produce numai explicatia. Daca trace-ul esueaza, semnalul si livrarea raman neschimbate.

Pentru fiecare scanare si simbol, randul JSONL contine biasul si valorile EMA, ultima etapa trecuta, filtrul la care s-a oprit, datele celui mai avansat candidat (sweep, entry, SL, TP si R:R), varsta confirmarii si un R:R shadow pentru al doilea pivot 1H. Campul shadow nu participa la decizie. Raportul trebuie analizat separat pentru fiecare simbol: pragurile v0.1 au fost definite initial pentru BTC, iar simbolurile adaugate ulterior au un istoric mai scurt si volatilitati diferite.

Istoricul cumulativ este restaurat si salvat la fiecare scanare activa intr-un cache separat, cu prefixul `ai-crypto-diag-v1-<branch>-`. Fiecare rulare publica separat `scan-diag-<run_id>-<attempt>`, pastrat 14 zile, astfel incat datele sa poata fi reconstruite daca dispare cache-ul. Un pas final cu `always()` scrie `SCAN_ERROR` cand testele, scannerul sau procesul se opresc inainte sa produca randul normal. `cache_reset`, `restored_rows`, `run_id` si `run_attempt` fac intreruperile de persistenta vizibile.

Artifactele descarcate se rezuma local cu:

```text
python -m ai_crypto_monitor.scan_diagnostics summarize <director-artifacte>
```

## Programare

### De ce `schedule` singur nu ajunge

Pe 2026-09-28, cu workflow-ul `active`, repository public si branch implicit `main`, GitHub a creat o singura rulare `schedule` (17:02:56 UTC) din aproximativ 40 de sloturi cron asteptate intre 08:46 si 18:31 UTC, iar dupa commitul 819c3b7 nu a creat rularile de la 18:40 si 18:50 UTC. Sloturile lipsa nu apar deloc in istoric (nici esuate, nici anulate, nici in coada), deci nu au fost create de planificatorul GitHub. Documentatia GitHub spune ca evenimentul `schedule` poate fi intarziat, iar unele rulari pot fi abandonate la incarcare mare, mai ales la inceputul orei. Prin urmare `schedule` nu poate garanta pauza maxima de 15 minute.

### Declansator principal: lant cu Environment wait timers, fara PAT si fara cont extern

Fiecare rulare este un "tic" cu trei joburi:

1. `wait`: singurul job care foloseste un environment. Asteapta wait timer-ul environment-ului primit de la ticul anterior (`inputs.wait_env`). In timpul asteptarii nu este ocupat niciun runner, iar timpul de asteptare nu este facturat. La pornire manuala sau din cron (fara `wait_env`) jobul este sarit.
2. `monitor`: ruleaza `scheduler/cadence.py`, care nu doarme. Planificatorul verifica ora reala in `Europe/Brussels`; numai intre `08:00` si `22:00` ruleaza testele si scannerul `--dry-run`. Apoi alege environment-ul de asteptare pentru succesor. Verifica si ca wait timer-ul chiar a fost aplicat: daca rularea porneste cu peste 30 de secunde inainte de `not_before`, ori environment-ul nu este in lista de mai jos, jobul esueaza si lantul se opreste (protectie contra buclelor rapide daca un environment lipseste sau nu are timer).
3. `next-tick`: singurul job cu `actions: write`. Porneste succesorul prin `workflow_dispatch` cu tokenul efemer al rularii (`github.token`) si reincearca de cel mult 3 ori (dupa 5, 15 si 30 de secunde) la erori de retea, 408, 429 sau 5xx. Ruleaza si daca scanarea a esuat, ca o eroare OKX sa nu rupa lantul. Anularea manuala a unei rulari opreste lantul.

Ziua: fiecare tic alege 9 sau 10 minute, astfel incat scanarea urmatoare sa cada cat mai aproape de `:00:45`, `:10:45`, ..., `:50:45` (ultima in jurul `21:50:45`). Faza de 45 s dupa minutul rotund face ca fiecare lumanare 15m de confirmare sa fie scanata in cel mult ~6,5 minute dupa inchidere (la `:00`/`:30` in ~45 s, la `:15`/`:45` in ~5 min 45 s), deci in fereastra de valabilitate de 420 s. Overhead-ul folosit de planificator (25 s) este cel masurat live. Seara: dupa ultima scanare, succesorul asteapta intr-un environment overnight si porneste in jurul orei `08:00`, fara rulari in timpul noptii. Durata noptii este aleasa automat din fusul `Europe/Brussels`: 610 minute intr-o noapte normala (CET sau CEST), 550 in noaptea trecerii la ora de vara, 670 in noaptea trecerii la ora de iarna.

Environments de creat in `Settings` > `Environments` (nume exacte; fara secrete, fara reviewers; `Deployment branches and tags` = `No restriction`):

| Environment | Wait timer (minute) | Folosit pentru |
|---|---|---|
| `scan-wait-9m` | 9 | tic de zi, aliniere la grila de 10 minute |
| `scan-wait-10m` | 10 | tic de zi |
| `scan-overnight-550m` | 550 | noaptea trecerii la ora de vara |
| `scan-overnight-610m` | 610 | noapte normala |
| `scan-overnight-670m` | 670 | noaptea trecerii la ora de iarna |

Pornirea si recuperarea: cronul `52 5,6 * * *` (07:52 ora Bruxelles, vara si iarna) si cronul rar `7 6-20 * * *` (o data pe ora) pornesc lantul daca nu exista. `concurrency` pastreaza, pentru fiecare branch, o singura rulare activa si cel mult una in asteptare, deci un cron care porneste in timp ce lantul este viu este anulat de urmatorul tic. Grupul este izolat pe ref (`${{ github.ref }}`), astfel incat un lant de demonstratie pe alt branch nu poate anula sau intarzia o rulare de pe `main`, si invers. Daca pornirea de dimineata ratata nu este acoperita de cron, un `Run workflow` manual pe `main` reporneste lantul.

Pe alte branchuri decat `main`, lantul continua numai pentru un numar limitat de ticuri (inputul `ticks`), folosit pentru demonstratii.

Poarta de fereastra din `scheduler/cadence.py` ramane activa in toate cazurile, deci nicio declansare nu poate produce teste, apeluri OKX sau scanari intre `22:00` si `07:59`.

### Audit al pauzelor reale

`diagnostics/scan_gap_audit.py` citeste istoricul rularilor de pe `main` prin API-ul GitHub si ia ca moment al scanarii finalizarea reusita a pasului `Dry-run scan`. Rularile cu pasul sarit de poarta de timp sau esuat nu sunt numarate. Pentru ziua locala aleasa, auditul masoara pauzele din fereastra `08:00-22:00 Europe/Brussels`, inclusiv de la `08:00` la prima scanare si de la ultima scanare pana la `22:00` (sau pana la momentul auditului). Verdictul este `FAIL` daca o pauza depaseste 15 minute sau daca exista o scanare in afara ferestrei.

Workflow-ul `.github/workflows/scan-gap-audit.yml` ruleaza auditul manual (cu data optionala) si zilnic la 21:17 UTC. Are numai permisiuni de citire si foloseste tokenul efemer `github.token`, nu secrete. Un `FAIL` face rularea rosie. Local: `python diagnostics/scan_gap_audit.py --date 2026-09-28`.
