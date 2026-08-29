# Fejlesztői eszközök

Ezek a fájlok az app **működéséhez nem kellenek**: a szülőmappa nélkülük is
teljes értékű, és ez a mappa nyugodtan törölhető. Akkor van rájuk szükség, ha az
appon változtatsz, és le akarod ellenőrizni, hogy nem rontottál el semmit.

Mindegyik az app mappájából indítandó:

```bash
python3 devtools/logictest.py
python3 devtools/apitest.py
python3 devtools/faketv.py --port 8475
python3 devtools/selftest.py <médiamappa>
```

| eszköz | mit mér | kell hozzá készülék? |
|---|---|---|
| `logictest.py` | 42 ellenőrzés: állapotgép, folytatás, ütközések, némítás, elemváltás, akadozás | nem |
| `apitest.py` | 25 ellenőrzés: útvonalak, hibás bemenetek, párhuzamosság, platformfüggés, forgalommérés | nem |
| `faketv.py` | hamis DLNA-készülék a hálózaton | nem |
| `selftest.py` | 16 ellenőrzés: a teljes lánc valódi TV-vel és valódi fájllal | **igen** |

Egyik sem nyúl a saját beállításaidhoz: a szervert külön állapotmappával
indítják (`server.py --data`), ideiglenes gyökérrel.

---

## Miért négy eszköz

Négyféle hibaosztály van, és mindegyikhez más próba kell.

**Időzítési hibák.** A készülék betöltés közben visszautasíthatja a tekerést,
elfogadhatja anélkül, hogy megmozdulna, vagy magától visszaugorhat a fájl
elejére. Ezeket élőben nem lehet parancsra előhívni – utánzott készülék kell
hozzájuk (`logictest.py`).

**Bemeneti és párhuzamossági hibák.** Hibás JSON, képtelen számok, eltűnő
mappák, egyszerre érkező mentések. Ezekhez futó szerver kell, készülék nem
(`apitest.py`).

**Felületi hibák.** A kezelőfelület csak akkor működik, ha van kiválasztható
készülék – de a nappaliban nem kell ehhez bekapcsolni a TV-t (`faketv.py`).

**Valódi terhelés.** Egy több gigabájtos fájl máshogy viselkedik, mint egy
szintetikus tesztklip: más a méret, a konténer és a pufferelés. Ezt csak a
valódi láncon lehet megmérni (`selftest.py`).

---

## `logictest.py` – logikai önellenőrzés

Utánzott készüléken (`MockTV`) futtatja a valódi `dlna.Player` állapotgépet,
tízszeres órával, így egy több perces forgatókönyv néhány másodperc alatt lefut.
A SOAP-réteg van kicserélve; **maga a tesztelt kód változatlan**.

A készülék viselkedése forgatókönyvenként állítható:

| kapcsoló | mit utánoz |
|---|---|
| `seek_lockout=N` | a megadott pozícióig `701`-gyel utasítja vissza a tekerést |
| `seek_ignored=True` | elfogadja a tekerést, de nem mozdul |
| `seek_fail_first=N` | az első N tekerési kísérletet elutasítja |
| `restart_at=N` | a megadott pozíciónál magától visszaugrik a fájl elejére |
| `mute_stuck=True` | a némítást bekapcsolni tudja, kikapcsolni nem – a némítás beragad |
| `valtas_kesik=N` | elemváltás után N virtuális mp-ig még az előző fájl állását jelenti |
| `megall=True` | `PLAYING`-et jelent, de a kép áll – a hálózat nem viszi |
| `idegen_uri=…` | mást jelent, mint amit elindítottunk (a TV-n átváltottak) |
| `nincs_pozicio=True` | `RelTime = NOT_IMPLEMENTED`: sosem jelent pozíciót |

Amit lefed: folytatás háromféle készüléken · a mentett pont védelme ·
feladás és értesítés · felhasználói felülbírálás · váratlan újraindulás ·
egyidejű elemváltások · téves riasztások · tekerési egység megválasztása ·
némítás, és a beragadó némítás felismerése · hétköznapi utak (mentés menet közben,
elemváltás, léptetés, végignézett elem).

---

## `apitest.py` – a HTTP-réteg önellenőrzése

Elindítja a valódi `server.py`-t ideiglenes gyökérrel és külön
állapotmappával, majd kívülről méri:

- fájlkiszolgálás: olvashatatlan és nulla bájtos fájl, gyökéren kívüli út;
- hibás bemenetek: rossz típusú JSON-törzs, képtelen számok (`inf`, `nan`),
  hibás kérés-sor, darabolt (`chunked`) törzs – ezek egyike sem lehet `500`-as;
- állapotkezelés: 25 párhuzamos mentés veszteség nélkül, hibás pozícióérték
  utáni továbbműködés, ismeretlen beállításkulcsok kiszűrése;
- revíziószám: elavult revízióval nem lehet felülírni a sort, és a
  pozíciómentés nem avítja el a megnyitott lapok revízióját;
- eltűnő mappa `404`-et ad, nem `500`-at;
- forgalommérés: a kiszolgált fájl és az átkódolt adás is beleszámít;
- a futás végén a szerver naplójában egyetlen kivétel sem lehet;
- platformfüggés: nem UTF-8 kódlapon sem vész el ékezetes kiírás, más
  meghajtón lévő állapotfájl nem állítja meg az indulást, és a hamis TV
  `SO_REUSEPORT` nélkül is elindul. Windows nem kell hozzá: mindhárom hiba
  előidézhető azzal, hogy a hiányzó darabot elvesszük.

---

## `faketv.py` – hamis DLNA-készülék

Valódi hálózati eszköznek látszik: válaszol az SSDP M-SEARCH-re, kiszolgálja az
eszközleírót, és feldolgozza az AVTransport / RenderingControl /
ConnectionManager SOAP-hívásokat. Az app ugyanúgy megtalálja és vezérli, mint
egy TV-t – tehát a **kezelőfelület is végigpróbálható** anélkül, hogy a
nappaliban bekapcsolna a képernyő.

```bash
python3 devtools/faketv.py --port 8475 --rate 10 --media-duration 120
```

Futás közben átállítható, hogy a valódi készülékek hibáit utánozza:

```bash
curl 'http://127.0.0.1:8475/control?offline=1'          # a készülék "eltűnik"
curl 'http://127.0.0.1:8475/control?seek_lockout=25'    # 25 mp-ig 701-et ad
curl 'http://127.0.0.1:8475/control?seek_ignore=1'      # elfogadja, nem mozdul
curl 'http://127.0.0.1:8475/control?fail_action=Play'   # HTTP 500 arra a hívásra
curl 'http://127.0.0.1:8475/control?slow=1.3'           # lassú válaszok
curl 'http://127.0.0.1:8475/control?duration_zero=0'    # mégis jelentsen hosszt
curl 'http://127.0.0.1:8475/control?volume_zero=0'
```

Megfigyelés:

```bash
curl 'http://127.0.0.1:8475/log'      # mit kapott az apptól, JSON-ban
curl 'http://127.0.0.1:8475/state'    # a hamis készülék belső állapota
curl 'http://127.0.0.1:8475/log?clear=1'
```

Ha egyszerre van a hálózaton valódi TV és hamis készülék, a felderítés
mindkettőt felsorolja – teszteléskor **név vagy UDN szerint válaszd ki** a
hamisat, nehogy a valódi készüléket vezéreld.

---

## `selftest.py` – végponttól végpontig

A megadott mappa legnagyobb médiafájljával végigméri a teljes láncot valódi
készüléken. Szintetikus tesztklip helyett azzal dolgozik, amit ténylegesen
néznél.

```bash
python3 devtools/selftest.py ~/Videok/Sorozat
```

Amit ellenőriz: a lejátszás ténylegesen elindul-e (**és nő-e a pozíció**, nem
csak a készülék mond `PLAYING`-et) · tekerés · szünet, ismételt szünet,
folytatás · `Stop` utáni újraindítás · a pozíció mentése leállításkor ·
folytatás a mentett pontról · a mentett pont védelme · némítás és feloldás az
eredeti hangerő visszaadásával · elemváltáskor a régi elem pontja.

---

## Munkamódszer

Két szabály, amit érdemes betartani:

1. **A tesztnek buknia kell a javítás előtti kódon.** Egy ellenőrzés, ami a
   hibás változaton is átmegy, nem bizonyít semmit. Új ellenőrzés írásakor
   érdemes visszaállítani a hibát, és megnézni, hogy tényleg elbukik-e.

2. **A készülék válaszának hinni nem elég.** A DLNA-eszközök rendszeresen
   jelentenek sikert olyan műveletre, amit nem hajtottak végre. Minden állítást
   visszaolvasással kell alátámasztani – ez a projekt legtöbb hibája pontosan
   ebből fakadt.
