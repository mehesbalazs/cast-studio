#!/usr/bin/env python3
"""Logikai önellenőrzés TV nélkül, gyorsított idővel.

A `selftest.py` a valódi TV-vel és a valódi fájloddal mér – de vannak
versenyhelyzetek, amiket élőben nem lehet előhívni parancsra: a készülék
betöltés közben visszautasíthatja a tekerést, sőt el is fogadhatja anélkül,
hogy megmozdulna. Ez a teszt ezeket a készülékeket utánozza, és a Player
igazi állapotgépét futtatja rajtuk.

Használat:
    python3 devtools/logictest.py
"""

import os
import sys
import threading
import time

sys.dont_write_bytecode = True
# Az app egy szinttel feljebb van: ez a mappa csak a fejlesztői eszközöké.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import dlna                                             # noqa: E402

SPEED = 10.0            # ennyiszeres a virtuális lejátszási óra
OK, BAD = [], []


def say(ok, label, detail=''):
    (OK if ok else BAD).append(label)
    print('  [%s] %-44s %s' % ('OK ' if ok else 'HIBA', label, detail))


class MockTV(dlna.Player):
    """Hisense-szerű készülék: a hosszt nem jelenti, és szeszélyesen teker."""

    def __init__(self, seek_lockout=0.0, seek_ignored=False, duration=2713.0,
                 seek_fail_first=0, mute_broken=False):
        dlna.Player.__init__(self)
        self.renderer = {'avtransport': 'mock://avt', 'rendering': 'mock://rcs',
                         'name': 'MockTV', 'udn': 'uuid:mock',
                         'host': '10.0.0.237', 'model': 'Mock'}
        self.tv_state = 'STOPPED'
        self.tv_pos = 0.0
        self.tv_started = 0.0
        self.tv_duration = duration
        self.seek_lockout = seek_lockout     # eddig a pozícióig 701-et ad
        self.seek_ignored = seek_ignored     # elfogadja, de nem mozdul
        self.seek_fail_first = seek_fail_first   # az első N próbát elutasítja
        self.restart_at = 0.0                # ennél a pozíciónál ugrik vissza 0-ra
        self.seeks = []
        self.tv_uri = ''
        self.tv_volume = 22
        self.tv_muted = False
        self.mute_broken = mute_broken
        self.tvlock = threading.Lock()

    def tick(self):
        with self.tvlock:
            if self.tv_state == 'PLAYING':
                now = time.time()
                self.tv_pos += (now - self.tv_started) * SPEED
                self.tv_started = now

    def _avt(self, action, args=None, timeout=8.0, with_code=False):
        ok, msg, code = self._device(action, dict(list(args or [])))
        return (ok, msg, code) if with_code else (ok, msg)

    def _device(self, action, a):
        self.tick()
        with self.tvlock:
            if action in ('Stop', 'SetAVTransportURI'):
                self.tv_state, self.tv_pos = 'STOPPED', 0.0
                if action == 'SetAVTransportURI':
                    self.tv_uri = a.get('CurrentURI', '')
                return True, '', ''
            if action == 'Play':
                self.tv_state = 'TRANSITIONING'
                self.tv_started = time.time()
                self._ready = time.time() + 2.0 / SPEED
                return True, '', ''
            if action == 'Pause':
                if self.tv_state != 'PLAYING':
                    return False, 'A készülék most nem tudja ezt végrehajtani.', '701'
                self.tv_state = 'PAUSED_PLAYBACK'
                return True, '', ''
            if action == 'GetTransportInfo':
                if (self.tv_state == 'TRANSITIONING'
                        and time.time() >= getattr(self, '_ready', 0)):
                    self.tv_state, self.tv_started = 'PLAYING', time.time()
                if (self.restart_at and self.tv_state == 'PLAYING'
                        and self.tv_pos >= self.restart_at):
                    self.restart_at = 0.0      # egyszer ugrik vissza magától
                    self.tv_pos = 0.0
                    self.tv_started = time.time()
                if self.tv_state == 'PLAYING' and self.tv_pos >= self.tv_duration:
                    self.tv_state, self.tv_pos = 'STOPPED', 0.0
                return True, ('<CurrentTransportState>%s</CurrentTransportState>'
                              % self.tv_state), ''
            if action == 'GetPositionInfo':
                return True, ('<TrackDuration>00:00:00</TrackDuration>'
                              '<RelTime>%s</RelTime>'
                              % dlna.seconds_to_hms(self.tv_pos)), ''
            if action == 'Seek':
                self.seeks.append(round(self.tv_pos, 1))
                if self.seek_fail_first > 0:
                    self.seek_fail_first -= 1
                    return False, 'A készülék most nem tudja ezt végrehajtani.', '701'
                if self.tv_pos < self.seek_lockout:
                    return False, 'A készülék most nem tudja ezt végrehajtani.', '701'
                if not self.seek_ignored:
                    tgt = a.get('Target', '0:00:00')
                    if a.get('Unit') == 'X_DLNA_REL_BYTE':
                        # a valódi készülék bájtot kap, és abból számol időt
                        size = 4_600_000_000
                        self.tv_pos = float(tgt) * self.tv_duration / size
                    else:
                        self.tv_pos = dlna.hms_to_seconds(tgt)
                    self.tv_started = time.time()
                return True, '', ''
        return True, '', ''

    def _rcs(self, action, args=None):
        a = dict(list(args or []))
        with self.tvlock:
            if action == 'SetVolume':
                self.tv_volume = int(a.get('DesiredVolume', 0))
                return True, ''
            if action == 'SetMute':
                # A romlott készülék elfogadja, de nem hajtja végre.
                if not self.mute_broken:
                    self.tv_muted = a.get('DesiredMute') in (1, '1', 'true')
                return True, ''
            if action == 'GetVolume':
                return True, '<CurrentVolume>%d</CurrentVolume>' % self.tv_volume
            if action == 'GetMute':
                # Beégetett "némítva" válasz - pontosan ez a mért hiba.
                m = 1 if (self.mute_broken or self.tv_muted) else 0
                return True, '<CurrentMute>%d</CurrentMute>' % m
        return True, ''

    def _poll_loop(self):
        """Ugyanaz a _poll_once, csak a gyorsított órához igazított ütemben."""
        while True:
            with self.lock:
                busy = self.state in ('PLAYING', 'TRANSITIONING')
            if self._stop.wait((1.2 if busy else 4.0) / SPEED):
                return
            try:
                self._poll_once()
            except Exception as e:                       # pragma: no cover
                print('  POLL HIBA:', e)


class Store:
    """A server.py mentési szabályainak mása, memóriában."""

    RESUME_MIN, RESUME_TAIL = 20.0, 45.0

    def __init__(self, initial=None):
        self.pos = dict(initial or {})

    def load(self, path):
        return float(self.pos.get(path, 0.0))

    def save(self, path, pos, dur):
        if dur > 0 and pos > dur - self.RESUME_TAIL:
            self.clear(path)
            return
        if pos < self.RESUME_MIN:
            return
        self.pos[path] = round(pos, 1)

    def clear(self, path):
        self.pos.pop(path, None)


A = {'path': '/media/elso.mkv', 'name': 'elso.mkv', 'title': 'első',
     'url': 'http://10.0.0.240:8420/api/media?p=elso', 'kind': 'video',
     'mime': 'video/x-matroska', 'size': 4_600_000_000}
B = dict(A, path='/media/masodik.mkv', name='masodik.mkv', title='második',
         url='http://10.0.0.240:8420/api/media?p=masodik')
C = dict(A, path='/media/harmadik.mkv', name='harmadik.mkv', title='harmadik',
         url='http://10.0.0.240:8420/api/media?p=harmadik')


def player(store, **kw):
    p = MockTV(**kw)
    p.position_load, p.position_save, p.position_clear = (
        store.load, store.save, store.clear)
    p.duration_probe = lambda path: kw.get('duration', 2713.0)
    return p


def run(p, seconds):
    end = time.time() + seconds
    while time.time() < end:
        time.sleep(0.2)
    p.tick()


def main():
    print('\n  Logikai önellenőrzés – utánzott TV, %gx gyorsított idő\n' % SPEED)
    mark = 375.0

    print('  Folytatás háromféle készüléken')
    for label, kw in (('a tekerés azonnal átmegy', {}),
                      ('a TV töltés közben visszautasítja', {'seek_lockout': 30.0}),
                      ('a TV elfogadja, de nem mozdul', {'seek_ignored': True})):
        st = Store({A['path']: mark})
        p = player(st, **kw)
        p.set_queue([dict(A)], 0)
        run(p, 4.5)
        landed = p.tv_pos >= mark - dlna.RESUME_LAND
        p.shutdown()
        if kw.get('seek_ignored'):
            say(not landed and st.pos.get(A['path']) == mark,
                'reménytelen eset: a pont megmarad', 'mentett=%s' % st.pos.get(A['path']))
        else:
            say(landed, 'folytatás – %s' % label, 'TV: %.0f mp' % p.tv_pos)

    print('\n  A mentett pont védelme')
    st = Store({A['path']: mark})
    p = player(st, seek_lockout=1e9)          # ez a készülék sosem teker
    p.set_queue([dict(A)], 0)
    run(p, 12.0)                              # túl a tízmásodperces mentési kapun
    p.shutdown()
    say(st.pos.get(A['path']) == mark, 'sikertelen tekerés nem írja felül a pontot',
        'mentett=%s' % st.pos.get(A['path']))

    st = Store({A['path']: mark})
    p = player(st, seek_ignored=True)
    p.set_queue([dict(A)], 0)
    run(p, 5.0)
    p.stop()
    p.shutdown()
    say(st.pos.get(A['path']) == mark, 'Stop be nem állt folytatás közben sem ír felül',
        'mentett=%s' % st.pos.get(A['path']))

    st = Store({A['path']: mark})
    p = player(st, seek_ignored=True)
    p.set_queue([dict(A)], 0)
    run(p, 26)                       # a feladási küszöbön túl
    gave_up = p.snapshot()['error']
    run(p, 16)                       # a lejátszás túlfut a mentett ponton
    p.shutdown()
    say(bool(gave_up), 'feladáskor szól a felhasználónak', gave_up[:46] + '…' if gave_up else '')
    say(st.pos.get(A['path'], 0) > mark, 'a pont újra mozdul, ha túljutott rajta',
        'mentett=%s' % st.pos.get(A['path']))

    print('\n  A felhasználó felülbírálja a folytatást')
    st = Store({A['path']: mark})
    p = player(st, seek_fail_first=3)     # az első próbák elbuknak
    p.set_queue([dict(A)], 0)
    run(p, 2.0)
    p.seek(50.0)                          # a felhasználó máshova teker
    run(p, 6.0)
    hova = p.tv_pos
    p.shutdown()
    say(hova < mark, 'kézi tekerés után nem rángat vissza a mentett pontra',
        'a TV a %.0f. mp-nél jár' % hova)

    st = Store({A['path']: mark})
    p = player(st, seek_fail_first=2)
    p.set_queue([dict(A)], 0)
    run(p, 1.5)
    p.pause()
    time.sleep(9)                         # hosszú szünet a folytatás közben
    p.resume()
    run(p, 8.0)
    landed = p.tv_pos >= mark - dlna.RESUME_LAND
    err = p.snapshot()['error']
    p.shutdown()
    say(landed and not err, 'hosszú szünet után is befejezi a folytatást',
        'TV: %.0f mp%s' % (p.tv_pos, (' / uzenet: ' + err) if err else ''))

    print('\n  Váratlan újraindulás a készüléken')
    st = Store()
    p = player(st)
    p.set_queue([dict(A)], 0)
    run(p, 3.0)
    p.restart_at = p.tv_pos + 40      # 40 virtuális mp múlva magától visszaugrik
    run(p, 12.0)
    p.shutdown()
    say(p.tv_pos > 60, 'magától visszaugró készüléket visszatekeri',
        'TV: %.0f mp (nem 0 közeléből folytatja)' % p.tv_pos)

    st = Store()
    p = player(st)
    p.set_queue([dict(A)], 0)
    run(p, 3.0)
    p.restart_at = p.tv_pos + 40
    run(p, 26.0)                      # a visszatekerés UTÁN is legyen mentés
    p.shutdown()
    mentve = st.pos.get(A['path'], 0)
    say(mentve > 60, 'az újraindulás nem írja felül kis értékkel a pontot',
        'mentett=%s' % mentve)

    print('\n  Ütközések és téves riasztások')

    st = Store()
    p = player(st)
    p.set_queue([dict(A), dict(B)], 0)
    run(p, 12.0)
    elotte = st.pos.get(A['path'], 0)
    p.select({'avtransport': 'mock://avt', 'rendering': 'mock://rcs',
              'name': 'Másik TV', 'udn': 'uuid:masik', 'host': '10.0.0.9',
              'model': 'X'})
    run(p, 3.0)
    p.shutdown()
    say(st.pos.get(A['path'], 0) >= elotte and p.index == 0,
        'másik készülékre váltás nem lép tovább, nem töröl',
        'index=%d mentett=%s (előtte %s)' % (p.index, st.pos.get(A['path'], 'nincs'), elotte))

    st = Store()
    p = player(st)
    p.set_queue([dict(A), dict(B), dict(C)], 0)
    run(p, 8.0)
    hibak = []
    def valt(i):
        try:
            p.switch(i)
        except Exception as e:
            hibak.append(repr(e))
    szalak = [threading.Thread(target=valt, args=(i,)) for i in (1, 2, 1, 2)]
    for t in szalak:
        t.start()
    for t in szalak:
        t.join(timeout=30)
    run(p, 2.0)
    tv_uri = p.tv_uri
    ui = p.queue[p.index]['url'] if 0 <= p.index < len(p.queue) else ''
    p.shutdown()
    say(not hibak and tv_uri == ui,
        'egyszerre indított váltásoknál a TV és a felület egyezik',
        'TV=%s felület=%s' % (tv_uri[-9:], ui[-9:]))

    st = Store()
    p = player(st, seek_ignored=True)     # elfogadja a tekerést, de nem mozdul
    p.set_queue([dict(A)], 0)
    run(p, 4.0)
    seekek = len(p.seeks)
    p.seek(600.0)                          # a felhasználó előre tekerne
    run(p, 12.0)
    ujabb = len(p.seeks) - seekek
    uzenet = p.snapshot()['error']
    p.shutdown()
    say(ujabb <= 1 and not uzenet,
        'ki nem szolgált tekerés nem indít visszatekerő kört',
        '%d további Seek, üzenet: %r' % (ujabb, uzenet[:40]))

    st = Store({A['path']: 900.0})
    p = player(st)
    p.set_queue([dict(A)], 0)
    run(p, 5.0)
    p.restart_fixes = 99                  # a keret elfogyott
    p.restart_at = p.tv_pos + 30
    run(p, 26.0)
    p.shutdown()
    say(st.pos.get(A['path'], 0) >= 900.0,
        'kimerült keret mellett sem írja felül a pontot',
        'mentett=%s' % st.pos.get(A['path']))

    print('\n  Tekerési egység megválasztása')
    st = Store({A['path']: mark})
    p = player(st, seek_lockout=1e9)      # minden tekerést 701-gyel utasít el
    p.set_queue([dict(A)], 0)
    run(p, 4.0)
    egy_korben = len(p.seeks) / max(1, p.resume_tries)
    p.shutdown()
    say(egy_korben <= 1.05, 'a 701-re nem próbál másik tekerési egységet',
        '%d próbálkozás, %d Seek hívás' % (p.resume_tries, len(p.seeks)))

    st = Store()
    p = player(st)
    p.set_queue([dict(A)], 0)
    run(p, 3.0)
    p.seek(600.0)
    run(p, 1.0)
    egyseg = p.seek_unit
    p.shutdown()
    say(egyseg == 'REL_TIME', 'a bevált egységet megjegyzi', str(egyseg))

    print('\n  Némítás')
    p = player(Store())
    p.select(dict(p.renderer))
    p.set_mute(True)
    n_hangero, n_allapot = p.tv_volume, p.tv_muted
    p.set_mute(False)
    p.shutdown()
    say(n_allapot and not p.tv_muted and n_hangero == 22,
        'szabályos készüléken a hangerőhöz nem nyúl',
        'némítva: hangerő=%d, készülék némítva=%s' % (n_hangero, n_allapot))

    p = player(Store(), mute_broken=True)   # SetMute elfogadva, de hatástalan
    p.select(dict(p.renderer))
    kezdo = p.tv_volume
    p.set_mute(True)
    nemitva = (p.tv_volume, p.snapshot()['muted'])
    p.set_mute(False)
    feloldva = (p.tv_volume, p.snapshot()['muted'])
    p.shutdown()
    say(nemitva == (0, True), 'romlott némításnál a hangerőn keresztül némít',
        'hangerő=%d muted=%s' % nemitva)
    say(feloldva == (kezdo, False), 'feloldáskor visszaáll az eredeti hangerő',
        'hangerő=%d (eredeti %d) muted=%s' % (feloldva[0], kezdo, feloldva[1]))

    p = player(Store(), mute_broken=True)
    p.select(dict(p.renderer))
    p.shutdown()
    say(p.snapshot()['muted'] is False and p.mute_readback is False,
        'a beégetett "némítva" válasz nem téveszti meg',
        'muted=%s, visszaolvasás megbízható=%s'
        % (p.snapshot()['muted'], p.mute_readback))

    print('\n  Hétköznapi utak')
    st = Store()
    p = player(st)
    p.set_queue([dict(A)], 0)
    run(p, 12)
    p.shutdown()
    say(st.pos.get(A['path'], 0) > 20, 'menet közben jegyzi a helyet',
        'mentett=%s' % st.pos.get(A['path']))

    st = Store()
    p = player(st)
    p.set_queue([dict(A), dict(B)], 0)
    run(p, 6)
    at = p.position
    p.switch(1)
    run(p, 1)
    p.shutdown()
    say(abs(st.pos.get(A['path'], 0) - at) < 3, 'másik elemre váltva elmenti a régit',
        'váltáskor %.0f mp, mentve %s' % (at, st.pos.get(A['path'])))

    st = Store()
    p = player(st)
    p.set_queue([dict(A), dict(B)], 0)
    run(p, 6)
    p.skip(1)
    run(p, 1)
    p.shutdown()
    say(st.pos.get(A['path'], 0) > 20, 'következő elemre lépve elmenti a régit',
        'mentett=%s' % st.pos.get(A['path']))

    st = Store({'/media/rovid.mkv': 300.0})
    short = dict(A, path='/media/rovid.mkv', name='rovid.mkv')
    p = MockTV(duration=400.0)
    p.position_load, p.position_save, p.position_clear = st.load, st.save, st.clear
    p.duration_probe = lambda path: 400.0
    p.set_queue([short], 0)
    run(p, 14)
    p.shutdown()
    say('/media/rovid.mkv' not in st.pos, 'végignézett elem pontja törlődik',
        'mentett=%s' % st.pos.get('/media/rovid.mkv', 'nincs'))

    print('\n  %d rendben, %d hiba\n' % (len(OK), len(BAD)))
    return 1 if BAD else 0


if __name__ == '__main__':
    sys.exit(main())
