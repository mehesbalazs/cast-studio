# Cast Studio

Helyi médialejátszó, ami a gépeden lévő fájlokat és mappákat küldi a TV-re **DLNA/UPnP**
protokollon. Nulla telepítés: csak a macOS-en eleve ott lévő Python 3 kell hozzá.

Kimérve és kipróbálva a hálózatodon lévő **Hisense VIDAA TV**-vel (10.0.0.237).

**Hordozható:** a mappa bárhová másolható (pendrive, másik gép), és mindent visz magával.
Az app a saját mappáján kívülre semmit nem ír — se rejtett könyvtárba, se a böngésző
tárolójába, se `__pycache__`-t.

## Indítás

A Finderből: dupla kattintás a `start.command` fájlra. Terminálból, az app mappájából:

```bash
python3 server.py
```

Jelenleg itt van: `~/cast-studio` — de bárhová átmozgatható.

A terminál kiírja a megnyitandó címet:

```
  Cast Studio  ·  helyi médialejátszó DLNA-s TV-hez
  ──────────────────────────────────────────────────────────────
  Felület           http://localhost:8420/?t=49d26d1b10613127da3a
  TV ezt látja      http://10.0.0.240:8420
  Gyökérkönyvtár    /home/felhasznalo
  Átkódolás         ffmpeg elérhető
  Állapotfájl       data/state.json

  A TV keresése az oldal megnyitásakor automatikus.
  Leállítás: Ctrl+C
```

A `--verbose` kapcsolóval látszik, mit kér le a TV — soronként idő, kliens,
státusz és a fájl neve, a nyers URL-ek helyett:

```
  07:41:51  10.0.0.237      GET  206  média      Egy-resz.mp4
  07:41:56  127.0.0.1       GET  404  média      passwd
```

A színezés csak valódi terminálban jelenik meg; naplófájlba írva tiszta szöveg marad.

Kapcsolók:

| Kapcsoló | Mit csinál |
|---|---|
| `--root /Volumes/Media` | melyik mappából lehet tallózni (alapértelmezés: a home könyvtárad) |
| `--port 8420` | melyik porton figyeljen |
| `--no-token` | ne kérjen tokent (csak megbízható hálózaton) |
| `--no-open` | ne nyissa meg magától a böngészőt |
| `--verbose` | kérésnaplózás (látszik, mit tölt le a TV) |
| `--keep-playing` | kilépéskor ne állítsa meg a TV-t |
| `--data /tmp/proba` | hova írja az állapotfájlt (teszteléshez) |

## Használat

1. Nyisd meg a kiírt `http://localhost:...` címet. Az app magától megkeresi a TV-t,
   és ki is választja — a fejlécben látod a nevét és az állapotát.
2. Bal oldalon tallózz: egy fájlra kattintva bekerül a sorba; a mappa sorára húzva
   az egérrel megjelenik egy `+` gomb, ami az egész mappát beteszi.
   Az **Almappákkal** pipával rekurzívan olvassa be.
3. A **Küldés a TV-re** gombbal indul a lejátszás.

Billentyűk: `szóköz` = lejátszás/szünet, `←`/`→` = 10 mp, `Shift+←`/`Shift+→` = előző/következő,
`/` = ugrás a keresőmezőre.

A lejátszási sor húzd-és-ejtsd módszerrel átrendezhető, és a böngésző bezárása után is megmarad.
Ha nincs TV kiválasztva, a sorra kattintva a böngészőben nézhető meg a fájl (helyi előnézet).

## Folytatás onnan, ahol abbahagytad

Lejátszás közben az app tízmásodpercenként megjegyzi, hol tartasz, és leállításkor
(illetve kilépéskor) is elmenti. Ha ugyanazt a fájlt később újraindítod, magától
odatekeri a TV-t — a lejátszási sorban kék jelölés mutatja a pontot, pl. `▸ 42:13`.

Két helyzetben szándékosan nem kínálja fel:

- az első 20 másodpercben (nincs mit folytatni);
- ha a vége előtti 45 másodpercen belül jártál — ilyenkor végignézettnek tekinti,
  és törli a pontot, tehát legközelebb elölről indul.

A pontok a `data/state.json` fájlban vannak, fájlonként legfeljebb 500 megjegyezve
(a legrégebbiek kiesnek). A fogaskerék alatt kikapcsolható.

### Miért nem elég egyszer odatekerni

A tekerés nem „kilövöm és elfelejtem" művelet. A készülék a fájl megnyitása közben
kétféleképpen is átejtheti az appot: vagy hibával utasítja vissza a parancsot, vagy
elfogadja, de nem mozdul rá. Mindkettő ugyanoda vezetne: a film elölről menne, és
tíz másodperc múlva a rendszeres mentés **felül is írná azt a pontot**, ahonnan
folytatni akartál — a megjegyzett hely tehát végleg elveszne.

Ezért az app megjegyzi a célt, és **ellenőrzi, hogy tényleg odaért-e**:

- amíg a folytatás nem ért célba, egyetlen új pontot sem ment el;
- három másodpercenként újrapróbálja, legfeljebb nyolcszor — a szünet nem
  számít bele, tehát egy hosszabb megállás nem szakítja félbe a folytatást;
- ha te magad tekersz közben, te veszed át az irányítást: a függőben lévő
  folytatás elmarad, nem ránt vissza a mentett pontra;

Van egy másik csapda is, amit mérve találtunk meg ezen a Hisense TV-n: ha
közvetlenül egy tekerés után szünetelteted, majd folytatod, a **készülék magától
visszaugorhat a film elejére**. Az app ezt észreveszi (a pozíció harminc
másodpercnél nagyobbat esik vissza anélkül, hogy kérted volna), és visszateker
oda, ahol tartottál. Közben egy pontot sem ment, hogy a nullához közeli állás ne
írhassa felül a valódit.
- ha végleg nem sikerül, kiírja, és a mentett pontot **akkor sem írja felül**,
  amíg a lejátszás túl nem jutott rajta. Így egy sikertelen folytatás után is
  megvan, ahova vissza akartál térni.

A megjegyzett hely nem csak leállításkor frissül, hanem akkor is, amikor másik
elemre váltasz, továbblépsz a sorban, vagy kilépsz az appból.

## Önellenőrzés

A `devtools/` mappában négy eszköz van, mert négyféle hibát fognak meg.
Az app működéséhez egyik sem kell.

**Logikai teszt – TV nélkül, pár perc:**

```bash
python3 devtools/logictest.py
```

Utánzott készüléken futtatja a valódi állapotgépet, tízszeres órával. Olyan
versenyhelyzeteket idéz elő, amiket élőben nem lehet parancsra előhívni: a TV
töltés közben visszautasítja a tekerést, elfogadja anélkül, hogy megmozdulna,
vagy magától visszaugrik a fájl elejére.

**Hamis TV a hálózaton – a teljes lánc, valódi készülék nélkül:**

```bash
python3 devtools/faketv.py --port 8475
```

Ez egy valódi hálózati DLNA-készüléknek látszik: válaszol az SSDP-keresésre,
eszközleírót ad, és kiszolgálja a SOAP-hívásokat. Az app ugyanúgy megtalálja és
vezérli, mint egy TV-t – tehát a felület is végigpróbálható anélkül, hogy a
nappaliban bekapcsolna a képernyő. Futás közben átállítható, hogy a valódi
készülékek hibáit utánozza:

```bash
curl 'http://127.0.0.1:8475/control?offline=1'        # a TV "eltűnik"
curl 'http://127.0.0.1:8475/control?seek_lockout=25'  # 25 mp-ig nem teker
curl 'http://127.0.0.1:8475/control?seek_ignore=1'    # elfogadja, de nem mozdul
curl 'http://127.0.0.1:8475/control?slow=1.3'         # lassú válaszok
curl 'http://127.0.0.1:8475/log'                      # mit kapott az apptól
```

**HTTP-réteg – TV nélkül, néhány másodperc:**

```bash
python3 devtools/apitest.py
```

Az útvonalakat, a hibás bemeneteket és a párhuzamos mentést méri. Külön
állapotmappával indítja a szervert, tehát a saját sorodhoz nem nyúl.

**Végponttól végpontig – a valódi TV-vel és a valódi fájloddal:**

```bash
python3 devtools/selftest.py ~/Videok/Sorozat
```

A mappa legnagyobb médiafájljával végigméri a teljes láncot: elindul-e a lejátszás
(és **tényleg nő-e a pozíció**, nem csak a TV mond PLAYING-et), működik-e a tekerés,
elmenti-e leállításkor a pozíciót, folytatja-e onnan, és nem vész-e el a mentett pont.

Szintetikus tesztklip helyett azzal dolgozik, amit ténylegesen nézel — a kettő máshogy
viselkedik méret, konténer és pufferelés szempontjából.

## Ha nem indul el semmi

Az app kimondja, mi a baj, ahelyett hogy némán töltene:

- **Megszakadt kapcsolat a szerverrel**: ha három egymást követő lekérdezés
  elbukik, a fejléc pirosra vált. Ha közben újraindítottad a `server.py`-t,
  külön kiírja, hogy új címet írt ki a terminál — a régi lap tokenje már
  nem érvényes.
- **Sikertelen mentés** (írásvédett mappa, tele lemez, zárolt pendrive):
  megjelenik a hiba, ahelyett hogy a sor némán elveszne a következő indításig.

- **Kikapcsolt vagy alvó TV**: indításkor piros sáv jelzi, hogy nem látszik DLNA-képes
  készülék, és a küldés gomb letiltva marad.
- **Lejátszás közben eltűnő TV**: három sikertelen lekérdezés után „nem válaszol"
  állapotra vált.
- **Elindított, de be nem induló fájl**: ha 30 másodpercen belül nem kezd játszani,
  hibaüzenetet ad a fájl nevével — nem pörög tovább a „betöltés" felirat.

Ha a TV nem látszik, először azt ellenőrizd, hogy be van-e kapcsolva: a Hisense mély
alvásban teljesen lekerül a hálózatról (ilyenkor a `ping` sem válaszol).

## Leállítás és a TV puffere

`Ctrl+C` azonnal leállítja az appot (mérve: 0–1 mp, lejátszás közben is), és
alapból **a TV-n is megállítja a filmet**.

Ez azért kell, mert a TV nem valós időben streamel, hanem előre letölti, amit tud.
Mérve: egy 46 MB-os fájlt 5 kérésben, ~26 mp alatt lehúzott, és utána a szerver
nélkül is végigjátszotta. Egy valódi, több gigabájtos filmnél viszont a puffer
elfogyna, és a kép **megfagyna egy véletlenszerű ponton** – ezért tisztább megállítani.

Ha mégis szeretnéd, hogy a pufferelt rész lemenjen, indítsd `--keep-playing`
kapcsolóval. Ha közben a TV-n másra váltottál, az app nem nyúl hozzá: kilépés előtt
ellenőrzi, hogy tényleg az általa indított felvétel megy-e.

## Hordozhatóság – mit ír a lemezre

Egyetlen fájlt, a saját mappájában:

```
cast-studio/data/state.json      beállítások, lejátszási sor, utolsó mappa, kiválasztott TV
```

A fájl az első mentéskor jön létre; törölheted, ilyenkor az app alapértelmezéssel indul.
Az írás atomi (`.tmp` + átnevezés), tehát félbeszakadt mentés nem hagy sérült fájlt.

Amit **nem** csinál:

- nem ír a böngésző `localStorage`-ába (a korábbi verzió maradványait indításkor törli is);
- nem hoz létre `__pycache__` mappát (`sys.dont_write_bytecode`);
- nem ír `/tmp`-be, a home könyvtárba vagy bárhová a mappáján kívül;
- nem telepít semmit, és nem igényel internetet.

Ellenőrizve: a mappát átmásolva egy másik helyre és onnan indítva a lejátszási sor,
a beállítások és a kiválasztott TV változatlanul jöttek vissza.

## Hogyan működik

A TV nem éri el a gépeden lévő fájlokat, és a böngészőből választott fájl `blob:` URL-jét
sem tudja megnyitni. Ezért a `server.py` három dolgot csinál:

- kiszolgálja a felületet a `localhost`-on;
- kiszolgálja a médiafájlokat a gép LAN IP-jén, HTTP Range támogatással, hogy a TV
  tudjon tekerni is (a Hisense ténylegesen 206-os részletkéréseket küld);
- **vezérli a TV-t**: SSDP-vel megkeresi, majd UPnP AVTransport SOAP-hívásokkal indítja,
  szünetelteti, tekeri. Ez azért a szerverben van, mert a böngésző nem tud nyers UDP
  multicastot küldeni, a TV pedig nem ad CORS-fejlécet a SOAP-válaszokhoz.

A lejátszási sort is a szerver lépteti: figyeli a TV állapotát, és amikor egy elem véget ér,
elindítja a következőt. A DLNA-ban nincs beépített sor fogalom, ezért kell ez a réteg.

## Formátumok

Az app nem tippel: a kiválasztott TV-től lekéri a `GetProtocolInfo`-t, és **a TV saját
listája** dönti el, mi kap figyelmeztető jelölést a fájllistában. A beállítások panelen
megnézheted a teljes listát.

A te Hisense TV-d 37 formátumot fogad, köztük natívan az **MKV-t és az AVI-t** is —
tehát a legtöbb letöltött filmhez semmilyen átalakítás nem kell. Ez tesztelve van:
egy MKV végigjátszódott rajta.

Ha egy fájl mégsem szerepel a TV listáján, a szerver menet közben átkódolja MP4-re
(`ffmpeg` 9.0.1 megvan a gépeden: `/opt/homebrew/bin/ffmpeg`). Ilyenkor a tekerés korlátozott.

## Tekerés más márkáknál

A DLNA három tekerési egységet ismer, és a gyártók eltérően valósítják meg őket. Az app
először megkérdezi a készüléket (`GetDeviceCapabilities`), de ha az — mint a Hisense —
nem árulja el, akkor sorra próbálja: `REL_TIME` → `ABS_TIME` → `X_DLNA_REL_BYTE`
(bájtpozíció a hosszból és a fájlméretből számolva). Amelyik átment, azt megjegyzi,
így utána egyből a jót használja.

## Amit tudni érdemes erről a TV-ről

Két dolog a Hisense DLNA-megvalósításának hiányossága, nem az appé:

- **A TV nem jelenti a médiahosszt** (`GetPositionInfo` mindig `00:00:00` hosszt ad).
  Ezt megkerüli az app: a hosszt helyben, `ffprobe`-bal állapítja meg, így a
  csúszka és a tekerés pontosan működik.
- **A hangerő-visszaolvasás nem működik**: a TV elfogadja a `SetVolume` hívást,
  de utána mindig 0-t jelent. A csúszka ezért nem feltétlenül tükrözi a valóságot —
  hangerőhöz a TV távirányítója a megbízható.
- **A `GetDeviceCapabilities` nem sorolja fel a tekerési módokat**, ezért az app
  kipróbálással deríti ki (nála a `REL_TIME` a nyerő).

## Feliratok

Ha a videó mellett van azonos nevű `.srt` (pl. `Film.mp4` + `Film.hu.srt`), az app
átadja a TV-nek a DIDL-metaadatban, és a listában `CC` jelöli. A külső feliratot viszont
minden gyártó máshogy kezeli, és a Hisense-nél ez nem megbízható — ha nem jelenik meg,
a fájlba ágyazott feliratsáv a biztos megoldás.

## Biztonság

A szerver a gép minden hálózati felületén figyel, mert a TV-nek el kell érnie.
Ezért indításkor kap egy véletlen tokent — token nélkül semmilyen API-hívás nem megy át,
tehát a hálózat többi gépe nem tudja a fájljaidat átböngészni. A token a megnyitott
URL-ben van, ezért a linket ne oszd meg. A tallózás a `--root` alatti fára korlátozódik;
a `..` és a symlinkek nem visznek ki belőle.

## Fájlok

A használathoz ez az öt dolog kell:

- `server.py` — HTTP-kiszolgáló: fájllista, Range-streamelés, SRT→VTT, hossz-mérés, DLNA API
- `dlna.py` — UPnP-réteg: SSDP-felderítés, SOAP-vezérlés, DIDL-metaadat, sorléptetés
- `public/index.html` — a teljes felület (HTML + CSS + JS egy fájlban)
- `start.command` — dupla kattintásos indító macOS-re
- `data/state.json` — az egyetlen dolog, amit az app ír (futás közben jön létre)

A `devtools/` mappa csak akkor kell, ha az appon **változtatsz**; a program
nélküle is teljes értékű, és nyugodtan törölhető:

- `devtools/logictest.py` — logikai önellenőrzés utánzott készüléken, TV nélkül
- `devtools/apitest.py` — a HTTP-réteg önellenőrzése (útvonalak, hibás bemenetek, párhuzamosság)
- `devtools/faketv.py` — hamis DLNA-készülék a hálózaton, a teljes lánc próbájához
- `devtools/selftest.py` — végponttól végpontig önellenőrzés valódi médiafájllal
- `devtools/README.md` — mikor melyiket érdemes futtatni

## Két megnyitott lap

Az állapotfájl revíziószámot visel. Ha két lapon nyitod meg a felületet, és az
egyiken átírod a sort, a másik nem söpri felül szó nélkül: szól, hogy közben
változott, és hogy töltsd újra. A megtekintési pontokat ez nem érinti — azok
összefésülődnek, mert a lejátszó és a felület más kulcsokat ír.

Az üzenetek (hibák, figyelmeztetések) azonosítót kapnak, és néhány másodpercig
lekérhetők maradnak: így mindkét lapon megjelennek, de egyiken sem kétszer.
