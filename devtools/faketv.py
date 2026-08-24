#!/usr/bin/env python3
"""faketv.py - hamis DLNA MediaRenderer a Cast Studio végponttól végpontig
teszteléséhez. Valódi hálózati eszköz: SSDP-re válaszol, eszközleírót ad,
és kiszolgálja az AVTransport / RenderingControl / ConnectionManager SOAP
hívásokat. Menet közben átállítható, hogy a valódi TV hibáit utánozza.

    python3 devtools/faketv.py --port 8475 --rate 10 --media-duration 120

Futás közbeni vezérlés (bármelyik gépről, HTTP GET):

    curl 'http://127.0.0.1:8475/control?offline=1'          # a TV "kikapcsol"
    curl 'http://127.0.0.1:8475/control?offline=0'
    curl 'http://127.0.0.1:8475/control?seek_lockout=25'    # 25 mp-ig 701
    curl 'http://127.0.0.1:8475/control?seek_ignore=1'      # elfogadja, nem mozdul
    curl 'http://127.0.0.1:8475/control?fail_action=Play'   # HTTP 500 arra
    curl 'http://127.0.0.1:8475/control?slow=9'             # lassú válasz (timeout)
    curl 'http://127.0.0.1:8475/control?duration_zero=0'    # jelentsen hosszt
    curl 'http://127.0.0.1:8475/control?volume_zero=0'

Megfigyelés:

    curl 'http://127.0.0.1:8475/log'      # SOAP-hívások naplója JSON-ban
    curl 'http://127.0.0.1:8475/state'    # a hamis TV belső állapota
    curl 'http://127.0.0.1:8475/log?clear=1'
"""

import argparse
import json
import os
import re
import socket
import struct
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs
from xml.sax.saxutils import escape as xesc
from xml.sax.saxutils import unescape as xunesc

sys.dont_write_bytecode = True


def konzol_utf8():
    """Windowson a kodlap tipikusan cp1252, amiben nincs 'o' kettos ekezettel:
    a magyar kiiras csobe iranyitva UnicodeEncodeError-t dobna."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding='utf-8', errors='replace')
        except (AttributeError, ValueError, OSError):
            pass


konzol_utf8()

SSDP_ADDR = '239.255.255.250'
SSDP_PORT = 1900
AVT = 'urn:schemas-upnp-org:service:AVTransport:1'
RCS = 'urn:schemas-upnp-org:service:RenderingControl:1'
CMS = 'urn:schemas-upnp-org:service:ConnectionManager:1'

# Amit a valódi Hisense elfogad (rövidített, de valósághű lista).
SINK = ','.join(
    'http-get:*:%s:DLNA.ORG_OP=01;DLNA.ORG_FLAGS=01700000000000000000000000000000' % m
    for m in ('video/mp4', 'video/x-matroska', 'video/avi', 'video/x-msvideo',
              'video/mpeg', 'video/quicktime', 'video/x-flv', 'video/3gpp',
              'video/webm', 'audio/mpeg', 'audio/mp4', 'audio/x-wav',
              'audio/flac', 'audio/x-flac', 'audio/aac', 'audio/ogg',
              'image/jpeg', 'image/png', 'image/gif', 'image/bmp'))


def hms(sec):
    sec = max(0, int(sec))
    return '%d:%02d:%02d' % (sec // 3600, (sec % 3600) // 60, sec % 60)


def hms_to_s(text):
    try:
        parts = [float(p) for p in str(text).strip().split(':')]
    except ValueError:
        return 0.0
    total = 0.0
    for p in parts:
        total = total * 60 + p
    return total


def outbound_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.settimeout(0.3)
        s.connect(('8.8.8.8', 80))
        return s.getsockname()[0]
    except OSError:
        return '127.0.0.1'
    finally:
        s.close()


class TV(object):
    """A hamis készülék állapota és szeszélyei."""

    def __init__(self, args):
        self.lock = threading.RLock()
        self.name = args.name
        self.udn = args.udn
        self.rate = args.rate                    # ennyiszeres lejátszási óra
        self.media_duration = args.media_duration
        self.load_delay = args.load_delay        # ennyi ideig TRANSITIONING

        # szeszélyek
        self.duration_zero = args.duration_zero  # TrackDuration mindig 00:00:00
        self.volume_zero = args.volume_zero      # GetVolume mindig 0
        self.seek_lockout = args.seek_lockout    # ennyi mp-ig 701-gyel elutasít
        self.seek_ignore = args.seek_ignore      # elfogadja, de nem mozdul
        self.fail_action = args.fail_action      # erre HTTP 500
        self.slow = args.slow                    # ennyit vár válasz előtt
        self.offline = args.offline              # nem válaszol egyáltalán
        self.fetch = args.fetch                  # letöltse-e ténylegesen a médiát
        self.probe = getattr(args, 'probe', False)  # ffprobe-bal mérje a hosszt

        # állapot
        self.state = 'NO_MEDIA_PRESENT'
        self.uri = ''
        self.metadata = ''
        self.pos = 0.0
        self.ticked = time.time()
        self.loaded_at = 0.0
        self.volume = 12
        self.muted = False
        self.log = []
        self.fetched = []                        # (url, status, bytes)

    # -- óra -------------------------------------------------------------
    def tick(self):
        with self.lock:
            now = time.time()
            if self.state == 'TRANSITIONING' and now - self.loaded_at >= self.load_delay:
                self.state = 'PLAYING'
                self.ticked = now
            if self.state == 'PLAYING':
                self.pos += (now - self.ticked) * self.rate
                self.ticked = now
                if self.media_duration > 0 and self.pos >= self.media_duration:
                    self.pos = 0.0
                    self.state = 'STOPPED'      # vége a felvételnek
            else:
                self.ticked = now

    def note(self, action, args, result=''):
        with self.lock:
            self.log.append({'t': round(time.time(), 3), 'action': action,
                             'args': args, 'result': result,
                             'state': self.state, 'pos': round(self.pos, 1)})
            if len(self.log) > 4000:
                del self.log[:1000]
        print('  %s  %-22s %-14s pos=%6.1f %s'
              % (time.strftime('%H:%M:%S'), action, self.state, self.pos,
                 result), flush=True)

    # -- a felvétel valódi hossza (mint egy igazi készüléknél) ------------
    def _probe(self, url):
        import subprocess
        try:
            out = subprocess.run(
                ['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
                 '-of', 'default=nw=1:nk=1', url],
                capture_output=True, text=True, timeout=25).stdout.strip()
            value = float(out)
        except Exception:
            return
        with self.lock:
            if self.uri == url and value > 0:
                self.media_duration = value
                print('  ---- mért hossz: %.1f mp' % value, flush=True)

    # -- média letöltése (a Range-kiszolgálás megmozgatásához) -----------
    def _fetch(self, url):
        try:
            req = urllib.request.Request(url, headers={'Range': 'bytes=0-262143',
                                                       'Connection': 'close'})
            with urllib.request.urlopen(req, timeout=8) as r:
                data = r.read(300000)
                with self.lock:
                    self.fetched.append([url, r.status, len(data)])
        except Exception as e:
            with self.lock:
                self.fetched.append([url, 'ERROR', str(e)])


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    server_version = 'FakeTV/1.0 UPnP/1.0 DLNADOC/1.50'

    def log_message(self, *a):
        pass

    # -- kimenet ---------------------------------------------------------
    def _send(self, code, body, ctype='text/xml; charset="utf-8"'):
        data = body.encode('utf-8') if isinstance(body, str) else body
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('EXT', '')
        self.end_headers()
        try:
            self.wfile.write(data)
        except OSError:
            pass

    def _soap_ok(self, action, service, inner):
        env = ('<?xml version="1.0"?>'
               '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
               's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
               '<s:Body><u:%sResponse xmlns:u="%s">%s</u:%sResponse>'
               '</s:Body></s:Envelope>' % (action, service, inner, action))
        self._send(200, env)

    def _soap_err(self, code, desc):
        env = ('<?xml version="1.0"?>'
               '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/">'
               '<s:Body><s:Fault><faultcode>s:Client</faultcode>'
               '<faultstring>UPnPError</faultstring><detail>'
               '<UPnPError xmlns="urn:schemas-upnp-org:control-1-0">'
               '<errorCode>%s</errorCode><errorDescription>%s</errorDescription>'
               '</UPnPError></detail></s:Fault></s:Body></s:Envelope>'
               % (code, xesc(desc)))
        self._send(500, env)

    # -- kérések ---------------------------------------------------------
    def _gate(self):
        """A kikapcsolt TV egyáltalán nem válaszol; a lassú TV várat magára."""
        if DEV.offline:
            try:
                self.connection.close()
            except OSError:
                pass
            self.close_connection = True
            return False
        if DEV.slow > 0:
            time.sleep(DEV.slow)
        return True

    def do_GET(self):
        parsed = urlparse(self.path)
        q = parse_qs(parsed.query)

        # A vezérlő- és naplóvégpontok akkor is élnek, ha a TV "ki van kapcsolva".
        if parsed.path == '/control':
            return self._control(q)
        if parsed.path == '/log':
            with DEV.lock:
                out = list(DEV.log)
                if q.get('clear'):
                    DEV.log = []
            return self._send(200, json.dumps(out, ensure_ascii=False),
                              'application/json; charset=utf-8')
        if parsed.path == '/state':
            DEV.tick()
            with DEV.lock:
                out = {'state': DEV.state, 'pos': round(DEV.pos, 1), 'uri': DEV.uri,
                       'volume': DEV.volume, 'muted': DEV.muted,
                       'offline': DEV.offline, 'slow': DEV.slow,
                       'seek_lockout': DEV.seek_lockout,
                       'seek_ignore': DEV.seek_ignore,
                       'fail_action': DEV.fail_action,
                       'duration_zero': DEV.duration_zero,
                       'fetched': DEV.fetched[-20:],
                       'metadata': DEV.metadata}
            return self._send(200, json.dumps(out, ensure_ascii=False),
                              'application/json; charset=utf-8')

        if not self._gate():
            return
        if parsed.path in ('/desc.xml', '/'):
            return self._send(200, DESC_XML)
        if parsed.path.endswith('.xml'):
            return self._send(200, SCPD_XML)
        self._send(404, 'not found', 'text/plain')

    def do_POST(self):
        if not self._gate():
            return
        try:
            length = int(self.headers.get('Content-Length') or 0)
        except ValueError:
            length = 0
        raw = self.rfile.read(length).decode('utf-8', 'replace')
        soapaction = self.headers.get('SOAPAction', '')
        m = re.search(r'#(\w+)"?$', soapaction.strip())
        action = m.group(1) if m else ''
        # A SOAP-argumentumok XML-szövegek: vissza kell fejteni az entitásokat,
        # különben a '&amp;' maradna a médiaURL-ben (és a token elveszne).
        args = dict((k, xunesc(v, {'&quot;': '"', '&apos;': "'"}))
                    for k, v in re.findall(r'<(\w+)>(.*?)</\1>', raw, re.S))
        args.pop('InstanceID', None)

        if DEV.fail_action and action == DEV.fail_action:
            DEV.note(action, args, 'ERŐLTETETT 500')
            return self._soap_err('501', 'Action Failed (kikényszerített hiba)')

        path = urlparse(self.path).path
        if path.startswith('/avt'):
            return self._avt(action, args)
        if path.startswith('/rcs'):
            return self._rcs(action, args)
        if path.startswith('/cms'):
            return self._cms(action, args)
        self._soap_err('401', 'Invalid Action')

    # -- AVTransport ------------------------------------------------------
    def _avt(self, action, args):
        DEV.tick()
        with DEV.lock:
            if action == 'SetAVTransportURI':
                DEV.uri = args.get('CurrentURI', '')
                DEV.metadata = args.get('CurrentURIMetaData', '')[:4000]
                DEV.state = 'STOPPED'
                DEV.pos = 0.0
                DEV.loaded_at = time.time()
                url = DEV.uri
                DEV.note(action, {'CurrentURI': url}, '')
                if DEV.fetch and url.startswith('http'):
                    threading.Thread(target=DEV._fetch, args=(url,),
                                     daemon=True).start()
                if DEV.probe and url.startswith('http'):
                    threading.Thread(target=DEV._probe, args=(url,),
                                     daemon=True).start()
                return self._soap_ok(action, AVT, '')

            if action == 'Play':
                if not DEV.uri:
                    DEV.note(action, args, '701 nincs média')
                    return self._soap_err('701', 'Transition not available')
                DEV.state = 'TRANSITIONING'
                DEV.loaded_at = time.time()
                DEV.ticked = time.time()
                DEV.note(action, args, '')
                return self._soap_ok(action, AVT, '')

            if action == 'Pause':
                if DEV.state != 'PLAYING':
                    DEV.note(action, args, '701 nem játszik')
                    return self._soap_err('701', 'Transition not available')
                DEV.state = 'PAUSED_PLAYBACK'
                DEV.note(action, args, '')
                return self._soap_ok(action, AVT, '')

            if action == 'Stop':
                DEV.state = 'STOPPED'
                DEV.pos = 0.0
                DEV.note(action, args, '')
                return self._soap_ok(action, AVT, '')

            if action == 'Seek':
                unit = args.get('Unit', '')
                target = args.get('Target', '')
                since = time.time() - DEV.loaded_at
                if DEV.seek_lockout > 0 and since < DEV.seek_lockout:
                    DEV.note(action, args, '701 zárolva (%.1f mp)' % since)
                    return self._soap_err('701', 'Transition not available')
                if unit not in ('REL_TIME', 'ABS_TIME', 'X_DLNA_REL_BYTE'):
                    DEV.note(action, args, '710 ismeretlen egység')
                    return self._soap_err('710', 'Seek mode not supported')
                if DEV.seek_ignore:
                    DEV.note(action, args, 'elfogadva, de nem mozdul')
                    return self._soap_ok(action, AVT, '')
                if unit == 'X_DLNA_REL_BYTE':
                    DEV.note(action, args, '710 bájt-tekerés nem megy')
                    return self._soap_err('710', 'Seek mode not supported')
                DEV.pos = hms_to_s(target)
                DEV.ticked = time.time()
                DEV.note(action, args, 'ugrás %.1f' % DEV.pos)
                return self._soap_ok(action, AVT, '')

            if action == 'GetTransportInfo':
                DEV.note(action, args)
                return self._soap_ok(
                    action, AVT,
                    '<CurrentTransportState>%s</CurrentTransportState>'
                    '<CurrentTransportStatus>OK</CurrentTransportStatus>'
                    '<CurrentSpeed>1</CurrentSpeed>' % DEV.state)

            if action == 'GetPositionInfo':
                dur = ('00:00:00' if DEV.duration_zero
                       else hms(DEV.media_duration))
                DEV.note(action, args)
                return self._soap_ok(
                    action, AVT,
                    '<Track>1</Track><TrackDuration>%s</TrackDuration>'
                    '<TrackMetaData></TrackMetaData>'
                    '<TrackURI>%s</TrackURI>'
                    '<RelTime>%s</RelTime><AbsTime>%s</AbsTime>'
                    '<RelCount>0</RelCount><AbsCount>0</AbsCount>'
                    % (dur, xesc(DEV.uri), hms(DEV.pos), hms(DEV.pos)))

            if action == 'GetMediaInfo':
                DEV.note(action, args)
                dur = ('00:00:00' if DEV.duration_zero
                       else hms(DEV.media_duration))
                return self._soap_ok(
                    action, AVT,
                    '<NrTracks>1</NrTracks><MediaDuration>%s</MediaDuration>'
                    '<CurrentURI>%s</CurrentURI>'
                    '<CurrentURIMetaData></CurrentURIMetaData>'
                    '<PlayMedium>NETWORK</PlayMedium>'
                    % (dur, xesc(DEV.uri)))

            if action == 'GetDeviceCapabilities':
                # Mint a Hisense: nem árulja el a tekerési módokat.
                DEV.note(action, args)
                return self._soap_ok(
                    action, AVT,
                    '<PlayMedia>NETWORK</PlayMedia><RecMedia>NOT_IMPLEMENTED</RecMedia>'
                    '<RecQualityModes>NOT_IMPLEMENTED</RecQualityModes>')

            if action == 'GetTransportSettings':
                DEV.note(action, args)
                return self._soap_ok(action, AVT,
                                     '<PlayMode>NORMAL</PlayMode>'
                                     '<RecQualityMode>NOT_IMPLEMENTED</RecQualityMode>')

        DEV.note(action or '(üres)', args, '401 ismeretlen')
        self._soap_err('401', 'Invalid Action')

    # -- RenderingControl -------------------------------------------------
    def _rcs(self, action, args):
        with DEV.lock:
            if action == 'GetVolume':
                DEV.note(action, args)
                val = 0 if DEV.volume_zero else DEV.volume
                return self._soap_ok(action, RCS,
                                     '<CurrentVolume>%d</CurrentVolume>' % val)
            if action == 'SetVolume':
                try:
                    DEV.volume = int(args.get('DesiredVolume', 0))
                except ValueError:
                    pass
                DEV.note(action, args)
                return self._soap_ok(action, RCS, '')
            if action == 'GetMute':
                DEV.note(action, args)
                return self._soap_ok(action, RCS, '<CurrentMute>%d</CurrentMute>'
                                     % (1 if DEV.muted else 0))
            if action == 'SetMute':
                DEV.muted = args.get('DesiredMute') in ('1', 'true')
                DEV.note(action, args)
                return self._soap_ok(action, RCS, '')
        self._soap_err('401', 'Invalid Action')

    # -- ConnectionManager ------------------------------------------------
    def _cms(self, action, args):
        if action == 'GetProtocolInfo':
            DEV.note(action, args)
            return self._soap_ok(action, CMS,
                                 '<Source></Source><Sink>%s</Sink>' % xesc(SINK))
        if action == 'GetCurrentConnectionIDs':
            DEV.note(action, args)
            return self._soap_ok(action, CMS, '<ConnectionIDs>0</ConnectionIDs>')
        self._soap_err('401', 'Invalid Action')

    # -- futás közbeni átállítás ------------------------------------------
    def _control(self, q):
        changed = {}
        ints = ('slow', 'seek_lockout', 'media_duration', 'load_delay',
                'rate', 'volume')
        flags = ('offline', 'seek_ignore', 'duration_zero', 'volume_zero', 'fetch')
        with DEV.lock:
            for k in ints:
                if k in q:
                    setattr(DEV, k, float(q[k][0]))
                    changed[k] = float(q[k][0])
            for k in flags:
                if k in q:
                    setattr(DEV, k, q[k][0] not in ('0', 'false', ''))
                    changed[k] = getattr(DEV, k)
            if 'fail_action' in q:
                DEV.fail_action = q['fail_action'][0]
                changed['fail_action'] = DEV.fail_action
            if 'state' in q:
                DEV.state = q['state'][0]
                changed['state'] = DEV.state
            if 'pos' in q:
                DEV.pos = float(q['pos'][0])
                DEV.ticked = time.time()
                changed['pos'] = DEV.pos
        print('  ---- vezérlés: %s' % changed, flush=True)
        self._send(200, json.dumps({'ok': True, 'changed': changed}),
                   'application/json; charset=utf-8')


# --------------------------------------------------------------------------
# SSDP
# --------------------------------------------------------------------------

def ssdp_server(ip, port, udn, name, stop):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        # Windowson a socket modulban nincs SO_REUSEPORT: az attributumhiba
        # nem OSError, kulon el kell kapni, kulonben elszall a szal.
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    except (OSError, AttributeError):
        pass
    try:
        sock.bind(('', SSDP_PORT))
    except OSError as e:
        print('  SSDP: nem sikerult a 1900-as portra kotni: %s' % e, flush=True)
        return
    for iface in ('0.0.0.0', ip):
        try:
            mreq = struct.pack('4s4s', socket.inet_aton(SSDP_ADDR),
                               socket.inet_aton(iface))
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
        except OSError:
            pass
    sock.settimeout(0.5)
    location = 'http://%s:%d/desc.xml' % (ip, port)
    targets = ('urn:schemas-upnp-org:device:MediaRenderer:1', 'ssdp:all',
               'upnp:rootdevice', udn)
    print('  SSDP figyel a %s:%d címen, LOCATION=%s' % (SSDP_ADDR, SSDP_PORT, location),
          flush=True)
    while not stop.is_set():
        try:
            data, addr = sock.recvfrom(4096)
        except socket.timeout:
            continue
        except OSError:
            break
        text = data.decode('utf-8', 'replace')
        if not text.upper().startswith('M-SEARCH'):
            continue
        if DEV.offline:
            continue
        m = re.search(r'(?im)^ST:\s*(\S+)', text)
        st = m.group(1).strip() if m else 'ssdp:all'
        if st not in targets:
            continue
        reply_st = ('urn:schemas-upnp-org:device:MediaRenderer:1'
                    if st in ('ssdp:all', 'upnp:rootdevice') else st)
        resp = ('HTTP/1.1 200 OK\r\n'
                'CACHE-CONTROL: max-age=1800\r\n'
                'DATE: %s\r\n'
                'EXT:\r\n'
                'LOCATION: %s\r\n'
                'SERVER: Darwin/20.0 UPnP/1.0 FakeTV/1.0\r\n'
                'ST: %s\r\n'
                'USN: %s::%s\r\n'
                'BOOTID.UPNP.ORG: 1\r\n\r\n'
                % (time.strftime('%a, %d %b %Y %H:%M:%S GMT', time.gmtime()),
                   location, reply_st, udn, reply_st))
        try:
            sock.sendto(resp.encode(), addr)
            print('  SSDP válasz -> %s:%d (ST=%s)' % (addr[0], addr[1], st), flush=True)
        except OSError:
            pass
    sock.close()


DESC_TEMPLATE = """<?xml version="1.0"?>
<root xmlns="urn:schemas-upnp-org:device-1-0" xmlns:dlna="urn:schemas-dlna-org:device-1-0">
  <specVersion><major>1</major><minor>0</minor></specVersion>
  <device>
    <deviceType>urn:schemas-upnp-org:device:MediaRenderer:1</deviceType>
    <friendlyName>%(name)s</friendlyName>
    <manufacturer>FakeTV Labs</manufacturer>
    <modelName>Hamis VIDAA</modelName>
    <modelNumber>2026</modelNumber>
    <UDN>%(udn)s</UDN>
    <dlna:X_DLNADOC>DMR-1.50</dlna:X_DLNADOC>
    <serviceList>
      <service>
        <serviceType>urn:schemas-upnp-org:service:AVTransport:1</serviceType>
        <serviceId>urn:upnp-org:serviceId:AVTransport</serviceId>
        <SCPDURL>/avt.xml</SCPDURL>
        <controlURL>/avt/control</controlURL>
        <eventSubURL>/avt/event</eventSubURL>
      </service>
      <service>
        <serviceType>urn:schemas-upnp-org:service:RenderingControl:1</serviceType>
        <serviceId>urn:upnp-org:serviceId:RenderingControl</serviceId>
        <SCPDURL>/rcs.xml</SCPDURL>
        <controlURL>/rcs/control</controlURL>
        <eventSubURL>/rcs/event</eventSubURL>
      </service>
      <service>
        <serviceType>urn:schemas-upnp-org:service:ConnectionManager:1</serviceType>
        <serviceId>urn:upnp-org:serviceId:ConnectionManager</serviceId>
        <SCPDURL>/cms.xml</SCPDURL>
        <controlURL>/cms/control</controlURL>
        <eventSubURL>/cms/event</eventSubURL>
      </service>
    </serviceList>
  </device>
</root>
"""

SCPD_XML = ('<?xml version="1.0"?><scpd xmlns="urn:schemas-upnp-org:service-1-0">'
            '<specVersion><major>1</major><minor>0</minor></specVersion>'
            '<actionList/></scpd>')

DEV = None
DESC_XML = ''


def main():
    global DEV, DESC_XML
    ap = argparse.ArgumentParser(description='Hamis DLNA MediaRenderer')
    ap.add_argument('--port', type=int, default=8475)
    ap.add_argument('--bind', default='0.0.0.0')
    ap.add_argument('--ip', default='', help='a LOCATION-ben hirdetett cím')
    ap.add_argument('--name', default='Hamis TV (teszt)')
    ap.add_argument('--udn', default='uuid:faketv-0000-0000-0000-000000000001')
    ap.add_argument('--rate', type=float, default=1.0, help='lejátszási óra szorzó')
    ap.add_argument('--media-duration', type=float, default=120.0)
    ap.add_argument('--load-delay', type=float, default=1.5)
    ap.add_argument('--seek-lockout', type=float, default=0.0)
    ap.add_argument('--seek-ignore', action='store_true')
    ap.add_argument('--fail-action', default='')
    ap.add_argument('--slow', type=float, default=0.0)
    ap.add_argument('--offline', action='store_true')
    ap.add_argument('--duration-zero', dest='duration_zero', action='store_true',
                    default=True, help='TrackDuration mindig 00:00:00 (alap)')
    ap.add_argument('--real-duration', dest='duration_zero', action='store_false')
    ap.add_argument('--volume-zero', dest='volume_zero', action='store_true',
                    default=True)
    ap.add_argument('--real-volume', dest='volume_zero', action='store_false')
    ap.add_argument('--no-fetch', dest='fetch', action='store_false', default=True)
    ap.add_argument('--probe', action='store_true',
                    help='a valodi hosszt ffprobe-bal meri a kapott URL-bol')
    args = ap.parse_args()

    ip = args.ip or outbound_ip()
    DEV = TV(args)
    DESC_XML = DESC_TEMPLATE % {'name': xesc(args.name), 'udn': args.udn}

    httpd = ThreadingHTTPServer((args.bind, args.port), Handler)
    httpd.daemon_threads = True
    stop = threading.Event()
    threading.Thread(target=ssdp_server,
                     args=(ip, args.port, args.udn, args.name, stop),
                     daemon=True).start()
    print('\n  Hamis DLNA TV: %s' % args.name, flush=True)
    print('  Leíró:  http://%s:%d/desc.xml' % (ip, args.port), flush=True)
    print('  Napló:  http://127.0.0.1:%d/log' % args.port, flush=True)
    print('  Óra: %gx · hossz: %g mp · betöltés: %g mp\n'
          % (args.rate, args.media_duration, args.load_delay), flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        httpd.server_close()


if __name__ == '__main__':
    main()
