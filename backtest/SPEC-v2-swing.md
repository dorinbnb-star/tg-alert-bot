# Specificație backtest: swing v2, pullback în trend

## 1. Scop și statut

Experiment separat pentru un pullback în direcția trendului, pe 1D / 4H / 1H.
Nu modelează metoda manuală a lui Doru sau intrarea ETH LONG la 2.257,69.
Metoda manuală se va specifica separat, după jurnal. Documentul nu autorizează
implementarea sau rularea. Implementarea așteaptă mesajul „DA, implementează”.

## 2. Convenții preluate din v3

Referințele de mai jos sunt la [SPEC-v3-canal.md](SPEC-v3-canal.md). Se preiau
convențiile enumerate, fără logica de canal și fără variantele market din v3.
Regulile explicite v2 au prioritate unde diferă.

| Subiect | Secțiuni v3 |
|---|---|
| UTC, lumânări închise, goluri, momentul disponibilității | §2, §24 |
| ATR14 Wilder; calcul EMA, dar cu Daily obligatoriu pentru v2 | §3.1, §3.2 |
| Ordin limit, fill, gap prin SL, prioritatea SL, activarea ulterioară a stopurilor | §9.3, §10.2, §12.3, §14 |
| Ieșire la timeout; termenul v2 este de șapte zile | §13 |
| Risc planificat 2% cu costuri, maximum trei poziții și prioritatea portofoliului | §15 |
| costs.json, funding la decontările reale pe cantitatea rămasă, acoperire incompletă | §16 |
| Baseline aleator și cele 200 de seed-uri | §22, cu adaptarea v2 de la §7 |
| Structura livrabilelor și metricile, cu etapele v2 | §23 |

## 3. Trend, pivoți și semnal

- LONG doar cu ultima lumânare Daily disponibilă: close > EMA50 > EMA200.
  SHORT doar cu close < EMA50 < EMA200. Egalitate, date lipsă sau indicatori
  neîncălziți înseamnă fără setup. Trendul trebuie să fie valid la sweep și la
  fiecare închidere 1H până la confirmare inclusiv; pierderea lui anulează
  secvența. Se rulează pe toate lumânările, fără orarul botului live.
- Pivot high 4H: high strict mai mare decât cele două high-uri din stânga și
  cele două din dreapta; pivot low invers. Egalitățile nu sunt pivoți. Pivotul
  de la i devine cunoscut la închiderea lui i+2. Pentru un sweep, pivotul
  trebuie să fie deja cunoscut la deschiderea lumânării 1H respective.
- SWEEP LONG: low 1H < nivelul unui pivot low 4H confirmat. Dacă aceeași
  lumânare traversează mai mulți pivoți eligibili, se alege cel mai recent
  după ora lumânării pivot. SHORT: high 1H > pivot high, cu aceeași prioritate.
- RECLAIM LONG: prima închidere 1H strict peste nivel după sweep, inclusiv
  în lumânarea sweep-ului. SHORT: prima închidere strict sub nivel.
- CONFIRMARE LONG: prima lumânare 1H ulterioară reclaim-ului care închide
  strict peste high-ul lumânării de reclaim. SHORT: close strict sub low-ul
  reclaim-ului. O închidere la sau dincolo de pivot pe partea sweep-ului,
  înainte de confirmare, anulează secvența. O secvență activă nu își mută
  pivotul și nu își resetează ceasul când apar noi extreme.
- Pentru calculul SL, extrema episodului de sweep este minimul low-urilor
  de la sweep până la reclaim inclusiv pentru LONG, respectiv maximul
  high-urilor pentru SHORT. O nouă extremă dincolo de aceasta între reclaim
  și confirmare, inclusiv în bara de confirmare, anulează secvența.
- Fereastra exactă de patru lumânări și consumarea pivotului se stabilesc
  conform §9 înainte de implementare.

## 4. Intrare, invalidare și ieșiri

- La confirmare se fixează planul. Entry limit la nivelul pivotului recuperat,
  rotunjit la tick în jos pentru buy și în sus pentru sell. Ordinul este activ
  în următoarele 12 lumânări 1H complete, de la s+1 până la s+12 inclusiv,
  unde s este bara de confirmare. Expiră la închiderea lui s+12 dacă nu s-a
  executat. Fără fill pe bara de confirmare. Înainte de fill, un open la sau
  dincolo de SL ori ținta finală anulează ordinul. Ambiguitățile intrabar
  urmează regulile de execuție limit din §2.
- La confirmare, buffer = max(0,10 × ATR14 1H, 2 × tickSz). SL LONG =
  extrema sweep-ului minus buffer, rotunjit în jos la tick; SHORT =
  extrema plus buffer, rotunjit în sus. Fără ATR sau tickSz utilizabil nu
  se creează ordin. Parametrii nevalidați, inclusiv bufferul, sunt IPOTEZĂ.
- TP se alege dintre pivoții 4H cunoscuți la confirmare, strict pe partea
  profitabilă față de entry: pivot high pentru LONG, pivot low pentru SHORT.
  Sortează nivelurile distincte după distanța de entry; alege primul,
  respectiv al doilea în varianta separată. Ținta lipsește sau devine
  nevalidă după rotunjire conservatoare spre entry: respinge setup-ul.
  Nu înlocui ținta cu un multiplu de R și nu o schimba după confirmare.
- R:R brut planificat = distanța entry–TP / distanța entry–SL. În varianta
  fără filtru se verifică în continuare geometria entry/SL/TP. Pragurile
  1,8 / 2,0 / 2,5 / 3,0 sunt IPOTEZE și se aplică inclusiv la egalitate.
- Ieșirea de referință: 100% la TP, SL sau timeout. Varianta separată:
  50% la 2R față de fill, folosind distanța inițială fill–SL; restul la
  TP-ul structural, stop sau timeout. Testează varianta doar dacă TP-ul
  structural este strict dincolo de 2R după rotunjire. Altfel respinge;
  nu repoziționa țintele. După atingerea și executarea primei tranșe,
  stopul restului devine prețul efectiv de intrare, rotunjit la tick spre
  protecție, de la următoarea bară 1H. Acesta este breakeven de preț,
  înainte de costuri. Dacă SL și TP pot fi atinse în aceeași bară, aplică
  prioritatea SL. Dacă ambele ținte sunt atinse fără conflict cu SL,
  execută tranșele în ordinea lor.
- Timeout: ieșire la market la primul open 1H disponibil la sau după
  168 de ore de la fill; aplică costurile și regulile de gap din §2.

## 5. Univers și date

Cele 18 contracte OKX USDT-SWAP: BTC, ETH, SOL, XRP, BNB, AVAX, LINK, LTC,
SUI, UNI, AAVE, TIA, WLD, WIF, STRK, VIRTUAL, ENA, YGG. Perioada începe
în martie 2022, pe aceleași serii 1D/4H/1H și funding folosite în v3.
Se înregistrează și se verifică SHA-256 pentru manifest și fișierele
folosite; orice diferență se raportează înainte de comparație. Pentru
listări ulterioare sau goluri nu se inventează date. Simbolul intră în
test numai după ce are ferestrele continue necesare pentru EMA200 Daily,
ATR14 1H și pivoții 4H. Se publică acoperirea și data eligibilității pe
simbol. BTC și ETH se raportează împreună și separat, ca subgrup
declarat dinainte; verdictul principal este pentru universul complet.

## 6. Selecție și validare

Împărțire cronologică 60/20/20 pe axa UTC comună a perioadei eligibile,
cu limite fixate înainte de test. Ultimele 40% reprezintă două evaluări
distincte: walk-forward și final. Indicatorii pot folosi istoricul anterior
limitei, dar ordinele și pozițiile nu trec între segmente. La limită,
ordinele se anulează; pozițiile fără ieșire completă se raportează ca
cenzurate și se exclud din metricile segmentului. Nu se citesc rezultate
de după limită pentru selecție.

Lista variantelor și alegerile aprobate la §9 se fixează înainte de rulare.
Pe dezvoltare se compară TP1 structural versus al doilea pivot, ieșirea
integrală versus tranșe și pragurile R:R. Se alege după cel mai mare R
mediu net; la egalitate, preferă drawdown-ul mai mic, apoi mai multe
tranzacții eligibile, apoi identificatorul variantei în ordine alfabetică.
Se raportează și mărimea eșantionului fiecărei variante. Varianta aleasă
se îngheață și rulează o singură dată pe walk-forward și o singură dată
pe final, fără recalibrare. Perioada finală a fost deja văzută la v3,
dar nu pentru regulile v2.

Numai după PASS: opțional, o singură evaluare pe cele 40 de monede din
v3_universe.json, cu varianta înghețată, aceeași fereastră finală și
aceleași criterii. Este un test pe alte simboluri, deja folosite la v3,
nu pe date complet nevăzute.

## 7. Baseline aleator și raport

Adaptarea §22 v3: păstrează universul, trendul, pivotul și episodul de
sweep/reclaim. La reclaim, alege uniform o bară dintre sloturile 1H
ulterioare prevăzute de fereastra de confirmare. Dacă nu există sloturi
sau episodul se invalidează înainte de ori la închiderea aleasă, nu
creează ordin și nu reia extragerea. Nu exclude retrospectiv sloturi
pe baza lumânărilor viitoare. La ora aleasă se folosesc doar niveluri și ATR
disponibile atunci; se aplică același tip de ordin, costuri, funding,
ieșiri, filtru R:R și reguli de portofoliu. Rulează seed-urile fixe
0–199 pe exact același segment final. Raportează distribuția R-ului mediu
net și câte seed-uri au cel puțin o tranzacție cu funding complet.
Percentila 90 se calculează prin rangul ceil(0,90 × N) din valorile
valide ordonate crescător; fără valori valide criteriul nu este îndeplinit.

Raportul separă sweep, reclaim, confirmare, ordine create, fill, expirare
și fiecare motiv de respingere. Livrabilele viitoare: CSV de setup-uri,
CSV de tranzacții, JSON de metrici și raport Markdown, toate distincte
de v3. Se raportează metricile §23.2 v3, inclusiv costuri ×2, funding
incomplet, rezultate pe split, simbol, direcție și variantă.

## 8. Verdict automat pe perioada finală

N = tranzacții închise, cu funding complet; se raportează separat și
totalul execuțiilor. Dacă N < 30, verdictul este NECONCLUDENT, indiferent
de rest. Dacă N ≥ 30, PASS cere simultan:

1. R mediu net ≥ +0,15R;
2. profit factor net ≥ 1,2;
3. R mediu net cu costuri ×2 > 0, pe aceleași execuții și același risc inițial;
4. R mediu net strict peste percentila 90 a baseline-ului aleator.

La N ≥ 30, orice criteriu neîndeplinit sau indisponibil înseamnă FAIL.
Profit factor = suma R net pozitive împărțită la valoarea absolută a
sumei R net negative; fără pierderi și cu câștiguri este infinit, iar
fără câștiguri și fără pierderi este nedefinit.

## 9. Decizii deschise

1. **SL minim.** Da, bufferul poate produce un SL sub 1,5 × ATR14 1H:
   dacă sweep-ul e foarte puțin adânc și 2 tick-uri sunt sub 1,5 ATR,
   distanța structurală plus buffer poate rămâne sub prag. IPOTEZĂ:
   distanța de la entry planificat la SL să fie maximul dintre distanța
   structurală și 1,5 × ATR14 1H la confirmare. Recalculează R:R și
   cantitatea după rotunjire. Compară cu formula structurală pe dezvoltare.
2. **Adâncime maximă.** IPOTEZĂ: maximum 1,0 × ATR14 1H, înghețat la
   deschiderea lumânării sweep-ului, aplicat excursiei maxime din episod
   până la reclaim inclusiv. Peste prag anulează episodul; nu declara
   automat că este o spargere. Compară cu varianta fără plafon.
3. **Consumarea pivotului.** Propunere: la prima confirmare validă de
   structură, înainte de filtrele SL/TP/R:R, per simbol, direcție și ora
   lumânării pivot 4H. Un ordin necreat, neexecutat, anulat sau expirat
   nu reactivează pivotul. Un episod fără confirmare nu îl consumă.
4. **Fereastra de patru lumânări.** Propunere, IPOTEZĂ: include atât
   reclaim-ul, cât și confirmarea, în patru bare consecutive s…s+3,
   unde s este sweep-ul. Reclaim-ul poate fi în s, confirmarea trebuie
   să fie strict ulterioară. Alternativa de aprobat înainte de test:
   reclaim până la s+3 inclusiv, apoi confirmare în maximum patru bare
   începând cu prima bară după reclaim.
