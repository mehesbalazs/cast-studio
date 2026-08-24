#!/usr/bin/env python3
"""
Cast Studio - helyi médiakiszolgáló és DLNA-vezérlő a TV-hez.

Kiszolgálja a felületet (localhost) és a médiafájlokat (LAN IP), és UPnP/DLNA
hívásokkal vezérli a TV-t. Csak Python 3 stdlib kell hozzá.

Indítás:
    python3 server.py                 # gyökér: a home könyvtárad
    python3 server.py --root /Volumes/Media --port 8420
"""

import argparse
import collections
import json
import math
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, quote

# Hordozható működés: ne keletkezzen __pycache__ az app mappájában.
sys.dont_write_bytecode = True

import dlna

# --------------------------------------------------------------------------
# Fájltípusok
# --------------------------------------------------------------------------

MIME = {
    '.mp4': 'video/mp4', '.m4v': 'video/mp4', '.webm': 'video/webm',
    '.mkv': 'video/x-matroska', '.avi': 'video/x-msvideo', '.mov': 'video/quicktime',
    '.mpg': 'video/mpeg', '.mpeg': 'video/mpeg', '.ogv': 'video/ogg',
    '.ts': 'video/mp2t', '.m2ts': 'video/mp2t', '.mts': 'video/mp2t',
    '.3gp': 'video/3gpp', '.wmv': 'video/x-ms-wmv', '.flv': 'video/x-flv',
    '.mp3': 'audio/mpeg', '.m4a': 'audio/mp4', '.aac': 'audio/aac',
    '.wav': 'audio/wav', '.flac': 'audio/flac', '.ogg': 'audio/ogg',
    '.oga': 'audio/ogg', '.opus': 'audio/ogg', '.weba': 'audio/webm',
    '.wma': 'audio/x-ms-wma',
    '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.png': 'image/png',
    '.gif': 'image/gif', '.webp': 'image/webp', '.bmp': 'image/bmp',
    '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8',
    '.css': 'text/css; charset=utf-8', '.svg': 'image/svg+xml',
    '.json': 'application/json; charset=utf-8', '.ico': 'image/x-icon',
}

VIDEO_EXT = {'.mp4', '.m4v', '.webm', '.mkv', '.avi', '.mov', '.mpg', '.mpeg',
             '.ogv', '.ts', '.m2ts', '.mts', '.3gp', '.wmv', '.flv'}
AUDIO_EXT = {'.mp3', '.m4a', '.aac', '.wav', '.flac', '.ogg', '.oga', '.opus',
             '.weba', '.wma'}
IMAGE_EXT = {'.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp'}
SUB_EXT = {'.srt', '.vtt'}
MEDIA_EXT = VIDEO_EXT | AUDIO_EXT | IMAGE_EXT

LANG_NAMES = {'hu': 'magyar', 'en': 'angol', 'eng': 'angol', 'hun': 'magyar',
              'de': 'német', 'ger': 'német', 'fr': 'francia', 'es': 'spanyol',
              'it': 'olasz', 'ro': 'román', 'sk': 'szlovák', 'sr': 'szerb',
              'hr': 'horvát', 'cz': 'cseh', 'cs': 'cseh', 'pl': 'lengyel'}

NUM_RE = re.compile(r'(\d+)')


def natkey(name):
    """Természetes rendezés: 'S01E02' < 'S01E10'."""
    return [int(p) if p.isdigit() else p.lower() for p in NUM_RE.split(name)]


def kind_of(ext):
    if ext in VIDEO_EXT:
        return 'video'
    if ext in AUDIO_EXT:
        return 'audio'
    if ext in IMAGE_EXT:
        return 'image'
    return 'other'


# --------------------------------------------------------------------------
# Konzolkimenet
# --------------------------------------------------------------------------

class Out:
    """Egységes, igazított konzolkiírás. Színek csak valódi terminálban."""

    LABEL_W = 16
    RULE_W = 62

    def __init__(self, stream=None):
        self.stream = stream or sys.stdout
        self.color = bool(getattr(self.stream, 'isatty', lambda: False)())

    def _c(self, code, text):
        return '\033[%sm%s\033[0m' % (code, text) if self.color else text

    def dim(self, t):
        return self._c('2', t)

    def bold(self, t):
        return self._c('1', t)

    def green(self, t):
        return self._c('32', t)

    def yellow(self, t):
        return self._c('33', t)

    def red(self, t):
        return self._c('31', t)

    def cyan(self, t):
        return self._c('36', t)

    def line(self, text=''):
        try:
            self.stream.write(text + '\n')
            self.stream.flush()
        except (OSError, ValueError):
            pass

    def rule(self):
        self.line('  ' + self.dim('─' * self.RULE_W))

    def title(self, text, subtitle=''):
        self.line('')
        head = '  ' + self.bold(text)
        if subtitle:
            head += self.dim('  ·  ' + subtitle)
        self.line(head)
        self.rule()

    def row(self, label, value):
        self.line('  %s  %s' % (self.dim(label.ljust(self.LABEL_W)), value))

    def note(self, text):
        self.line('  ' + self.dim(text))

    def warn(self, text):
        self.line('  ' + self.yellow('figyelem') + '  ' + text)

    def error(self, text):
        self.line('  ' + self.red('hiba') + '      ' + text)

    def request(self, client, method, code, path):
        """Egy kérés egy sorban: idő, kliens, művelet, státusz, tárgy."""
        try:
            n = int(code)
        except (TypeError, ValueError):
            n = 0
        text = str(n if n else code)
        if n >= 500:
            status = self.red(text)
        elif n >= 400:
            status = self.yellow(text)
        elif n == 206:
            status = self.dim(text)
        elif n >= 200:
            status = self.green(text)
        else:
            status = text
        self.line('  %s  %-15s %-4s %-3s  %s' % (
            self.dim(time.strftime('%H:%M:%S')), client, method, status,
            describe_request(path)))


def konzol_utf8():
    """A kiírás ne vesszen el ékezeten, ha nem UTF-8 a rendszer kódlapja.

    Csőbe vagy fájlba irányítva a Python nem a konzolt, hanem a kódlapot
    használja. Windowson ez tipikusan cp1252, amiben nincs 'ő': a kiírás
    UnicodeEncodeError-t dob, azt pedig az Out.line elnyeli (a ValueError
    leszármazottja). Némán tűnnének el az indulási sorok, köztük a
    megnyitandó cím - ezért még az első kiírás előtt átállítjuk.
    """
    for stream in (sys.stdout, sys.stderr):
        kodlap = (getattr(stream, 'encoding', '') or '').lower()
        if kodlap.replace('-', '') == 'utf8':
            continue
        try:
            stream.reconfigure(encoding='utf-8', errors='replace')
        except (AttributeError, ValueError, OSError):
            pass


konzol_utf8()

OUT = Out()

# Melyik végpont mit csinál - a nyers URL helyett ez kerül a naplóba.
REQUEST_LABELS = {
    '/api/media': 'média',
    '/api/stream': 'átkódolás',
    '/api/sub': 'felirat',
    '/api/browse': 'mappa',
    '/api/scan': 'keresés',
    '/api/state': 'állapot',
    '/api/info': 'infó',
}


def describe_request(raw):
    parsed = urlparse(raw)
    path = parsed.path
    target = parse_qs(parsed.query).get('path', [''])[0]
    name = os.path.basename(target.rstrip('/')) or target

    if path in REQUEST_LABELS:
        label = REQUEST_LABELS[path]
        return '%-10s %s' % (label, name) if name else label
    if path.startswith('/api/dlna/'):
        return '%-10s %s' % ('TV', path[len('/api/dlna/'):])
    if path in ('/', '/index.html'):
        return 'oldal'
    return path


# --------------------------------------------------------------------------
# Konfiguráció
# --------------------------------------------------------------------------

class Config:
    root = os.path.expanduser('~')
    port = 8420
    token = ''
    ffmpeg = None
    ffprobe = None
    verbose = False


CFG = Config()

# Hordozható működés: minden, amit az app megjegyez, az app mappájában marad.
APP_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(APP_DIR, 'data')
STATE_PATH = os.path.join(DATA_DIR, 'state.json')
# Újrabelépő zár: a mentés a beolvasástól az írásig egyben tartja a fájlt,
# és közben maga is beolvas. Enélkül két egyidejű mentés felülírja egymást.
STATE_LOCK = threading.RLock()

DEFAULT_STATE = {
    'settings': {'base': '', 'subs': True, 'hidden': False, 'recursive': False,
                 'repeat': 'OFF', 'volume': 30, 'udn': '', 'resume': True},
    'queue': [],
    'cwd': '',
    'positions': {},           # fájl -> hol tartottál benne
    'rev': 0,                  # csak a felület által birtokolt részt követi
}

POSITION_LIMIT = 500           # ennyi film megtekintési pontját tartjuk meg
RESUME_MIN = 20.0              # ennél korábbi pontot nem érdemes megjegyezni
RESUME_TAIL = 45.0             # a vége előtti sávban végignézettnek tekintjük


class StateUnreadable(Exception):
    """A fájl létezik, de most nem olvasható - ilyenkor nem szabad felülírni."""


def load_state(strict=False):
    with STATE_LOCK:
        try:
            with open(STATE_PATH, 'r', encoding='utf-8') as fh:
                stored = json.load(fh)
        except FileNotFoundError:
            return json.loads(json.dumps(DEFAULT_STATE))
        except OSError as e:
            # Jogosultsági vagy lemezhiba: a fájl tartalma megvan, csak most
            # nem látjuk. Alapértelmezéssel válaszolni és aztán rámenteni
            # egyenlő lenne a felhasználó adatainak eldobásával.
            if strict:
                raise StateUnreadable(str(e))
            return json.loads(json.dumps(DEFAULT_STATE))
        except ValueError:
            return json.loads(json.dumps(DEFAULT_STATE))
    state = json.loads(json.dumps(DEFAULT_STATE))
    if isinstance(stored, dict):
        if isinstance(stored.get('settings'), dict):
            # Csak az ismert kulcsokat vesszük át: egy elrontott vagy idegen
            # fájl különben korlátlanul növelné az állapotot.
            state['settings'].update({k: v for k, v in stored['settings'].items()
                                      if k in DEFAULT_STATE['settings']})
        if isinstance(stored.get('queue'), list):
            state['queue'] = stored['queue'][:5000]
        if isinstance(stored.get('cwd'), str):
            state['cwd'] = stored['cwd']
        if isinstance(stored.get('rev'), int):
            state['rev'] = stored['rev']
        if isinstance(stored.get('positions'), dict):
            state['positions'] = {k: v for k, v in stored['positions'].items()
                                  if valid_position(v)}
    return state


def valid_position(val):
    """Megtekintési pont-e? Csak ilyet engedünk az állapotfájlba."""
    if not isinstance(val, dict):
        return False
    for key in ('pos', 'dur', 'at'):
        num = val.get(key)
        if not isinstance(num, (int, float)) or isinstance(num, bool):
            return False
        if num != num or num in (float('inf'), float('-inf')):
            return False
    return True


def position_age(item):
    val = item[1]
    return val.get('at', 0) if isinstance(val, dict) else 0


def save_state(patch):
    # A teljes beolvas-összefésül-kiír folyamat egyetlen kritikus szakasz. A
    # lejátszó tízmásodpercenként ment pozíciót, a felület közben beállítást és
    # sort - ha ezek egymásba futnak, az egyik mentés nyomtalanul elveszne.
    with STATE_LOCK:
        return _save_locked(patch)


def _save_locked(patch):
    try:
        state = load_state(strict=True)
    except StateUnreadable as e:
        return json.loads(json.dumps(DEFAULT_STATE)), str(e)
    if isinstance(patch.get('settings'), dict):
        state['settings'].update({k: v for k, v in patch['settings'].items()
                                  if k in DEFAULT_STATE['settings']})
    if isinstance(patch.get('queue'), list):
        state['queue'] = patch['queue'][:5000]
    if isinstance(patch.get('cwd'), str):
        state['cwd'] = patch['cwd']
    # A megtekintési pontokat összefésüljük, nem felülírjuk: a felület és a
    # lejátszó ugyanabba a fájlba ír, csak más kulcsot.
    if isinstance(patch.get('positions'), dict):
        merged = dict(state['positions'])
        for key, val in patch['positions'].items():
            if val is None:
                merged.pop(key, None)
            elif valid_position(val):
                merged[key] = val
            # Ami nem megtekintési pont, azt eldobjuk: egyetlen hibás érték
            # különben a későbbi mentéseket is elrontaná.
        if len(merged) > POSITION_LIMIT:
            oldest = sorted(merged.items(), key=position_age)
            for key, _ in oldest[:len(merged) - POSITION_LIMIT]:
                merged.pop(key, None)
        state['positions'] = merged
    # A revíziószám csak azt követi, amit a felület birtokol. A lejátszó
    # tízmásodpercenkénti pozíciómentése nem érintheti, különben minden
    # megnyitott lap revíziója pillanatok alatt elavulna.
    if any(k in patch for k in ('queue', 'settings', 'cwd')):
        state['rev'] = int(state.get('rev', 0)) + 1
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        tmp = STATE_PATH + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as fh:
            json.dump(state, fh, ensure_ascii=False, indent=1)
        os.replace(tmp, STATE_PATH)          # atomi csere, ne maradjon fél fájl
    except OSError as e:
        return state, str(e)
    return state, ''


def remember_position(path, pos, dur):
    """Elmenti, hol tartasz egy fájlban. A vége felé inkább elfelejti."""
    if not path:
        return
    if dur > 0 and pos > dur - RESUME_TAIL:
        forget_position(path)      # végignézve: legközelebb elölről
        return
    if pos < RESUME_MIN:
        # Túl korai pont: nem mentjük - de a KORÁBBIT sem dobjuk el. Enélkül
        # egy elölről induló lejátszás pár másodperc alatt letörölné azt a
        # pontot, ahonnan folytatni akartunk.
        return
    save_state({'positions': {path: {'pos': round(pos, 1),
                                     'dur': round(dur, 1),
                                     'at': time.time()}}})


def forget_position(path):
    if path and path in load_state().get('positions', {}):
        save_state({'positions': {path: None}})


def saved_position(path):
    entry = load_state().get('positions', {}).get(path)
    if not isinstance(entry, dict):
        return 0.0
    try:
        return float(entry.get('pos', 0))
    except (TypeError, ValueError):
        return 0.0


# A TV-vezérlés állapota és a legutóbb megtalált eszközök.
PLAYER = dlna.Player()
RENDERERS = {}
RENDERERS_LOCK = threading.Lock()


def media_base(explicit=''):
    """Az a cím, amelyen a TV eléri a fájljainkat."""
    if explicit:
        return explicit.rstrip('/')
    ips = lan_addresses()
    return 'http://%s:%d' % (ips[0], CFG.port) if ips else ''


def media_url(path, base='', endpoint='/api/media'):
    url = '%s%s?path=%s' % (media_base(base), endpoint, quote(path, safe=''))
    if CFG.token:
        url += '&t=' + CFG.token
    return url


def safe_path(p):
    """Feloldja az utat, és None-t ad, ha kilógna a gyökérből."""
    if not p:
        p = CFG.root
    ap = os.path.realpath(os.path.abspath(os.path.expanduser(p)))
    try:
        if os.path.commonpath([ap, CFG.root]) != CFG.root:
            return None
    except ValueError:
        return None
    return ap


def rovid_ut(path, base):
    """Rövid út a kiíráshoz. Windowson más meghajtóra nincs relatív út."""
    try:
        return os.path.relpath(path, base)
    except ValueError:
        return path


def strip_sub_suffix(base):
    """'Film.hu' -> 'Film' (nyelvi utótag levágása)."""
    m = re.match(r'^(.*)\.([A-Za-z]{2,3})$', base)
    if m and m.group(2).lower() in LANG_NAMES:
        return m.group(1)
    return base


def guess_lang(name):
    m = re.search(r'\.([A-Za-z]{2,3})\.(srt|vtt)$', name, re.I)
    if m:
        code = m.group(1).lower()
        if code in LANG_NAMES:
            return code
    return ''


def list_dir(path, show_hidden=False):
    """Egy könyvtár tartalma: almappák + médiafájlok, feliratokkal párosítva."""
    dirs, files = [], []
    subs_by_base = {}
    try:
        entries = list(os.scandir(path))
    except (PermissionError, FileNotFoundError, NotADirectoryError) as e:
        raise e

    for e in entries:
        name = e.name
        if not show_hidden and name.startswith('.'):
            continue
        full = os.path.join(path, name)
        try:
            is_dir = e.is_dir(follow_symlinks=True)
        except OSError:
            continue
        if is_dir:
            dirs.append({'name': name, 'path': full})
            continue

        ext = os.path.splitext(name)[1].lower()
        if ext in SUB_EXT:
            base = os.path.splitext(name)[0]
            entry = {'name': name, 'path': full, 'lang': guess_lang(name)}
            for key in {base, strip_sub_suffix(base)}:
                subs_by_base.setdefault(key, []).append(entry)
            continue
        if ext not in MEDIA_EXT:
            continue
        try:
            st = e.stat()
            size, mtime = st.st_size, st.st_mtime
        except OSError:
            size, mtime = 0, 0
        files.append({
            'name': name, 'path': full, 'ext': ext, 'size': size, 'mtime': mtime,
            'kind': kind_of(ext), 'mime': MIME.get(ext, 'application/octet-stream'),
            'subs': [],
        })

    for f in files:
        f['subs'] = subs_by_base.get(os.path.splitext(f['name'])[0], [])

    dirs.sort(key=lambda d: natkey(d['name']))
    files.sort(key=lambda f: natkey(f['name']))
    return dirs, files


def scan_recursive(path, show_hidden=False, max_files=3000, max_depth=8):
    """Rekurzív médiakeresés egy mappában, természetes sorrendben."""
    out = []
    base_depth = path.rstrip(os.sep).count(os.sep)

    for cur, dirnames, _ in os.walk(path, followlinks=False):
        if cur.count(os.sep) - base_depth >= max_depth:
            dirnames[:] = []
        if not show_hidden:
            dirnames[:] = [d for d in dirnames if not d.startswith('.')]
        dirnames.sort(key=natkey)
        try:
            _, files = list_dir(cur, show_hidden)
        except OSError:
            continue
        out.extend(files)
        if len(out) >= max_files:
            return out[:max_files], True
    return out, False


def srt_to_vtt(text):
    text = text.lstrip('﻿').replace('\r\n', '\n').replace('\r', '\n')
    text = re.sub(r'(\d{2}:\d{2}:\d{2}),(\d{3})', r'\1.\2', text)
    return 'WEBVTT\n\n' + text


def read_text(path):
    with open(path, 'rb') as fh:
        raw = fh.read()
    for enc in ('utf-8-sig', 'utf-8', 'cp1250', 'iso-8859-2', 'latin-1'):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode('utf-8', 'replace')


_LAN_CACHE = {'at': 0.0, 'ips': []}
_LAN_LOCK = threading.Lock()


def lan_addresses():
    """A gép LAN IP-i, elsőként a legvalószínűbb kimenő címmel.

    30 másodpercig gyorsítótárazva: enélkül minden médiaURL-hez elindulna
    egy külön ifconfig folyamat.
    """
    now = time.time()
    with _LAN_LOCK:
        if _LAN_CACHE['ips'] and now - _LAN_CACHE['at'] < 30:
            return list(_LAN_CACHE['ips'])
    ips = _lan_addresses_uncached()
    with _LAN_LOCK:
        _LAN_CACHE['at'] = now
        _LAN_CACHE['ips'] = list(ips)
    return ips


def _lan_addresses_uncached():
    found = []

    def add(ip):
        if ip and not ip.startswith('127.') and ip not in found:
            found.append(ip)

    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.settimeout(0.2)
            s.connect(('8.8.8.8', 80))      # nem küld csomagot, csak route-ot néz
            add(s.getsockname()[0])
        finally:
            s.close()                       # hálózat nélkül is záruljon
    except OSError:
        pass

    try:
        out = subprocess.run(['/sbin/ifconfig', '-a'], capture_output=True,
                             text=True, timeout=3).stdout
        for m in re.finditer(r'inet (\d+\.\d+\.\d+\.\d+)', out):
            add(m.group(1))
    except (OSError, subprocess.SubprocessError):
        try:
            for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
                add(info[4][0])
        except OSError:
            pass

    def rank(ip):
        if ip.startswith('192.168.'):
            return 0
        if ip.startswith('10.'):
            return 1
        if re.match(r'^172\.(1[6-9]|2\d|3[01])\.', ip):
            return 2
        if ip.startswith('169.254.'):
            return 9
        return 5

    # A kimenő cím marad elöl, a többit relevancia szerint rendezzük mögé.
    if found:
        head, tail = found[0], found[1:]
        tail.sort(key=rank)
        return [head] + tail
    return []


DURATION_CACHE = {}
DURATION_LOCK = threading.Lock()


def probe_duration(path):
    """A fájl hossza másodpercben. A TV-k jó része nem jelenti, mi viszont tudjuk."""
    if not CFG.ffprobe:
        return 0.0
    try:
        st = os.stat(path)
    except OSError:
        return 0.0
    key = (path, st.st_mtime, st.st_size)
    with DURATION_LOCK:
        if key in DURATION_CACHE:
            return DURATION_CACHE[key]
    value = 0.0
    try:
        out = subprocess.run(
            [CFG.ffprobe, '-v', 'error', '-show_entries', 'format=duration',
             '-of', 'default=nw=1:nk=1', path],
            capture_output=True, text=True, timeout=20).stdout.strip()
        if out and out != 'N/A':
            value = float(out)
    except (OSError, subprocess.SubprocessError, ValueError):
        value = 0.0
    with DURATION_LOCK:
        DURATION_CACHE[key] = value
    return value


def probe_codecs(path):
    """(video_codec, pix_fmt, audio_codec) az ffprobe-tól, vagy (None, None, None)."""
    if not CFG.ffprobe:
        return None, None, None
    try:
        out = subprocess.run(
            [CFG.ffprobe, '-v', 'error', '-show_entries',
             'stream=codec_type,codec_name,pix_fmt', '-of', 'json', path],
            capture_output=True, text=True, timeout=15).stdout
        data = json.loads(out)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None, None, None
    v = a = pix = None
    for st in data.get('streams', []):
        if st.get('codec_type') == 'video' and v is None:
            v, pix = st.get('codec_name'), st.get('pix_fmt')
        elif st.get('codec_type') == 'audio' and a is None:
            a = st.get('codec_name')
    return v, pix, a


# --------------------------------------------------------------------------
# HTTP kiszolgáló
# --------------------------------------------------------------------------

# realpath, nem abspath: a lenti ellenőrzés is feloldott utat hasonlít össze,
# és a /tmp -> /private/tmp féle symlinkek különben az egész felületet 404-ezik.
PUBLIC_DIR = os.path.join(os.path.dirname(os.path.realpath(__file__)), 'public')
CHUNK = 256 * 1024

# Mennyit kért és kapott a készülék az utóbbi időben. Csak akkor kérdezzük le,
# ha a lejátszás akadozik - addig egyetlen sor egy kiszolgált kérésenként.
MEDIA_STATS = collections.deque(maxlen=4096)
MEDIA_LOCK = threading.Lock()


def media_served(bytes_out):
    with MEDIA_LOCK:
        MEDIA_STATS.append((time.time(), bytes_out))


def media_rate(window):
    """(kérés, MB) az utolsó `window` másodpercben."""
    hatar = time.time() - max(1.0, window)
    with MEDIA_LOCK:
        friss = [b for t, b in MEDIA_STATS if t >= hatar]
    return len(friss), sum(friss) / 1048576.0


def stall_report(haladt, eltelt):
    """A UPnP-réteg jelzi, hogy akad a kép; a hálózati számokat mi tesszük mellé."""
    kerés, mb = media_rate(eltelt + 2)
    reszlet = '%d kérés, %.0f MB' % (kerés, mb)
    OUT.warn('akadozik a lejátszás: %.0f mp videó %.0f mp alatt - közben %s'
             % (haladt, eltelt, reszlet))
    return reszlet


CORS = {
    'Access-Control-Allow-Origin': '*',
    'Access-Control-Allow-Methods': 'GET, HEAD, OPTIONS',
    'Access-Control-Allow-Headers': 'Range, Content-Type',
    'Access-Control-Expose-Headers': 'Content-Length, Content-Range, Accept-Ranges',
}


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    server_version = 'CastStudio/1.0'

    timeout = 60

    # -- segédek ----------------------------------------------------------
    def log_message(self, fmt, *args):
        if CFG.verbose:
            OUT.note('%s  %s' % (self.address_string(), fmt % args))

    def log_request(self, code='-', size='-'):
        if not CFG.verbose:
            return
        try:
            n = int(code)
        except (TypeError, ValueError):
            n = 0
        # A másodpercenkénti állapotlekérdezés elárasztaná a naplót: csak ha hibás.
        # Hibás kérés-sornál (400/414/505) a stdlib még a self.path felvétele
        # előtt naplóz, ezért itt semmit nem szabad biztosra venni.
        path = getattr(self, 'path', '-')
        if n == 200 and path.startswith('/api/dlna/state'):
            return
        OUT.request(self.address_string(), getattr(self, 'command', '-'), code, path)

    def log_error(self, fmt, *args):
        if CFG.verbose:
            OUT.warn('%s  %s' % (self.address_string(), fmt % args))

    def handle_one_request(self):
        # A TV a lejátszás végén nyersen bontja a kapcsolatot; ez nem hiba.
        try:
            BaseHTTPRequestHandler.handle_one_request(self)
        except (ConnectionResetError, BrokenPipeError, socket.timeout, OSError):
            self.close_connection = True
        except Exception:
            # Egy naplózási vagy feldolgozási hiba ne szakítsa meg a kiszolgálást.
            self.close_connection = True

    def _head(self, code, headers, body_len=None):
        self.send_response(code)
        for k, v in headers.items():
            self.send_header(k, v)
        if body_len is not None:
            self.send_header('Content-Length', str(body_len))
        self.end_headers()

    def send_json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode('utf-8')
        self._head(code, dict(CORS, **{'Content-Type': 'application/json; charset=utf-8',
                                       'Cache-Control': 'no-store'}), len(body))
        if self.command != 'HEAD':
            self._write(body)

    def send_text(self, code, text, ctype='text/plain; charset=utf-8'):
        body = text.encode('utf-8')
        self._head(code, dict(CORS, **{'Content-Type': ctype,
                                       'Cache-Control': 'no-store'}), len(body))
        if self.command != 'HEAD':
            self._write(body)

    def _write(self, data):
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True

    # -- útválasztás ------------------------------------------------------
    def do_OPTIONS(self):
        self._head(204, CORS, 0)

    def do_HEAD(self):
        self.route()

    def do_GET(self):
        self.route()

    def do_POST(self):
        parsed = urlparse(self.path)
        q = parse_qs(parsed.query)
        if CFG.token and q.get('t', [''])[0] != CFG.token:
            return self.send_json(403, {'error': 'Érvénytelen vagy hiányzó token.'})
        MAX_BODY = 16 * 1024 * 1024
        try:
            length = int(self.headers.get('Content-Length') or 0)
        except ValueError:
            length = 0
        if length > MAX_BODY:
            return self.send_json(413, {'error': 'Túl nagy kérés.'})
        if self.headers.get('Transfer-Encoding'):
            # Darabolt törzset nem olvasunk; némán elnyelni viszont rosszabb
            # lenne, mint kimondani, hogy nem tudjuk feldolgozni.
            self.close_connection = True
            return self.send_json(411, {'error': 'Add meg a kérés hosszát (Content-Length).'})
        raw = self.rfile.read(length) if length > 0 else b'{}'
        try:
            payload = json.loads(raw.decode('utf-8') or '{}')
        except ValueError:
            return self.send_json(400, {'error': 'Hibás JSON.'})

        try:
            if parsed.path == '/api/state':
                payload = payload if isinstance(payload, dict) else {}
                # Ha a hívó megmondta, milyen állapotra épít, és azóta más is
                # írt, nem söpörjük el szó nélkül a másik lap munkáját.
                if (isinstance(payload.get('rev'), int)
                        and any(k in payload for k in ('queue', 'settings', 'cwd'))
                        and payload['rev'] != load_state().get('rev', 0)):
                    return self.send_json(409, {
                        'error': 'Közben egy másik lap is módosította a sort.',
                        'state': load_state()})
                state, err = save_state(payload)
                if err:
                    return self.send_json(500, {'error': 'Nem sikerült menteni: %s' % err})
                return self.send_json(200, state)
            if parsed.path == '/api/dlna/queue':
                if not isinstance(payload, dict):
                    return self.send_json(400, {'error': 'Hibás kérés: objektum kell.'})
                return self.dlna_set_queue(payload)
            return self.send_json(404, {'error': 'Ismeretlen végpont.'})
        except Exception as exc:
            return self.send_json(500, {'error': '%s: %s' % (type(exc).__name__, exc)})

    def route(self):
        parsed = urlparse(self.path)
        path = parsed.path
        q = parse_qs(parsed.query)

        def p(name, default=''):
            return q.get(name, [default])[0]

        try:
            if path.startswith('/api/'):
                if CFG.token and p('t') != CFG.token:
                    return self.send_json(403, {'error': 'Érvénytelen vagy hiányzó token.'})
                if path == '/api/info':
                    return self.api_info()
                if path == '/api/state':
                    return self.send_json(200, load_state())
                if path == '/api/browse':
                    return self.api_browse(p('path'), p('hidden') == '1')
                if path == '/api/scan':
                    return self.api_scan(p('path'), p('hidden') == '1')
                if path == '/api/media':
                    return self.serve_file(p('path'))
                if path == '/api/sub':
                    return self.serve_sub(p('path'))
                if path == '/api/stream':
                    return self.serve_stream(p('path'), p('mode', 'auto'), p('ss', '0'))
                if path.startswith('/api/dlna/'):
                    return self.dlna_route(path[len('/api/dlna/'):], p)
                return self.send_json(404, {'error': 'Ismeretlen végpont.'})

            if path == '/favicon.ico':
                # A böngésző akkor is kéri, ha a HTML beágyazott ikont ad meg.
                icon = ("<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'>"
                        "<text y='.9em' font-size='90'>\U0001F4FA</text></svg>")
                return self.send_text(200, icon, 'image/svg+xml')
            if path in ('/', '/index.html'):
                return self.serve_static('index.html')
            return self.serve_static(path.lstrip('/'))
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        except Exception as exc:  # nem dőljön el a szerver egy kérés miatt
            # A részletek a konzolra valók: a válaszba írva abszolút utak és
            # belső adatok szivárognának ki a hálózatra.
            OUT.error('Kérés közben hiba: %s: %s' % (type(exc).__name__, exc))
            try:
                self.send_json(500, {'error': 'Váratlan hiba a kiszolgálóban.'})
            except OSError:
                pass

    # -- DLNA / TV-vezérlés -----------------------------------------------
    def dlna_route(self, action, p):
        if action == 'discover':
            try:
                timeout = float(p('timeout', '5'))
            except ValueError:
                timeout = 5.0
            found = dlna.discover(timeout=max(1.0, min(timeout, 15.0)))
            with RENDERERS_LOCK:
                RENDERERS.clear()
                for r in found:
                    RENDERERS[r['udn']] = r
            cur = PLAYER.renderer
            return self.send_json(200, {
                'renderers': [{k: v for k, v in r.items() if k != 'location'}
                              for r in found],
                'selected': cur['udn'] if cur else None,
            })

        if action == 'select':
            udn = p('udn')
            with RENDERERS_LOCK:
                r = RENDERERS.get(udn)
            if not r:
                # Újraindítás után üres a gyorsítótár – keressünk rá magunktól.
                for found in dlna.discover(timeout=5):
                    with RENDERERS_LOCK:
                        RENDERERS[found['udn']] = found
                    if found['udn'] == udn:
                        r = found
            if not r:
                return self.send_json(404, {'error': 'Ez az eszköz most nem érhető el a hálózaton.'})
            PLAYER.select(r)
            return self.send_json(200, PLAYER.snapshot())

        if action == 'state':
            return self.send_json(200, PLAYER.snapshot())

        if not PLAYER.renderer:
            return self.send_json(409, {'error': 'Előbb válassz TV-t.'})

        # A hibás bemenet a kérés hibája (400), nem a készüléké (502).
        if action == 'play':
            try:
                idx = int(p('index', '0'))
            except ValueError:
                return self.send_json(400, {'error': 'Érvénytelen sorszám.'})
            ok, err = PLAYER.switch(idx)
        elif action == 'toggle':
            ok, err = PLAYER.toggle()
        elif action == 'pause':
            ok, err = PLAYER.pause()
        elif action == 'resume':
            ok, err = PLAYER.resume()
        elif action == 'stop':
            ok, err = PLAYER.stop()
        elif action == 'next':
            ok, err = PLAYER.skip(1)
        elif action == 'prev':
            ok, err = PLAYER.skip(-1)
        elif action == 'seek':
            try:
                target = float(p('to', '0'))
            except (ValueError, OverflowError):
                return self.send_json(400, {'error': 'Érvénytelen időpont.'})
            # A végtelen és a NaN átcsúszna a float() szűrőjén, de a TV-nek
            # küldött időformátumot már nem lehet belőle előállítani.
            if not math.isfinite(target) or target < 0:
                return self.send_json(400, {'error': 'Érvénytelen időpont.'})
            ok, err = PLAYER.seek(target)
        elif action == 'volume':
            try:
                value = float(p('level', '50'))
                if not math.isfinite(value):
                    raise ValueError('nem véges')
                level = max(0, min(100, int(value)))
            except (ValueError, OverflowError):
                return self.send_json(400, {'error': 'Érvénytelen hangerő.'})
            ok, err = PLAYER.set_volume(level)
        elif action == 'mute':
            ok, err = PLAYER.set_mute(p('on') == '1')
        elif action == 'repeat':
            mode = p('mode', 'OFF').upper()
            PLAYER.repeat = mode if mode in ('OFF', 'ALL', 'SINGLE') else 'OFF'
            ok, err = True, ''
        else:
            return self.send_json(404, {'error': 'Ismeretlen művelet: %s' % action})

        if not ok:
            client_error = err in ('A lejátszási sor üres.', 'Nincs ilyen elem a sorban.')
            return self.send_json(400 if client_error else 502,
                                  {'error': err, 'state': PLAYER.snapshot()})
        return self.send_json(200, PLAYER.snapshot())

    def dlna_set_queue(self, payload):
        """A böngésző átadja a sort; innentől a szerver lépteti."""
        if not PLAYER.renderer:
            return self.send_json(409, {'error': 'Előbb válassz TV-t.'})
        base = payload.get('base', '')
        if not isinstance(base, str):
            base = ''
        raws = payload.get('items')
        if not isinstance(raws, list):
            return self.send_json(400, {'error': 'Hibás kérés: nincs lejátszási sor.'})

        items, skipped = [], []
        # A kért sorszám a KÜLDÖTT listára vonatkozik; ha közben kiesik elem,
        # az elmozdulna. Ezért a kiszolgálható elemek eredeti helyét is
        # megjegyezzük, és abból számoljuk vissza a kezdő elemet.
        origins = []
        for pos, raw in enumerate(raws):
            if not isinstance(raw, dict):
                skipped.append('%d. elem' % (pos + 1))
                continue
            path = safe_path(raw.get('path', '') if isinstance(raw.get('path'), str) else '')
            if not path or not os.path.isfile(path):
                nev = raw.get('title') or raw.get('path') or '%d. elem' % (pos + 1)
                skipped.append(os.path.basename(str(nev)))
                continue
            try:
                meret = os.path.getsize(path)
            except OSError:
                meret = 0
            if meret <= 0:
                # Üres fájlra a TV a Range-kérésre 416-ot kap, tehát sosem indul
                # el - a felületen viszont "menne", és pontot is gyűjtene.
                skipped.append(os.path.basename(path))
                continue
            ext = os.path.splitext(path)[1].lower()
            sub = None
            subs = raw.get('subs')
            for sr in (subs if isinstance(subs, list) else []):
                if not isinstance(sr, dict) or not isinstance(sr.get('path'), str):
                    continue
                sp = safe_path(sr.get('path', ''))
                if sp and os.path.isfile(sp):
                    sub = media_url(sp, base, '/api/sub')
                    break

            mime = MIME.get(ext, 'application/octet-stream')
            kind = kind_of(ext)
            endpoint = '/api/media'
            # Amit a TV nem vesz fel a listájára, azt menet közben átkódoljuk.
            if (PLAYER.mimes and mime not in PLAYER.mimes
                    and kind == 'video' and CFG.ffmpeg):
                endpoint, mime = '/api/stream', 'video/mp4'

            origins.append(pos)
            items.append({
                'path': path,
                'size': meret,                   # a bájt alapú tekeréshez kell
                'name': os.path.basename(path),
                'title': raw.get('title') or os.path.splitext(os.path.basename(path))[0],
                'kind': kind,
                'mime': mime,
                'url': media_url(path, base, endpoint),
                'sub': sub,
            })
        if not items:
            return self.send_json(400, {'error': 'Nincs kiszolgálható elem a sorban.'})
        try:
            index = int(payload.get('index', 0))
        except (TypeError, ValueError):
            index = 0
        # A kért elem helyét a szűrt listában keressük meg: kiesett fájlok
        # miatt különben más indulna el, mint amire kattintottál.
        index = next((i for i, o in enumerate(origins) if o >= index), len(items) - 1)
        mode = str(payload.get('repeat', 'OFF')).upper()
        PLAYER.repeat = mode if mode in ('OFF', 'ALL', 'SINGLE') else 'OFF'
        PLAYER.auto_resume = bool(load_state()['settings'].get('resume', True))
        ok, err = PLAYER.set_queue(items, max(0, min(index, len(items) - 1)))
        if not ok:
            return self.send_json(502, {'error': err, 'state': PLAYER.snapshot()})
        snap = PLAYER.snapshot()
        # A kihagyott fájlokról szólni kell: enélkül a felület más elemet
        # jelölne meg játszóként, mint ami valójában megy.
        snap['skipped'] = skipped
        snap['paths'] = [it['path'] for it in items]
        return self.send_json(200, snap)

    # -- API --------------------------------------------------------------
    def api_info(self):
        addrs = lan_addresses()
        port = CFG.port
        self.send_json(200, {
            'root': CFG.root,
            'rootName': os.path.basename(CFG.root) or CFG.root,
            'port': port,
            'addresses': addrs,
            'mediaBase': 'http://%s:%d' % (addrs[0], port) if addrs else '',
            'shortcuts': self.shortcuts(),
            'ffmpeg': bool(CFG.ffmpeg),
            'sep': os.sep,
            'stateFile': STATE_PATH,
        })

    def shortcuts(self):
        out = []
        home = os.path.realpath(os.path.expanduser('~'))
        cands = [('Kezdőlap', home)]
        for label, sub in [('Filmek', 'Movies'), ('Videók', 'Videos'),
                           ('Zene', 'Music'), ('Képek', 'Pictures'),
                           ('Letöltések', 'Downloads'), ('Asztal', 'Desktop'),
                           ('Dokumentumok', 'Documents')]:
            cands.append((label, os.path.join(home, sub)))
        cands.append(('Kötetek', '/Volumes'))
        for label, path in cands:
            real = safe_path(path)
            if real and os.path.isdir(real) and all(s['path'] != real for s in out):
                out.append({'label': label, 'path': real})
        return out

    def api_browse(self, raw, hidden):
        path = safe_path(raw)
        if not path or not os.path.isdir(path):
            return self.send_json(404, {'error': 'A mappa nem érhető el: %s' % (raw or CFG.root)})
        try:
            dirs, files = list_dir(path, hidden)
        except PermissionError:
            return self.send_json(403, {'error': 'Nincs jogosultság a mappához.'})
        except OSError:
            # A mappa eltűnhet a két lépés között; ez nem szerverhiba.
            return self.send_json(404, {'error': 'A mappa időközben eltűnt.'})
        parent = os.path.dirname(path)
        if not safe_path(parent) or parent == path:
            parent = None
        self.send_json(200, {
            'path': path, 'parent': parent, 'root': CFG.root,
            'crumbs': self.crumbs(path), 'dirs': dirs, 'files': files,
        })

    def crumbs(self, path):
        out, cur = [], path
        while True:
            out.append({'name': os.path.basename(cur) or cur, 'path': cur})
            if cur == CFG.root:
                break
            parent = os.path.dirname(cur)
            if parent == cur or not safe_path(parent):
                break
            cur = parent
        out.reverse()
        if out:
            out[0]['name'] = os.path.basename(CFG.root) or CFG.root
        return out

    def api_scan(self, raw, hidden):
        path = safe_path(raw)
        if not path or not os.path.isdir(path):
            return self.send_json(404, {'error': 'A mappa nem érhető el.'})
        files, truncated = scan_recursive(path, hidden)
        self.send_json(200, {'path': path, 'files': files, 'truncated': truncated})

    # -- fájlkiszolgálás --------------------------------------------------
    def serve_static(self, rel):
        full = os.path.realpath(os.path.join(PUBLIC_DIR, rel.lstrip('/')))
        try:
            inside = os.path.commonpath([full, PUBLIC_DIR]) == PUBLIC_DIR
        except ValueError:
            inside = False
        if not inside or not os.path.isfile(full):
            return self.send_text(404, 'Nincs ilyen oldal.')
        ext = os.path.splitext(full)[1].lower()
        try:
            with open(full, 'rb') as fh:
                body = fh.read()
        except OSError:
            return self.send_text(404, 'Az oldal nem olvasható.')
        self._head(200, {'Content-Type': MIME.get(ext, 'application/octet-stream'),
                         'Cache-Control': 'no-cache'}, len(body))
        if self.command != 'HEAD':
            self._write(body)

    def serve_sub(self, raw):
        path = safe_path(raw)
        if not path or not os.path.isfile(path):
            return self.send_text(404, 'Nincs ilyen felirat.')
        ext = os.path.splitext(path)[1].lower()
        if ext not in SUB_EXT:
            return self.send_text(400, 'Nem feliratfájl.')
        try:
            text = read_text(path)
        except OSError:
            return self.send_text(404, 'A felirat nem olvasható.')
        if ext == '.srt' or not text.lstrip().startswith('WEBVTT'):
            text = srt_to_vtt(text)
        self.send_text(200, text, 'text/vtt; charset=utf-8')

    def serve_file(self, raw):
        path = safe_path(raw)
        if not path or not os.path.isfile(path):
            return self.send_text(404, 'Nincs ilyen fájl.')
        # Előbb megnyitjuk, és csak a nyitott leíróból mérünk hosszt. Ha a fájl
        # olvashatatlan vagy közben eltűnik, még 404-et tudunk adni - a hosszt
        # kiírva viszont már csak egy 200-as, üres válasz maradna a TV-nek.
        try:
            fh = open(path, 'rb')
            size = os.fstat(fh.fileno()).st_size
        except OSError:
            try:
                fh.close()
            except (OSError, NameError, UnboundLocalError):
                pass
            return self.send_text(404, 'A fájl nem olvasható.')

        try:
            return self._pump_file(fh, path, size)
        finally:
            try:
                fh.close()
            except OSError:
                pass

    def _pump_file(self, fh, path, size):
        ext = os.path.splitext(path)[1].lower()
        headers = dict(CORS)
        headers['Content-Type'] = MIME.get(ext, 'application/octet-stream')
        headers['Accept-Ranges'] = 'bytes'
        headers['Cache-Control'] = 'no-cache'
        # A DLNA-kliensek ezeket várják, különben van, amelyik el sem indítja.
        headers['transferMode.dlna.org'] = 'Streaming'
        headers['contentFeatures.dlna.org'] = dlna.DLNA_FLAGS

        start, end = 0, size - 1
        status = 200
        rng = self.headers.get('Range')
        if rng:
            m = re.match(r'^bytes=(\d*)-(\d*)$', rng.strip())
            if m:
                a, b = m.group(1), m.group(2)
                if a == '':
                    if b == '':
                        return self.range_error(size, headers)
                    start = max(0, size - int(b))
                else:
                    start = int(a)
                    if b != '':
                        end = min(int(b), size - 1)
                if start > end or start >= size:
                    return self.range_error(size, headers)
                status = 206
                headers['Content-Range'] = 'bytes %d-%d/%d' % (start, end, size)

        length = end - start + 1
        self._head(status, headers, length)
        if self.command == 'HEAD':
            return

        remaining = length
        kiment = 0
        try:
            fh.seek(start)
            while remaining > 0:
                data = fh.read(min(CHUNK, remaining))
                if not data:
                    # A fájl kifogyott alólunk: a megígért hosszt már nem
                    # tudjuk kiszolgálni, ezért a kapcsolatot is lezárjuk -
                    # különben a kliens a hiányzó bájtokra várna.
                    self.close_connection = True
                    break
                self.wfile.write(data)
                remaining -= len(data)
                kiment += len(data)
        except (BrokenPipeError, ConnectionResetError):
            # A TV lejátszás közben bont és újranyit - ez normális.
            self.close_connection = True
        except OSError:
            self.close_connection = True
        finally:
            # A megszakadt átvitel is forgalom volt: akadozásnál épp az mutatja
            # meg, hogy a készülék elkérte, majd eldobta az adatot.
            media_served(kiment)

    def range_error(self, size, headers):
        headers['Content-Range'] = 'bytes */%d' % size
        self._head(416, headers, 0)

    def serve_stream(self, raw, mode, ss):
        """Élő átcsomagolás/átkódolás ffmpeg-gel (ha telepítve van)."""
        if not CFG.ffmpeg:
            return self.send_text(503, 'Az ffmpeg nincs telepítve a gépen.')
        path = safe_path(raw)
        if not path or not os.path.isfile(path):
            return self.send_text(404, 'Nincs ilyen fájl.')
        try:
            offset = max(0.0, float(ss))
        except ValueError:
            offset = 0.0

        vcodec, pix, acodec = probe_codecs(path)
        if mode == 'auto':
            can_copy_v = (vcodec == 'h264' and (pix or 'yuv420p').startswith('yuv420p'))
            mode = 'remux' if can_copy_v or vcodec is None else 'transcode'
        copy_audio = acodec in ('aac', 'mp3')

        args = [CFG.ffmpeg, '-hide_banner', '-loglevel', 'error']
        if offset > 0:
            args += ['-ss', '%.3f' % offset]
        args += ['-i', path]
        if mode == 'remux':
            args += ['-c:v', 'copy']
        else:
            args += ['-c:v', 'libx264', '-preset', 'veryfast', '-crf', '21',
                     '-pix_fmt', 'yuv420p', '-maxrate', '8M', '-bufsize', '16M']
        args += ['-c:a', 'copy'] if copy_audio else ['-c:a', 'aac', '-ac', '2', '-b:a', '192k']
        args += ['-sn', '-dn', '-map', '0:v:0?', '-map', '0:a:0?',
                 '-f', 'mp4', '-movflags', 'frag_keyframe+empty_moov+default_base_moof',
                 'pipe:1']

        proc = subprocess.Popen(args, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, bufsize=0)
        headers = dict(CORS)
        headers['Content-Type'] = 'video/mp4'
        headers['Cache-Control'] = 'no-store'
        headers['Connection'] = 'close'
        self.close_connection = True
        self.send_response(200)
        for k, v in headers.items():
            self.send_header(k, v)
        self.end_headers()
        try:
            if self.command != 'HEAD':
                while True:
                    data = proc.stdout.read(CHUNK)
                    if not data:
                        break
                    self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            self._reap(proc)

    @staticmethod
    def _reap(proc):
        """Az ffmpeg elengedése úgy, hogy se zombi, se nyitott cső ne maradjon."""
        try:
            proc.kill()
        except OSError:
            pass
        try:
            if proc.stdout:
                proc.stdout.close()
        except OSError:
            pass
        try:
            proc.wait(timeout=5)            # enélkül zombiként maradna
        except (subprocess.SubprocessError, OSError):
            pass


# --------------------------------------------------------------------------
# Indítás
# --------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description='Cast Studio - helyi médialejátszó DLNA-s TV-hez')
    ap.add_argument('--root', default=os.path.expanduser('~'),
                    help='a böngészhető gyökérkönyvtár (alap: a home könyvtárad)')
    ap.add_argument('--port', type=int, default=8420, help='HTTP port (alap: 8420)')
    ap.add_argument('--no-token', action='store_true',
                    help='ne kérjen hozzáférési tokent (csak megbízható hálózaton)')
    ap.add_argument('--no-open', action='store_true', help='ne nyissa meg a böngészőt')
    ap.add_argument('--verbose', action='store_true', help='kérésnaplózás')
    ap.add_argument('--data', default='',
                    help='hova írja az állapotfájlt (alap: az app data/ mappája)')
    ap.add_argument('--keep-playing', action='store_true',
                    help='kilépéskor ne állítsa meg a TV-t (a pufferelt részt lejátssza)')
    args = ap.parse_args()

    if args.data:
        # Teszteléshez: az állapot ne az app mappájába kerüljön, hanem oda,
        # ahova kérték. Enélkül minden próba a valódi lejátszási sort írná.
        global DATA_DIR, STATE_PATH
        DATA_DIR = os.path.realpath(os.path.abspath(os.path.expanduser(args.data)))
        STATE_PATH = os.path.join(DATA_DIR, 'state.json')

    CFG.root = os.path.realpath(os.path.abspath(os.path.expanduser(args.root)))
    if not os.path.isdir(CFG.root):
        OUT.line('')
        OUT.error('A megadott gyökér nem létező mappa:')
        OUT.note('  %s' % CFG.root)
        OUT.line('')
        sys.exit(1)
    CFG.port = args.port
    CFG.token = '' if args.no_token else secrets.token_hex(10)
    CFG.verbose = args.verbose
    CFG.ffmpeg = shutil.which('ffmpeg')
    CFG.ffprobe = shutil.which('ffprobe')
    dlna.debug_log = lambda msg: OUT.note(msg) if CFG.verbose else None
    PLAYER.duration_probe = probe_duration
    PLAYER.position_load = saved_position
    PLAYER.position_save = remember_position
    PLAYER.position_clear = forget_position
    PLAYER.stall_report = stall_report
    PLAYER.auto_resume = bool(load_state()['settings'].get('resume', True))

    try:
        httpd = ThreadingHTTPServer(('0.0.0.0', CFG.port), Handler)
    except OSError as e:
        OUT.line('')
        OUT.error('Nem sikerült a %d porton elindulni: %s' % (CFG.port, e))
        OUT.note('Fut már egy példány? Vagy próbáld másik porttal:')
        OUT.note('  python3 server.py --port %d' % (CFG.port + 1))
        OUT.line('')
        sys.exit(1)
    httpd.daemon_threads = True

    url = 'http://localhost:%d/' % CFG.port
    if CFG.token:
        url += '?t=' + CFG.token
    ips = lan_addresses()

    OUT.title('Cast Studio', 'helyi médialejátszó DLNA-s TV-hez')
    OUT.row('Felület', OUT.cyan(url))
    if ips:
        OUT.row('TV ezt látja', 'http://%s:%d' % (ips[0], CFG.port))
    else:
        OUT.row('TV ezt látja', OUT.yellow('nincs hálózati cím'))
    OUT.row('Gyökérkönyvtár', CFG.root)
    OUT.row('Átkódolás', 'ffmpeg elérhető' if CFG.ffmpeg
            else OUT.dim('nincs — brew install ffmpeg'))
    OUT.row('Állapotfájl', rovid_ut(STATE_PATH, APP_DIR))
    OUT.line('')
    if not ips:
        OUT.warn('A TV így nem éri el a fájlokat. Csatlakozz a hálózatra.')
    OUT.note('A TV keresése az oldal megnyitásakor automatikus.')
    OUT.note('Leállítás: Ctrl+C')
    OUT.line('')

    if not args.no_open:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        OUT.line('')
        OUT.note('Leállítás…')
    finally:
        PLAYER.shutdown()          # a TV-t figyelő szál is álljon meg
        try:
            PLAYER.remember_now()  # a kilépés ne dobjon el tíz másodpercet
        except Exception:
            pass
        if not args.keep_playing:
            try:
                if PLAYER.stop_if_ours():
                    OUT.note('A TV-n megállítottam a lejátszást.')
            except Exception:
                pass
        httpd.server_close()
        OUT.note('Kész.')
        OUT.line('')


if __name__ == '__main__':
    main()
