#!/usr/bin/env python3
"""Végponttól végpontig önellenőrzés VALÓDI médiafájllal.

Nem szintetikus klippel dolgozik, hanem azzal, amit tényleg nézel - mert a
kettő máshogy viselkedik (méret, konténer, pufferelés).

Használat:
    python3 devtools/selftest.py ~/Videok/Sorozat
"""

import json
import os
import shutil
import subprocess
import tempfile
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

# Az app egy szinttel feljebb van: ez a mappa csak a fejlesztői eszközöké.
APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PORT = 8499
PASS, FAIL = [], []


def say(ok, label, detail=''):
    (PASS if ok else FAIL).append(label)
    print('  [%s] %-42s %s' % ('OK ' if ok else 'HIBA', label, detail))


def api(path, data=None, timeout=30):
    url = 'http://localhost:%d%s' % (PORT, path)
    req = urllib.request.Request(
        url, data=json.dumps(data).encode() if data else None,
        headers={'Content-Type': 'application/json'} if data else {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def state():
    return api('/api/dlna/state')


def raw_status(path):
    """A HTTP státusz akkor is, ha hibás - a hibaágakat is mérni akarjuk."""
    try:
        with urllib.request.urlopen('http://localhost:%d%s' % (PORT, path), timeout=45) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception:
        return 0


def wait_for(cond, seconds, every=3):
    end = time.time() + seconds
    last = {}
    while time.time() < end:
        try:
            last = state()
        except Exception:
            pass
        if cond(last):
            return True, last
        time.sleep(every)
    return False, last


def pick_media(folder, count=1):
    """A legnagyobb médiafájlok - azok a legjellemzőbb terhelés."""
    found = []
    for name in sorted(os.listdir(folder)):
        p = os.path.join(folder, name)
        ext = os.path.splitext(name)[1].lower()
        if os.path.isfile(p) and ext in ('.mkv', '.mp4', '.avi', '.m4v', '.mov'):
            found.append((os.path.getsize(p), p))
    found.sort(reverse=True)
    picked = [p for _, p in found[:count]]
    return picked if count > 1 else (picked[0] if picked else None)


def main():
    if len(sys.argv) < 2:
        sys.exit('Használat: python3 devtools/selftest.py <médiamappa>')
    folder = os.path.abspath(sys.argv[1])
    media = pick_media(folder)
    if not media:
        sys.exit('Nem találtam médiafájlt itt: %s' % folder)

    print('\n  Önellenőrzés valódi fájllal')
    print('  ' + '-' * 62)
    print('  fájl: %s' % os.path.basename(media))
    print('  méret: %.2f GB\n' % (os.path.getsize(media) / 1e9))

    # Külön állapotmappa: a próba ne írja át a valódi lejátszási sorodat.
    adat = tempfile.mkdtemp(prefix='caststudio-teszt-')
    srv = subprocess.Popen(
        [sys.executable, '-u', os.path.join(APP, 'server.py'),
         '--root', folder, '--port', str(PORT), '--no-token', '--no-open',
         '--data', adat],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        time.sleep(3)
        say(True, 'szerver elindult')

        found = api('/api/dlna/discover?timeout=8', timeout=40)['renderers']
        say(bool(found), 'TV megtalálva',
            found[0]['name'] if found else 'nincs DLNA eszköz a hálózaton')
        if not found:
            return
        udn = found[0]['udn']
        sel = api('/api/dlna/select?udn=' + urllib.parse.quote(udn), timeout=40)
        say(bool(sel.get('renderer')), 'TV kiválasztva',
            '%d támogatott formátum' % len(sel.get('mimes', [])))

        base = 'http://%s:%d' % (api('/api/info')['addresses'][0], PORT)
        payload = {'items': [{'path': media, 'title': 'Önellenőrzés'}],
                   'index': 0, 'base': base, 'repeat': 'OFF'}

        # 1. tényleges lejátszás: a pozíciónak NŐNIE kell
        api('/api/dlna/queue', payload, timeout=60)
        ok, st = wait_for(lambda s: s.get('state') == 'PLAYING' and s.get('position', 0) > 2, 90)
        say(ok, 'lejátszás ténylegesen elindul',
            'poz=%.0f mp' % st.get('position', 0) if ok
            else 'a TV %s állapotban maradt, a pozíció nem nőtt' % st.get('state'))
        if not ok:
            return

        # 2. tekerés
        api('/api/dlna/seek?to=300')
        ok, st = wait_for(lambda s: s.get('position', 0) > 280, 30)
        say(ok, 'tekerés a 300. másodpercre', 'poz=%.0f mp' % st.get('position', 0))

        # 3. szünet / folytatás / ismételt gombnyomás
        api('/api/dlna/pause')
        ok, st = wait_for(lambda s: s.get('state') == 'PAUSED_PLAYBACK', 25)
        say(ok, 'szünet', st.get('state', '?'))

        code = raw_status('/api/dlna/pause')     # már szünetel: ne panaszkodjon
        say(code == 200, 'ismételt szünet nem hibázik', 'HTTP %s' % code)

        api('/api/dlna/toggle')
        before = state().get('position', 0)
        ok, st = wait_for(lambda s: s.get('state') == 'PLAYING'
                                    and s.get('position', 0) > before, 40)
        say(ok, 'folytatás a szünetből', 'poz=%.0f mp' % st.get('position', 0))

        api('/api/dlna/stop')
        time.sleep(3)
        code = raw_status('/api/dlna/toggle')    # a jelentett hiba: Stop utáni Start
        say(code == 200, 'Start a Stop után nem hibázik', 'HTTP %s' % code)
        ok, st = wait_for(lambda s: s.get('position', 0) > 2, 60)
        say(ok, 'Stop után újra elindul', 'poz=%.0f mp' % st.get('position', 0))

        # 4. leállítás menti-e a pozíciót
        api('/api/dlna/stop')
        time.sleep(3)
        marks = api('/api/state').get('positions', {})
        saved = marks.get(media, {}).get('pos', 0)
        say(saved > 200, 'leállításkor elmenti a pozíciót', '%.0f mp' % saved)

        # 5. folytatás onnan
        api('/api/dlna/queue', payload, timeout=60)
        ok, st = wait_for(lambda s: s.get('position', 0) > saved - 30, 120)
        say(ok, 'folytatás a mentett pontról',
            'poz=%.0f mp (várt: ~%.0f)' % (st.get('position', 0), saved))

        # 6. a folytatás ne írja felül kisebb értékkel a mentett pontot
        marks = api('/api/state').get('positions', {})
        still = marks.get(media, {}).get('pos', 0)
        say(still >= saved - 40, 'a mentett pontot nem írja felül a folytatás',
            '%.0f mp (mentett volt: %.0f)' % (still, saved))

        # 7. némítás: a készülék válaszának hinni nem elég, mérni kell
        api('/api/dlna/volume?level=22')
        time.sleep(2)
        elotte = state().get('volume', -1)
        api('/api/dlna/mute?on=1')
        time.sleep(2)
        kozben = state()
        say(kozben.get('muted') is True and kozben.get('volume') == 0,
            'némítás megtörténik', 'muted=%s hangerő=%s'
            % (kozben.get('muted'), kozben.get('volume')))
        api('/api/dlna/mute?on=0')
        time.sleep(2)
        utana = state()
        say(utana.get('muted') is False and utana.get('volume') == elotte,
            'feloldás visszaadja az eredeti hangerőt',
            'muted=%s hangerő=%s (előtte %s)'
            % (utana.get('muted'), utana.get('volume'), elotte))

        # 8. másik elemre váltva marad-e meg az elhagyott elem pontja
        others = [m for m in pick_media(folder, 3) if m != media]
        if others:
            two = {'items': [{'path': media, 'title': 'egyes'},
                             {'path': others[0], 'title': 'kettes'}],
                   'index': 0, 'base': base, 'repeat': 'OFF'}
            api('/api/dlna/queue', two, timeout=60)
            ok, st = wait_for(lambda s: s.get('position', 0) > 30, 90)
            at = st.get('position', 0)
            api('/api/dlna/play?index=1')
            time.sleep(4)
            marks = api('/api/state').get('positions', {})
            kept = marks.get(media, {}).get('pos', 0)
            say(kept >= at - 15, 'másik elemre váltva megmarad a régi pontja',
                'váltáskor %.0f mp, mentve %.0f mp' % (at, kept))

        api('/api/dlna/stop')
    finally:
        srv.terminate()
        try:
            srv.wait(timeout=10)
        except subprocess.TimeoutExpired:
            srv.kill()
        shutil.rmtree(adat, ignore_errors=True)

    print('\n  ' + '-' * 62)
    print('  %d rendben, %d hiba' % (len(PASS), len(FAIL)))
    if FAIL:
        print('  megbukott: %s' % ', '.join(FAIL))
    sys.exit(1 if FAIL else 0)


if __name__ == '__main__':
    main()
