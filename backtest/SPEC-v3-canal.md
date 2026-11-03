# Specificație backtest v3 canal

## 1. Statut și scop

Acest document definește experimentul separat **v3 canal**. Nu conține cod și nu
autorizează implementarea. Implementarea începe numai după mesajul explicit
`DA, implementează`.

Strategia testată este una singură:

```text
canal orizontal 4H -> spargere 4H -> retest -> confirmare 1H
-> intrare limit sau market la următorul open
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

Pentru fiecare lumânare 4H evaluată pentru starea ARMED `B[t]`, canalul se calculează
exclusiv din lumânările `B[t-N] ... B[t-1]`. Lumânarea `B[t]` nu participă la
canal, ATR(ref), regresie, pivoți sau margini.

După trecerea în EXITED se păstrează canalul înghețat la ieșire, conform
secțiunii 6; nu se mută fereastra până la spargere sau anularea ieșirii.

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

Se aplică următoarea succesiune de stări, separat pe fiecare simbol, numai la
închideri 4H. Regula de unicitate și eligibilitate din secțiunea 5.4 rămâne
neschimbată. Nu se caută o nouă ieșire cât timp există un setup, ordin sau poziție.

### 6.1 ARMED: canal valid înaintea primei ieșiri

La închiderea `B[t]`, canalul calculat din `B[t-N] ... B[t-1]` trebuie să fie
valid conform secțiunii 5 și să îndeplinească `L <= close[t-1] <= U`.
Egalitatea este în interior. Numai atunci simbolul poate fi ARMED pentru
evaluarea lui `close[t]`. Cât timp nu s-a produs ieșirea, canalul se recalculează
la fiecare lumânare, iar aceste condiții se verifică din nou.

Un canal descoperit cu close-ul anterior deja în afara `[L, U]` nu devine ARMED
și nu generează spargere, nici retroactiv. Dacă noul canal nu mai este valid
sau condiția close-ului anterior nu mai este îndeplinită, nu se păstrează o
armare care ar ocoli aceste verificări.

### 6.2 EXITED: prima închidere în afara canalului

Prima lumânare evaluată din ARMED cu `close > U` produce EXITED LONG;
cu `close < L` produce EXITED SHORT. Close-ul egal cu o margine nu este ieșire.
Lumânarea de ieșire poate fi chiar `B[t]` la care s-au îndeplinit condițiile
ARMED; nu este obligatorie încă o lumânare de așteptare.

La ieșire se îngheață `U`, `L`, `W`, identitatea canalului și `ATR(ref)`,
folosind exclusiv date cunoscute la închiderea lumânării anterioare ieșirii.
Se memorează direcția și timestampul ieșirii. Canalul nu se recalculează în EXITED.

### 6.3 BREAKOUT: prag atins după ieșire

În EXITED, inclusiv pe lumânarea care produce ieșirea, se verifică pragul pe
aceeași parte, față de canalul și ATR(ref) înghețate:

```text
LONG breakout:  close[t] >= U + 0,5 * ATR(ref)
SHORT breakout: close[t] <= L - 0,5 * ATR(ref)
```

Pragul `0,5 * ATR(ref)` este **IPOTEZĂ**. Egalitatea trece condiția.

La BREAKOUT se păstrează geometria înghețată la ieșire și se înregistrează:

- direcția;
- timestampul spargerii;
- TP-ul de referință;
- contextul Daily disponibil la acel moment;
- volumul și SMA20 de volum 4H, numai pentru varianta de volum.

Nu se cere ca open-ul lumânării de spargere să fie în canal.
Secțiunile 7+ privind retestul, confirmarea, intrarea și managementul se aplică
de la BREAKOUT, fără schimbarea regulilor lor. Termenele acestor secțiuni pornesc
de la spargere, nu de la prima ieșire.

### 6.4 Revenire, schimbare de parte și expirare

Înainte de BREAKOUT, fiecare close din EXITED se tratează în această ordine:

1. Dacă revine în `[L, U]`, inclusiv egalitatea, ieșirea se anulează ca revenire.
   Se eliberează canalul înghețat și se revine la evaluarea ARMED, cu recalculare
   normală. Următoarea lumânare trebuie să îndeplinească din nou secțiunea 6.1;
   revenirea nu autorizează folosirea unui canal devenit invalid.
2. Dacă închide pe partea opusă canalului înghețat, devine o ieșire nouă în
   direcția opusă, cu aceleași `U`, `L`, `W`, ATR(ref) și identitate. Timestampul
   ieșirii se actualizează și numărătoarea începe din nou la 1. Se verifică
   imediat pragul de BREAKOUT pentru noua direcție, inclusiv pe această lumânare.
3. Pentru direcția curentă se verifică pragul din secțiunea 6.3. Dacă este atins,
   se produce BREAKOUT, inclusiv dacă aceasta este ultima lumânare permisă.
4. **IPOTEZĂ:** dacă pragul nu este atins în 3 lumânări 4H consecutive, numărate
   inclusiv de la lumânarea ieșirii (pozițiile 1, 2, 3), ieșirea se anulează la
   închiderea celei de-a treia, cu motivul `EXIT_NO_BREAKOUT`.

După `EXIT_NO_BREAKOUT`, simbolul nu poate fi rearmat cât timp close-urile
rămân în afara unui canal valid. Este necesar un close 4H în interiorul unui
canal valid calculat numai din lumânările anterioare lui; apoi se reia
evaluarea normală din secțiunea 6.1. Nu se generează spargeri retroactive.

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

## 9. Variantele de intrare

Se compară două variante. Ele folosesc același canal, aceeași confirmare, același
SL structural, aceleași ieșiri și aceleași costuri, cu excepția tipului de
intrare descris mai jos.

### 9.1 `ENTRY_LIMIT`: prețul planificat

**IPOTEZĂ:** bufferul este `0,10 * ATR(ref)` în direcția spargerii:

```text
LONG:  entry_plan = U + 0,10 * ATR(ref)
SHORT: entry_plan = L - 0,10 * ATR(ref)
```

Ordinul se creează la închiderea confirmării, dar devine activ numai la open-ul
următoarei lumânări 1H. Lumânarea de confirmare nu poate umple ordinul.

### 9.2 `ENTRY_LIMIT`: expirare

**IPOTEZĂ:** ordinul este activ exact 12 lumânări 1H, de la prima lumânare de
după confirmare până la a 12-a inclusiv. Dacă nu este umplut până la închiderea
celei de-a 12-a, se anulează cu motivul `ORDER_EXPIRED`.

### 9.3 `ENTRY_LIMIT`: fill

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

### 9.4 `ENTRY_MARKET_NEXT_OPEN`

Această variantă nu cere un al doilea retest după confirmare. Intrarea este
planificată la open-ul primei lumânări 1H de după confirmare. Înaintea intrării,
se calculează prețul efectiv de intrare prin aplicarea slippage-ului advers de
market din `backtest/costs.json` la acel open. Acesta este `entry_actual`.

Cu `entry_actual` se calculează SL-ul din secțiunea 10, inclusiv componenta
`minimum_sl`. La open se aplică următoarele verificări, în ordinea scrisă:

1. pentru LONG, dacă `open >= target`, iar pentru SHORT, dacă `open <= target`,
   intrarea se respinge cu `TARGET_AT_OPEN`; `target` este TP-ul unic pentru
   ieșirea de referință și TP1 pentru varianta cu tranșe;
2. pentru LONG, dacă `open <= SL`, iar pentru SHORT, dacă `open >= SL`, intrarea
   se respinge cu `SL_AT_OPEN`;
3. dacă `open <= U - 0,5 * ATR(ref)` pentru LONG sau
   `open >= L + 0,5 * ATR(ref)` pentru SHORT, intrarea se respinge cu
   `INVALID_AT_OPEN`;
4. pentru varianta cu tranșe se verifică `TP1_TOO_CLOSE` față de
   `entry_actual`, conform secțiunii 12.1;
5. dacă distanța până la SL depășește `3 * ATR(ref)`, intrarea se respinge cu
   `SL_TOO_WIDE`;
6. se aplică filtrul R:R al variantei, calculat față de `entry_actual`.

Prima condiție adevărată stabilește motivul respingerii. Dacă toate verificările
trec și există un slot de portofoliu, fill-ul are loc la `entry_actual`. Nu există
ordin în așteptare și nici expirare de 12 lumânări pentru această variantă.
Open-ul este o observație nouă, ulterioară confirmării; nicio informație din
high/low/close-ul lumânării nou deschise nu este folosită pentru decizie.

## 10. Stop loss și riscul inițial

### 10.1 Calcul

Se definește `entry_basis` astfel:

- pentru `ENTRY_LIMIT`, `entry_basis = entry_plan`;
- pentru `ENTRY_MARKET_NEXT_OPEN`, `entry_basis = entry_actual`.

Pentru LONG:

```text
structural_sl = U - 1,0 * ATR(ref)
minimum_sl    = entry_basis - 1,5 * ATR1H(confirmare)
SL            = min(structural_sl, minimum_sl)
distance      = entry_basis - SL
```

Pentru SHORT:

```text
structural_sl = L + 1,0 * ATR(ref)
minimum_sl    = entry_basis + 1,5 * ATR1H(confirmare)
SL            = max(structural_sl, minimum_sl)
distance      = SL - entry_basis
```

Multiplicatorii `1,0`, `1,5` și limita următoare sunt **IPOTEZE**.

Setup-ul se respinge cu `SL_TOO_WIDE` dacă:

```text
distance > 3 * ATR(ref)
```

Pentru `ENTRY_LIMIT`, verificarea se face la confirmare. Pentru
`ENTRY_MARKET_NEXT_OPEN`, se face la open-ul următor, după calcularea
`entry_actual` și înainte de fill.

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
gross_rr = abs(TP - entry_basis) / abs(entry_basis - SL)
```

Costurile nu modifică filtrul geometric R:R, dar intră în dimensionare și în
rezultatul net.

Pentru `ENTRY_LIMIT`, când SL-ul structural este componenta dominantă, limita
inferioară a R:R-ului poate fi calculată direct. Pentru LONG:

```text
entry_plan = U + 0,10 * ATR(ref)
SL         = U - 1,00 * ATR(ref)
TP         = U + W
risk       = 1,10 * ATR(ref)
reward     = W - 0,10 * ATR(ref)
```

Deoarece `W >= 4 * ATR(ref)`:

```text
gross_rr >= (4 - 0,10) / (0,10 + 1,00)
         >= 3,90 / 1,10
         >= 3,5454...
```

Calculul SHORT este simetric. Această limită este valabilă numai pentru intrarea
limit și numai când SL-ul structural domină. Nu se aplică atunci când
`minimum_sl` lărgește riscul și nu se generalizează la
`ENTRY_MARKET_NEXT_OPEN`, unde prețul real de intrare depinde de open și de
slippage.

Se compară două variante identice în rest:

- `REF_RR2`: `gross_rr >= 2`;
- `REF_RR3`: `gross_rr >= 3`.

Ambele variante REF se păstrează. Raportul arată câte setup-uri și trade-uri
diferă între ele. Pentru `ENTRY_LIMIT`, comparația 2R versus 3R este de regulă
mai informativă în familia cu tranșe, `SPLIT_RR2` versus `SPLIT_RR3`, deoarece
variantele REF diferă numai în cazurile în care `minimum_sl` domină. Pentru
`ENTRY_MARKET_NEXT_OPEN`, rezultatele filtrelor REF 2R și 3R se raportează
separat, fără a presupune limita de 3,5454 de mai sus.

## 12. Varianta ulterioară: tranșe structurale

Această variantă se rulează după referință și nu îi modifică rezultatele.

### 12.1 Nivelurile structurale

**IPOTEZĂ:** se caută pivoți 4H confirmați în ultimele 1.080 de lumânări 4H
anterioare confirmării, echivalentul a aproximativ 180 de zile.

Selecția nivelurilor folosește `entry_plan` ca referință geometrică pentru ambele
tipuri de intrare și se face la confirmare. Pentru `ENTRY_MARKET_NEXT_OPEN`,
`entry_plan` nu înlocuiește `entry_actual` în calculul SL-ului, al distanței 1R
sau al filtrului R:R. Dacă open-ul următor a ajuns la TP1, se aplică
`TARGET_AT_OPEN`.

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

**IPOTEZĂ:** TP1 este prea aproape dacă distanța sa față de intrare este sub
`1R`, unde `1R = abs(entry_basis - SL)`. În acest caz setup-ul se respinge cu
`TP1_TOO_CLOSE`; nu se sare la următorul pivot. Pentru intrarea market,
verificarea se face la open, după ce `entry_actual` și SL sunt cunoscute.

Filtrul R:R se aplică la TP2:

- `SPLIT_RR2`: `abs(TP2-entry_basis) / 1R >= 2`;
- `SPLIT_RR3`: `abs(TP2-entry_basis) / 1R >= 3`.

Setup-urile respinse cu `MISSING_STRUCTURAL_TARGET` și `TP1_TOO_CLOSE` se numără
separat. Pentru fiecare se rulează și o simulare contrafactuală cu TP-ul unic din
secțiunea 11, etichetată `SHADOW`. Simularea păstrează varianta de intrare,
SL-ul, costurile și ordinea evenimentelor, dar nu ocupă un slot, nu modifică
equity și nu influențează celelalte tranzacții. Rezultatul `SHADOW` se raportează
separat și nu intră în portofoliu, selecție sau metricile principale.

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
3. pentru `ENTRY_MARKET_NEXT_OPEN` se calculează `entry_actual` și SL, apoi se
   aplică în ordine `TARGET_AT_OPEN`, `SL_AT_OPEN`, `INVALID_AT_OPEN`,
   `TP1_TOO_CLOSE` dacă este cazul, `SL_TOO_WIDE`, filtrul R:R și limita de
   poziții; dacă toate trec, se face fill-ul market la open cu slippage;
4. pentru un setup `ENTRY_LIMIT` neumplut, dacă ținta de anulare și entry sunt
   ambele în range, setup-ul se anulează ca `TARGET_BEFORE_ENTRY`;
5. se procesează fill-ul ordinului limit;
6. după orice fill, pentru aceeași lumânare se procesează întâi SL, apoi
   TP1/TP2/TP;
7. la close se procesează, în ordine: invalidarea 4H dacă este graniță 4H,
   expirarea setup-ului/ordinului, retestul, confirmarea și calculele de stop;
8. valorile calculate la close devin active în lumânarea următoare.

Dacă un ordin se umple în timpul unei lumânări care închide ulterior 4H înapoi în
canal, fill-ul este anterior close-ului și tranzacția rămâne deschisă. Regula de
anulare 4H se aplică numai dacă ordinul era încă neumplut.

## 15. Dimensionare, capital și limita de poziții

Capitalul folosit este equity realizat la momentul activării ordinului limit,
respectiv la open-ul intrării market. PnL-ul nerealizat nu intră în equity pentru
o nouă dimensionare.

```text
risk_budget = 0,02 * equity
```

Costul estimat pe unitate include:

- taxa de intrare; pentru `ENTRY_LIMIT` include și slippage-ul estimat de la
  `entry_plan`, iar pentru `ENTRY_MARKET_NEXT_OPEN` slippage-ul de intrare nu se
  adaugă din nou, deoarece este deja inclus în `entry_actual`;
- taxa și slippage-ul de stop la SL, cu multiplicatorul de stop din
  `backtest/costs.json`.

```text
risk_unit = abs(entry_basis - SL) + estimated_cost_per_unit
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

### 16.1 Acoperirea reală verificată la OKX

Verificarea a fost făcută la 3 noiembrie 2026, din două surse publice oficiale:

- [`GET /api/v5/public/funding-rate-history`](https://www.okx.com/docs-v5/en/#public-data-rest-api-get-funding-rate-history),
  care oferă maximum trei luni și acoperă coada recentă;
- [`GET /api/v5/public/market-data-history`](https://www.okx.com/docs-v5/en/#public-data-rest-api-get-historical-market-data),
  cu `module=3`, `instType=SWAP` și agregare lunară, care returnează arhivele
  istorice. Documentația precizează că backfill-ul este în curs și acoperirea
  poate varia după instrument și perioadă.

Arhiva a fost interogată în ferestre de maximum 10 luni. Pentru fiecare contract
s-a deschis primul ZIP disponibil și s-a luat cel mai mic `funding_time` din CSV,
nu doar data numelui de fișier. Datele de început sunt UTC:

| Contract | Primul funding real disponibil | Prima sursă verificată |
| --- | ---: | --- |
| BTC-USDT-SWAP | 2022-02-28 16:00:00 | [2022-03](https://static.okx.com/cdn/okex/traderecords/swaprates/monthly/202203/BTC-USDT-SWAP-fundingrates-2022-03.zip?v=1001) |
| ETH-USDT-SWAP | 2022-02-28 16:00:00 | [2022-03](https://static.okx.com/cdn/okex/traderecords/swaprates/monthly/202203/ETH-USDT-SWAP-fundingrates-2022-03.zip?v=1001) |
| SOL-USDT-SWAP | 2022-02-28 16:00:03 | [2022-03](https://static.okx.com/cdn/okex/traderecords/swaprates/monthly/202203/SOL-USDT-SWAP-fundingrates-2022-03.zip?v=1001) |
| XRP-USDT-SWAP | 2022-02-28 16:00:00 | [2022-03](https://static.okx.com/cdn/okex/traderecords/swaprates/monthly/202203/XRP-USDT-SWAP-fundingrates-2022-03.zip?v=1001) |
| BNB-USDT-SWAP | 2022-12-23 08:00:42 | [2022-12](https://static.okx.com/cdn/okex/traderecords/swaprates/monthly/202212/BNB-USDT-SWAP-fundingrates-2022-12.zip?v=1001) |
| AVAX-USDT-SWAP | 2022-02-28 16:00:02 | [2022-03](https://static.okx.com/cdn/okex/traderecords/swaprates/monthly/202203/AVAX-USDT-SWAP-fundingrates-2022-03.zip?v=1001) |
| LINK-USDT-SWAP | 2022-02-28 16:00:00 | [2022-03](https://static.okx.com/cdn/okex/traderecords/swaprates/monthly/202203/LINK-USDT-SWAP-fundingrates-2022-03.zip?v=1001) |
| LTC-USDT-SWAP | 2022-02-28 16:00:00 | [2022-03](https://static.okx.com/cdn/okex/traderecords/swaprates/monthly/202203/LTC-USDT-SWAP-fundingrates-2022-03.zip?v=1001) |
| SUI-USDT-SWAP | 2023-05-05 08:00:13 | [2023-05](https://static.okx.com/cdn/okex/traderecords/swaprates/monthly/202305/SUI-USDT-SWAP-fundingrates-2023-05.zip?v=1001) |
| UNI-USDT-SWAP | 2022-02-28 16:00:00 | [2022-03](https://static.okx.com/cdn/okex/traderecords/swaprates/monthly/202203/UNI-USDT-SWAP-fundingrates-2022-03.zip?v=1001) |
| AAVE-USDT-SWAP | 2022-02-28 16:00:03 | [2022-03](https://static.okx.com/cdn/okex/traderecords/swaprates/monthly/202203/AAVE-USDT-SWAP-fundingrates-2022-03.zip?v=1001) |
| TIA-USDT-SWAP | 2023-11-01 00:00:07 | [2023-11](https://static.okx.com/cdn/okex/traderecords/swaprates/monthly/202311/TIA-USDT-SWAP-fundingrates-2023-11.zip?v=1001) |
| WLD-USDT-SWAP | 2023-07-24 16:00:16 | [2023-07](https://static.okx.com/cdn/okex/traderecords/swaprates/monthly/202307/WLD-USDT-SWAP-fundingrates-2023-07.zip?v=1001) |
| WIF-USDT-SWAP | 2024-04-15 16:00:00 | [2024-04](https://static.okx.com/cdn/okex/traderecords/swaprates/monthly/202404/WIF-USDT-SWAP-fundingrates-2024-04.zip?v=1001) |
| STRK-USDT-SWAP | 2024-02-20 16:00:00 | [2024-02](https://static.okx.com/cdn/okex/traderecords/swaprates/monthly/202402/STRK-USDT-SWAP-fundingrates-2024-02.zip?v=1001) |
| VIRTUAL-USDT-SWAP | 2024-12-11 12:00:00 | [2024-12](https://static.okx.com/cdn/okex/traderecords/swaprates/monthly/202412/VIRTUAL-USDT-SWAP-fundingrates-2024-12.zip?v=1001) |
| ENA-USDT-SWAP | 2025-09-17 08:00:00 | [2025-09](https://static.okx.com/cdn/okex/traderecords/swaprates/monthly/202509/ENA-USDT-SWAP-fundingrates-2025-09.zip?v=1001) |
| YGG-USDT-SWAP | 2022-02-28 16:00:02 | [2022-03](https://static.okx.com/cdn/okex/traderecords/swaprates/monthly/202203/YGG-USDT-SWAP-fundingrates-2022-03.zip?v=1001) |

Ora 16:00 UTC din 28 februarie corespunde începutului zilei de 1 martie în
UTC+8, fusul folosit de OKX pentru modulele istorice 1, 2, 3 și 11. Pentru
contractele mai noi, primul funding din tabel este prima decontare disponibilă
după `listTime` al contractului curent. ENA-USDT-SWAP, de exemplu, are
`listTime` 17 septembrie 2025 07:30 UTC, deci data din tabel nu este un gol de
backfill pentru contractul curent.

Acoperirea nu este limitată la ultimele luni: contractele vechi au arhivă din
martie 2022, iar cele noi din ziua listării contractului curent. Arhiva lunară
ajunge până în octombrie 2026 la data verificării, iar endpointul de trei luni
acoperă perioada recentă. Prin urmare, regula `FUNDING_INCOMPLETE` rămâne o
protecție pentru goluri punctuale sau pentru eventuale lumânări mai vechi decât
arhiva de funding, nu un motiv de a înlocui funding-ul real cu o estimare. Un
trade marcat astfel rămâne exclus din metrica principală, conform regulii de mai
sus.

## 17. Filtrul R:R și setup-urile eliminate

Pentru fiecare familie de ieșire se rulează separat pragul 2R și pragul 3R.
Comparația se face separat pentru `ENTRY_LIMIT` și
`ENTRY_MARKET_NEXT_OPEN`. Toate celelalte reguli și aceeași ordine a
evenimentelor rămân identice.

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
Până când EMA50 și EMA200 Daily sunt ambele disponibile, toate câmpurile Daily
ale trade-ului sunt `NA`. Lipsa lor nu blochează intrarea.

## 20. Date, acoperire și încălzire

Se descarcă istoricul maxim disponibil OKX pentru cele 18 contracte din
watchlist, pentru 1D, 4H și 1H, plus funding-ul real. Perioada efectivă se
raportează separat pe simbol.

Încălzirea minimă include numai datele folosite de strategie: N lumânări 4H
continue, ATR14 4H, ATR14 1H și pivoții confirmați necesari. Datele Daily nu fac
parte din încălzire.

Nu se deschid tranzacții în perioada de încălzire. Un simbol cu istoric
insuficient nu este extrapolat și nu intră în agregatul perioadei în care nu era
tranzacționabil.

## 21. Împărțire cronologică și selecție

**IPOTEZĂ:** după încălzirea strategiei, axa calendaristică globală se împarte:

- 60% dezvoltare;
- 20% walk-forward;
- 20% perioadă finală neatinsă.

Pentru fiecare simbol se calculează `eligible_from`, primul timestamp 1H la care
încălzirea strategiei este completă. Începutul axei globale este cel mai mic
`eligible_from` dintre simboluri, iar sfârșitul este ultimul timestamp comun al
setului descărcat. Granițele 60/20/20 sunt aceleași date UTC pentru toate
simbolurile. Un simbol intră în calcul numai de la propriul `eligible_from`;
perioadele anterioare nu sunt completate și nu sunt tratate ca zero trade-uri.

Ordinea selecției este:

1. pentru fiecare dintre `ENTRY_LIMIT` și `ENTRY_MARKET_NEXT_OPEN`, pe dezvoltare
   se compară `REF_RR2` și `REF_RR3`;
2. pentru fiecare tip de intrare se compară separat cele șase variante
   `SPLIT_RR2/RR3 x BE_AFTER_TP1 / BE_AFTER_TP2 / NO_BE`;
3. se alege combinația dintre tipul de intrare, familia de ieșire și variantă cu
   cel mai mare R mediu net;
4. dacă diferența dintre primele două este `<= 0,05R` (**IPOTEZĂ**), se alege
   varianta cu drawdown maxim mai mic; dacă și acesta este egal, se alege varianta
   în ordinea fixă: `ENTRY_LIMIT`, TP unic, fără BE, prag 2R;
5. filtrul de volum și cele două variante CHoCH se testează apoi separat peste
   varianta aleasă; aceeași regulă de selecție se aplică;
6. varianta finală se blochează înainte de walk-forward;
7. se rulează fără modificări pe walk-forward și apoi o singură dată pe perioada
   finală;
8. rezultatele slabe pe walk-forward sau final se raportează; nu autorizează
   ajustarea parametrilor sau repetarea perioadei finale.

Aceiași parametri se aplică tuturor monedelor.
Comparația raportează distinct rata de umplere a `ENTRY_LIMIT`, respingerile
`TARGET_AT_OPEN`, `SL_AT_OPEN` și `INVALID_AT_OPEN` pentru
`ENTRY_MARKET_NEXT_OPEN`, precum și setup-urile care au produs trade numai în
varianta market.

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

Baseline-ul păstrează detectarea canalului, direcția spargerii, tipul de intrare,
SL-ul, familia de ieșire, costurile și regulile de portofoliu. El înlocuiește
numai retestul și confirmarea.

Pentru fiecare canal spart:

1. se construiește lista lumânărilor 1H de după spargere și dinaintea primei
   invalidări, ținte atinse sau expirări de 12 lumânări 4H;
2. se alege uniform o lumânare din listă;
3. close-ul ei devine confirmarea aleatoare;
4. pentru `ENTRY_MARKET_NEXT_OPEN` se aplică intrarea și verificările din
   secțiunea 9.4; pentru `ENTRY_LIMIT` ordinul se activează în lumânarea următoare
   la `entry_plan` și respectă aceeași expirare de 12 lumânări 1H;
5. SL-ul se calculează cu aceeași formulă și ATR1H disponibil la close-ul
   lumânării alese;
6. se aplică același filtru R:R și aceeași ieșire ca variantei comparate.

Se folosesc 200 de seed-uri fixe `0...199` (**IPOTEZĂ**). Fiecare seed rulează
portofoliul complet, inclusiv limita de trei poziții. Raportul arată distribuția
R-ului mediu net aleator și percentila rezultatului strategiei reale în acea
distribuție. Baseline-ul nu este folosit pentru ajustarea parametrilor după
perioada finală.

## 23. Metrici și formatul raportului

Livrabilele viitoarei implementări vor fi:

- `backtest_results/v3-setups.csv`: un rând pentru fiecare canal spart;
- `backtest_results/v3-trades.csv`: un rând pentru fiecare intrare executată;
- `backtest_results/v3-metrics.json`: metrici structurate;
- `backtest_results/v3-report.md`: raport lizibil și tabele comparative.

### 23.1 Funnel de setup-uri

Pentru fiecare split, variantă, simbol și direcție:

- canale valide;
- intrări în ARMED, treceri în EXITED și spargeri BREAKOUT, separat;
- reveniri din EXITED în canal și anulări `EXIT_NO_BREAKOUT`, numărate separat;
- schimbări de parte în EXITED, cu noul timestamp al ieșirii;
- retesturi;
- confirmări;
- ordine limit create;
- ordine limit umplute și rata lor de umplere;
- intrări market acceptate;
- respingeri market `TARGET_AT_OPEN`, `SL_AT_OPEN` și `INVALID_AT_OPEN`, pe
  fiecare motiv;
- setup-uri care au produs trade numai prin `ENTRY_MARKET_NEXT_OPEN`;
- anulări pe fiecare motiv;
- respingeri `SL_TOO_WIDE`, `MISSING_STRUCTURAL_TARGET`, `TP1_TOO_CLOSE`, R:R și
  `REJECTED_POSITION_LIMIT`;
- numărul separat al respingerilor `MISSING_STRUCTURAL_TARGET` și
  `TP1_TOO_CLOSE`, plus rezultatele lor `SHADOW` cu TP unic;
- setup-uri eliminate de 3R față de 2R și rezultatul lor în varianta 2R.

### 23.2 Metrici de tranzacționare

Pentru corectura definiției spargerii, prima rerulare după aprobarea
implementării se limitează strict la perioada de dezvoltare. Walk-forward și
perioada finală nu se rulează în acest pas. Raportul păstrează timestampurile
ARMED, EXITED și BREAKOUT, identitatea canalului și motivul fiecărei anulări;
ARMED se numără la intrarea în stare, nu la fiecare recalculare din aceeași stare.
Revenirile și expirările se raportează chiar dacă nu s-a creat un setup BREAKOUT.

Comparația retest versus intrare fără retest este un pas ulterior, separat,
folosind aceleași canale și costuri. Rezultatele rămân exploratorii, pe istoric
deja văzut; această rerulare nu constituie validare pe date neatinse.

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
- rezultate agregate și separat pe monedă, direcție, split, tip de intrare și
  variantă;
- acoperirea datelor și trade-urile `FUNDING_INCOMPLETE`.

MFE/MAE folosesc numai intervalul dintre fill și ieșirea finală, raportat la
`R0`, fără a folosi date ulterioare ieșirii.

## 24. Cerințe de determinism și no-look-ahead pentru implementare

Implementarea viitoare trebuie să demonstreze prin teste:

1. pivotul nu este disponibil înaintea celor două lumânări din dreapta;
2. canalul și ATR(ref) nu se schimbă din momentul ieșirii EXITED, nici la BREAKOUT;
3. lumânarea ieșirii și lumânările ulterioare până la BREAKOUT nu intră în canal;
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
16. schimbarea numărului de procese nu schimbă rezultatele;
17. `ENTRY_MARKET_NEXT_OPEN` folosește numai open-ul lumânării noi, fără
    high/low/close din viitor;
18. `TARGET_AT_OPEN`, `SL_AT_OPEN` și `INVALID_AT_OPEN` respectă prioritatea din
    secțiunea 9.4 și împiedică fill-ul;
19. pentru intrarea market, `minimum_sl`, `SL_TOO_WIDE` și R:R folosesc
    `entry_actual`, cu slippage inclus;
20. WIF-USDT-SWAP, august 2025: canalul `U=1.0386`, `L=0.8942` nu produce
    BREAKOUT. Cazul furnizat pentru regresie are close 4H `0.8572` la
    `2025-08-15 12:00 UTC`, canal devenit valid abia la `2025-08-16 04:00 UTC`
    (close `0.8811`) și vechea spargere la `2025-08-16 08:00 UTC` (close `0.8677`).
    Fixture-ul păstrează explicit convenția timestampului OHLC și ora închiderii,
    astfel încât eticheta lumânării să nu fie confundată cu momentul semnalului;
21. ieșire mică, apoi prag atins pe lumânarea următoare: BREAKOUT pe a doua,
    cu canalul și ATR(ref) înghețate la prima;
22. ieșire urmată de close în `[L, U]`: nicio spargere, revenire la evaluarea
    ARMED; egalitatea cu oricare margine este revenire;
23. ieșire fără prag în trei lumânări: `EXIT_NO_BREAKOUT` la a treia, fără
    rearmare până la revenirea cerută în secțiunea 6.4; prag atins chiar pe
    a treia lumânare produce BREAKOUT, nu expirare;
24. canal descoperit cu close anterior în afară: nicio armare și nicio spargere;
25. toate tranzițiile ARMED/EXITED/BREAKOUT sunt simetrice LONG/SHORT;
26. trecerea pe partea opusă în EXITED păstrează canalul și ATR(ref), schimbă
    direcția, reinițializează numărătoarea și verifică imediat noul prag.

După implementare se raportează pentru cazurile de audit `11 UNI (2022-06-21)`
și `13 LTC (2026-10-08)` dacă spargerea se păstrează și de ce, cu close-urile,
canalul și tranzițiile relevante. Acestea sunt verificări punctuale ale
detectorului, nu autorizarea unei rerulări de portofoliu pe walk-forward/final.
Observația furnizată de utilizator — 39 din 67 setup-uri pe 8 monede aveau
close-ul anterior deja în afara canalului — motivează corectura, fără a fi
prezentată ca rezultat al unei rerulări efectuate pentru această specificație.

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
- v3 primește un pipeline separat, izolat și testat independent, inclusiv toate
  testele no-look-ahead din secțiunea 24;
- pipeline-ul v3 nu depinde de trecerea golden test-ului v1, iar repararea lui v1
  nu este condiție pentru implementarea sau raportarea v3;
- `backtest/simulate.py` este legat de cadența, `detect_entry`, 15m și variantele
  v1; nu se reutilizează pentru logica strategiei v3;
- funcțiile generice de cost, funding, ordine temporală și raportare pot fi
  extrase/reutilizate numai dacă testele lor rămân verzi și nu copiază regulile
  v3 în două locuri.

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

Decizie încă deschisă pentru validarea ulterioară: termenul de 3 lumânări 4H
în EXITED este **IPOTEZĂ**, valoare de pornire pentru corectură, nu prag validat.
Nu se selectează acum alt termen pe baza rezultatelor deja văzute.

Implementarea noii succesiuni de stări necesită aprobarea explicită separată
`DA, implementează`. Comparația cu intrarea fără retest rămâne un task ulterior;
această corectură nu îi definește și nu îi autorizează regulile de execuție.

Valorile de
pornire din fosta listă 1-12 și 14 sunt aprobate. Funding-ul real are acoperire
suficientă conform secțiunii 16.1, iar `FUNDING_INCOMPLETE` rămâne protecția
pentru goluri. V3 va folosi pipeline-ul separat stabilit în secțiunea 25.

Regula pentru țintele structurale rămâne neschimbată: lipsa lor produce
`MISSING_STRUCTURAL_TARGET`, iar rezultatul cu TP unic este doar `SHADOW`. Nu se
introduce în această versiune o țintă alternativă bazată pe W.

Implementarea rămâne oprită până la mesajul explicit `DA, implementează`.
