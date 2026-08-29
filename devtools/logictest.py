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


def konzol_utf8():
    """Windowson a kodlap tipikusan cp1252, amiben nincs 'o' kettos ekezettel:
    a magyar kiiras csobe iranyitva UnicodeEncodeError-t dobna."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding='utf-8', errors='replace')
        except (AttributeError, ValueError, OSError):
            pass


konzol_utf8()

SPEED = 10.0            # ennyiszeres a virtuális lejátszási óra
OK, BAD = [], []


def say(ok, label, detail=''):
    (OK if ok else BAD).append(label)
    print('  [%s] %-44s %s' % ('OK ' if ok else 'HIBA', label, detail))


class MockTV(dlna.Player):
    """Hisense-szerű készülék: a hosszt nem jelenti, és szeszélyesen teker."""

    def __init__(self, seek_lockout=0.0, seek_ignored=False, duration=2713.0,
                 seek_fail_first=0, mute_stuck=False):
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
        self.valtas_kesik = 0.0              # váltás után ennyi virtuális mp-ig
        self.regi_uri = ''                   # még az ELŐZŐ fájl állását jelenti
        self.regi_pos = 0.0
        self.valtas_ig = 0.0
        self.megall = False                  # PLAYING-et mond, de a kép áll
        self.idegen_uri = ''                 # mást jelent, mint amit indítottunk
        self.nincs_pozicio = False           # RelTime = NOT_IMPLEMENTED
        self.uri_atir = False                # átírja a kapott cím gépnevét
        self.seeks = []
        self.tv_uri = ''
        self.tv_volume = 22
        self.tv_muted = False
        self.mute_stuck = mute_stuck
        self.set_mute_hivasok = []      # amit a némításról a készüléknek küldtünk
        self.tvlock = threading.Lock()

    def tick(self):
        with self.tvlock:
            if self.tv_state == 'PLAYING':
                now = time.time()
                if not self.megall:
                    self.tv_pos += (now - self.tv_started) * SPEED
                self.tv_started = now

    def _avt(self, action, args=None, timeout=8.0, with_code=False):
        ok, msg, code = self._device(action, dict(list(args or [])))
        return (ok, msg, code) if with_code else (ok, msg)

    def _device(self, action, a):
        self.tick()
        with self.tvlock:
            if action in ('Stop', 'SetAVTransportURI'):
                if action == 'Stop':
                    # Amit a készülék a váltás után még jelenteni fog.
                    self.regi_uri, self.regi_pos = self.tv_uri, self.tv_pos
                self.tv_state, self.tv_pos = 'STOPPED', 0.0
                if action == 'SetAVTransportURI':
                    self.tv_uri = a.get('CurrentURI', '')
                    if self.valtas_kesik:
                        self.valtas_ig = time.time() + self.valtas_kesik / SPEED
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
                # A valódi készülék XML-t ad vissza, tehát a tokenes URL '&'
                # jele '&amp;'-ként érkezik - a teszt is így adja.
                kesik = self.valtas_ig and time.time() < self.valtas_ig
                pos = self.regi_pos if kesik else self.tv_pos
                uri = self.idegen_uri or (self.regi_uri if kesik else self.tv_uri)
                if self.uri_atir and uri:
                    uri = uri.replace('10.0.0.240:8420', 'sajat-gep.local:8420')
                rel = ('NOT_IMPLEMENTED' if self.nincs_pozicio
                       else dlna.seconds_to_hms(pos))
                return True, ('<TrackDuration>00:00:00</TrackDuration>'
                              '<RelTime>%s</RelTime><TrackURI>%s</TrackURI>'
                              % (rel, uri.replace('&', '&amp;'))), ''
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
                kert = a.get('DesiredMute') in (1, '1', 'true')
                self.set_mute_hivasok.append(kert)
                # A "beragadós" készülék bekapcsolni tudja a némítást,
                # kikapcsolni nem - pontosan ez a mért hiba.
                if kert or not self.mute_stuck:
                    self.tv_muted = kert
                return True, ''
            if action == 'SelectPreset':
                return True, ''
            if action == 'GetVolume':
                return True, '<CurrentVolume>%d</CurrentVolume>' % self.tv_volume
            if action == 'GetMute':
                return True, ('<CurrentMute>%d</CurrentMute>'
                              % (1 if self.tv_muted else 0))
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
     'url': 'http://10.0.0.240:8420/api/media?p=elso&t=abc123', 'kind': 'video',
     'mime': 'video/x-matroska', 'size': 4_600_000_000}
B = dict(A, path='/media/masodik.mkv', name='masodik.mkv', title='második',
         url='http://10.0.0.240:8420/api/media?p=masodik&t=abc123')
C = dict(A, path='/media/harmadik.mkv', name='harmadik.mkv', title='harmadik',
         url='http://10.0.0.240:8420/api/media?p=harmadik&t=abc123')


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

    print('\n  Akadozás felismerése')

    # A készülék PLAYING-et jelent, de a kép áll: a hálózat nem viszi. Ezt egy
    # leolvasásból nem lehet kimondani (a TV egész másodperceket jelent),
    # ezért ablakban mérjük - a valódi, hibátlan felvételen a legrosszabb
    # arány is 0,93 volt, a küszöb 0,5.
    st = Store()
    p = player(st)
    jelentes = []
    p.stall_report = lambda h, e: jelentes.append((h, e)) or '7 kérés, 12 MB'
    p.set_queue([dict(A)], 0)
    run(p, 2.0)
    p.megall = True
    # A mérési ablak 10 VALÓS másodperc, a virtuális óra viszont tízszeres:
    # két ablaknyit kell várni, hogy a második teljes egészében az álló képre
    # essen. (Az elsőbe még belelóg a fagyasztás előtti haladás.)
    run(p, 23.0)
    p.shutdown()
    say(jelentes, 'álló kép PLAYING állapotban akadozásnak számít',
        ('%.0f mp videó %.0f mp alatt' % jelentes[0]) if jelentes else 'nincs jelzés')
    say('kadozik' in (p.error or ''), 'az akadozásról a felhasználó is értesül',
        p.error or 'nincs üzenet')

    # Valódi készülék valódi felvétele, hibátlan lejátszásból: erre egyetlen
    # riasztás sem eshet. Az adat a saját mérésünkből való (Hisense VIDAA,
    # ~2 perc, másodperc-pontos jelentéssel), és nem gyorsított: pont azt a
    # kvantálást tartalmazza, ami a hamis riasztásokat okozná.
    FELVETEL = [
     (0.00, 12), (1.26, 13), (2.54, 15), (3.82, 16), (5.14, 17), (6.45, 18),
     (7.78, 20), (9.09, 21), (10.36, 22), (11.62, 24), (12.88, 25), (14.18, 26),
     (15.56, 27), (16.81, 29), (18.09, 30), (19.35, 31), (20.64, 33), (21.91, 34),
     (23.19, 35), (24.46, 36), (25.72, 38), (27.02, 39), (28.37, 40), (29.62, 41),
     (30.89, 43), (32.22, 44), (33.52, 46), (34.78, 47), (36.06, 48), (37.33, 49),
     (38.62, 51), (39.98, 52), (41.33, 53), (42.77, 55), (44.03, 56), (45.30, 57),
     (46.58, 59), (47.87, 60), (49.12, 61), (50.39, 62), (51.68, 64), (53.10, 65),
     (54.36, 66), (55.65, 68), (56.97, 69), (58.23, 70), (59.52, 71), (60.79, 73),
     (62.05, 74), (63.33, 75), (64.60, 76), (65.97, 78), (67.31, 79), (68.74, 81),
     (70.02, 82), (71.37, 83), (72.76, 85), (74.00, 86), (75.25, 87), (76.52, 89),
     (77.79, 90), (79.06, 91), (80.49, 92), (81.84, 94), (83.11, 95), (84.37, 96),
     (85.64, 98), (86.91, 99), (88.19, 100), (89.47, 101), (90.76, 103), (92.20, 104),
     (93.50, 105), (94.77, 107), (96.08, 108), (97.46, 109), (98.82, 111), (100.13, 112),
     (101.50, 113), (102.87, 115), (104.13, 116), (105.50, 117), (106.87, 119), (108.11, 120),
     (109.43, 121), (110.81, 123), (112.20, 124), (113.52, 126), (114.80, 127), (116.12, 128),
    ]
    q = dlna.Player()
    q.seeked_at = -1000.0                 # ne a tekerési türelmi idő döntsön
    talalat = []
    for t, v in FELVETEL:
        r = q._akadas_meres(float(v), most=t)
        if r:
            talalat.append((t, r))
    say(not talalat, 'hibátlan valódi felvételre nem ad riasztást',
        'ablakok: %.0f mp felvétel, riasztás: %s'
        % (FELVETEL[-1][0], talalat or 'nincs'))

    # Szünet nem akadozás: ott nem is kell haladnia a képnek.
    st = Store()
    p = player(st)
    jelentes = []
    p.stall_report = lambda h, e: jelentes.append((h, e)) or ''
    p.set_queue([dict(A)], 0)
    run(p, 3.0)
    p.pause()
    run(p, 13.0)
    p.resume()
    run(p, 3.0)
    p.shutdown()
    say(not jelentes, 'a szünet nem számít akadozásnak',
        'jelzés: %s' % (jelentes or 'nincs'))

    # Pozíciót nem jelentő készüléken a "nem haladt" örökké igaz lenne.
    st = Store()
    p = player(st)
    p.nincs_pozicio = True
    jelentes = []
    p.stall_report = lambda h, e: jelentes.append((h, e)) or ''
    p.set_queue([dict(A)], 0)
    run(p, 25.0)                      # két teljes mérési ablak
    p.shutdown()
    say(not jelentes, 'pozíciót nem jelentő készüléken nincs hamis riasztás',
        'jelzés: %s' % (jelentes or 'nincs'))

    print('\n  A készüléken másra váltottak')

    # Az elemváltás pár másodperces átállását még meg kell várni, a tartósan
    # idegen tartalmat viszont ki kell mondani: "lejátszás" alatt befagyott
    # pozíciót mutatni némán hazugság lenne.
    st = Store()
    p = player(st)
    p.set_queue([dict(A)], 0)
    run(p, 3.0)
    p.idegen_uri = 'http://10.0.0.240:8420/api/media?p=masvalami&t=abc123'
    run(p, 4.0)                       # az IDEGEN_TURELEM 10 valós mp
    korai = (p.state, p.error)
    run(p, 10.0)
    p.shutdown()
    say(korai[0] == 'PLAYING' and not korai[1],
        'a rövid átállást még nem kiabálja ki', 'közben: %s / %s'
        % (korai[0], korai[1] or 'nincs üzenet'))
    say(p.state == 'STOPPED' and 'nem azt játssza' in (p.error or ''),
        'a tartósan idegen tartalomról szól, nem fagy be némán',
        '%s / %s' % (p.state, p.error or 'nincs üzenet'))

    # A fájlt a `path=` paraméter azonosítja, nem a gépnév: van készülék,
    # amelyik a kapott címet átírja. Ha ezt idegen tartalomnak vennénk,
    # hibátlan lejátszás közben mondanánk le a követésről.
    alap = A['url']
    say(dlna._uri_azonos(alap.replace('10.0.0.240:8420', 'gep.local:8420'), alap),
        'az átírt gépnév nem tesz másik fájllá', 'gépnév csere')
    say(not dlna._uri_azonos(alap.replace('p=elso', 'p=masodik'), alap),
        'másik fájlt továbbra is megkülönböztet', 'más path=')

    st = Store()
    p = player(st)
    p.uri_atir = True
    p.set_queue([dict(A)], 0)
    run(p, 14.0)                      # jóval az IDEGEN_TURELEM (10 mp) fölött
    p.shutdown()
    say(p.state == 'PLAYING' and not p.error and p.position > 0,
        'a címet átíró készüléket nem nyilvánítja idegennek',
        '%s / %s / pos=%.0f' % (p.state, p.error or 'nincs üzenet', p.position))

    # Az idegen szakasz után a sornak tovább kell lépnie a rész végén. Egy itt
    # beragadó "mi állítottuk le" jelzés ezt némán blokkolná.
    st = Store()
    p = player(st, duration=260.0)
    p.set_queue([dict(A), dict(B)], 0)
    run(p, 2.0)
    p.idegen_uri = 'http://10.0.0.240:8420/api/media?p=masvalami&t=abc123'
    run(p, 12.0)                      # túl a türelmen: szól róla
    p.idegen_uri = ''                 # a készülék visszaáll a mi tartalmunkra
    run(p, 14.0)                      # az elem eléri a végét
    p.shutdown()
    say(p.index == 1, 'idegen tartalom után is lép a sor a rész végén',
        'index=%d' % p.index)

    print('\n  Elemváltás: a készülék még az előzőt jelenti')

    # Valódi készüléken mérve: a váltás után az első leolvasás még az ELŐZŐ
    # rész 936. másodpercét adta vissza, a következő pedig az új rész nulláját.
    # A kettő közti esést újraindulásnak vettük, és az ÚJ részt tekertük oda.
    st = Store()
    p = player(st)
    p.valtas_kesik = 4.0
    p.set_queue([dict(A), dict(B)], 0)
    run(p, 15.0)                      # az első elem fusson jó messzire
    elozo_allas = p.tv_pos
    p.seeks = []
    p.switch(1)
    run(p, 6.0)                       # az új elem ennyi alatt ~60 mp-ig jut
    p.shutdown()
    say(not p.seeks and p.tv_pos < elozo_allas * 0.6,
        'váltáskor nem tekeri az újat az előző elem állására',
        'előző=%.0f mp, új=%.0f mp, tekerés=%s'
        % (elozo_allas, p.tv_pos, p.seeks or 'nincs'))
    say(st.pos.get(B['path'], 0) < elozo_allas * 0.6,
        'az új elem pontja sem az előzőé lesz',
        'mentett=%s' % st.pos.get(B['path'], 'nincs'))

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
    kezdo = p.tv_volume
    p.set_mute(True)
    nemitva = (p.tv_volume, p.snapshot()['muted'])
    p.set_mute(False)
    feloldva = (p.tv_volume, p.snapshot()['muted'])
    kertek = list(p.set_mute_hivasok)
    p.shutdown()
    say(nemitva == (0, True), 'némításkor a hangerő nullára megy',
        'hangerő=%d muted=%s' % nemitva)
    say(feloldva == (kezdo, False), 'feloldáskor visszaáll az eredeti hangerő',
        'hangerő=%d (eredeti %d) muted=%s' % (feloldva[0], kezdo, feloldva[1]))
    say(True not in kertek,
        'a készülék némítását SOHA nem kapcsolja be',
        'kiküldött SetMute kérések: %s' % (kertek or 'egy sem'))

    # Nulláról indulva a feloldás ne találjon ki hangerőt.
    p = player(Store())
    p.tv_volume = 0
    p.select(dict(p.renderer))
    p.set_mute(True)
    nemitva = p.tv_volume
    p.set_mute(False)
    feloldva = p.tv_volume
    p.shutdown()
    say(nemitva == 0 and feloldva == 0,
        'nulla hangerőről a feloldás sem emeli meg',
        'némítva=%d feloldva=%d' % (nemitva, feloldva))

    # Bármilyen kiindulásból pontos oda-vissza.
    jo = []
    for kezdo in (5, 17, 44, 100):
        p = player(Store())
        p.tv_volume = kezdo
        p.select(dict(p.renderer))
        p.set_mute(True)
        n = p.tv_volume
        p.set_mute(False)
        jo.append((kezdo, n, p.tv_volume))
        p.shutdown()
    say(all(n == 0 and vissza == k for k, n, vissza in jo),
        'a feloldás pontosan az eredeti hangerőt adja vissza',
        ' '.join('%d→0→%d' % (k, v) for k, _, v in jo))

    # Kétszer némítva a második ne a már nullázott értéket mentse el.
    p = player(Store())
    p.tv_volume = 37
    p.select(dict(p.renderer))
    p.set_mute(True)
    p.set_mute(True)
    p.set_mute(False)
    vissza = p.tv_volume
    p.shutdown()
    say(vissza == 37, 'ismételt némítás nem felejti el az eredeti hangerőt',
        'feloldva=%d (eredeti 37)' % vissza)

    # Ugyanannak a készüléknek az újraválasztása ne felejtse el a némításunkat.
    p = player(Store())
    p.tv_volume = 28
    p.select(dict(p.renderer))
    p.set_mute(True)
    p.select(dict(p.renderer))          # ugyanaz az eszköz még egyszer
    megvan = p.snapshot()['muted']
    p.set_mute(False)
    vissza = p.tv_volume
    p.shutdown()
    say(megvan and vissza == 28, 'újraválasztás után is tudja, hogy mi némítottunk',
        'muted=%s feloldva=%d (eredeti 28)' % (megvan, vissza))

    # Feloldhatatlan készülék-némításnál ne mutassuk feloldottnak.
    p = player(Store(), mute_stuck=True)
    p.tv_muted = True
    p.select(dict(p.renderer))
    p.set_mute(False)
    p.shutdown()
    say(p.snapshot()['muted'] is True,
        'ha a némítás bent maradt, nem mutatja feloldottnak',
        'muted=%s' % p.snapshot()['muted'])

    # A hangerő állítása feloldja a saját némításunkat, egy kérésből.
    p = player(Store())
    p.tv_volume = 40
    p.select(dict(p.renderer))
    p.set_mute(True)
    p.set_volume(15)
    allapot = p.snapshot()
    p.shutdown()
    say(allapot['muted'] is False and p.tv_volume == 15,
        'a hangerő állítása feloldja a némítást',
        'muted=%s hangerő=%d' % (allapot['muted'], p.tv_volume))

    # A készülék saját némítását viszont NEM oldja fel, tehát nem is hazudik.
    p = player(Store(), mute_stuck=True)
    p.tv_muted = True
    p.select(dict(p.renderer))
    p.set_volume(15)
    allapot = p.snapshot()
    p.shutdown()
    say(allapot['muted'] is True,
        'idegen némítást a hangerő állítása nem tüntet el',
        'muted=%s' % allapot['muted'])

    # Beragadós készülék: a némítást bekapcsolni tudja, kikapcsolni nem.
    p = player(Store(), mute_stuck=True)
    p.tv_muted = True                    # a távirányítóról már némítva van
    p.select(dict(p.renderer))
    nemitottnak_latja = p.snapshot()['muted']
    p.set_mute(False)
    uzenet = p.snapshot()['error']
    hangero = p.tv_volume
    p.shutdown()
    say(nemitottnak_latja, 'felismeri a készüléken már beállított némítást')
    say(hangero == 22 and 'távirányító' in uzenet,
        'feloldhatatlan némításnál szól, és a hangerőhöz nem nyúl',
        'hangerő=%d, üzenet: %s' % (hangero, (uzenet or 'NINCS')[:40]))

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
