# Fejlesztői eszközök

Ezek a fájlok az app **működéséhez nem kellenek** – a `cast-studio` mappa
nélkülük is teljes értékű. Csak akkor van rájuk szükség, ha az appon
változtatsz, és le akarod ellenőrizni, hogy nem rontottál el semmit.

Mindegyik az app mappájából indítandó:

```bash
python3 devtools/logictest.py
python3 devtools/apitest.py
python3 devtools/selftest.py ~/Downloads/Sorozat-mappa
python3 devtools/faketv.py --port 8475
```

| fájl | mit csinál | kell hozzá TV? |
|---|---|---|
| `logictest.py` | 21 ellenőrzés: az állapotgép, a folytatás és a versenyhelyzetek utánzott készüléken, gyorsított órával | nem |
| `apitest.py` | 18 ellenőrzés: útvonalak, hibás bemenetek, párhuzamos állapotmentés | nem |
| `faketv.py` | hamis DLNA-készülék a hálózaton; futás közben átállítható, hogy a valódi TV-k hibáit utánozza | nem |
| `selftest.py` | 14 ellenőrzés: a teljes lánc a valódi TV-vel és a valódi médiafájloddal | **igen** |

Egyik sem nyúl a saját beállításaidhoz: a szervert külön állapotmappával
indítják (`server.py --data`), és ideiglenes gyökérrel dolgoznak.

A részletes leírás az app `README.md`-jének „Önellenőrzés" fejezetében van.
