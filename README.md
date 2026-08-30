# Cast Studio

Helyi médialejátszó, ami a géped fájljait és mappáit **DLNA/UPnP** protokollon
küldi egy hálózatra kötött TV-re. Böngészőből kezelhető, telepítést nem igényel:
csak Python 3 kell hozzá, külső csomag nélkül.

A fejlesztés és a mérések egy **Hisense VIDAA** okostévén készültek. Az app nem
erre az egy készülékre van szabva – a lejátszható formátumokat, a tekerési módot
és a némítás módját mind a csatlakoztatott készüléktől kérdezi meg –, de a
dokumentumban jelzett kerülőmegoldásokat ennek a TV-nek a hiányosságai hívták
életre. A legtöbb hasonló hiba más gyártóknál is előfordul.

---

## Tartalom

- [Mire való](#mire-való)
- [Követelmények](#követelmények)
- [Indítás](#indítás)
- [Parancssori kapcsolók](#parancssori-kapcsolók)
- [Használat](#használat)
- [Felépítés](#felépítés)
- [HTTP API](#http-api)
- [Az állapotfájl](#az-állapotfájl)
- [Folytatás onnan, ahol abbahagytad](#folytatás-onnan-ahol-abbahagytad)
- [Készülékek eltérései és a kezelésük](#készülékek-eltérései-és-a-kezelésük)
- [Formátumok és átkódolás](#formátumok-és-átkódolás)
- [Feliratok](#feliratok)
- [Biztonsági modell](#biztonsági-modell)
- [Hordozhatóság](#hordozhatóság)
- [Hibakeresés](#hibakeresés)
- [Önellenőrzés](#önellenőrzés)

---

## Mire való

A TV nem fér hozzá a géped fájljaihoz, és a böngészőből kiválasztott fájl
`blob:` URL-jét sem tudja megnyitni. Ezt a szakadékot hidalja át az app:

1. kiszolgálja a kezelőfelületet a `localhost`-on;
2. kiszolgálja a médiafájlokat a gép LAN-címén, HTTP Range támogatással;
3. **vezérli a TV-t** UPnP AVTransport hívásokkal: elindítja, szünetelteti,
   tekeri, lépteti a lejátszási sort.

A vezérlés azért a szerverben van és nem a böngészőben, mert a böngésző nem tud
nyers UDP multicastot küldeni (ez kell az SSDP-felderítéshez), a TV pedig nem ad
CORS-fejlécet a SOAP-válaszaihoz.

---

## Követelmények

| | |
|---|---|
| **Python** | 3.9 vagy újabb, kizárólag a szabványos könyvtárral |
| **Rendszer** | macOS, Linux vagy Windows – lásd [Operációs rendszerek](#operációs-rendszerek) |
| **Hálózat** | a gép és a TV ugyanazon az alhálózaton, multicast (SSDP) engedélyezve |
| **TV** | bármilyen DLNA MediaRenderer (AVTransport szolgáltatással) |
| **ffmpeg** | *nem kötelezett* – ha van, méri a médiahosszt és szükség esetén átkódol |

---

## Indítás

Az app mappájából:

```bash
python3 server.py
```

macOS-en a Finderből dupla kattintás a `start.command`, Windowson a
Fájlkezelőből a `start.bat` fájlra. Windowson a parancs `py -3 server.py`
vagy `python server.py`.

A terminál kiírja a megnyitandó címet:

```
  Cast Studio  ·  helyi médialejátszó DLNA-s TV-hez
  ──────────────────────────────────────────────────────────────
  Felület           http://localhost:8420/?t=49d26d1b10613127da3a
  TV ezt látja      http://192.168.0.10:8420
  Gyökérkönyvtár    /home/felhasznalo
  Átkódolás         ffmpeg elérhető
  Állapotfájl       data/state.json

  A TV keresése az oldal megnyitásakor automatikus.
  Leállítás: Ctrl+C
```

A `--verbose` kapcsolóval látszik, mit kér le a TV – soronként idő, kliens,
státusz és a fájl neve a nyers URL helyett:

```
  07:41:51  192.168.0.42    GET  206  média      Egy-resz.mp4
  07:41:56  127.0.0.1       GET  404  média      passwd
```

A színezés csak valódi terminálban jelenik meg; fájlba irányítva tiszta szöveg.

---

## Parancssori kapcsolók

| Kapcsoló | Mit csinál |
|---|---|
| `--root <mappa>` | melyik mappából lehet tallózni (alapértelmezés: a home könyvtár) |
| `--port <szám>` | melyik porton figyeljen (alap: 8420) |
| `--no-token` | ne kérjen tokent (csak megbízható hálózaton) |
| `--no-open` | ne nyissa meg magától a böngészőt |
| `--verbose` | kérésnaplózás |
| `--data <mappa>` | hova írja az állapotfájlt (alap: az app `data/` mappája) |
| `--keep-playing` | kilépéskor ne állítsa meg a TV-t |

---

## Használat

1. Nyisd meg a kiírt `http://localhost:…` címet. Az app magától megkeresi a
   készülékeket, és kiválasztja az elsőt (vagy a korábban használtat).
2. Bal oldalon tallózz: egy fájlra kattintva bekerül a sorba; a mappa sorára
   húzva az egérrel megjelenik egy `+` gomb, ami az egész mappát beteszi.
   Az **Almappákkal** pipával rekurzívan olvassa be.
3. A **Küldés a TV-re** gombbal indul a lejátszás.

Billentyűk: `szóköz` = lejátszás/szünet, `←`/`→` = 10 másodperc,
`Shift+←`/`Shift+→` = előző/következő elem, `/` = ugrás a keresőmezőre.

A lejátszási sor húzd-és-ejtsd módszerrel átrendezhető, és a böngésző bezárása
után is megmarad. Ha nincs készülék kiválasztva, a sorra kattintva a fájl a
böngészőben nézhető meg (helyi előnézet).

---

## Felépítés

```
server.py           HTTP-kiszolgáló + JSON API
  ├── fájlböngésző          gyökérre korlátozott listázás, feliratpárosítás
  ├── médiakiszolgálás      HTTP Range (206), DLNA-fejlécek
  ├── SRT → WebVTT          menet közbeni feliratkonverzió
  ├── hosszmérés            ffprobe, ha a készülék nem jelenti a hosszt
  ├── átkódolás             ffmpeg, ha a formátum nem szerepel a készülék listáján
  └── állapotkezelés        atomi mentés, revíziószám, megtekintési pontok

dlna.py             UPnP-réteg
  ├── discover()            SSDP M-SEARCH, eszközleíró letöltése
  ├── soap()                SOAP-hívás, UPnP-hibakódok fordítása
  ├── build_didl()          DIDL-Lite metaadat a SetAVTransportURI-hoz
  └── Player                állapotgép + háttérszál, ami a készüléket figyeli

public/index.html   a teljes kezelőfelület egy fájlban (HTML + CSS + JS)
```

**A `Player` osztály** tartja a lejátszási sort, és egy háttérszál
másodpercenként (szünetben ritkábban) lekérdezi a készülék állapotát. Ez a szál
felel a sor léptetéséért is: a DLNA-ban nincs beépített lejátszási sor fogalom,
ezért amikor egy elem véget ér, ez a réteg indítja a következőt. Ugyanez a szál
figyeli, hogy a lejátszás tart-e a valós idővel, és hogy a válasz tényleg arról
az elemről szól-e, amit elindítottunk.

A `Player` a készülékparancsokat zárral sorosítja: egy elem indítása három
hívásból áll (`Stop`, `SetAVTransportURI`, `Play`), és két egyidejű indítás
összefésülődve azt eredményezné, hogy a készülék az egyik fájlt játssza, a
felület a másikat mutatja.

---

## HTTP API

Minden `/api/` alatti végpont tokent igényel (`?t=…`), kivéve ha
`--no-token`-nel indult. A statikus felület token nélkül is elérhető, hogy az
oldal betölthető legyen.

### Böngészés és kiszolgálás

| Végpont | Leírás |
|---|---|
| `GET /api/info` | gyökér, port, LAN-címek, gyorsmenük, elérhető-e ffmpeg, kiszolgált forgalom |
| `GET /api/browse?path=…&hidden=0` | egy mappa tartalma (mappák, fájlok, felirat-párok) |
| `GET /api/scan?path=…` | rekurzív beolvasás (legfeljebb 3000 elem) |
| `GET /api/media?path=…` | médiafájl, HTTP Range támogatással |
| `GET /api/stream?path=…` | menet közbeni átkódolás MP4-re |
| `GET /api/sub?path=…` | felirat WebVTT-ként (SRT-ből konvertálva) |

### Állapot

| Végpont | Leírás |
|---|---|
| `GET /api/state` | beállítások, lejátszási sor, utolsó mappa, megtekintési pontok, `rev` |
| `POST /api/state` | ugyanezek mentése; `rev` megadásakor ütközés esetén `409` |

### Készülékvezérlés

| Végpont | Leírás |
|---|---|
| `GET /api/dlna/discover?timeout=5` | SSDP-felderítés |
| `GET /api/dlna/select?udn=…` | készülék kiválasztása, képességek lekérdezése |
| `GET /api/dlna/state` | pillanatnyi állapot (lásd lentebb) |
| `POST /api/dlna/queue` | a sor átadása és indítása |
| `GET /api/dlna/play?index=N` | ugrás a sor N. elemére |
| `GET /api/dlna/toggle` \| `pause` \| `resume` \| `stop` | lejátszásvezérlés |
| `GET /api/dlna/next` \| `prev` | léptetés a sorban |
| `GET /api/dlna/seek?to=<mp>` | tekerés |
| `GET /api/dlna/volume?level=0..100` | hangerő |
| `GET /api/dlna/mute?on=0\|1` | némítás |
| `GET /api/dlna/repeat?mode=OFF\|ALL\|SINGLE` | ismétlés |

Az állapotválasz:

```jsonc
{
  "renderer": { "name": "…", "udn": "…", "host": "…", "model": "…" },
  "mimes": ["video/mp4", "…"],   // amit a készülék elfogad
  "state": "PLAYING",            // a készülék által jelentett állapot
  "index": 0,                    // hányadik elem megy a sorból
  "path": "/…/film.mkv",         // a játszott fájl azonosítója
  "position": 123.0,             // másodperc
  "duration": 2713.0,
  "volume": 22, "muted": false,
  "repeat": "OFF",
  "seekUnit": "REL_TIME",        // amelyik tekerési egység bevált nála
  "queueLength": 16,
  "online": true,
  "error": "", "errorId": 0,
  "title": "…"
}
```

A `POST /api/dlna/queue` válasza ezt kiegészíti két mezővel: `skipped` a
kihagyott (hiányzó vagy üres) fájlok neveivel, `paths` pedig a ténylegesen
elindított sorral – így a felület tudja, mit dobjon ki a saját listájából.

**Hibaüzenetek.** Az `error` mező azonosítót visel (`errorId`), és néhány
másodpercig lekérhető marad. Enélkül az az ügyfél vinné el az üzenetet, amelyik
véletlenül elsőként kérdez rá – két megnyitott lap közül a másik sosem látná.

---

## Az állapotfájl

Egyetlen fájl, az app `data/` mappájában (vagy ahova a `--data` mutat):

```jsonc
{
  "settings": {
    "base": "http://192.168.0.10:8420",  // ezt a címet kapja a TV a fájlokhoz
    "subs": true,                        // külső feliratok átadása
    "hidden": false,                     // rejtett fájlok mutatása
    "recursive": false,                  // mappa beolvasása almappákkal
    "repeat": "OFF",
    "volume": 30,
    "udn": "uuid:…",                     // legutóbb használt készülék
    "resume": true                       // folytatás onnan, ahol abbahagytad
  },
  "queue": [ { "path": "…", "name": "…", "kind": "video", "subs": [] } ],
  "cwd": "/…",                           // utoljára megnyitott mappa
  "positions": {
    "/…/film.mkv": { "pos": 375.0, "dur": 2713.3, "at": 1787397818.4 }
  },
  "rev": 12
}
```

Az írás atomi (`.tmp` fájl + átnevezés), így félbeszakadt mentés nem hagy sérült
állapotot. A beolvasás–összefésülés–kiírás egyetlen kritikus szakasz: a
lejátszó tízmásodpercenként ment pozíciót, miközben a felület beállítást és
sort ment, és zár nélkül ezek felülírnák egymást.

A `rev` **csak a felület által birtokolt részt** (`settings`, `queue`, `cwd`)
követi. Ha két lapon van nyitva a felület, és az egyiken átírod a sort, a másik
mentése `409`-et kap ahelyett, hogy szó nélkül elsöpörné. A megtekintési pontok
ezt nem érintik: azok kulcsonként fésülődnek össze.

Az állapotfájl törölhető – az app alapértelmezéssel indul újra.

---

## Folytatás onnan, ahol abbahagytad

Lejátszás közben az app tízmásodpercenként megjegyzi a pozíciót, és
leállításkor, elemváltáskor, léptetéskor és kilépéskor is elmenti. Ha ugyanazt a
fájlt később újraindítod, a készüléket odatekeri – a lejátszási sorban kék
jelölés mutatja a pontot (`▸ 42:13`).

Két helyzetben szándékosan nem kínálja fel:

- az első 20 másodpercben (nincs mit folytatni);
- ha a vége előtti 45 másodpercen belül jártál – ilyenkor végignézettnek
  tekinti, és törli a pontot.

Fájlonként legfeljebb 500 pontot tart meg (a legrégebbiek kiesnek). A funkció
kikapcsolható a beállításokban.

### Miért nem elég egyszer odatekerni

A tekerés nem „kilövöm és elfelejtem" művelet. A készülék a fájl megnyitása
közben kétféleképpen is átejtheti a hívót: vagy hibával utasítja vissza a
parancsot (`701 Transition not available`), vagy elfogadja, de nem mozdul rá.
Mindkettő ugyanoda vezetne: a film elölről menne, és tíz másodperc múlva a
rendszeres mentés **felül is írná** a pontot, ahonnan folytatni akartál.

Ezért az app megjegyzi a célt, és ellenőrzi, hogy tényleg odaért-e:

- amíg a folytatás nem ért célba, egyetlen új pontot sem ment el;
- három másodpercenként újrapróbálja, legfeljebb nyolcszor – a szünet nem
  számít bele, tehát egy hosszabb megállás nem szakítja félbe;
- ha te magad tekersz közben, te veszed át az irányítást: a függőben lévő
  folytatás elmarad;
- ha végleg nem sikerül, kiírja, és a mentett pontot **akkor sem írja felül**,
  amíg a lejátszás túl nem jutott rajta.

### Mikor van vége egy elemnek

A sor léptetéséhez **két egybehangzó leolvasás** kell. Egy elem vége
visszafordíthatatlan esemény: törli a megtekintési pontot, és elindítja a
következő részt. A készülékek viszont lejátszás közben is jelentenek
pillanatnyi `STOPPED`-ot - egyetlen ilyenre lépni azt jelentené, hogy egy
üzemzavar elveszi azt a pontot, ahonnan folytatnál. A vége így 1,2 másodperccel
később derül ki; cserébe nem lehet véletlenül elveszíteni semmit.

### Váratlan újraindulás

Előfordul, hogy a készülék magától visszaugrik a fájl elejére – mérve: tekerés
után közvetlenül kiadott szünet, majd folytatás után. Az app ezt onnan ismeri
fel, hogy két egymást követő lekérdezés között a pozíció harminc másodpercnél
nagyobbat esik, és nem te tekertél. Ilyenkor visszateker oda, ahol tartottál, és
addig egy pontot sem ment, nehogy a nullához közeli állás írja felül a valódit.

A visszaesést az **előző leolvasáshoz** méri, nem egy korábbi maximumhoz: egy ki
nem szolgált tekerési cél különben hamis riasztást váltana ki, és az app a saját
farkába harapva rángatná a készüléket.

---

## Készülékek eltérései és a kezelésük

A DLNA-t a gyártók eltérően valósítják meg. Az app nem tippel: megkérdezi a
készüléket, és ahol az nem ad használható választ, ott kiméri.

### Médiahossz

Sok készülék `00:00:00` hosszt jelent a `GetPositionInfo`-ra. Ilyenkor az app
`ffprobe`-bal állapítja meg a hosszt helyben, így a csúszka és a tekerés pontos
marad. `ffprobe` nélkül a csúszka nem használható, a lejátszás igen.

### Tekerési egység

A DLNA három tekerési egységet ismer. Az app először megkérdezi a készüléket
(`GetDeviceCapabilities`); ha az nem árulja el, sorra próbálja őket:

```
REL_TIME  →  ABS_TIME  →  X_DLNA_REL_BYTE
```

Amelyik átmegy, azt megjegyzi. A bájt alapú változat célját a hosszból és a
fájlméretből becsli, ezért változó bitrátánál pontatlan – ez a legvégső eset.

**A `701`-es hibakód nem egységhiba.** Azt jelenti, hogy a készülék *most* nem
tud tekerni (például még tölt), nem azt, hogy rossz az egység. Ilyenkor az app
nem lép tovább a következő egységre – különben egy elfogadott, de rossz egység
máshova vinné a lejátszást, a bevált egységet pedig elfelejtené –, hanem később
újrapróbálja ugyanazzal.

### Ha akadozik a lejátszás

A készülék `PLAYING`-et jelenthet úgy is, hogy a kép valójában áll: a fájlt nem
kapja meg elég gyorsan. Az app ezt figyeli, de **nem egyetlen leolvasásból**:
a készülékek egész másodperceket jelentenek, 1,2 másodperces lekérdezés mellett
tehát hol 1, hol 2 másodpercet lépnek, ami önmagában is 0,6-os arányt adhat
hibátlan kép mellett. Ezért tízmásodperces ablakban méri, mennyi videó ment le
mennyi valós idő alatt. Egy hibátlan, nyolcperces felvételen visszajátszva a
legrosszabb ablak aránya `0,93` volt – a küszöb `0,5`.

Van készülék, amelyik **egyáltalán nem jelent pozíciót** (`RelTime =
NOT_IMPLEMENTED`). Ott a „nem haladt" örökké igaz lenne, és az app percenként
riasztana hibátlan lejátszás közben – ezért csak akkor mér, ha a készülék az
adott elemhez mutatott már nem nulla állást.

Ha akad, a napló nem csak annyit mond, hogy „akad", hanem azt is, mennyit kért
és kapott közben a készülék:

```
  figyelem  akadozik a lejátszás: 3 mp videó 10 mp alatt - közben 61 kérés, 168 MB
```

Ez a két szám dönti el, mi a baj: sok kérés és sok bájt mellett a készülék
megkapta az adatot, de nem tudta feldolgozni; kevés bájt mellett a hálózat
vagy a lemez nem bírta.

Érdemes tudni, mennyi tartalék van: a referenciakészülék egy ~1,5 MB/mp-es
videóhoz **~16 MB/mp-ot kér**, másodpercenként hat `Range`-kéréssel, és a kapott
adat nagy részét eldobja. Ez a készülék dolga, nem a szerveré – kimérve az is,
hogy ha a nyitott végű `Range`-re rövidebb választ adunk, a lejátszás
**összeomlik**, tehát nem szabad megpróbálni. A tartalék viszont ennyivel
kisebb: terhelt hálózaton ez az elsőként elfogyó erőforrás.

### Melyik elemről szól a válasz

Elemváltáskor a készülék **még másodpercekig az előző fájl állását jelenti**.
Valódi készüléken kimérve, közvetlenül egy váltás után:

```
váltás E09-re
  a TV jelenti: RelTime=00:15:36   ← ez még az ELŐZŐ rész
  a TV jelenti: RelTime=00:00:00   ← innentől az új
```

A kettő közti esést az app korábban úgy értelmezte, hogy a készülék magától
visszaugrott a fájl elejére, és „visszatekerte" – az **új** részt a **régi**
rész állására. Nem kell találgatni: a `GetPositionInfo` a `TrackURI`-ban
megmondja, melyik fájlnál tart a készülék. Ha az nem az, amit elindítottunk, a
leolvasásból egyetlen mezőt sem szabad felhasználni.

Az összehasonlítás nem lehet nyers szövegegyezés: a válasz XML, tehát a
tokenes URL `&` jele `&amp;`-ként érkezik, és van készülék, amelyik újra is
kódolja az útvonalat. Az app mindkettőt feloldja, mielőtt döntene. A **gépnév
pedig egyáltalán nem azonosít** – van készülék, amelyik a kapott címet átírja –,
ezért a döntés az útvonalra és a lekérdezésre épül: a fájlt a `path=` paraméter
jelöli ki.

Ha a készülék egyáltalán nem árulja el a `TrackURI`-t, marad az idő: a váltás
utáni néhány másodpercben az app nem gyanakszik újraindulásra.

Az eltérést viszont **csak néhány másodpercig magyarázza az átállás**. Ha a
készüléken közben másik bemenetre vagy alkalmazásra váltottak, az eltérés
tartós – ilyenkor az app a leolvasást továbbra sem használja fel, de nem is
hallgat róla: „lejátszás" felirat alatt befagyott pozíciót mutatni ugyanaz a
néma hiba lenne, ami ellen az egész app épült.

### Némítás

A referenciakészüléken a némítás **egyirányú**: bekapcsolni lehet, kikapcsolni
nem. Kimérve, ismert állapotból indulva:

```
SetMute 1                    →  GetMute = 1     ✓ végrehajtja
SetMute 0                    →  GetMute = 1     ✗ nem old fel
SetMute 0 ×3                 →  GetMute = 1
SelectPreset FactoryDefaults →  GetMute = 1
SetVolume 0 → SetVolume 30   →  GetMute = 1
```

A `GetMute` egyébként igazat mond: a távirányítóval bekapcsolt némítást is
helyesen jelenti, és feloldás után `0`-ra vált. Csak a DLNA-n keresztüli
feloldás hiányzik.

Ebből egy szabály következik, ami nem csak a némításra igaz:

> **Olyan parancsot nem adunk ki, amit nem tudunk visszavonni.**

Ezért az app **soha nem kapcsolja be a készülék némítását.** A némítás a
hangerőn keresztül történik: elmenti az aktuális hangerőt, nullára állítja,
feloldáskor visszaadja. Ez visszaolvasható, ellenőrizhető, és nem tud
beragadni. (Ha a készülék egyáltalán nem hirdet `RenderingControl`
szolgáltatást, sem a hangerő, sem a némítás nem érhető el rajta – ilyenkor az
app ezt megmondja, ahelyett hogy úgy tenne, mintha sikerült volna.)

A visszaadás **pontos, és soha nem talál ki értéket**:

- ha némításkor 12 volt a hangerő, feloldáskor 12 lesz;
- ha némításkor **nulla** volt, feloldáskor is nulla marad – nem ugrik fel egy
  alapértékre. Nem volt mit elnémítani, tehát nincs mit visszaadni;
- ha a némítás nem az apptól származik (a távirányítóról vagy egy másik
  programból), a feloldás **hozzá sem nyúl a hangerőhöz** – nincs elmentett
  érték, amit vissza lehetne állítani, és a meglévőt elrontani nem szabad.

A készülék saját némítását – amit a távirányítóról vagy egy másik alkalmazásból
kapcsoltak be – feloldáskor megpróbálja megszüntetni (`SetMute 0`). Ha a
készülék erre sem reagál, **kiírja**, hogy a némítás csak a távirányítóval
szüntethető meg, ahelyett hogy csendben a hangerőt állítgatná tovább.

A csúszka némítás alatt nullát mutat – ez nem hiba, hanem az igazság. A
nullánál nagyobb hangerő beállítása magától feloldja az app saját némítását,
egyetlen kérésből – a készülék némítását viszont nem, tehát ha az áll, a
felület továbbra is jogosan mutat némítást.

A `muted` mező csak azt jelenti, amit a készülék némításnak vall, illetve amit
az app maga kapcsolt be. A nulla hangerő önmagában **nem** számít némításnak:
ha annak vennénk, a feloldás gomb olyat ígérne, amit nem tud teljesíteni.
Fordítva viszont igaz: ha a készülék némítását nem sikerült feloldani, az app
**továbbra is némítottnak mutatja magát**, mert a hang tényleg nincs meg.

Az app megjegyzi, hogy a némítás tőle származik-e. Ez három dolgot old meg:
egy ismételt némítás nem írja felül a már elmentett hangerőt a nullával;
ugyanannak a készüléknek az újraválasztása nem felejti el a saját némításunkat;
és egy idegen eredetű némítás feloldásakor nem nyúlunk a hangerőhöz.

### Hangerő-visszaolvasás

Az app elfogadja a készülék által jelentett `0..100` értéket. A `0` valós érték
(a némítás így valósul meg), nem „nem tudom". Közvetlenül egy hangerőváltás
után a készülék rövid ideig még a régi vagy nulla értéket jelentheti, ezért a
saját beállításunkat vesszük mérvadónak, és nem olvassuk vissza folyamatosan.

---

## Formátumok és átkódolás

A csatlakoztatott készülék `GetProtocolInfo` válasza dönti el, mi kap
figyelmeztető jelölést a fájllistában; a teljes lista a beállítások panelen
megnézhető. A referenciakészülék 37 formátumot fogad, köztük natívan az MKV-t és
az AVI-t – a legtöbb letöltött filmhez így semmilyen átalakítás nem kell.

Ha egy fájl mégsem szerepel a listán, és van `ffmpeg`, a szerver menet közben
MP4-re kódolja (`/api/stream`). Ilyenkor a tekerés korlátozott, mert a
konverzió folyamatosan halad, és nincs kész fájl, amiben ugrálni lehetne.

---

## Feliratok

Ha a videó mellett azonos nevű `.srt` van (`Film.mp4` + `Film.hu.srt`), az app
átadja a készüléknek a DIDL-metaadatban, és a listában `CC` jelöli. A `.srt`
menet közben WebVTT-re konvertálódik a `/api/sub` végponton.

A külső feliratot minden gyártó máshogy kezeli, és sok készüléken nem
megbízható. Ha nem jelenik meg, a fájlba ágyazott feliratsáv a biztos megoldás.

**Időbélyegek.** A WebVTT tizedespontot vár vessző helyett, és az óramezőt
legalább két számjegyen. A `0:00:01,000` alak – amit több feliratszerkesztő is
így ír ki – mindkettőt sérti, és a lejátszó ilyenkor **némán, hibaüzenet nélkül
elhagyja az egész feliratot**. Az app ezért az időzítő sorokat átírja, az órát
kiegészíti, az ezredmásodpercet feltölti. Csak a `-->` jelet tartalmazó sorokat
bántja: a feliratszövegben is állhat időbélyegnek látszó szám.

**Kódolás.** A magyar feliratok ritkán UTF-8-ak. Az app előbb a bájtsorrend-jelet
(BOM) nézi meg, és csak utána próbálkozik sorban: `utf-8` → `cp1250` →
`iso-8859-2`. A sorrend nem cserélhető fel, és a BOM-vizsgálat sem hagyható ki:
a `cp1250` szinte minden bájtsort elfogad, tehát egy UTF-16-os fájlt is
„sikeresen" dekódolna – csupa NUL-lal tűzdelt, olvashatatlan szöveggé. (A
Windows Jegyzettömb „Unicode" mentése pontosan ilyen fájlt ad.)

---

## Biztonsági modell

A szerver a gép minden hálózati felületén figyel, mert a TV-nek el kell érnie a
fájlokat. Ezért indításkor véletlen tokent kap: token nélkül egyetlen API-hívás
sem megy át, tehát a hálózat többi gépe nem tudja a fájlokat átböngészni. A
token a megnyitott URL-ben van – a linket ne oszd meg.

- **A tallózás a `--root` alatti fára korlátozódik.** Az utak feloldva
  (`realpath`) és `commonpath`-tal ellenőrizve vannak, tehát sem a `..`, sem egy
  kifelé mutató symlink nem visz ki belőle.
- **Az SSDP-válaszok szűrve vannak.** Egy készülék csak a saját címét
  hirdetheti: a válaszban megadott `LOCATION` hosztjának egyeznie kell a
  válaszoló IP-címével. Enélkül a hálózat bármelyik gépe rávehetné az appot,
  hogy egy tetszőleges címre indítson kérést a nevünkben. A loopback és a
  link-local címek ki vannak zárva, és a felderítés legfeljebb 32 leírót tölt le.
- **A hibaüzenetek nem szivárogtatnak.** A kivételek részletei a konzolra
  mennek; a HTTP-válasz általános mondatot ad, hogy abszolút utak és belső
  adatok ne kerüljenek ki a hálózatra.
- **A készülékek szövege sem jut ki nyersen.** A UPnP-hibakódok magyar mondattá
  fordulnak; a készülék által küldött, tetszőleges hosszú és nyelvű leírás csak
  a naplóba kerül.

---

## Hordozhatóság

Az app mappája bárhová másolható, és mindent visz magával. A saját mappáján
kívülre semmit nem ír:

- nem használ böngészőoldali tárolót (`localStorage`);
- nem hoz létre `__pycache__`-t (`sys.dont_write_bytecode`);
- nem ír `/tmp`-be vagy a home könyvtárba;
- nem telepít semmit, és nem igényel internetet.

Az egyetlen írt fájl a `data/state.json`, ami az első mentéskor jön létre.

### Operációs rendszerek

Egy kódbázis, három rendszer: nincs benne fordítandó rész, és nincs olyan
hívás, ami csak POSIX-on létezik (`fcntl`, `pwd`, `os.fork`, `SIGKILL`).
Az útvonalakat végig az `os.path` kezeli, a felület pedig a `\` és a `/`
elválasztót egyaránt érti.

Amit a rendszerek eltérése miatt külön kezel az app:

| eltérés | hol jönne elő | mit csinál |
|---|---|---|
| a hálózati kártyák lekérdezése | Windowson nincs `ifconfig` | a saját címet route-lekérdezéssel állapítja meg, a külső parancs csak tartalék |
| a konzol kódlapja | Windowson tipikusan `cp1252`, amiben nincs `ő` | induláskor UTF-8-ra állítja a kimenetet – enélkül csőbe irányítva **némán eltűnnének** az ékezetes sorok, köztük a megnyitandó cím |
| meghajtóbetűk | `--data` másik meghajtón, mint az app | a kiíráshoz nem erőlteti a relatív utat (`relpath` más meghajtóra kivételt dob) |
| `SO_REUSEPORT` | Windowson nincs ilyen socket-beállítás | a `devtools/faketv.py` az `AttributeError`-t is elkapja |

Amit **nem** kezel, és Windowson tudni kell róla:

- **Tűzfal.** Az első indításkor a Windows Defender rákérdez a bejövő
  kapcsolatra. Engedélyezni kell (magánhálózatra elég), különben a TV nem
  tudja letölteni a fájlt, és a felület sem jön be másik gépről.
- **Másik meghajtó.** A gyökér alapból a home könyvtár (`C:\Users\...`), és az
  app azon kívülre nem enged. Ha a filmek `D:`-n vannak, indításkor meg kell
  adni: `py -3 server.py --root D:\Filmek`.
- **Rejtett fájlok.** Az app a `.`-tal kezdődő neveket rejti el; a Windows
  rejtett *attribútumát* nem nézi, az ilyen fájlok látszanak.
- **Hibaüzenetek.** A hálózati hibák szövegét a rendszer hibakódjából
  fordítja; a Windows kódjait (10060, 10061) nem ismeri fel egyenként, ezért
  ott az általános „Nem sikerült elérni a TV-t" üzenet jön.

A fenti pontok macOS-en lettek megmérve úgy, hogy a hiányzó darabot elvettük
(kódlap, meghajtó, socket-beállítás) – **valódi Windowson nem futott próba.**
A `devtools/apitest.py` mindhárom esetet ellenőrzi, rendszertől függetlenül.

### Leállítás és a készülék puffere

`Ctrl+C` azonnal leállítja az appot, és alapból a készüléken is megállítja a
lejátszást. Ez azért kell, mert a készülékek nem valós időben streamelnek, hanem
előre letöltik, amit tudnak: egy kisebb fájl a szerver nélkül is végigjátszódna,
egy több gigabájtos filmnél viszont a puffer elfogyna, és a kép megfagyna egy
véletlenszerű ponton.

A `--keep-playing` kapcsolóval a pufferelt rész lemehet. Kilépés előtt az app
ellenőrzi (`GetMediaInfo`), hogy tényleg az általa indított tartalom megy-e: ha
közben a készüléken másra váltottak, nem nyúl hozzá. Az összehasonlítás itt is
az útvonalra és a lekérdezésre épül, nem nyers szövegegyezésre - a válasz XML,
tehát a tokenes URL `&` jele `&amp;`-ként érkezik, és nyersen összevetve
alapbeállítással **soha** nem egyezne, vagyis kilépéskor sosem állna meg a kép.

---

## Hibakeresés

Az app kimondja, mi a baj, ahelyett hogy némán töltene:

| Tünet | Mit mutat |
|---|---|
| nincs DLNA-készülék a hálózaton | piros sáv, és a küldés gomb letiltva marad |
| a készülék eltűnik lejátszás közben | három sikertelen lekérdezés után „nem válaszol" |
| a fájl nem indul el 30 másodpercen belül | hibaüzenet a fájl nevével, nem végtelen „betöltés" |
| megszakad a kapcsolat a szerverrel | három sikertelen lekérdezés után piros fejléc |
| a szerver újraindult (új token) | külön üzenet, hogy a terminálban új cím látható |
| az állapot mentése nem sikerül | hibaüzenet, ahelyett hogy a sor némán elveszne |
| a készülék némítása nem oldható fel | üzenet, hogy a távirányítót kell használni |
| áll a kép, pedig a készülék lejátszást jelent | üzenet a felületen, és a naplóban a kiszolgált kérés/bájt is |
| a készüléken másra váltottak | néhány másodperc után üzenet, nem befagyott „lejátszás" |

**Ha a készülék elfogadja a parancsokat, le is tölti a fájlt, mégsem indul el:**
egyes készülékek hosszabb használat után ilyen állapotba kerülnek – válaszolnak
az SSDP-re és a SOAP-hívásokra, sőt a médiát is lekérik, de a lejátszást nem
kezdik meg. Ilyenkor a készülék hálózati újraindítása segít (a menüből indított
újraindítás gyakran nem elég). Ez nem az app hibája: ugyanígy viselkedik minden
DLNA-vezérlővel.

**Ha a készülék nem látszik:** először azt ellenőrizd, be van-e kapcsolva.
Vigyázat: sok TV mély alvásban sem válaszol `ping`-re, miközben a DLNA-ja él –
és fordítva is előfordul. A megbízható próba a felderítés maga, nem a `ping`.

Sok készülék **minden újraindulásnál új UDN-t kap**. Az app ezért, ha a mentett
azonosítót nem találja, az elsőként megtalált készülékre esik vissza.

---

## Önellenőrzés

A `devtools/` mappában négy eszköz van; az app működéséhez egyik sem kell, és a
mappa törölhető. Részletek: [`devtools/README.md`](devtools/README.md).

```bash
python3 devtools/logictest.py          # 51 ellenőrzés, TV nélkül
python3 devtools/apitest.py            # 30 ellenőrzés, TV nélkül
python3 devtools/faketv.py --port 8475 # hamis DLNA-készülék a hálózatra
python3 devtools/selftest.py <mappa>   # 16 ellenőrzés valódi készülékkel
```

---

## Fájlok

A használathoz ez a hat dolog kell:

| | |
|---|---|
| `server.py` | HTTP-kiszolgáló: fájllista, Range-streamelés, SRT→VTT, hosszmérés, DLNA API |
| `dlna.py` | UPnP-réteg: SSDP-felderítés, SOAP-vezérlés, DIDL-metaadat, sorléptetés |
| `public/index.html` | a teljes kezelőfelület (HTML + CSS + JS egy fájlban) |
| `start.command` | dupla kattintásos indító macOS-re és Linuxra |
| `start.bat` | dupla kattintásos indító Windowsra |
| `data/state.json` | az egyetlen fájl, amit az app ír (futás közben jön létre) |
