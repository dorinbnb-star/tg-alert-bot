# Specificație backtest v3 canal

## 1. Statut și scop

Acest document definește experimentul separat **v3 canal**. Nu conține cod și nu
autorizează implementarea. Implementarea începe numai după mesajul explicit
`DA, implementează`.

Strategia testată este una singură:

```text
canal orizontal 4H -> spargere 4H -> retest -> confirmare 1H -> ordin limit
```

LONG și SHORT sunt perfect simetrice. Prima versiune nu tranzacționează în
interiorul canalului și nu include pullback-uri într-un trend. Strategia swing v2
rămâne un experiment separat.

Obiectivul este swing trading, cu o durată orientativă de 3-10 zile. Această
durată nu este o promisiune a timeframe-urilor. Poziția are o limită operațională
de 10 zile.

Backtestul v3 rulează 24/7 după închiderea fiecărei lumânări 1H. Nu importă
fereastra 08:00-22:00 a botului live.

### În afara scopului

- nicio modificare a botului live, a regulilor/configurației live sau Telegram;
- nicio logică de pullback în trend;
- nicio tranzacție în interiorul canalului;
- niciun filtru Daily, funding, BTC sau macro în experimentul de bază;
- nicio ajustare individuală a parametrilor pe monedă;
- exemplele ETH de la final nu sunt teste de acceptare fără datele originale.

## 2. Convenții temporale și date cunoscute

Se folosesc numai lumânări OKX complet închise:

- 4H pentru canal, spargere și management;
- 1H pentru retest, confirmare, ordin, fill și execuția poziției;
- 1D exclusiv pentru contextul înregistrat în raport.

Toate timestampurile sunt UTC în calcule. Fusul orar nu schimbă granițele
lumânărilor furnizate de OKX.

La timestampul de închidere al unei lumânări sunt cunoscute doar acea lumânare și
lumânările anterioare. Un calcul făcut la închidere devine utilizabil începând cu
următoarea lumânare, dacă o regulă nu precizează explicit alt moment.

Lipsurile din seria OHLC nu se completează și nu se interpolează. Un setup nu
poate traversa un gol. După un gol sunt necesare din nou toate ferestrele continue
cerute de indicatori și de canal.

## 3. Indicatori

### 3.1 True Range și ATR

Pentru o lumânare `t`:

```text
TR[t] = max(
    high[t] - low[t],
    abs(high[t] - close[t-1]),
    abs(low[t] - close[t-1])
)
```

`ATR14` folosește netezirea Wilder: primul ATR este media aritmetică a primelor
14 valori TR, apoi:

```text
ATR[t] = (13 * ATR[t-1] + TR[t]) / 14
```

`ATR(ref)` este `ATR14(4H)` calculat la închiderea ultimei lumânări 4H dinaintea
lumânării de spargere. Se îngheață împreună cu canalul și nu se recalculează
pentru acel setup.

`ATR1H(confirmare)` este `ATR14(1H)` cunoscut la închiderea lumânării de
confirmare. Se îngheață pentru calculul ordinului și al SL-ului inițial.

### 3.2 EMA Daily pentru context

EMA50 și EMA200 Daily se calculează numai din lumânări 1D complet închise.
Aceste valori nu acceptă și nu resping setup-uri în v3 canal.

## 4. Pivoți 4H

**IPOTEZĂ:** `k = 2` lumânări în stânga și două în dreapta. Alegerea oferă o
confirmare cu întârziere de 8 ore, compatibilă cu un experiment swing, fără a
face pivotul excesiv de rar.

O lumânare `i` este pivot high dacă:

```text
high[i] > high[i-2], high[i-1], high[i+1], high[i+2]
```

Este pivot low dacă:

```text
low[i] < low[i-2], low[i-1], low[i+1], low[i+2]
```

Comparațiile sunt stricte. Egalitatea invalidează pivotul. Pivotul devine
disponibil numai la închiderea lumânării `i+2`. Nicio decizie nu poate folosi
pivotul înaintea acelui moment.

## 5. Construirea canalului

### 5.1 Fereastra

**IPOTEZĂ:** `N = 60` lumânări 4H continue, adică 10 zile. Fereastra este destul
de lungă pentru mai multe atingeri ale marginilor, dar rămâne apropiată de
orizontul swing urmărit.

Pentru fiecare lumânare 4H candidată la spargere `B[t]`, canalul se calculează
exclusiv din lumânările `B[t-N] ... B[t-1]`. Lumânarea `B[t]` nu participă la
canal, ATR(ref), regresie, pivoți sau margini.

Sunt eligibili numai pivoții al căror candle index este în fereastra de N
lumânări și care au devenit disponibili cel târziu la închiderea `B[t-1]`.

### 5.2 Gruparea pivoților

Pivot highs și pivot lows se grupează separat. Pentru fiecare tip:

1. nivelurile se sortează crescător;
2. se enumeră toate secvențele contigue din lista sortată care conțin minimum
   două niveluri și au `max(level) - min(level) <= 0,5 * ATR(ref)`;
3. se alege secvența cu cele mai multe niveluri;
4. la egalitate se alege secvența cu intervalul `max-min` cel mai mic;
5. la o nouă egalitate se alege secvența cu cel mai recent timestamp maxim de
   confirmare al pivoților;
6. dacă egalitatea persistă, pentru pivot highs se alege mediana mai mare, iar
   pentru pivot lows mediana mai mică.

**IPOTEZĂ:** toleranța maximă a grupului este `0,5 * ATR(ref)`.

Mediana unui număr par de valori este media aritmetică a celor două valori
centrale.

```text
U = mediana grupului ales de pivot highs
L = mediana grupului ales de pivot lows
W = U - L
```

Canalul este invalid dacă lipsește unul dintre grupuri sau dacă `U <= L`.

### 5.3 Condițiile canalului

Toate condițiile următoare trebuie îndeplinite:

1. minimum două pivot highs în grupul superior și două pivot lows în grupul
   inferior;
2. **IPOTEZĂ:** `4 * ATR(ref) <= W <= 12 * ATR(ref)`;
3. se calculează regresia OLS a close-urilor din fereastră pe indecșii
   `x = 0 ... N-1`; dacă panta este `slope`, atunci:

   ```text
   abs(slope * N) < 0,3 * W
   ```

   Pragul `0,3 * W` este **IPOTEZĂ**;
4. cel puțin `ceil(0,80 * N)` close-uri sunt în intervalul inclusiv `[L, U]`;
   pentru `N=60` sunt necesare minimum 48;
5. toate cele N lumânări și toate valorile ATR necesare sunt continue și valide.

Nu se folosește max/min absolut al ferestrei pentru margini.

### 5.4 Identitatea și reutilizarea canalului

Identitatea unui canal este:

```text
(symbol, window_start_ms, window_end_ms, U_raw, L_raw)
```

Valorile raw nu sunt rotunjite. Același canal poate produce un singur setup.
După anularea setup-ului sau închiderea tranzacției, un nou canal pe același
simbol este eligibil numai dacă grupurile sale conțin cel puțin două pivot highs
și două pivot lows ale căror lumânări pivot sunt ulterioare spargerii canalului
precedent. Astfel, ferestrele glisante aproape identice nu generează setup-uri
duplicate.

Pe un simbol poate exista simultan cel mult un setup, un ordin sau o poziție v3.

## 6. Spargerea și înghețarea setup-ului

La închiderea `B[t]`:

```text
LONG breakout:  close[t] >= U + 0,5 * ATR(ref)
SHORT breakout: close[t] <= L - 0,5 * ATR(ref)
```

Pragul `0,5 * ATR(ref)` este **IPOTEZĂ**. Egalitatea trece condiția.

La spargere se îngheață:

- direcția;
- `U`, `L`, `W` și identitatea canalului;
- `ATR(ref)`;
- timestampul spargerii;
- TP-ul de referință;
- contextul Daily disponibil la acel moment;
- volumul și SMA20 de volum 4H, numai pentru varianta de volum.

Nu se cere ca open-ul lumânării de spargere să fie în canal.

## 7. Retest și confirmare

### 7.1 Zona de retest

Nivelul de referință este `U` pentru LONG și `L` pentru SHORT.

**IPOTEZĂ:** zona de retest este:

```text
[reference_level - 0,25 * ATR(ref),
 reference_level + 0,25 * ATR(ref)]
```

O lumânare 1H atinge zona dacă intervalele `[low, high]` și zona de retest se
intersectează, inclusiv la egalitate.

Retestul poate începe numai cu prima lumânare 1H care se deschide după închiderea
lumânării 4H de spargere. Nicio mișcare din lumânarea de spargere nu este retest.

### 7.2 Fereastra maximă

**IPOTEZĂ:** retestul și confirmarea trebuie să apară înainte de închiderea celei
de-a 12-a lumânări 4H ulterioare spargerii, adică în maximum 48 de ore.

Dacă zona nu este atinsă până la termen, setup-ul se închide cu motivul
`RETEST_TIMEOUT`. Dacă zona a fost atinsă, dar confirmarea nu apare până la
termen, motivul este `CONFIRMATION_TIMEOUT`.

### 7.3 Confirmarea 1H

Confirmarea poate fi aceeași lumânare 1H care atinge zona sau o lumânare 1H
ulterioară, dar trebuie să fie în fereastra de la secțiunea 7.2.

Pentru LONG, lumânarea de confirmare trebuie să îndeplinească simultan:

```text
close > U
close > high-ul lumânării 1H precedente
```

Pentru SHORT:

```text
close < L
close < low-ul lumânării 1H precedente
```

Comparațiile sunt stricte. Lumânarea precedentă trebuie să fie complet închisă și
continuă față de lumânarea de confirmare.

## 8. Anularea setup-ului înainte de fill

Regulile se aplică atât în așteptarea retestului/confirmării, cât și după
activarea ordinului limit. După fill, ele nu mai anulează tranzacția.

### 8.1 Revenire adâncă în canal

La fiecare închidere 4H:

```text
LONG:  close <= U - 0,5 * ATR(ref)
SHORT: close >= L + 0,5 * ATR(ref)
```

Pragul este **IPOTEZĂ**. Motiv: `CLOSE_BACK_IN_CHANNEL`.

Dacă aceeași închidere 1H este și închidere 4H și ar confirma setup-ul, anularea
4H are prioritate; ordinul nu se creează.

### 8.2 Ținta atinsă înainte de intrare

Pentru ieșirea de referință, ținta de anulare este TP-ul unic. Pentru varianta cu
tranșe este TP1.

Setup-ul se anulează dacă high/low 1H atinge ținta înainte de fill. Dacă aceeași
lumânare 1H atinge atât ținta de anulare, cât și prețul ordinului, se presupune
conservator că ținta a fost atinsă prima; setup-ul este anulat și nu există fill.

Motiv: `TARGET_BEFORE_ENTRY`.

## 9. Ordinul limit

### 9.1 Prețul planificat

**IPOTEZĂ:** bufferul este `0,10 * ATR(ref)` în direcția spargerii:

```text
LONG:  entry_plan = U + 0,10 * ATR(ref)
SHORT: entry_plan = L - 0,10 * ATR(ref)
```

Ordinul se creează la închiderea confirmării, dar devine activ numai la open-ul
următoarei lumânări 1H. Lumânarea de confirmare nu poate umple ordinul.

### 9.2 Expirare

**IPOTEZĂ:** ordinul este activ exact 12 lumânări 1H, de la prima lumânare de
după confirmare până la a 12-a inclusiv. Dacă nu este umplut până la închiderea
celei de-a 12-a, se anulează cu motivul `ORDER_EXPIRED`.

### 9.3 Fill

Pentru LONG:

- dacă `open <= entry_plan`, fill-ul raw este `open`;
- altfel, dacă `low <= entry_plan`, fill-ul raw este `entry_plan`;
- altfel nu există fill.

Pentru SHORT:

- dacă `open >= entry_plan`, fill-ul raw este `open`;
- altfel, dacă `high >= entry_plan`, fill-ul raw este `entry_plan`;
- altfel nu există fill.

Slippage-ul din `backtest/costs.json` se aplică advers fill-ului raw pentru a
obține prețul efectiv de intrare. Fill-ul raw și prețul efectiv se păstrează
separat în raport.

## 10. Stop loss și riscul inițial

### 10.1 Calcul

Pentru LONG:

```text
structural_sl = U - 1,0 * ATR(ref)
minimum_sl    = entry_plan - 1,5 * ATR1H(confirmare)
SL            = min(structural_sl, minimum_sl)
distance      = entry_plan - SL
```

Pentru SHORT:

```text
structural_sl = L + 1,0 * ATR(ref)
minimum_sl    = entry_plan + 1,5 * ATR1H(confirmare)
SL            = max(structural_sl, minimum_sl)
distance      = SL - entry_plan
```

Multiplicatorii `1,0`, `1,5` și limita următoare sunt **IPOTEZE**.

Setup-ul se respinge la confirmare cu `SL_TOO_WIDE` dacă:

```text
distance > 3 * ATR(ref)
```

### 10.2 Execuția SL

Pentru LONG, dacă `low <= SL`, prețul raw de ieșire este:

```text
min(SL, open)
```

Pentru SHORT, dacă `high >= SL`, prețul raw este:

```text
max(SL, open)
```

Se aplică apoi slippage-ul advers pentru stop din `costs.json`, inclusiv
`stop_slippage_multiplier`.

Dacă entry și SL sunt atinse în aceeași lumânare 1H, se consideră fill urmat de
SL. Dacă SL și orice TP sunt atinse în aceeași lumânare, SL are prioritate pentru
toată cantitatea încă deschisă.

## 11. Ieșirea de referință: TP unic

```text
LONG:  TP = U + W
SHORT: TP = L - W
```

Poziția se închide integral la TP. Dacă open-ul unei lumânări depășește favorabil
TP-ul, execuția rămâne conservator la TP, nu la open-ul mai bun.

R:R-ul brut folosit de filtru este:

```text
gross_rr = abs(TP - entry_plan) / abs(entry_plan - SL)
```

Costurile nu modifică filtrul geometric R:R, dar intră în dimensionare și în
rezultatul net.

Se compară două variante identice în rest:

- `REF_RR2`: `gross_rr >= 2`;
- `REF_RR3`: `gross_rr >= 3`.

## 12. Varianta ulterioară: tranșe structurale

Această variantă se rulează după referință și nu îi modifică rezultatele.

### 12.1 Nivelurile structurale

**IPOTEZĂ:** se caută pivoți 4H confirmați în ultimele 1.080 de lumânări 4H
anterioare confirmării, echivalentul a aproximativ 180 de zile.

Pentru LONG:

1. se iau pivot highs confirmați și disponibili la confirmare cu nivel
   `> entry_plan`;
2. nivelurile se sortează crescător;
3. nivelurile care diferă cu cel mult un `tickSz` oficial al instrumentului sunt
   același nivel; se păstrează nivelul pivotului confirmat cel mai recent;
4. `TP1` este primul nivel, `TP2` al doilea.

Pentru SHORT se folosesc pivot lows `< entry_plan`, sortate descrescător.

Sunt obligatorii exact relațiile:

```text
LONG:  entry_plan < TP1 < TP2
SHORT: entry_plan > TP1 > TP2
```

Dacă lipsesc două niveluri distincte, setup-ul se respinge cu
`MISSING_STRUCTURAL_TARGET`. Nu se inventează și nu se substituie un target.

**IPOTEZĂ:** TP1 este prea aproape dacă distanța sa față de entry este sub `1R`,
unde `1R = abs(entry_plan - SL)`. În acest caz setup-ul se respinge cu
`TP1_TOO_CLOSE`; nu se sare la următorul pivot.

Filtrul R:R se aplică la TP2:

- `SPLIT_RR2`: `abs(TP2-entry_plan) / 1R >= 2`;
- `SPLIT_RR3`: `abs(TP2-entry_plan) / 1R >= 3`.

### 12.2 Cantități și ordine de execuție

- 40% din cantitatea inițială iese la TP1;
- 30% iese la TP2;
- 30% rămâne pentru trailing.

Dacă TP1 și TP2 sunt atinse în aceeași lumânare fără SL, ambele tranșe ies la
prețurile lor. Dacă SL este atins în aceeași lumânare, SL are prioritate pentru
toată cantitatea care era deschisă la începutul lumânării.

### 12.3 Breakeven

Se compară trei variante prestabilite:

- `BE_AFTER_TP1`;
- `BE_AFTER_TP2`;
- `NO_BE`.

Prețul BE este primul tick care produce PnL net cel puțin zero pentru o unitate,
după taxa/slippage-ul de intrare deja suportat și taxa/slippage-ul estimate pentru
ieșirea prin stop. Pentru LONG se rotunjește în sus la tick; pentru SHORT în jos.

Stopul BE se calculează la închiderea lumânării care atinge pragul și devine activ
numai de la următoarea lumânare 1H.

### 12.4 Chandelier

Trailing-ul se aplică numai ultimilor 30% și devine activ după executarea TP2.

La fiecare închidere 4H:

```text
LONG candidate  = highest_high_since_entry - 3 * ATR14(4H curent)
SHORT candidate = lowest_low_since_entry  + 3 * ATR14(4H curent)
```

Multiplicatorul 3 este **IPOTEZĂ**. Noul stop devine activ de la următoarea
lumânare 1H. Stopul nu se poate îndepărta de preț:

```text
LONG:  stop_nou = max(stop_vechi, candidate, BE_dacă_este_activ)
SHORT: stop_nou = min(stop_vechi, candidate, BE_dacă_este_activ)
```

Raportul separă PnL-ul net al celor trei tranșe și rezultatul runnerului.

## 13. Timeout-ul poziției

Poziția expiră la `fill_timestamp + 240 ore`. Cantitatea rămasă se închide la
open-ul primei lumânări 1H al cărei start este mai mare sau egal cu acel moment,
cu taxa și slippage-ul de market din `costs.json`.

Timeout-ul este procesat la open, înainte de high/low-ul acelei lumânări.

## 14. Ordinea evenimentelor

Pentru fiecare timestamp 1H, ordinea este fixă:

1. se activează ordinele și stopurile calculate la close-ul precedent;
2. se procesează timeout-ul care cade exact la open;
3. pentru un setup neumplut, dacă ținta de anulare și entry sunt ambele în range,
   setup-ul se anulează ca `TARGET_BEFORE_ENTRY`;
4. se procesează fill-ul ordinului;
5. după fill, pentru aceeași lumânare se procesează întâi SL, apoi TP1/TP2/TP;
6. la close se procesează, în ordine: invalidarea 4H dacă este graniță 4H,
   expirarea setup-ului/ordinului, retestul, confirmarea și calculele de stop;
7. valorile calculate la close devin active în lumânarea următoare.

Dacă un ordin se umple în timpul unei lumânări care închide ulterior 4H înapoi în
canal, fill-ul este anterior close-ului și tranzacția rămâne deschisă. Regula de
anulare 4H se aplică numai dacă ordinul era încă neumplut.

## 15. Dimensionare, capital și limita de poziții

Capitalul folosit este equity realizat la momentul activării ordinului; PnL-ul
nerealizat nu intră în equity pentru o nouă dimensionare.

```text
risk_budget = 0,02 * equity
```

Costul estimat pe unitate include:

- taxa și slippage-ul de intrare la `entry_plan`;
- taxa și slippage-ul de stop la SL, cu multiplicatorul de stop din
  `backtest/costs.json`.

```text
risk_unit = abs(entry_plan - SL) + estimated_cost_per_unit
qty       = risk_budget / risk_unit
R0        = qty * risk_unit
net_R     = net_PnL / R0
```

`2%` este limita maximă planificată per trade, cu costurile estimate incluse.

Sunt permise maximum trei poziții deschise simultan în portofoliu. Ordinele
neumplute nu ocupă un slot. La fill se verifică din nou limita.

Dacă mai multe confirmări sau fill-uri au același timestamp, prioritatea este:

1. timestampul confirmării mai vechi;
2. la egalitate, ordinea fixă din `symbols` în configurația watchlistului.

După ocuparea celor trei sloturi, celelalte semnale/fill-uri eligibile se
înregistrează `REJECTED_POSITION_LIMIT`. Ordinul asociat este anulat. Pe același
simbol nu poate exista mai mult de o poziție v3.

## 16. Costuri și funding

Taxele, slippage-ul per simbol, multiplicatorul pentru stop și sensibilitatea x2
se citesc din `backtest/costs.json`. Fișierul nu este redefinit de această
specificație.

Toate intrările și ieșirile plătesc costul configurat. Raportul păstrează separat
fee, slippage și funding.

Funding-ul se aplică la fiecare timestamp real de decontare din datele
contractului și numai cantității rămase deschise la acel timestamp. Semnul ratei
se aplică potrivit direcției poziției. Intervalul dintre decontări se citește din
timestampurile contractului; nu se presupune un interval de 8 ore.

Dacă lipsesc date reale de funding pentru o perioadă cu poziție deschisă, trade-ul
este marcat `FUNDING_INCOMPLETE`. El apare în raportul monedei, dar este exclus din
metrica principală agregată. O estimare din `costs.json` poate fi raportată numai
secundar și explicit ca `ESTIMARE`; nu înlocuiește rezultatul principal.

Se rulează și sensibilitatea cu toate costurile configurate înmulțite cu 2.

## 17. Filtrul R:R și setup-urile eliminate

Pentru fiecare familie de ieșire se rulează separat pragul 2R și pragul 3R.
Toate celelalte reguli și aceeași ordine a evenimentelor rămân identice.

Setup-urile acceptate de varianta 2R și respinse exclusiv de varianta 3R sunt
etichetate `ELIMINATED_BY_RR3`. Pentru acestea se raportează rezultatul real din
rularea 2R: fill sau nefill, net R, MAE, MFE și durata. Ele nu sunt introduse
artificial în portofoliul 3R.

## 18. Filtre testate separat după bază

Experimentul de bază nu include filtrele de mai jos. Fiecare se adaugă singur
peste varianta de bază selectată; nu se combină între ele în prima rundă.

### 18.1 Volum

**IPOTEZĂ:** volumul 4H al lumânării de spargere trebuie să fie:

```text
volume_breakout >= 1,5 * SMA20(volume 4H)
```

SMA20 folosește cele 20 de lumânări 4H complet închise imediat anterioare
spargerii; lumânarea de spargere nu intră în SMA.

### 18.2 CHoCH

Structura anterioară este bullish dacă ultimele două pivot highs și ultimele două
pivot lows confirmate sunt ambele crescătoare. Este bearish dacă ambele sunt
descrescătoare.

CHoCH bearish apare când structura era bullish, iar un close 4H închide strict
sub cel mai recent pivot low confirmat. CHoCH bullish este definiția inversă.

Se compară două variante:

1. `CHOCH_PAUSE_12`: **IPOTEZĂ**, după CHoCH nu se acceptă nicio spargere v3 pe
   simbol până la închiderea a 12 lumânări 4H complete;
2. `CHOCH_NEW_STRUCTURE`: blocarea continuă până la confirmarea structurii noi.
   După CHoCH bullish sunt necesare, în ordine, un pivot low confirmat și apoi un
   close 4H strict peste primul pivot high confirmat format după CHoCH. Pentru
   bearish sunt necesare un pivot high confirmat și apoi un close strict sub
   primul pivot low confirmat format după CHoCH.

Baseline-ul fără filtru CHoCH rămâne raportat. Filtrul nu este prezentat drept
soluție dovedită.

Nu se testează în prima rundă funding, corelarea BTC, calendar macro sau filtru
Daily.

## 19. Contextul Daily în raport

Pentru fiecare trade, la momentul fill-ului, se înregistrează din ultima
lumânare 1D complet închisă:

- `daily_close`, `daily_ema50`, `daily_ema200`;
- `close_vs_ema50`: `ABOVE`, `EQUAL` sau `BELOW`;
- `close_vs_ema200`: `ABOVE`, `EQUAL` sau `BELOW`;
- `ema50_vs_ema200`: `ABOVE`, `EQUAL` sau `BELOW`;
- distanțele procentuale close-EMA50 și close-EMA200.

Aceste câmpuri sunt descriptive și nu influențează tranzacția.

## 20. Date, acoperire și încălzire

Se descarcă istoricul maxim disponibil OKX pentru cele 18 contracte din
watchlist, pentru 1D, 4H și 1H, plus funding-ul real. Perioada efectivă se
raportează separat pe simbol.

**IPOTEZĂ:** încălzirea minimă este de 250 lumânări Daily continue, astfel încât
EMA200 Daily să fie disponibilă stabil în raport. În plus trebuie să existe N
lumânări 4H continue, ATR14 4H, ATR14 1H și pivoții confirmați necesari.

Nu se deschid tranzacții în perioada de încălzire. Un simbol cu istoric
insuficient nu este extrapolat și nu intră în agregatul perioadei în care nu era
tranzacționabil.

## 21. Împărțire cronologică și selecție

**IPOTEZĂ:** după încălzire, axa calendaristică globală se împarte:

- 60% dezvoltare;
- 20% walk-forward;
- 20% perioadă finală neatinsă.

Granițele sunt aceleași date UTC pentru toate simbolurile. Un simbol intră în
calcul numai după data sa reală de disponibilitate și încălzire.

Ordinea selecției este:

1. pe dezvoltare se compară `REF_RR2` și `REF_RR3`;
2. separat se compară cele șase variante `SPLIT_RR2/RR3 x BE_AFTER_TP1 /
   BE_AFTER_TP2 / NO_BE`;
3. se alege familia de ieșire și varianta cu cel mai mare R mediu net;
4. dacă diferența dintre primele două este `<= 0,05R` (**IPOTEZĂ**), se alege
   varianta cu drawdown maxim mai mic; dacă și acesta este egal, se alege varianta
   mai simplă în ordinea: TP unic, fără BE, prag 2R;
5. filtrul de volum și cele două variante CHoCH se testează apoi separat peste
   varianta aleasă; aceeași regulă de selecție se aplică;
6. varianta finală se blochează înainte de walk-forward;
7. se rulează fără modificări pe walk-forward și apoi o singură dată pe perioada
   finală;
8. rezultatele slabe pe walk-forward sau final se raportează; nu autorizează
   ajustarea parametrilor sau repetarea perioadei finale.

Aceiași parametri se aplică tuturor monedelor.

### 21.1 Sensibilitate și platou

Valorile centrale nu se înlocuiesc automat cu vecinii. Pe dezvoltare se rulează
teste one-at-a-time cu restul parametrilor fixați:

- `N`: 48 / 60 / 72;
- breakout ATR: 0,4 / 0,5 / 0,6;
- toleranță retest ATR: 0,20 / 0,25 / 0,30;
- buffer entry ATR: 0 / 0,10 / 0,20;
- expirare ordin 1H: 8 / 12 / 16.

Toate aceste praguri sunt **IPOTEZE**. Parametrul central este considerat pe un
platou numai dacă rezultatele ambilor vecini au același semn pentru R mediu net,
iar niciun vecin nu diferă de centru cu mai mult de `0,15R` (**IPOTEZĂ**).

Dacă o valoare centrală este un vârf izolat, strategia nu trece criteriul de
robustețe; nu se alege retrospectiv vecinul cel mai profitabil.

## 22. Baseline cu intrări aleatoare

Baseline-ul păstrează detectarea canalului, direcția spargerii, SL-ul, familia de
ieșire, costurile și regulile de portofoliu. El înlocuiește numai retestul și
confirmarea.

Pentru fiecare canal spart:

1. se construiește lista lumânărilor 1H de după spargere și dinaintea primei
   invalidări, ținte atinse sau expirări de 12 lumânări 4H;
2. se alege uniform o lumânare din listă;
3. intrarea market este la open-ul lumânării următoare;
4. SL-ul se calculează cu aceeași formulă și ATR1H disponibil la close-ul
   lumânării alese;
5. se aplică același filtru R:R și aceeași ieșire ca variantei comparate.

Se folosesc 200 de seed-uri fixe `0...199` (**IPOTEZĂ**). Fiecare seed rulează
portofoliul complet, inclusiv limita de trei poziții. Raportul arată distribuția
R-ului mediu net aleator și percentila rezultatului strategiei reale în acea
distribuție. Baseline-ul nu este folosit pentru ajustarea parametrilor după
perioada finală.

## 23. Metrici și formatul raportului

Livrabilele viitoarei implementări vor fi:

- `backtest_results/v3-setups.csv`: un rând pentru fiecare canal spart;
- `backtest_results/v3-trades.csv`: un rând pentru fiecare ordin umplut;
- `backtest_results/v3-metrics.json`: metrici structurate;
- `backtest_results/v3-report.md`: raport lizibil și tabele comparative.

### 23.1 Funnel de setup-uri

Pentru fiecare split, variantă, simbol și direcție:

- canale valide;
- spargeri;
- retesturi;
- confirmări;
- ordine create;
- ordine umplute și rata de umplere;
- anulări pe fiecare motiv;
- respingeri `SL_TOO_WIDE`, `MISSING_STRUCTURAL_TARGET`, `TP1_TOO_CLOSE`, R:R și
  `REJECTED_POSITION_LIMIT`;
- setup-uri eliminate de 3R față de 2R și rezultatul lor în varianta 2R.

### 23.2 Metrici de tranzacționare

Metrica principală este **R mediu net după toate costurile**. Se mai raportează:

- numărul de trade-uri;
- win rate;
- profit factor net;
- drawdown maxim în R;
- drawdown maxim procentual pe curba de equity cu risc planificat de 2%;
- cea mai lungă serie de pierderi;
- durata medie și mediană;
- MFE și MAE în R;
- PnL brut, fee, slippage și funding separat;
- sensibilitatea la costuri x2;
- pentru tranșe: R net ponderat pentru TP1, TP2 și runner separat;
- contextul Daily la intrare;
- rezultate agregate și separat pe monedă, direcție, split și variantă;
- acoperirea datelor și trade-urile `FUNDING_INCOMPLETE`.

MFE/MAE folosesc numai intervalul dintre fill și ieșirea finală, raportat la
`R0`, fără a folosi date ulterioare ieșirii.

## 24. Cerințe de determinism și no-look-ahead pentru implementare

Implementarea viitoare trebuie să demonstreze prin teste:

1. pivotul nu este disponibil înaintea celor două lumânări din dreapta;
2. canalul și ATR(ref) nu se schimbă după spargere;
3. lumânarea de spargere nu intră în canal;
4. confirmarea nu poate umple ordinul în aceeași lumânare;
5. fill-ul începe numai în lumânarea următoare;
6. anulările funcționează înainte și după activarea ordinului;
7. target + entry în aceeași lumânare anulează setup-ul;
8. entry + SL în aceeași lumânare produce SL;
9. SL + TP în aceeași lumânare produce SL;
10. gap-ul prin SL folosește prețul cel mai defavorabil;
11. stopurile calculate la close devin active abia în lumânarea următoare;
12. funding-ul folosește timestampurile reale și cantitatea rămasă;
13. maximum trei poziții și ordinea de prioritate sunt deterministe;
14. LONG și SHORT sunt simetrice;
15. două rulări cu aceleași date și seed produc fișiere identice;
16. schimbarea numărului de procese nu schimbă rezultatele.

## 25. Starea reală a golden test-ului v1 și reutilizarea infrastructurii

Starea verificată în repo și în GitHub Actions este **NEREZOLVATĂ** la commitul:

```text
f7d991ef381c1bb281e440068d714aa4295e9e20
```

Workflow-ul [Backtest v1, run 37143674092](https://github.com/dorinbnb-star/tg-alert-bot/actions/runs/37143674092)
a eșuat la golden run 369:

- `0/18` simboluri au fost identice;
- suita a raportat 19 failures din 31 teste;
- diagnosticul adăugat de commitul `f7d991e` arată, pentru toate cele 18 simboluri,
  că EMA-urile live sunt reproduse de o fereastră de 239 lumânări 4H închise;
- nu au fost identificate goluri 4H în ferestrele diagnosticate;
- pașii detect, simulate și report au fost săriți după eșec.

Consecințe pentru v3:

- `backtest/replay.py` nu poate fi considerat echivalent cu live până când
  cardinalitatea ferestrei golden nu este corectată și testul nu trece 18/18;
- v3 poate reutiliza formatele de date și ideea de replay numai după această
  corecție sau printr-un replay v3 separat, demonstrat cu teste no-look-ahead;
- `backtest/simulate.py` este legat de cadența, `detect_entry`, 15m și variantele
  v1; nu se reutilizează pentru logica strategiei v3;
- funcțiile generice de cost, funding, ordine temporală și raportare pot fi
  extrase/reutilizate numai dacă testele lor rămân verzi și nu copiază regulile
  v3 în două locuri;
- problema golden v1 trebuie rezolvată sau izolată explicit înainte ca un raport
  v3 să fie declarat reproductibil.

Această specificație nu repară problema și nu modifică `replay.py` ori
`simulate.py`.

## 26. Exemple ilustrative, nu teste de acceptare

- ETH 4H: canal aproximativ 2.350-2.530, spargere în sus, retest aproximativ
  2.527-2.556, apoi mișcare spre aproximativ 2.800;
- același nivel, spart ulterior în jos și devenit rezistență în jur de 2.525,
  drept exemplu SHORT.

Prețurile sunt aproximative. Instrumentul exact, perioada și captura originală
nu au fost furnizate. Exemplele devin teste de acceptare numai după furnizarea
acestor date și aprobarea explicită a rezultatelor așteptate.

## 27. Decizii rămase deschise pentru aprobarea utilizatorului

Specificația are câte o interpretare executabilă pentru fiecare regulă, dar
următoarele ipoteze trebuie aprobate sau schimbate înainte de implementare:

1. pivot fractal `k=2`;
2. fereastra canalului `N=60` și regula de reînarmare cu patru pivoți noi;
3. toleranța grupurilor `0,5 ATR(ref)`, lățimea `4-12 ATR(ref)`, panta `0,3W` și
   minimum 80% close-uri în canal;
4. breakout `0,5 ATR(ref)`;
5. retest `±0,25 ATR(ref)` și termen de 12 lumânări 4H;
6. confirmarea 1H prin close dincolo de margine și dincolo de high/low-ul
   lumânării precedente;
7. buffer entry `0,10 ATR(ref)` și expirarea după 12 lumânări 1H;
8. SL la `1 ATR(ref)`, minimum `1,5 ATR1H`, respingere peste `3 ATR(ref)`;
9. lookback structural de 1.080 lumânări 4H, TP1 minimum 1R și trailing
   chandelier `3 ATR4H` activ după TP2;
10. calculul net al breakevenului și cele trei momente testate;
11. împărțirea 60/20/20, pragurile de egalitate/platou și sensibilitățile
    prestabilite;
12. baseline-ul aleator cu 200 de seed-uri;
13. excluderea din metrica principală a trade-urilor fără funding real complet;
14. rularea v3 24/7, fără fereastra operațională a botului live;
15. dacă repararea golden v1 este o condiție obligatorie înainte de codul v3 sau
    dacă v3 primește un replay separat, izolat și testat independent.

Până la aprobarea acestor decizii și mesajul `DA, implementează`, nu se scrie cod
de strategie.
