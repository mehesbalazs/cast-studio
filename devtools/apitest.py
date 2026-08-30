#!/usr/bin/env python3
"""A HTTP-réteg önellenőrzése: útvonalak, hibás bemenetek, párhuzamos mentés.

TV nem kell hozzá, és a saját beállításaidhoz sem nyúl: a szervert külön
állapotmappával indítja (--data), ideiglenes gyökérrel.

Használat:
    python3 devtools/apitest.py
"""

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request


def konzol_utf8():
    """Windowson a kodlap tipikusan cp1252, amiben nincs 'o' kettos ekezettel:
    a magyar kiiras csobe iranyitva UnicodeEncodeError-t dobna."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding='utf-8', errors='replace')
        except (AttributeError, ValueError, OSError):
            pass


konzol_utf8()

# Az app egy szinttel feljebb van: ez a mappa csak a fejlesztői eszközöké.
APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PORT = 8479
FAKE_UDN = 'uuid:faketv-0000-0000-0000-000000000001'
OK, BAD = [], []


def say(ok, label, detail=''):
    (OK if ok else BAD).append(label)
    print('  [%s] %-46s %s' % ('OK ' if ok else 'HIBA', label, detail))


def req(path, body=None, timeout=20):
    url = 'http://127.0.0.1:%d%s' % (PORT, path)
    r = urllib.request.Request(
        url, data=json.dumps(body).encode() if body is not None else None,
        headers={'Content-Type': 'application/json'} if body is not None else {})
    try:
        with urllib.request.urlopen(r, timeout=timeout) as x:
            return x.status, x.read().decode('utf-8', 'replace')
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode('utf-8', 'replace')
    except Exception as e:
        return 0, str(e)


def nyers(kérés, timeout=5):
    """Nyers bájtok a socketre - a hibás kérés-sorok próbájához."""
    s = socket.create_connection(('127.0.0.1', PORT), 5)
    s.settimeout(timeout)
    s.sendall(kérés)
    data = b''
    try:
        while True:
            c = s.recv(4096)
            if not c:
                break
            data += c
    except socket.timeout:
        pass
    s.close()
    return data


def platform_probak():
    """Windowsra jellemző buktatók, itthonról is mérhetően.

    Windows nem kell hozzá: mindhárom hibát elő lehet idézni azzal, hogy a
    hiányzó darabot elvesszük (kódlap, meghajtó, socket-beállítás).
    """
    # -- nem UTF-8 kódlap (Windowson tipikusan cp1252) -----------------
    # A súgó ékezetes; ha a kiírás elhasal rajta, ez látszik a kilépőkódon.
    kornyezet = dict(os.environ, PYTHONIOENCODING='cp1252')
    p = subprocess.run([sys.executable, os.path.join(APP, 'server.py'), '--help'],
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                       env=kornyezet, timeout=30)
    kimenet = p.stdout.decode('utf-8', 'replace')
    say(p.returncode == 0 and 'böngészhető' in kimenet,
        'nem UTF-8 kódlapon sem vész el az ékezetes kiírás',
        'kilépőkód %d' % p.returncode)

    sys.path.insert(0, APP)
    import server                                            # noqa: E402

    # -- az állapotfájl másik meghajtón (Windowson C: és D:) -----------
    eredeti = os.path.relpath

    def masik_meghajto(*a, **k):
        raise ValueError("path is on mount 'D:', start on mount 'C:'")

    os.path.relpath = masik_meghajto
    try:
        ut = server.rovid_ut('D:\\adat\\state.json', 'C:\\cast-studio')
        rendben, reszlet = ut == 'D:\\adat\\state.json', ut
    except ValueError as e:
        rendben, reszlet = False, str(e)
    finally:
        os.path.relpath = eredeti
    say(rendben, 'más meghajtón lévő állapotfájl nem állítja meg az indulást',
        reszlet)

    # -- SO_REUSEPORT hiánya (Windowson nincs ilyen socket-beállítás) --
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import faketv                                            # noqa: E402
    # Windowson eleve nincs ilyen attribútum: ott elvenni sem kell (és nem is
    # lehet - a mentés maga szállna el).
    mentett = getattr(socket, 'SO_REUSEPORT', None)
    if mentett is not None:
        del socket.SO_REUSEPORT
    allj = threading.Event()
    allj.set()                          # azonnal álljon meg, csak az indulás kell
    try:
        faketv.ssdp_server('127.0.0.1', 8478, 'uuid:proba', 'proba', allj)
        rendben, reszlet = True, ''
    except AttributeError as e:
        rendben, reszlet = False, str(e)
    finally:
        if mentett is not None:
            socket.SO_REUSEPORT = mentett
    say(rendben, 'a hamis TV SO_REUSEPORT nélkül is elindul', reszlet)

    # -- a kiszolgált forgalom számlálása (akadozáskor ez adja a bizonyítékot)
    server.MEDIA_STATS.clear()
    server.media_served(1048576)
    server.media_served(2 * 1048576)
    kerés, mb = server.media_rate(10)
    say(kerés == 2 and abs(mb - 3.0) < 0.01,
        'a kiszolgált forgalom számlálása pontos', '%d kérés, %.2f MB' % (kerés, mb))

    server.MEDIA_STATS.clear()
    server.MEDIA_STATS.append((time.time() - 60, 10 * 1048576))   # régi
    server.media_served(1048576)
    kerés, mb = server.media_rate(10)
    say(kerés == 1 and abs(mb - 1.0) < 0.01,
        'a régi forgalom kiesik a mérési ablakból', '%d kérés, %.2f MB' % (kerés, mb))


def main():
    root = tempfile.mkdtemp()
    adat = tempfile.mkdtemp()
    with open(os.path.join(root, 'ok.mp4'), 'wb') as fh:
        fh.write(b'x' * 5000)
    tilos = os.path.join(root, 'tilos.mp4')
    with open(tilos, 'wb') as fh:
        fh.write(b'y' * 4096)
    os.chmod(tilos, 0)
    ures = os.path.join(root, 'ures.mp4')
    open(ures, 'wb').close()
    # Ahogy a Notepad menti "Unicode"-ként: a cp1250 ezt is "sikeresen"
    # dekódolná, csupa NUL-lal tűzdelt olvashatatlan szöveggé.
    utf16 = os.path.join(root, 'felirat.srt')
    with open(utf16, 'wb') as fh:
        fh.write('1\n00:00:01,000 --> 00:00:02,000\nSzia, világ!\n'.encode('utf-16'))

    print('\n  A HTTP-réteg önellenőrzése (TV nélkül)\n')
    # A lejátszási sor ellenőrzései csak akkor érnek valamit, ha VAN kiválasztott
    # készülék: enélkül a szerver 409-cel visszafordul a validáció ELŐTT, és a
    # próbák némán semmit sem bizonyítanak. Ezért a hamis TV-t indítjuk mellé.
    # Rögzített UDN-nel választjuk ki, hogy egy valódi készülékhez véletlenül
    # se nyúljunk hozzá, ha épp be van kapcsolva a hálózaton.
    hamis = subprocess.Popen(
        [sys.executable, '-u', os.path.join(APP, 'devtools', 'faketv.py'),
         '--port', '8477', '--udn', FAKE_UDN, '--no-fetch'],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    srv = subprocess.Popen(
        [sys.executable, '-u', os.path.join(APP, 'server.py'),
         '--root', root, '--port', str(PORT), '--no-token', '--no-open',
         '--verbose', '--data', adat],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    q = urllib.parse.quote
    try:
        time.sleep(3.0)
        say(req('/api/info')[0] == 200, 'a szerver válaszol')

        req('/api/dlna/discover?timeout=6', timeout=40)
        c, _ = req('/api/dlna/select?udn=' + q(FAKE_UDN, safe=''), timeout=30)
        say(c == 200, 'a hamis TV kiválasztható (enélkül a sor-próbák vakok)',
            'HTTP %s' % c)

        # -- fájlkiszolgálás -------------------------------------------
        say(req('/api/media?path=' + q(tilos, safe=''))[0] == 404,
            'olvashatatlan fájl 404, nem üres 200')
        c, _ = req('/api/media?path=' + q(ures, safe=''))
        say(c == 200, 'nulla bájtos fájl kiszolgálható', 'HTTP %s' % c)
        say(req('/api/media?path=' + q('/etc/passwd', safe=''))[0] == 404,
            'gyökéren kívülre nem enged')
        say(req('/')[0] == 200 and req('/index.html')[0] == 200,
            'a felület kiszolgálható')

        # -- hibás bemenetek: 4xx, ne 500 ------------------------------
        rossz = [('tömb törzs', '/api/dlna/queue', [1, 2, 3]),
                 ('szöveg törzs', '/api/dlna/queue', 'nem objektum'),
                 ('items nem lista', '/api/dlna/queue', {'items': 'x'}),
                 ('szemét elemek', '/api/dlna/queue', {'items': [None, 1, 'x']}),
                 ('rossz subs', '/api/dlna/queue',
                  {'items': [{'path': os.path.join(root, 'ok.mp4'), 'subs': [7]}]}),
                 # A cím a DIDL-metaadatba kerül, ahol XML-escape megy rá:
                 # nem sztringre AttributeError, és 500-as lenne a válasz.
                 ('objektum cím', '/api/dlna/queue',
                  {'items': [{'path': os.path.join(root, 'ok.mp4'),
                              'title': {'x': 1}}]}),
                 ('lista cím', '/api/dlna/queue',
                  {'items': [{'path': os.path.join(root, 'ok.mp4'),
                              'title': [1, 2]}]})]
        baj = [n for n, u, b in rossz if req(u, b)[0] >= 500]
        say(not baj, 'hibás lejátszási sor nem szerverhiba', ', '.join(baj) or 'mind 4xx')

        # Ha mégis átcsúszna valami, a válasz akkor sem tartalmazhat abszolút
        # utat vagy kivételszöveget: az a konzolra való.
        szivargas = [n for n, u, b in rossz
                     for st, d in [req(u, b)]
                     if st >= 500 or '/Users' in d or 'Error' in d]
        say(not szivargas, 'a hibaválasz nem szivárogtat belső részletet',
            ', '.join(szivargas) or 'egyik sem')

        szamok = ['/api/dlna/volume?level=inf', '/api/dlna/volume?level=1e400',
                  '/api/dlna/volume?level=abc', '/api/dlna/seek?to=nan',
                  '/api/dlna/seek?to=inf', '/api/dlna/seek?to=-5',
                  '/api/dlna/play?index=999999']
        baj = [u for u in szamok if req(u)[0] >= 500]
        say(not baj, 'képtelen számértékek nem szerverhibák', ', '.join(baj) or 'mind 4xx')

        # -- hibás kérés-sor ne öljön kapcsolatot ----------------------
        for probe in (b'SZEMET\r\n\r\n', b'GET / HTTP/9.9\r\n\r\n'):
            nyers(probe)
        say(req('/api/info')[0] == 200, 'hibás kérés után is kiszolgál')

        d = nyers(b'POST /api/state HTTP/1.1\r\nHost: x\r\n'
                  b'Transfer-Encoding: chunked\r\n\r\n1a\r\n'
                  b'{"settings":{"volume":99}}\r\n0\r\n\r\n')
        say(b'411' in d, 'darabolt törzset nem nyel el némán', d[:24].decode('latin1'))

        # -- állapot: párhuzamos mentés ne vesszen el ------------------
        req('/api/state', {'queue': [{'path': '/x/%d.mkv' % i} for i in range(16)]})
        hiba = []

        def ir(i):
            c, _ = req('/api/state',
                       {'positions': {'p%02d' % i: {'pos': i + 30, 'dur': 100, 'at': i}}})
            if c != 200:
                hiba.append(c)

        szalak = [threading.Thread(target=ir, args=(i,)) for i in range(25)]
        for t in szalak:
            t.start()
        for t in szalak:
            t.join(timeout=30)
        st = json.loads(req('/api/state')[1])
        say(len(st['positions']) == 25 and not hiba,
            'párhuzamos mentésből semmi nem vész el',
            '%d / 25' % len(st['positions']))
        say(len(st['queue']) == 16, 'a lejátszási sor sértetlen marad')

        # -- hibás tartalom ne bénítsa meg a mentést -------------------
        req('/api/state', {'positions': {'x%03d' % i: i for i in range(510)}})
        c, b = req('/api/state', {'positions': {'jó': {'pos': 40, 'dur': 100, 'at': 1}}})
        say(c == 200 and '"jó"' in b, 'hibás pozícióérték után is ment', 'HTTP %s' % c)

        req('/api/state', {'settings': {'sz%d' % i: i for i in range(400)}})
        n = len(json.loads(req('/api/state')[1])['settings'])
        say(n <= 12, 'ismeretlen beállításkulcsok nem ragadnak be', '%d kulcs' % n)

        # -- két megnyitott lap ne írja felül egymást -------------------
        st = json.loads(req('/api/state')[1])
        rev = st.get('rev', 0)
        c1, _ = req('/api/state', {'rev': rev, 'queue': [{'path': '/a.mkv'}]})
        c2, b2 = req('/api/state', {'rev': rev, 'queue': [{'path': '/b.mkv'}]})
        say(c1 == 200 and c2 == 409,
            'elavult revízióval nem lehet felülírni a sort',
            'első %s, második %s' % (c1, c2))
        mostani = json.loads(req('/api/state')[1])
        say(len(mostani['queue']) == 1 and mostani['queue'][0]['path'] == '/a.mkv',
            'az elsőként mentett sor marad érvényben')

        elozo = mostani['rev']
        req('/api/state', {'positions': {'q': {'pos': 50, 'dur': 100, 'at': 2}}})
        say(json.loads(req('/api/state')[1])['rev'] == elozo,
            'a pozíciómentés nem avítja el a lapok revízióját')

        # -- felirat kódolása ------------------------------------------
        c, d = req('/api/sub?path=' + q(utf16, safe=''))
        say(c == 200 and 'Szia, világ!' in d and '\x00' not in d,
            'UTF-16-os felirat olvashatóan jön ki',
            'HTTP %s, %s' % (c, 'NUL a szövegben' if '\x00' in d else 'tiszta'))

        # -- forgalommérés: mindkét kiszolgáló út beleszámít -----------
        def forgalom():
            # Hiányzó kulcsra nullát adunk: egy elmaradt mező bukjon meg
            # ellenőrzésként, ne szakítsa félbe az egész futást.
            return json.loads(req('/api/info')[1]).get(
                'traffic', {'requests': 0, 'mb': 0.0})

        elotte = forgalom()
        req('/api/media?path=' + q(os.path.join(root, 'ok.mp4'), safe=''))
        utana = forgalom()
        say(utana['requests'] > elotte['requests'] and utana['mb'] >= elotte['mb'],
            'a kiszolgált fájl beleszámít a forgalomba',
            '%d -> %d kérés' % (elotte['requests'], utana['requests']))

        # Az átkódolt út külön kód, és eddig egyetlen bájtot sem jelentett:
        # akadozáskor "0 kérés, 0 MB" jött volna, ami az ellenkezőjére vezet.
        ffmpeg = shutil.which('ffmpeg')
        if ffmpeg:
            klip = os.path.join(root, 'proba.mp4')
            subprocess.run(
                [ffmpeg, '-y', '-hide_banner', '-loglevel', 'error',
                 '-f', 'lavfi', '-i', 'testsrc=size=64x64:rate=10:duration=2',
                 '-pix_fmt', 'yuv420p', klip],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)
            elotte = forgalom()
            c, _ = req('/api/stream?path=' + q(klip, safe='') + '&mode=remux')
            utana = forgalom()
            say(c == 200 and utana['mb'] > elotte['mb'],
                'az átkódolt adás is beleszámít a forgalomba',
                'HTTP %s, %.2f -> %.2f MB' % (c, elotte['mb'], utana['mb']))
        else:
            print('  [ -- ] az átkódolt adás mérése kihagyva: nincs ffmpeg')

        # -- eltűnő mappa ----------------------------------------------
        el = os.path.join(root, 'eltunik')
        os.makedirs(el, exist_ok=True)
        shutil.rmtree(el)
        say(req('/api/browse?path=' + q(el, safe=''))[0] == 404,
            'eltűnt mappa 404, nem 500')
    finally:
        hamis.terminate()
        try:
            hamis.wait(timeout=5)
        except subprocess.TimeoutExpired:
            hamis.kill()
        srv.terminate()
        try:
            napló = srv.communicate(timeout=10)[0].decode('utf-8', 'replace')
        except subprocess.TimeoutExpired:
            srv.kill()
            napló = ''
        os.chmod(tilos, 0o644)
        shutil.rmtree(root, ignore_errors=True)
        shutil.rmtree(adat, ignore_errors=True)

    say('Traceback' not in napló, 'egyetlen kivétel sem szállt el a naplóba')

    platform_probak()
    print('\n  %d rendben, %d hiba\n' % (len(OK), len(BAD)))
    if BAD:
        print('  megbukott: %s\n' % ', '.join(BAD))
    return 1 if BAD else 0


if __name__ == '__main__':
    sys.exit(main())
