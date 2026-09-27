# AI Crypto Trader prin GitHub Actions

Workflow: `.github/workflows/ai-crypto-trader.yml`.

## Stare initiala sigura

Workflow-ul accepta numai rulare manuala `workflow_dispatch`, in dry-run. Blocul `schedule` pentru minutele `01,16,31,46` este pregatit, dar comentat. Astfel, publicarea codului nu activeaza monitorizarea.

Scannerul:

- foloseste endpointurile publice Bybit, fara API key;
- foloseste implicit `https://api-demo.bybit.com`; domeniul poate fi schimbat prin `BYBIT_BASE_URL`;
- porneste cu `BTCUSDT`, configurabil in `ai_crypto_monitor/config-v0.1.json` la `symbols`;
- foloseste context 4H, structura 1H si ultima lumanare 15m inchisa pentru trigger;
- cere sweep, revenire, confirmare, Entry, SL structural, TP structural si R:R minim;
- respinge date incomplete, vechi sau semnale expirate;
- nu trimite WATCH, startup, raport periodic sau mesaj `NO_ENTRY`;
- pastreaza `Probabilitate: NECALIBRATA` si nu deschide ordine.

## Secrete GitHub

Nu incarca `.env`. In repository-ul GitHub:

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

Rularea manuala nu citeste secretele si nu poate trimite Telegram. Un rezultat `NO_ENTRY` este succes si ramane fara notificare.

## Deduplicare

La inceput se restaureaza cel mai recent cache `ai-crypto-dedup-*`. Fisierul este modificat numai dupa ce Telegram accepta alerta. Cache-ul nou este salvat numai cand pasul scannerului produce `alert_sent=true`. `concurrency` nu permite doua scanari simultane.

Risc rezidual: daca Telegram accepta alerta, dar GitHub nu reuseste sa salveze cache-ul, o rulare viitoare poate repeta acea alerta. Mesajul contine timestamp-ul confirmarii, iar logul GitHub arata esecul cache-ului; acest caz necesita verificare manuala.

## Activare ulterioara

Dupa ce dry-run-ul GitHub este verde si cele doua Secrets exista, decomenteaza blocul `schedule` din workflow. Nu schimba expresia cron: `1,16,31,46 * * * *` este UTC, iar minutele sunt identice in orice fus orar.

GitHub Actions poate porni cu cateva minute intarziere. Scannerul verifica prospetimea si expirarea la executie, deci o rulare prea tarzie nu trimite un entry expirat.
