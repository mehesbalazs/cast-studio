#!/usr/bin/env python3
"""DLNA / UPnP vezérlőréteg: eszközfelderítés és médiaküldés a TV-re.

A böngésző ezt nem tudná elvégezni (SSDP-hez nyers UDP kell, a TV pedig nem küld
CORS-fejlécet a SOAP-válaszokhoz), ezért az egész itt, a szerverben fut.
"""

import re
import socket
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import urljoin, urlparse, unquote, parse_qs
from xml.sax.saxutils import escape as xesc
from xml.sax.saxutils import unescape as xunesc

SSDP_ADDR = '239.255.255.250'
SSDP_PORT = 1900
AVT = 'urn:schemas-upnp-org:service:AVTransport:1'
RCS = 'urn:schemas-upnp-org:service:RenderingControl:1'
CMS = 'urn:schemas-upnp-org:service:ConnectionManager:1'


# --------------------------------------------------------------------------
# Alacsony szintű segédek
# --------------------------------------------------------------------------

def _tag(xml, name):
    m = re.search(r'<%s[^>]*>(.*?)</%s>' % (name, name), xml, re.S)
    return re.sub(r'\s+', ' ', m.group(1)).strip() if m else ''


def _uri_mag(u):
    """Az URL azonosító része: útvonal + lekérdezés, gépnév és port nélkül.

    A lekérdezést elemezve adjuk vissza, nem nyersen dekódolva: a dekódolás
    összemosná a paraméterhatárt egy '&' jelet tartalmazó fájlnévvel.
    """
    try:
        r = urlparse(u)
    except ValueError:
        return None
    if not r.path:
        return None
    return (unquote(r.path),
            tuple(sorted(parse_qs(r.query, keep_blank_values=True).items())))


def _uri_azonos(a, b):
    """Ugyanarra a médiafájlra mutat-e a két cím.

    A nyers egyezés kevés: a készülék XML-ben adja vissza, tehát a tokenes
    URL '&' jele '&amp;'-ként jön - és van készülék, amelyik újra is kódolja
    az útvonalat. A gépnév pedig egyáltalán nem azonosít: van készülék,
    amelyik a kapott címet átírja. A fájlt a `path=` paraméter jelöli ki,
    tehát az útvonal és a lekérdezés dönt.
    """
    if a == b:
        return True
    entitasok = {'&quot;': '"', '&apos;': "'"}
    a, b = xunesc(a, entitasok), xunesc(b, entitasok)
    if a == b:
        return True
    # Az újrakódolt útvonalat és a más gépnevet is ez fedi le: a `path=`
    # paraméter elemzett értéke dönt.
    mag = _uri_mag(a)
    return mag is not None and mag == _uri_mag(b)


def _outbound_ip(target='8.8.8.8'):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.settimeout(0.3)
        s.connect((target, 80))
        return s.getsockname()[0]
    except OSError:
        return '0.0.0.0'
    finally:
        s.close()


# Ennél hosszabb médiát nem tekintünk valósnak. A készülékek néha képtelen
# időt jelentenek (99:59:59, "inf", több mint három mező); ha ezt elhinnénk,
# az app a fájl végén túlra tekerne, vagy NaN kerülne a felület válaszába.
MAX_MEDIA = 24 * 3600.0


def hms_to_seconds(text):
    """'0:01:23.000' vagy '00:01:23' -> 83.0. Képtelen értékre 0."""
    if not text or text in ('NOT_IMPLEMENTED', 'NOT_IMPLEMENTED.'):
        return 0.0
    parts = text.strip().split(':')
    if len(parts) > 3:
        return 0.0
    try:
        parts = [float(p.replace(',', '.')) for p in parts]
    except ValueError:
        return 0.0
    total = 0.0
    for p in parts:
        total = total * 60 + p
    # A NaN minden összehasonlítást hamissá tesz, ezért ez a végtelent is kizárja.
    if not (0.0 <= total <= MAX_MEDIA):
        return 0.0
    return total


def seconds_to_hms(sec):
    try:
        sec = int(sec)
    except (ValueError, OverflowError):      # NaN, végtelen
        sec = 0
    sec = max(0, min(sec, int(MAX_MEDIA)))
    return '%d:%02d:%02d' % (sec // 3600, (sec % 3600) // 60, sec % 60)


def veges(x, alap=0.0):
    """Csak véges szám mehet ki a felületnek: a NaN érvénytelen JSON-t adna."""
    try:
        x = float(x)
    except (TypeError, ValueError):
        return alap
    return x if 0.0 <= x <= MAX_MEDIA else alap


# A szerver állítja be: a nyers hibarészletek a naplóba mennek, a felhasználó
# elé emberi mondat kerül.
debug_log = None


# A UPnP szabvány hibakódjai - a nyers "Transition not available: 701" helyett.
UPNP_ERRORS = {
    '701': 'A készülék most nem tudja ezt végrehajtani.',
    '702': 'A készülék éppen mást csinál.',
    '705': 'A lejátszás zárolva van a készüléken.',
    '710': 'A készülék nem támogatja ezt a tekerési módot.',
    '711': 'Érvénytelen tekerési pozíció.',
    '712': 'A készülék nem tud erre a helyre tekerni.',
    '714': 'A készülék nem ismeri ezt a formátumot.',
    '715': 'A készülék nem érte el a fájlt.',
    '716': 'A készülék nem találja a fájlt.',
    '718': 'Érvénytelen lejátszási példány.',
    '719': 'A készülék nem fogadta el a leírást.',
    '401': 'A készülék nem ismeri ezt a parancsot.',
    '402': 'A készülék nem fogadta el a paramétereket.',
    '501': 'A készülék nem tudta végrehajtani a parancsot.',
    '600': 'A készülék nem fogadta el a paramétereket.',
    '601': 'A készülék nem fogadta el a paramétereket.',
    '602': 'A készülék nem támogatja ezt a műveletet.',
    '604': 'A készülék szerint túl nagy az érték.',
}


def _friendly(exc):
    """Hálózati kivételből érthető mondat."""
    text = str(exc)
    low = text.lower()
    err = getattr(exc, 'errno', None)
    reason = getattr(exc, 'reason', None)
    if reason is not None:
        err = getattr(reason, 'errno', err) or err
        low = (low + ' ' + str(reason).lower())
    if isinstance(exc, socket.timeout) or 'timed out' in low or err == 60:
        return 'A TV nem válaszolt időben.'
    if err == 61 or 'refused' in low:
        return 'A TV visszautasította a kapcsolatot – lehet, hogy kikapcsolt.'
    if err in (51, 65, 101) or 'unreachable' in low or 'no route' in low:
        return 'A TV nem érhető el a hálózaton.'
    if 'name or service not known' in low or 'nodename' in low:
        return 'A TV címe nem oldható fel.'
    return 'Nem sikerült elérni a TV-t.'


# Ezek a hibakódok az állapotról szólnak, nem arról, hogy rossz a kérés:
# a készülék most nem tud tekerni, de később ugyanígy fog. Ilyenkor nincs
# értelme másik tekerési egységgel próbálkozni.
STATE_ERRORS = ('701', '702', '705', '718')


def soap(control_url, service, action, args=None, timeout=8.0, with_code=False):
    """SOAP-hívás egy UPnP szolgáltatásra.

    Visszaad: (ok, válasz_szöveg), `with_code` esetén (ok, szöveg, UPnP-kód).
    """
    def out(ok, msg, code=''):
        return (ok, msg, code) if with_code else (ok, msg)
    body = ''
    for key, val in (args or []):
        body += '<%s>%s</%s>' % (key, xesc(str(val)), key)
    env = ('<?xml version="1.0" encoding="utf-8"?>'
           '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
           's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
           '<s:Body><u:%s xmlns:u="%s">%s</u:%s></s:Body></s:Envelope>'
           % (action, service, body, action))
    req = urllib.request.Request(
        control_url, data=env.encode('utf-8'), method='POST',
        headers={'Content-Type': 'text/xml; charset="utf-8"',
                 'SOAPAction': '"%s#%s"' % (service, action),
                 'Connection': 'close',
                 'User-Agent': 'CastStudio/2.0 DLNADOC/1.50 UPnP/1.0'})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return out(True, r.read(200000).decode('utf-8', 'replace'))
    except urllib.error.HTTPError as e:
        text = e.read().decode('utf-8', 'replace')
        code = _tag(text, 'errorCode')
        # A készülék szövege tetszőleges hosszú, idegen nyelvű adat lehet: a
        # naplóba mehet, a felhasználó elé nem.
        desc = 'A készülék elutasította a parancsot (%s).' % (code or e.code)
        if debug_log:
            try:
                debug_log('SOAP %s -> UPnP %s (%s)' % (action, code, desc))
            except Exception:
                pass
        return out(False, UPNP_ERRORS.get(code, desc), code)
    except Exception as e:                      # időtúllépés, hálózati hiba
        if debug_log:
            try:
                debug_log('SOAP %s -> %s' % (action, e))
            except Exception:
                pass
        return out(False, _friendly(e))


# --------------------------------------------------------------------------
# Eszközfelderítés
# --------------------------------------------------------------------------

MAX_LOCATIONS = 32     # egy bőbeszédű vagy rosszindulatú válaszoló ne húzza el
SELECT_BUDGET = 12.0   # ennyi idő alatt végezzen a készülék kikérdezése
ERROR_TTL = 6.0        # ennyi ideig kérhető le ugyanaz az üzenet


def _sajat_cim(location, forras_ip):
    """A hirdetett cím tényleg a válaszoló gépé-e?"""
    try:
        host = urlparse(location).hostname or ''
    except ValueError:
        return False
    if host != forras_ip:
        return False
    return not (host.startswith('127.') or host.startswith('169.254.')
                or host == 'localhost')


def discover(timeout=5.0):
    """MediaRenderer eszközök keresése SSDP-vel. Visszaad: renderer-dict lista."""
    ip = _outbound_ip()
    locations = set()

    for st in ('urn:schemas-upnp-org:device:MediaRenderer:1', 'ssdp:all'):
        msg = ('M-SEARCH * HTTP/1.1\r\n'
               'HOST: %s:%d\r\n'
               'MAN: "ssdp:discover"\r\n'
               'MX: 2\r\n'
               'ST: %s\r\n\r\n' % (SSDP_ADDR, SSDP_PORT, st)).encode()
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
            if ip != '0.0.0.0':
                s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF,
                             socket.inet_aton(ip))
            s.bind((ip, 0))
            s.settimeout(0.4)
            for _ in range(2):
                s.sendto(msg, (SSDP_ADDR, SSDP_PORT))
            end = time.time() + timeout / 2
            while time.time() < end:
                try:
                    data, src = s.recvfrom(4096)
                except socket.timeout:
                    continue
                except OSError:
                    break
                text = data.decode('utf-8', 'replace')
                m = re.search(r'(?im)^LOCATION:\s*(\S+)', text)
                # A válaszoló csak saját magát hirdetheti. Enélkül a hálózat
                # bármelyik gépe rávehetné az appot, hogy egy tetszőleges
                # címre indítson kérést a nevünkben.
                if m and not _sajat_cim(m.group(1), src[0]):
                    continue
                if len(locations) >= MAX_LOCATIONS:
                    break
                if m:
                    locations.add(m.group(1).strip())
        except OSError:
            pass
        finally:
            s.close()

    renderers = []
    for loc in sorted(locations):
        r = describe(loc)
        if r and not any(x['udn'] == r['udn'] for x in renderers):
            renderers.append(r)
    return renderers


def _same_origin(url, base):
    """Ugyanaz a séma/hoszt/port? A vezérlő-URL-eknek az eszközön kell lennie."""
    a, b = urlparse(url), urlparse(base)
    return (a.scheme in ('http', 'https') and a.scheme == b.scheme
            and a.netloc.lower() == b.netloc.lower())


def describe(location):
    """Eszközleíró letöltése; None, ha nem MediaRenderer.

    A cím egy hálózati üzenetszórásból érkezik, tehát nem megbízható: csak
    http(s)-t követünk, és a vezérlő-URL-eknek is ugyanazon az eszközön kell
    lenniük - különben egy hamis eszköz helyi fájlt olvastathatna velünk, vagy
    idegen hosztra küldetné a hívásainkat.
    """
    if not location.lower().startswith(('http://', 'https://')):
        return None
    try:
        req = urllib.request.Request(location, headers={'Connection': 'close'})
        with urllib.request.urlopen(req, timeout=5) as r:
            xml = r.read(300000).decode('utf-8', 'replace')
    except Exception:
        return None
    if 'MediaRenderer' not in xml:
        return None

    services = {}
    for block in re.findall(r'<service>(.*?)</service>', xml, re.S):
        stype = _tag(block, 'serviceType')
        curl = _tag(block, 'controlURL')
        if not (stype and curl):
            continue
        full = urljoin(location, curl)
        if _same_origin(full, location):
            services[stype] = full
    if AVT not in services:
        return None

    host = re.match(r'https?://([^/:]+)', location)
    return {
        'udn': _tag(xml, 'UDN') or location,
        'name': _tag(xml, 'friendlyName') or 'Ismeretlen eszköz',
        'manufacturer': _tag(xml, 'manufacturer'),
        'model': (_tag(xml, 'modelName') + ' ' + _tag(xml, 'modelNumber')).strip(),
        'host': host.group(1) if host else '',
        'location': location,
        'avtransport': services.get(AVT, ''),
        'rendering': services.get(RCS, ''),
        'connection': services.get(CMS, ''),
    }


def supported_mimes(renderer):
    """A TV által elfogadott MIME-típusok a ConnectionManagertől."""
    if not renderer.get('connection'):
        return []
    ok, body = soap(renderer['connection'], CMS, 'GetProtocolInfo')
    if not ok:
        return []
    sink = _tag(body, 'Sink')
    mimes = set()
    for entry in sink.split(','):
        bits = entry.split(':')
        if len(bits) > 2 and '/' in bits[2]:
            mimes.add(bits[2].strip())
    return sorted(mimes)


# --------------------------------------------------------------------------
# DIDL-Lite metaadat
# --------------------------------------------------------------------------

# A készülékek eltérő tekerési egységeket fogadnak el; ebben a sorrendben próbáljuk.
SEEK_UNITS = ('REL_TIME', 'ABS_TIME', 'X_DLNA_REL_BYTE')

# A folytatás nem "kilövöm és elfelejtem" művelet. A készülék a fájl megnyitása
# közben visszautasíthatja a tekerést, sőt el is fogadhatja anélkül, hogy
# ténylegesen odaugrana. Ezért a célt megjegyezzük, ellenőrizzük, és újrapróbáljuk.
RESUME_LAND = 15.0     # ennyivel a cél előtt már célba értnek vesszük (kulcskocka)
RESUME_RETRY = 3.0     # ennyi másodpercenként próbálkozunk újra
RESUME_TRIES = 8       # ennyi sikertelen próba után feladjuk, és szólunk
# A készülék magától is visszaugorhat a fájl elejére (mérve: tekerés után
# közvetlenül kiadott szünet+folytatás után). Ha ezt nem vennénk észre, a film
# némán elölről menne tovább.
RESTART_DROP = 30.0    # ekkora visszaesést már nem magyarázhat mérési hiba
RESTART_FIXES = 2      # ennyiszer korrigáljuk egy elemen belül
SEEK_GRACE = 15.0      # tekerés után ennyi ideig nem gyanakszunk újraindulásra
START_GRACE = 5.0      # elemváltás után ennyi ideig lehet még az előző elem állása
STALL_WINDOW = 10.0    # ekkora ablakban nézzük, tart-e a lejátszás a valós idővel
STALL_RATIO = 0.5      # ennél lassabb haladás már akadozás, nem mérési hiba
STALL_TELL = 60.0      # ennyinél sűrűbben nem szólunk ugyanarról
IDEGEN_TURELEM = 10.0  # ennyi ideig hisszük, hogy a készülék még átáll
STALL_GAP = 30.0       # ennél hosszabb ablakból leolvasások maradtak ki
STOP_CONFIRM = 2       # ennyi egybehangzó leolvasás kell a sor léptetéséhez

UPNP_CLASS = {
    'video': 'object.item.videoItem',
    'audio': 'object.item.audioItem.musicTrack',
    'image': 'object.item.imageItem.photo',
}

# OP=01: bájt szerinti tekerés támogatott (a szerverünk tud Range-et)
DLNA_FLAGS = ('DLNA.ORG_OP=01;DLNA.ORG_CI=0;DLNA.ORG_FLAGS='
              '01700000000000000000000000000000')


def build_didl(title, url, mime, kind, sub_url=None):
    cls = UPNP_CLASS.get(kind, 'object.item.videoItem')
    protocol = 'http-get:*:%s:%s' % (mime, DLNA_FLAGS)
    extra = ''
    if sub_url:
        # Legjobb szándékú felirat-hivatkozás; sok TV figyelmen kívül hagyja.
        extra = ('<sec:CaptionInfoEx sec:type="srt">%s</sec:CaptionInfoEx>'
                 '<sec:CaptionInfo sec:type="srt">%s</sec:CaptionInfo>'
                 '<res protocolInfo="http-get:*:text/srt:*">%s</res>'
                 % (xesc(sub_url), xesc(sub_url), xesc(sub_url)))
    return ('<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" '
            'xmlns:dc="http://purl.org/dc/elements/1.1/" '
            'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/" '
            'xmlns:sec="http://www.sec.co.kr/">'
            '<item id="1" parentID="0" restricted="1">'
            '<dc:title>%s</dc:title>'
            '<upnp:class>%s</upnp:class>'
            '<res protocolInfo="%s">%s</res>'
            '%s'
            '</item></DIDL-Lite>'
            % (xesc(title), cls, xesc(protocol), xesc(url), extra))


# --------------------------------------------------------------------------
# Lejátszó: sorkezelés és állapotfigyelés
# --------------------------------------------------------------------------

class Player(object):
    """A TV-n futó lejátszás állapota. A sort a szerver lépteti tovább."""

    def __init__(self):
        self.lock = threading.RLock()
        self.renderer = None
        self.mimes = []
        self.queue = []              # [{path,name,kind,mime,url,sub}]
        self.index = -1
        self.repeat = 'OFF'
        self.state = 'STOPPED'       # TV által jelentett állapot
        self.position = 0.0
        self.duration = 0.0
        self.volume = 50
        self.muted = False
        self.volume_before_mute = 0  # ide állítjuk vissza feloldáskor
        self.muted_by_us = False     # mi némítottunk-e, vagy a készüléken volt
        self.error = ''
        self.error_id = 0            # hogy két megnyitott lap ne egye el egymás elől
        self.error_at = 0.0
        self.saw_playing = False     # láttuk-e már játszani az aktuális elemet
        self.started_at = 0.0
        self.duration_probe = None   # a szerver állítja be (ffprobe alapú)
        self.position_load = None    # hol tartottunk ebben a fájlban
        self.position_save = None
        self.position_clear = None
        # FIGYELEM: nem lehet 'resume', mert az elfedné a resume() metódust.
        self.auto_resume = True      # folytatás onnan, ahol abbahagytad
        self.resume_to = 0.0         # indulás után ide kell tekerni
        self.resume_tries = 0        # hányadik tekerési próbánál tartunk
        self.resume_at = 0.0         # mikor próbálkoztunk utoljára
        self.resume_okbol = 'folytatas'   # 'folytatas' vagy 'vissza'
        self.save_floor = 0.0        # ez alatt nem írjuk felül a mentett pontot
        self.last_pos = 0.0          # az előző leolvasás - ehhez mérjük a visszaesést
        self.restart_fixes = 0       # hányszor rángattuk vissza a készüléket
        self.seeked_at = 0.0         # mikor tekertél utoljára magad
        self.expect_uri = ''         # melyik fájlt indítottuk el a készüléken
        self.rate_pos = 0.0          # ettől a ponttól mérjük, halad-e a kép
        self.rate_at = 0.0
        self.stop_hits = 0        # hány egybehangzó leolvasás mondja, hogy vége
        self.stall_report = None     # a szerver akasztja ide a hálózati mérőit
        self.stall_told_at = 0.0
        self.saw_position = False    # jelentett-e valaha nem nulla állást
        self.idegen_ota = 0.0        # mióta jelent más fájlt a készülék
        self.idegen_szolt = False
        self.stall_reported = False  # jeleztük-e már, hogy nem indul be
        self._saved_at = 0.0         # mikor mentettük utoljára a pozíciót
        self.seek_modes = []         # amit a készülék hirdet magáról
        self.seek_unit = None        # ami ténylegesen bevált nála
        self.stopped_by_user = False # ne lépjen tovább, ha mi állítottuk le
        self.online = True           # válaszol-e még a készülék
        self.fails = 0               # egymás utáni sikertelen lekérdezések
        self._stop = threading.Event()
        # Az elemindítás három parancsból áll (Stop, URI, Play). Két egyidejű
        # indítás összefésülve azt eredményezné, hogy a TV az egyik filmet
        # játssza, a felület a másikat mutatja, és a pont rossz fájlhoz kerül.
        self.cmd_lock = threading.RLock()
        self._thread = None

    # -- eszköz ----------------------------------------------------------
    def select(self, renderer):
        with self.lock:
            masik = (renderer or {}).get('udn') != (self.renderer or {}).get('udn')
            if masik:
                # Az új készülék első válasza STOPPED lesz. Ha a lejátszás
                # könyvelését nem nulláznánk, ezt úgy olvasnánk, hogy "az előző
                # elem végigment": törölnénk a megtekintési pontját, és
                # magától elindulna a következő film.
                self.state = 'STOPPED'
                self.saw_playing = False
                self.stopped_by_user = True
                self.resume_to = 0.0
                self.last_pos = 0.0
                self.expect_uri = ''
                self.saw_position = False
                self.idegen_ota = 0.0
                self.idegen_szolt = False
                self.rate_pos = 0.0
                self.rate_at = 0.0
                self.stop_hits = 0
                self.muted = False
                self.muted_by_us = False
            self.renderer = renderer
            self.mimes = []
            self.seek_modes = []
            self.seek_unit = None
            self.online = True
            self.fails = 0
            self.error = ''
        if not renderer:
            return
        # A lekérdezések hálózaton mennek: lock nélkül, hogy a felület ne
        # fagyjon be. Van rájuk közös határidő is: egy lassú, de élő készüléken
        # ez a négy hívás egyenként nyolc másodpercig várhatna.
        hatarido = time.time() + SELECT_BUDGET
        mimes = supported_mimes(renderer)
        with self.lock:
            if self.renderer is renderer:
                self.mimes = mimes
        if time.time() < hatarido:
            self._read_seek_modes()
        if time.time() < hatarido:
            self._read_volume()
        with self.lock:
            self._ensure_thread()

    def _read_seek_modes(self):
        """A készülék által hirdetett tekerési egységek. Sok TV üresen hagyja."""
        ok, body = self._avt('GetDeviceCapabilities', timeout=5.0)
        if not ok:
            return
        modes = [m.strip() for m in _tag(body, 'SeekMode').split(',') if m.strip()]
        self.seek_modes = [m for m in modes if m in SEEK_UNITS]

    def _ensure_thread(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._thread.start()

    def shutdown(self):
        self._stop.set()

    def stop_if_ours(self):
        """Kilépéskor állítsuk le a TV-t, de csak ha tényleg a mi tartalmunk megy.

        A készülék előre letölti a fájlt a pufferébe, ezért a szerver nélkül is
        játszana tovább – nagy filmnél viszont a puffer végén megfagyna. Ha
        közben a felhasználó másra váltott a TV-n, nem nyúlunk hozzá.
        """
        with self.lock:
            r = self.renderer
            item = (self.queue[self.index]
                    if 0 <= self.index < len(self.queue) else None)
        if not r or not item:
            return False
        ok, body = soap(r['avtransport'], AVT, 'GetMediaInfo',
                        [('InstanceID', 0)], timeout=3.0)
        if not ok:
            return False
        current = _tag(body, 'CurrentURI')
        # Nyers szövegegyezés itt sem elég: a válasz XML, tehát a tokenes URL
        # '&' jele '&amp;'-ként érkezik - vagyis alapbeállítással SOHA nem
        # egyezne, és kilépéskor sosem állítanánk meg a készüléket.
        if not current or not _uri_azonos(current, item['url']):
            return False            # már nem a mi felvételünk megy
        self.remember_now()        # kilépés előtt jegyezzük meg, hol tartunk
        ok, _ = soap(r['avtransport'], AVT, 'Stop', [('InstanceID', 0)], timeout=3.0)
        return ok

    # -- vezérlés --------------------------------------------------------
    def _avt(self, action, args=None, timeout=8.0, with_code=False):
        r = self.renderer
        if not r:
            miss = 'Nincs kiválasztva eszköz.'
            return (False, miss, '') if with_code else (False, miss)
        return soap(r['avtransport'], AVT,
                    action, [('InstanceID', 0)] + list(args or []), timeout,
                    with_code)

    def _rcs(self, action, args=None):
        r = self.renderer
        if not r or not r.get('rendering'):
            return False, 'Az eszköz nem támogatja a hangerőszabályzást.'
        return soap(r['rendering'], RCS,
                    action, [('InstanceID', 0)] + list(args or []))

    def set_queue(self, items, index=0):
        with self.cmd_lock:
            self.remember_now()  # az eddigi elemnél is maradjon meg a pont
            with self.lock:
                self.queue = items
                self.index = -1
            return self._play_index(index)

    def switch(self, index):
        """Szándékos váltás másik elemre - előbb jegyezzük meg, hol tartunk.

        Ez nem tehető a play_index-be: azt a sor léptetése is hívja, ott pedig
        a végignézett elem pontját épp az imént töröltük.
        """
        with self.cmd_lock:
            self.remember_now()
            return self._play_index(index)

    def play_index(self, index):
        with self.cmd_lock:
            return self._play_index(index)

    def _play_index(self, index):
        with self.lock:
            if not self.queue:
                return False, 'A lejátszási sor üres.'
            if index < 0 or index >= len(self.queue):
                return False, 'Nincs ilyen elem a sorban.'
            item = self.queue[index]
            self.index = index
            self.saw_playing = False
            self.stopped_by_user = False
            self.started_at = time.time()
            self.position = 0.0
            self.duration = 0.0
            self.error = ''
            self.resume_to = 0.0
            self.resume_tries = 0
            self.resume_at = 0.0
            self.save_floor = 0.0
            self.last_pos = 0.0
            self.restart_fixes = 0
            self.seeked_at = 0.0
            self.expect_uri = item['url']
            self.saw_position = False
            self.idegen_ota = 0.0
            self.idegen_szolt = False
            self.rate_pos = 0.0
            self.rate_at = 0.0
            self.stop_hits = 0
            self.stall_reported = False
            self._saved_at = time.time()

        # Ha van megjegyzett pont ehhez a fájlhoz, a lejátszás beindulása után
        # oda tekerünk. Előbb nem lehet: a készülék csak betöltött médiát tekert.
        if self.auto_resume and self.position_load:
            try:
                mark = float(self.position_load(item['path']) or 0)
            except Exception:
                mark = 0.0
            if mark > 0:
                with self.lock:
                    self.resume_to = mark
                    self.resume_okbol = 'folytatas'

        didl = build_didl(item.get('title') or item['name'], item['url'],
                          item['mime'], item.get('kind', 'video'),
                          item.get('sub'))

        # Néhány készülék csak megállított állapotban fogad új URI-t.
        self._avt('Stop', timeout=5.0)
        ok, resp = self._avt('SetAVTransportURI',
                             [('CurrentURI', item['url']),
                              ('CurrentURIMetaData', didl)])
        if not ok:
            with self.lock:
                # A készülék a régit játssza tovább: ha az új elemet várnánk
                # tőle, minden leolvasást eldobnánk, és vakon maradnánk.
                self.expect_uri = ''
                self._jelez('A TV nem fogadta el a fájlt: %s' % resp)
            return False, self.error

        ok, resp = self._avt('Play', [('Speed', '1')])
        if not ok:
            with self.lock:
                self.expect_uri = ''
                self._jelez('Nem indult el a lejátszás: %s' % resp)
            return False, self.error

        with self.lock:
            self.state = 'TRANSITIONING'
            self._ensure_thread()
        if self.duration_probe:
            threading.Thread(target=self._probe_duration,
                             args=(index, item['path']), daemon=True).start()
        return True, ''

    def _probe_duration(self, index, path):
        """A TV sokszor 0 hosszt jelent; ilyenkor a saját mérésünk pótolja."""
        try:
            value = self.duration_probe(path)
        except Exception:
            return
        with self.lock:
            # Az index önmagában kevés: közben más sor is kerülhetett ide.
            still = (0 <= self.index < len(self.queue)
                     and self.index == index
                     and self.queue[index].get('path') == path)
            if value > 0 and still and self.duration <= 0:
                self.duration = value

    def pause(self):
        with self.lock:
            # Betöltés közben is elfogadjuk: a szóközt sokan azonnal megnyomják,
            # és a "nincs mit szüneteltetni" válasz után a film mégis elindulna.
            if self.state not in ('PLAYING', 'TRANSITIONING'):
                return True, ''      # nem játszik: nincs mit szüneteltetni
        ok, resp = self._avt('Pause')
        if ok:
            with self.lock:
                self.state = 'PAUSED_PLAYBACK'
        return ok, resp

    def resume(self):
        with self.lock:
            if not self.queue:
                # Puszta Play parancs a TV-n azt is elindíthatná, amit épp néz.
                return False, 'A lejátszási sor üres.'
            if self.state == 'PLAYING':
                return True, ''      # már megy: nincs mit folytatni
            was_stopped = self.state in ('STOPPED', 'NO_MEDIA_PRESENT')
            idx = self.index
            has_item = 0 <= idx < len(self.queue)
            self.stopped_by_user = False

        # A Stop lebontja a lejátszást a készüléken: ilyenkor a puszta Play
        # parancsot elfogadja, de nem történik semmi. Újra be kell tölteni a
        # fájlt - a mentett pozíció miatt onnan folytatódik, ahol abbahagytad.
        if was_stopped and has_item:
            return self.play_index(idx)

        ok, resp = self._avt('Play', [('Speed', '1')])
        if ok:
            with self.lock:
                self.state = 'PLAYING'
        return ok, resp

    def toggle(self):
        with self.lock:
            playing = self.state in ('PLAYING', 'TRANSITIONING')
        return self.pause() if playing else self.resume()

    def _jelez(self, uzenet):
        """Üzenet a felhasználónak. A hívó tartja a zárat.

        Azonosítót kap és rövid ideig lekérhető marad: enélkül az az ügyfél
        vinné el, amelyik éppen elsőként kérdez rá, a többi lap sosem látná.
        """
        self.error = uzenet
        self.error_id += 1
        self.error_at = time.time()

    def remember_now(self):
        """A pillanatnyi állást elmenti - leállításkor és kilépéskor kell."""
        if not self.position_save:
            return
        with self.lock:
            item = (self.queue[self.index]
                    if 0 <= self.index < len(self.queue) else None)
            pos, dur = self.position, self.duration
            pending, floor = self.resume_to, self.save_floor
        if pending > 0:
            return       # a folytatás még nem ért célba: a mentett pont az igazság
        if item and pos > 0 and pos > floor:
            try:
                self.position_save(item['path'], pos, dur)
            except Exception:
                pass

    def stop(self):
        self.remember_now()
        with self.lock:
            self.stopped_by_user = True
        ok, resp = self._avt('Stop')
        with self.lock:
            self.state = 'STOPPED'
            self.position = 0.0
            self.stall_reported = True   # szándékos leállás: nincs mit jelenteni
        return ok, resp

    def seek(self, seconds, belso=False, timeout=8.0):
        """Tekerés. Végigpróbálja az egységeket, és megjegyzi, melyik ment át.

        A `belso` jelzés a folytatás saját próbálkozásait különbözteti meg: ha a
        felhasználó teker, ő veszi át az irányítást, tehát a függőben lévő
        folytatást el kell engedni - különben visszarángatnánk a mentett pontra.
        """
        with self.lock:
            if not belso:
                # Te veszed át az irányítást. A készülék viszont másodpercekig
                # a régi helyet jelentheti - vagy el sem mozdul -, ezért rövid
                # ideig nem tekintjük "magától újraindult"-nak, amit látunk.
                self.resume_to = 0.0
                self.resume_tries = 0
                self.save_floor = 0.0
                self.seeked_at = time.time()
                self._saved_at = time.time()
            item = (self.queue[self.index]
                    if 0 <= self.index < len(self.queue) else None)
            duration = self.duration
            order = [u for u in [self.seek_unit] if u]
            for unit in list(self.seek_modes) + list(SEEK_UNITS):
                if unit not in order:
                    order.append(unit)

        last = 'A készülék nem fogadta el a tekerést.'
        for unit in order:
            target = self._seek_target(unit, seconds, item, duration)
            if target is None:
                continue
            ok, resp, code = self._avt('Seek', [('Unit', unit), ('Target', target)],
                                       timeout=timeout, with_code=True)
            if ok:
                with self.lock:
                    self.seek_unit = unit
                return True, ''
            last = resp
            if code in STATE_ERRORS or not code:
                # Nem az egységgel van baj, hanem az időzítéssel (vagy a
                # hálózattal). A többi egység végigpróbálása itt csak ártana:
                # egy elfogadott, de rossz egység máshova vinné a lejátszást,
                # a bevált egységet pedig elfelejtenénk.
                return False, last
        with self.lock:
            self.seek_unit = None        # tényleg rossz egység: kezdjük elölről
        return False, last

    @staticmethod
    def _seek_target(unit, seconds, item, duration):
        if unit in ('REL_TIME', 'ABS_TIME'):
            return seconds_to_hms(seconds)
        if unit == 'X_DLNA_REL_BYTE':
            # Bájtpozíció becslése; csak akkor van értelme, ha tudjuk a hosszt és a méretet.
            size = (item or {}).get('size') or 0
            if duration > 0 and size > 0:
                return str(int(size * max(0.0, seconds) / duration))
        return None

    def skip(self, delta):
        with self.lock:
            if not self.queue:
                return False, 'A lejátszási sor üres.'
            nxt = self.index + delta
            if nxt < 0:
                nxt = len(self.queue) - 1
            elif nxt >= len(self.queue):
                nxt = 0
        with self.cmd_lock:
            self.remember_now()
            return self._play_index(nxt)

    def set_volume(self, level):
        level = max(0, min(100, int(level)))
        ok, resp = self._rcs('SetVolume', [('Channel', 'Master'),
                                           ('DesiredVolume', level)])
        if ok:
            with self.lock:
                self.volume = level
                if level > 0 and self.muted_by_us:
                    # A hangerő kézi állítása feloldja a SAJÁT némításunkat.
                    # A készülék saját némítását nem érinti - ha az áll, a
                    # felület továbbra is jogosan mutat némítást.
                    self.muted_by_us = False
                    self.muted = False
        return ok, resp

    def set_mute(self, on):
        """Némítás - a hangerőn keresztül, nem a készülék némításával.

        Mérve: van készülék, amelyik a SetMute 1-et végrehajtja, de a SetMute
        0-t nem, tehát a némítás bent ragad, és az appból többé nem oldható
        fel. Olyan parancsot nem adunk ki, amit nem tudunk visszavonni; a
        hangerő viszont pontosan működik és vissza is olvasható.
        """
        on = bool(on)
        keszulek_nemitva = False
        if not on:
            # A készülék saját némítása is állhat - a távirányítóról, egy másik
            # alkalmazásból, vagy egy korábbi futásból. Feloldjuk, mielőtt a
            # hangerőt visszaadnánk, különben a hang akkor sem jönne vissza.
            keszulek_nemitva = not self._keszulek_nemitas_feloldasa()
        ok, resp = self._mute_hangeroval(on)
        if not ok:
            return False, resp
        with self.lock:
            # Ha a készülék saját némítását nem sikerült feloldani, a hang
            # továbbra sincs meg: ilyenkor feloldottnak mutatni hazugság lenne.
            self.muted = on or keszulek_nemitva
        return True, ''

    def _keszulek_nemitas_feloldasa(self):
        """A készülék SAJÁT némításának feloldása. Igaz, ha nincs némítva.

        Szabályos készüléken ez egy hívás. Van viszont olyan, amelyik a
        némítást csak bekapcsolni tudja: nála sem a SetMute 0, sem a szabvány
        szerinti FactoryDefaults visszaállítás, sem a hangerőváltás nem old fel
        semmit - mérve. Ilyenkor nincs mit tenni, csak szólni: a némítás a
        készülék távirányítójával szüntethető meg.
        """
        if not self._nemitva_jelent():
            return True                      # nem is volt bekapcsolva
        self._rcs('SetMute', [('Channel', 'Master'), ('DesiredMute', 0)])
        if not self._nemitva_jelent():
            return True                      # engedett
        with self.lock:
            self._jelez('A TV saját némítása be van kapcsolva, és azt az app '
                        'nem tudja feloldani. Nyomd meg a némítás gombot a TV '
                        'távirányítóján.')
        return False

    def _nemitva_jelent(self):
        ok, body = self._rcs('GetMute', [('Channel', 'Master')])
        return bool(ok) and _tag(body, 'CurrentMute') in ('1', 'true')

    def _mute_hangeroval(self, on):
        """Némítás a hangerőn keresztül.

        A feloldás pontosan azt adja vissza, ami a némítás előtt volt - akkor
        is, ha az nulla volt. Kitalált alapértékkel a hangerő a semmiből
        ugrana fel, ami a felhasználónak megmagyarázhatatlan.
        """
        with self.lock:
            if on:
                # Csak az ELSŐ némítás jegyzi meg a hangerőt. Enélkül egy
                # ismételt némítás a már nullázott értéket mentené el, és a
                # feloldás nullára állítana vissza.
                if not self.muted_by_us:
                    self.volume_before_mute = self.volume
                self.muted_by_us = True
                cel = 0
            elif not self.muted_by_us:
                # A némítás nem tőlünk származik (távirányító, másik program):
                # nincs mit visszaadni, és a hangerőhöz nem nyúlunk hozzá.
                return True, ''
            else:
                self.muted_by_us = False
                cel = self.volume_before_mute
        ok, resp = self._rcs('SetVolume', [('Channel', 'Master'),
                                           ('DesiredVolume', cel)])
        if ok:
            with self.lock:
                self.volume = cel
        return ok, resp

    def _read_volume(self):
        ok, body = self._rcs('GetVolume', [('Channel', 'Master')])
        if ok:
            try:
                reported = int(_tag(body, 'CurrentVolume') or 0)
            except ValueError:
                reported = -1
            if 0 <= reported <= 100:
                with self.lock:
                    self.volume = reported

        # Csak azt tekintjük némításnak, amit a készülék annak vall. A nulla
        # hangerő nem az: ha annak vennénk, a feloldás gomb olyasmit ígérne,
        # amit nem tud teljesíteni - nincs mit visszaadni.
        jelentett = self._nemitva_jelent()      # hálózati hívás: zár nélkül
        with self.lock:
            # A saját némításunkról a készülék mit sem tud: nála csak a
            # hangerő nulla. Ugyanannak az eszköznek az újraválasztása
            # különben elfelejtené, hogy mi némítottunk.
            self.muted = jelentett or self.muted_by_us

    # -- állapotfigyelés -------------------------------------------------
    def _poll_loop(self):
        while True:
            with self.lock:
                # Aktív lejátszásnál sűrűn, egyébként ritkán kérdezzük a TV-t.
                busy = self.state in ('PLAYING', 'TRANSITIONING')
            if self._stop.wait(1.2 if busy else 4.0):
                return
            with self.lock:
                if not self.renderer:
                    continue
            try:
                self._poll_once()
            except Exception as e:
                # Elnyelve a lekérdezés némán elhalna, és onnantól semmi nem
                # frissülne - legalább a naplóban legyen nyoma.
                if debug_log:
                    try:
                        debug_log('lekérdezési hiba: %r' % (e,))
                    except Exception:
                        pass

    def _rate_reset(self, pos, most=None):
        """Új mérési ablak. A hívó tartja a zárat."""
        self.rate_pos = pos
        self.rate_at = time.time() if most is None else most

    def _akadas_meres(self, pos, most=None):
        """Tart-e a lejátszás a valós idővel. A hívó tartja a zárat.

        A `most` az órát adja meg: így egy valódi felvétel visszajátszható
        anélkül, hogy a teszt a folyamat óráját állítgatná.

        Egyetlen leolvasásból nem dönthető el: a készülék egész másodperceket
        jelent, 1,2 másodperces lekérdezés mellett tehát hol 1, hol 2 mp-et
        lép - ez önmagában 0,6-os arányt is adhat úgy, hogy a kép hibátlan.
        Ezért ablakban mérünk, és csak a tartós lemaradás számít.

        Visszaadja a (videó mp, valós mp) párost, ha akadozást talált.
        """
        most = time.time() if most is None else most
        # Van készülék, amelyik egyáltalán nem jelent pozíciót: a RelTime
        # NOT_IMPLEMENTED, amiből nálunk 0 lesz. Ott a "nem haladt" örökké
        # igaz lenne, és percenként riasztanánk hibátlan lejátszás közben.
        # Csak akkor mérünk, ha a készülék EHHEZ az elemhez mutatott már
        # nem nulla állást.
        if not self.saw_position:
            self._rate_reset(pos, most)
            return None
        # Tekerés és folytatás közben a pozíció ugrál: ilyenkor nincs mit mérni.
        if self.resume_to > 0 or most - self.seeked_at < SEEK_GRACE:
            self._rate_reset(pos, most)
            return None
        if self.rate_at <= 0:
            self._rate_reset(pos, most)
            return None
        eltelt = most - self.rate_at
        if eltelt < STALL_WINDOW:
            return None
        haladt = pos - self.rate_pos
        self._rate_reset(pos, most)
        if eltelt > STALL_GAP:
            # Kimaradtak leolvasások: idegen tartalom ment a készüléken, nem
            # válaszolt, vagy elaludt a gép. Ilyenkor a "nem haladt" nem a
            # lejátszásról szól - és percekben mért abszurd számot írnánk ki.
            return None
        # A negatív haladás újraindulás, azt a visszaesés-figyelő intézi.
        if 0 <= haladt < eltelt * STALL_RATIO:
            return (haladt, eltelt)
        return None

    def _akadas_jelentes(self, haladt, eltelt):
        """Akadozás: naplóba mindig, a felhasználónak ritkábban.

        A hálózati számlálókat a szerver adja hozzá (a UPnP-réteg nem lát
        HTTP-t): enélkül csak annyi látszana, hogy "akad", azt viszont nem,
        hogy közben mennyit kért és kapott a készülék.
        """
        reszlet = ''
        if self.stall_report:
            try:
                reszlet = self.stall_report(haladt, eltelt) or ''
            except Exception:
                reszlet = ''
        if debug_log:
            try:
                debug_log('akadozás: %.0f mp videó %.0f mp alatt%s'
                          % (haladt, eltelt, (' - ' + reszlet) if reszlet else ''))
            except Exception:
                pass
        with self.lock:
            if time.time() - self.stall_told_at < STALL_TELL:
                return
            self.stall_told_at = time.time()
            self._jelez('Akadozik a lejátszás: %.0f másodperc videó ment le '
                        '%.0f másodperc alatt. A TV nem kapja meg elég gyorsan '
                        'a fájlt.' % (haladt, eltelt))

    def _mas_elemrol_szol(self, track_uri):
        """Igaz, ha a leolvasás nem arról az elemről szól, amit elindítottunk.

        Elemváltáskor a készülék még másodpercekig az ELŐZŐ fájl állását
        jelenti. Mérve, valódi készüléken: a váltás után az első leolvasás az
        előző rész 936. másodpercét adta vissza, a következő pedig az újnak a
        nulláját - a kettő közti esést újraindulásnak vettük, és az ÚJ részt
        tekertük a 936. másodpercre. A készülék viszont a GetPositionInfo-ban
        megmondja, melyik fájlnál tart; nem kell találgatni.

        Az eltérést viszont csak IDEGEN_TURELEM ideig magyarázza az átállás.
        Ha tovább tart - mert a készüléken átváltottak másik bemenetre -, a
        leolvasást továbbra sem használjuk fel, de nem is hallgatunk róla:
        némán "lejátszás" alatt befagyott pozíciót mutatni hazugság lenne.
        """
        if not track_uri:
            return False            # nem árulja el - lásd a START_GRACE-t
        with self.lock:
            vart = self.expect_uri
            if not vart or _uri_azonos(track_uri, vart):
                self.idegen_ota = 0.0
                self.idegen_szolt = False
                return False
            most = time.time()
            if self.idegen_ota <= 0:
                self.idegen_ota = most
            if most - self.idegen_ota < IDEGEN_TURELEM:
                return True         # még átállhat: ez a normális elemváltás
            if not self.idegen_szolt:
                self.idegen_szolt = True
                self.state = 'STOPPED'
                # A sort nem kell külön letiltani: idegen tartalomnál a
                # lekérdezés már korábban visszatér, tehát a léptetés ki sem
                # számolódik. Egy itt beragadó "mi állítottuk le" jelzés viszont
                # a visszaváltás UTÁN is blokkolná a következő rész indítását.
                self._jelez('A TV most nem azt játssza, amit innen indítottunk. '
                            'Ha átváltottál rajta, indítsd újra a lejátszást.')
            return True

    def _poll_once(self):
        ok, body = self._avt('GetTransportInfo', timeout=5.0)
        if not ok:
            with self.lock:
                self.fails += 1
                if self.fails >= 3 and self.online:
                    self.online = False
                    self._jelez('A TV nem válaszol (kikapcsolt vagy lecsatlakozott).')
            return
        with self.lock:
            self.fails = 0
            self.online = True
        state = _tag(body, 'CurrentTransportState') or 'STOPPED'

        ok, body = self._avt('GetPositionInfo', timeout=5.0)
        if not ok:
            # Az állapot megvan, a pozíció nem. A nullát elhinni a legrosszabb,
            # amit tehetnénk: pontosan úgy néz ki, mint egy magától elölről
            # induló készülék, és fölösleges visszatekerést váltana ki - épp
            # azt az akadozást okozva, ami ellen a figyelő készült.
            with self.lock:
                self.state = state
            return
        pos = hms_to_seconds(_tag(body, 'RelTime'))
        dur = hms_to_seconds(_tag(body, 'TrackDuration'))
        track_uri = _tag(body, 'TrackURI') or ''

        if self._mas_elemrol_szol(track_uri):
            # Nem a mi elemünkről szól: egyetlen mezőt sem szabad róla írni.
            return

        advance = False
        do_seek = 0.0
        remember = None
        akadas = None
        with self.lock:
            self.state = state
            self.position = pos
            if dur > 0:
                self.duration = dur
            if state != 'PLAYING':
                # Szünet vagy megállás nem akadozás: kezdjük elölről a mérést.
                self._rate_reset(pos)
            if state == 'PLAYING':
                self.saw_playing = True
                # Két egymást követő leolvasás közti nagy visszaesés az egyetlen
                # megbízható jele annak, hogy a készülék magától újraindult.
                # A ki nem szolgált tekerési célhoz mérve hamis riasztás lenne.
                elozo = self.last_pos
                # Ha a készülék nem árulja el, melyik fájlnál tart, csak az
                # idő véd: elemváltás után pár másodpercig még az előző elem
                # állása jöhet, és azt nem szabad újraindulásnak venni.
                frissen_indult = (not track_uri
                                  and time.time() - self.started_at < START_GRACE)
                visszaesett = (elozo - pos > RESTART_DROP
                               and time.time() - self.seeked_at > SEEK_GRACE
                               and not frissen_indult)
                self.last_pos = pos
                if pos > 0:
                    self.saw_position = True
                akadas = self._akadas_meres(pos)
                if self.resume_to > 0:
                    # Amíg a folytatás nem ért célba, egy pontot sem mentünk: a
                    # nulláról induló lejátszás különben pár másodperc alatt
                    # letörölné azt, ahonnan folytatni akartunk.
                    if pos >= self.resume_to - RESUME_LAND:
                        self.resume_to = 0.0            # odaért, mehet tovább
                        self._saved_at = time.time()
                    elif pos <= 0:
                        pass          # a készülék PLAYING-et mond, de még tölt
                    elif time.time() - self.resume_at < RESUME_RETRY:
                        pass          # az előző próbálkozás még friss
                    elif self.resume_tries >= RESUME_TRIES:
                        # Feladjuk. A mentett pontot viszont nem írjuk felül,
                        # amíg a lejátszás túl nem jutott rajta - különben egy
                        # sikertelen folytatás pont azt törölné, ahova vissza
                        # akartunk térni.
                        self.save_floor = max(self.save_floor, self.resume_to)
                        self._jelez(
                            'A TV nem tekert vissza oda, ahol tartottál (%s). '
                            'A mentett pont megmarad.' % seconds_to_hms(self.resume_to)
                            if self.resume_okbol == 'vissza' else
                            'Nem sikerült a folytatáshoz tekerni (%s) - a TV '
                            'elölről játssza. A mentett pont megvan.'
                            % seconds_to_hms(self.resume_to))
                        self.resume_to = 0.0
                        self._saved_at = time.time()
                    else:
                        self.resume_tries += 1
                        self.resume_at = time.time()
                        do_seek = self.resume_to
                elif visszaesett:
                    # A készülék magától visszaugrott a fájl elejére. A mentést
                    # MINDENKÉPPEN letiltjuk eddig a pontig - akkor is, ha a
                    # visszatekerési keret már elfogyott -, különben a nullához
                    # közeli állás írná felül a valódit.
                    if elozo > self.save_floor:
                        self.save_floor = elozo
                    if self.restart_fixes < RESTART_FIXES:
                        self.restart_fixes += 1
                        self.resume_to = elozo
                        self.resume_tries = 0
                        self.resume_at = 0.0
                        self.resume_okbol = 'vissza'
                # Menet közben tízmásodpercenként jegyezzük meg, hol tartunk.
                elif (self.position_save and pos > self.save_floor and pos > 0
                        and time.time() - self._saved_at > 10):
                    self._saved_at = time.time()
                    item = (self.queue[self.index]
                            if 0 <= self.index < len(self.queue) else None)
                    if item:
                        remember = (item['path'], pos, self.duration)

            # Ha 30 mp alatt sem indult el, ne pörögjön némán a "betöltés".
            if (not self.saw_playing and not self.stall_reported
                    and not self.stopped_by_user
                    and self.index >= 0
                    and time.time() - self.started_at > 30
                    and state in ('TRANSITIONING', 'STOPPED', 'NO_MEDIA_PRESENT')):
                self.stall_reported = True
                self.state = 'STOPPED'
                name = (self.queue[self.index].get('title')
                        if 0 <= self.index < len(self.queue) else 'a fájl')
                self._jelez('A TV nem indította el ezt: %s. Ellenőrizd, hogy be '
                            'van-e kapcsolva, és hogy viszi-e ezt a formátumot.'
                            % name)

            # Vége az elemnek: játszott, most megállt, és nem mi állítottuk le.
            # Egyetlen leolvasás viszont kevés: a készülékek lejátszás közben is
            # jelentenek pillanatnyi STOPPED-ot, a léptetés pedig visszafordít-
            # hatatlan - törli azt a pontot, ahonnan folytatnál, és átugrik a
            # következő részre. Ezért két egybehangzó leolvasás kell hozzá.
            settled = time.time() - self.started_at > 4.0
            if (state in ('STOPPED', 'NO_MEDIA_PRESENT') and self.saw_playing
                    and settled and not self.stopped_by_user):
                self.stop_hits += 1
            else:
                self.stop_hits = 0
            if self.stop_hits >= STOP_CONFIRM:
                self.stop_hits = 0
                advance = True

        if akadas:
            self._akadas_jelentes(*akadas)
        if do_seek > 0:
            self.seek(do_seek, belso=True, timeout=4.0)
        if remember:
            try:
                self.position_save(*remember)
            except Exception:
                pass
        if advance:
            self._on_item_finished()

    def _on_item_finished(self):
        with self.lock:
            if not self.queue:
                return
            done = (self.queue[self.index]
                    if 0 <= self.index < len(self.queue) else None)
        if done and self.position_clear:
            try:
                self.position_clear(done['path'])   # végignézve: kezdje elölről
            except Exception:
                pass
        with self.lock:
            if self.repeat == 'SINGLE':
                nxt = self.index
            else:
                nxt = self.index + 1
                if nxt >= len(self.queue):
                    if self.repeat == 'ALL':
                        nxt = 0
                    else:
                        self.state = 'STOPPED'
                        self.saw_playing = False
                        return
        self.play_index(nxt)

    # -- állapot kiolvasása ----------------------------------------------
    def snapshot(self):
        with self.lock:
            r = self.renderer
            # Az üzenet néhány másodpercig lekérhető marad, és azonosítót visel:
            # így minden megnyitott lap megkapja, de egyik sem kapja meg kétszer.
            friss = time.time() - self.error_at < ERROR_TTL
            error = self.error if friss else ''
            if not friss:
                self.error = ''
            return {
                'renderer': {'name': r['name'], 'udn': r['udn'],
                             'host': r['host'], 'model': r['model']} if r else None,
                'mimes': self.mimes,
                'state': self.state,
                'index': self.index,
                'position': round(veges(self.position), 1),
                'duration': round(veges(self.duration), 1),
                'volume': self.volume,
                'muted': self.muted,
                'repeat': self.repeat,
                'seekUnit': self.seek_unit,
                'queueLength': len(self.queue),
                'resume': self.auto_resume,
                'online': self.online,
                'error': error,
                'errorId': self.error_id if error else 0,
                'title': (self.queue[self.index].get('title')
                          if 0 <= self.index < len(self.queue) else ''),
                # A felület ebből tudja azonosítani a játszott elemet akkor is,
                # ha a sor közben átrendeződött vagy rövidült.
                'path': (self.queue[self.index].get('path')
                         if 0 <= self.index < len(self.queue) else ''),
            }
