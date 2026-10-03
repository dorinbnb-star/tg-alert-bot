# AI Crypto Trader prin GitHub Actions

Workflow: `.github/workflows/ai-crypto-trader.yml`.

## Stare curenta: alerte Telegram doar pentru ENTRY confirmat

Workflow-ul este declansat la fiecare 10 minute (vezi `Programare`) si ruleaza scannerul numai intre `08:00` inclusiv si `22:00` exclusiv in fusul `Europe/Brussels`. In intervalul `22:00-07:59` nu se ruleaza testele si nu se apeleaza OKX; se face doar checkout pentru planificatorul `scheduler/cadence.py`.

Doua cai, care se exclud reciproc:

- **Live** (pasul `Live scan`): numai pe `main` si numai pentru ticurile pornite de `schedule` sau de lantul insusi (`github-actions[bot]`). Este singurul pas care citeste secretele `TELEGRAM_TOKEN` si `TELEGRAM_CHAT_ID` si singurul care ruleaza `--send`. Trimite pe Telegram numai cand exista `NOW=ENTER` cu setup complet confirmat; pentru `NO_ENTRY`, setup neconfirmat, semnal expirat sau duplicat nu trimite nimic. Nu exista mesaje WATCH, startup, heartbeat, periodice sau de eroare.
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
NOW=ENTER LONG BTC-USDT-SWAP
BIAS: LONG (trend 4H si 1H peste EMA50)
SETUP: sweep low 1H <nivel pivot> + reintrare + confirmare 15m
TRIGGER: lumanarea 15m HH:MM-HH:MM (Bruxelles) a inchis la <pret>, peste maximul lumanarii de reintrare <nivel>
Entry: <pret> (pret verificat <pret live>)
SL / invalidare: <nivel> (sub extremul sweep <nivel>)
TP: <primul pivot 1H opus>
R:R: <valoare>
Pro: <EMA 4H/1H, adancime sweep, R:R>
Contra: <limitari: fara OI/CVD/heatmap, TP partial, R:R aproape de minim, drift>
Valabil pana la HH:MM (Bruxelles). Probabilitate: necalibrata.
Date: OKX, lumanari inchise 4H/1H/15m. Doar alerta, fara ordine automate.
```

Nu exista calibrare a probabilitatii pe date istorice; alerta spune explicit `Probabilitate: necalibrata` si nu afiseaza scoruri ca probabilitati.

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
