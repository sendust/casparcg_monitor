#!/usr/bin/env python3
"""CasparCG server monitor for headless Ubuntu.

Python standard library only. Serves a web UI (static/index.html) and a JSON API:
  GET  /api/status          system, GPU, network, CasparCG process and OSC state
                            (?hist_since=t also returns graph history points newer than t)
  GET  /api/history?since=t usage history (CPU, GPU, NIC) for the graphs, points newer than t
  GET  /api/log?key=...     last N lines of the newest CasparCG log (unchanged -> {"unchanged": true})
  POST /api/kill            terminate the CasparCG server process (SIGTERM, then SIGKILL)
  POST /api/amcp            {"command": "PLAY 1-1 AMB LOOP"} -> raw AMCP response
  POST /api/service         {"action": "start"|"stop"|"restart"} on the CasparCG systemd unit
                            (needs the polkit rule in 50-casparcg-monitor.rules)
"""
import collections
import glob
import json
import os
import platform
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CLK_TCK = os.sysconf('SC_CLK_TCK')

DEFAULT_CONFIG = {
    'http': {'bind': '0.0.0.0', 'port': 8070},
    'sample_interval': 1.0,
    'history_minutes': 60,              # CPU/GPU/NIC usage graphs: kept in memory at sample_interval
    'casparcg': {
        'process_name': 'casparcg-server-2.5',
        'service_name': 'casparcg',
        'host': '127.0.0.1',
        'amcp_port': 5250,
        'osc_port': 6250,
        'osc_stale_seconds': 1.5,
        'log_dir': '/var/log/casparcg',
        'log_lines': 1000,
        'kill_timeout': 5,
    },
    'preview': {'webrtc_port': 8889, 'path': 'sdi1_va12'},
    # Watches RssAnon of the main casparcg process. CEF 142's MallocDumpProvider reads glibc mallinfo()
    # (32-bit int fields) and crashes with SIGILL once the glibc heap passes 2 GiB; on the 2026-11-10
    # crash that point corresponded to RssAnon = 1.69 GiB (crash_ref_bytes).
    'memory_watch': {
        'warn_bytes': 1342177280,       # 1.25 GiB
        'crit_bytes': 1610612736,       # 1.50 GiB
        'crash_ref_bytes': 1815166976,  # 1.69 GiB, RssAnon observed at the crash
        'history_interval': 60,         # seconds between stored samples
        'history_hours': 48,
        'trend_hours': 6,               # window for the growth-rate fit
    },
}


def merge(base, override):
    out = dict(base)
    for k, v in override.items():
        out[k] = merge(base[k], v) if isinstance(v, dict) and isinstance(base.get(k), dict) else v
    return out


def load_config(path):
    with open(path, encoding='utf-8') as f:
        return merge(DEFAULT_CONFIG, json.load(f))


def read(path, default=None):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return default


def boot_time():
    for line in open('/proc/stat'):
        if line.startswith('btime'):
            return int(line.split()[1])
    return 0


# --------------------------------------------------------------------------- CPU / memory / system

def cpu_static():
    model, cores, phys, core = None, set(), None, None
    for line in open('/proc/cpuinfo'):
        key, _, val = line.partition(':')
        key, val = key.strip(), val.strip()
        if key == 'model name' and model is None:
            model = val
        elif key == 'physical id':
            phys = val
        elif key == 'core id':
            core = val
        elif not key and (phys, core) != (None, None):
            cores.add((phys, core))
            phys = core = None
    if (phys, core) != (None, None):
        cores.add((phys, core))
    threads = os.cpu_count() or 0
    return {
        'model': model or platform.processor() or 'unknown',
        'arch': platform.machine(),
        'sockets': len({p for p, _ in cores}) or 1,
        'cores': len(cores) or threads,
        'threads': threads,
    }


def cpu_times():
    """{'cpu': (busy, total), 'cpu0': ...} from /proc/stat."""
    out = {}
    for line in open('/proc/stat'):
        if not line.startswith('cpu'):
            break
        name, *vals = line.split()
        vals = [int(v) for v in vals[:8]]
        idle = vals[3] + vals[4]
        total = sum(vals)
        out[name] = (total - idle, total)
    return out


def meminfo():
    m = {}
    for line in open('/proc/meminfo'):
        k, v = line.split(':', 1)
        m[k] = int(v.split()[0]) * 1024
    return {
        'total': m['MemTotal'],
        'available': m.get('MemAvailable', m['MemFree']),
        'free': m['MemFree'],
        'swap_total': m.get('SwapTotal', 0),
        'swap_free': m.get('SwapFree', 0),
    }


# --------------------------------------------------------------------------- GPU

GPU_FIELDS = ['index', 'name', 'utilization.gpu', 'utilization.encoder', 'utilization.decoder',
              'temperature.gpu', 'power.draw', 'power.limit', 'memory.used', 'memory.total']


def gpu_stats():
    try:
        res = subprocess.run(
            ['nvidia-smi', '--query-gpu=' + ','.join(GPU_FIELDS), '--format=csv,noheader,nounits'],
            capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired) as e:
        return [], str(e)
    if res.returncode != 0:
        return [], (res.stderr or res.stdout).strip()

    def num(s):
        try:
            return float(s)
        except ValueError:
            return None  # "[N/A]", "[Not Supported]"

    gpus = []
    for line in res.stdout.strip().splitlines():
        v = [x.strip() for x in line.split(',')]
        if len(v) != len(GPU_FIELDS):
            continue
        gpus.append({
            'index': v[0], 'name': v[1],
            'util': num(v[2]), 'enc': num(v[3]), 'dec': num(v[4]),
            'temp': num(v[5]), 'power': num(v[6]), 'power_limit': num(v[7]),
            'mem_used': num(v[8]), 'mem_total': num(v[9]),  # MiB
        })
    return gpus, None


# --------------------------------------------------------------------------- network

def link_up_ifaces():
    out = []
    for name in sorted(os.listdir('/sys/class/net')):
        if name == 'lo':
            continue
        path = '/sys/class/net/' + name
        try:
            flags = int(read(path + '/flags', '0'), 16)
        except ValueError:
            flags = 0
        if flags & 0x1 and read(path + '/carrier') == '1':  # IFF_UP and carrier present
            out.append(name)
    return out


def iface_addrs():
    try:
        res = subprocess.run(['ip', '-j', 'addr', 'show'], capture_output=True, text=True, timeout=5)
        data = json.loads(res.stdout or '[]')
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return {}
    out = {}
    for i in data:
        addrs = [a for a in i.get('addr_info', []) if a.get('scope') != 'link']
        addrs.sort(key=lambda a: a.get('family') != 'inet')  # IPv4 first
        out[i['ifname']] = {
            'addrs': ['%s/%s' % (a['local'], a['prefixlen']) for a in addrs],
            'mac': i.get('address'),
        }
    return out


def iface_bytes(name):
    base = '/sys/class/net/%s/statistics/' % name
    return int(read(base + 'rx_bytes', '0')), int(read(base + 'tx_bytes', '0'))


# --------------------------------------------------------------------------- CasparCG process

def find_caspar(process_name):
    """Return (main_pid, [cef_child_pids]). CEF children run the same binary with --type=..."""
    comm_name = process_name[:15]  # /proc/<pid>/comm is truncated to 15 chars
    mains, children = [], []
    for pid in os.listdir('/proc'):
        if not pid.isdigit():
            continue
        if read('/proc/%s/comm' % pid) != comm_name:
            continue
        try:
            with open('/proc/%s/cmdline' % pid, 'rb') as f:
                # Chromium rewrites its title as one space-separated string, so match on words
                args = f.read().replace(b'\0', b' ').split()
        except OSError:
            continue
        if any(a.startswith(b'--type=') for a in args):
            children.append(int(pid))
        else:
            mains.append(int(pid))
    main = min(mains, key=proc_start_ticks) if mains else None
    return main, children


def proc_stat_fields(pid):
    with open('/proc/%d/stat' % pid) as f:
        data = f.read()
    return data[data.rindex(')') + 2:].split()  # fields from #3 (state) onward


def proc_start_ticks(pid):
    try:
        return int(proc_stat_fields(pid)[19])
    except (OSError, IndexError, ValueError):
        return 0


def proc_cpu_ticks(pid):
    try:
        f = proc_stat_fields(pid)
        return int(f[11]) + int(f[12])  # utime + stime
    except (OSError, IndexError, ValueError):
        return 0


def proc_mem(pid):
    """(rss, pss) in bytes."""
    rss = pss = 0
    for line in (read('/proc/%d/status' % pid) or '').splitlines():
        if line.startswith('VmRSS:'):
            rss = int(line.split()[1]) * 1024
    for line in (read('/proc/%d/smaps_rollup' % pid) or '').splitlines():
        if line.startswith('Pss:'):
            pss = int(line.split()[1]) * 1024
    return rss, pss


def proc_rss_anon(pid):
    """RssAnon in bytes (anonymous resident memory: malloc heap, arenas, mmap'd chunks, stacks)."""
    for line in (read('/proc/%d/status' % pid) or '').splitlines():
        if line.startswith('RssAnon:'):
            return int(line.split()[1]) * 1024
    return None


class MemWatch:
    """History, trend and warn/crit level of the main casparcg process's RssAnon."""
    LEVELS = ('ok', 'warn', 'crit')

    def __init__(self, cfg):
        self.cfg = cfg
        self.pid = None
        self.hist = []          # [(t, bytes)], one per history_interval, current pid only
        self.level = 'ok'

    def _fit(self, now):
        """Least-squares slope (bytes/hour) over the trend window; None with < 30 min of data."""
        pts = [(t, v) for t, v in self.hist if t >= now - self.cfg['trend_hours'] * 3600]
        if len(pts) < 2 or pts[-1][0] - pts[0][0] < 1800:
            return None
        n = len(pts)
        mt = sum(t for t, _ in pts) / n
        mv = sum(v for _, v in pts) / n
        den = sum((t - mt) ** 2 for t, _ in pts)
        return sum((t - mt) * (v - mv) for t, v in pts) / den * 3600 if den else None

    def update(self, pid, now):
        if pid is None:
            self.pid, self.hist, self.level = None, [], 'ok'
            return None
        if pid != self.pid:
            self.pid, self.hist = pid, []
        cur = proc_rss_anon(pid)
        if cur is None:
            return None
        c = self.cfg
        if not self.hist or now - self.hist[-1][0] >= c['history_interval']:
            self.hist.append((now, cur))
            cutoff = now - c['history_hours'] * 3600
            while self.hist and self.hist[0][0] < cutoff:
                self.hist.pop(0)

        level = 'crit' if cur >= c['crit_bytes'] else 'warn' if cur >= c['warn_bytes'] else 'ok'
        if level != self.level:
            print('MEMWATCH casparcg pid %d RssAnon %.0f MiB: %s -> %s (warn %.0f / crit %.0f MiB)'
                  % (pid, cur / 2**20, self.level, level, c['warn_bytes'] / 2**20, c['crit_bytes'] / 2**20),
                  file=sys.stderr, flush=True)
            self.level = level

        slope = self._fit(now)
        eta = {}
        for key in ('warn_bytes', 'crit_bytes', 'crash_ref_bytes'):
            if slope and slope > 0 and cur < c[key]:
                eta[key[:-6]] = (c[key] - cur) / slope * 3600   # seconds
        # Send at most ~240 points (24 h at 6-min resolution) to keep /api/status small
        recent = [p for p in self.hist if p[0] >= now - 24 * 3600]
        step = max(1, -(-len(recent) // 240))
        history = [[round(t), v] for t, v in recent[::step]]
        if recent and history[-1][0] != round(recent[-1][0]):
            history.append([round(recent[-1][0]), recent[-1][1]])
        return {
            'rss_anon': cur,
            'level': level,
            'warn': c['warn_bytes'],
            'crit': c['crit_bytes'],
            'crash_ref': c['crash_ref_bytes'],
            'slope_bph': slope,       # bytes per hour, None until 30 min of data
            'eta': eta,               # {'warn': s, 'crit': s, 'crash_ref': s} when growing
            'history': history,
        }


def kill_caspar(process_name, timeout):
    main, children = find_caspar(process_name)
    if not main:
        return {'ok': False, 'message': 'CasparCG process is not running'}
    sent = 'SIGTERM'
    try:
        os.kill(main, signal.SIGTERM)
    except ProcessLookupError:
        pass
    except PermissionError as e:
        return {'ok': False, 'message': 'permission denied: %s' % e}
    deadline = time.time() + timeout
    while time.time() < deadline and os.path.exists('/proc/%d' % main):
        time.sleep(0.1)
    if os.path.exists('/proc/%d' % main):
        sent = 'SIGKILL'
        try:
            os.kill(main, signal.SIGKILL)
        except ProcessLookupError:
            pass
        time.sleep(0.5)
    # Leftover CEF helper processes
    _, left = find_caspar(process_name)
    for pid in left:
        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    return {'ok': True, 'message': 'pid %d stopped (%s), %d helper(s) cleaned' % (main, sent, len(left))}


def service_state(name):
    """ActiveState/SubState/NRestarts of a systemd unit; None if systemctl is unavailable."""
    try:
        res = subprocess.run(['systemctl', 'show', name, '--property=LoadState,ActiveState,SubState,NRestarts,UnitFileState'],
                             capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return None
    props = dict(line.split('=', 1) for line in res.stdout.splitlines() if '=' in line)
    return {
        'name': name,
        'installed': props.get('LoadState') == 'loaded',
        'active': props.get('ActiveState'),
        'sub': props.get('SubState'),
        'enabled': props.get('UnitFileState'),
        'restarts': int(props['NRestarts']) if props.get('NRestarts', '').isdigit() else None,
    }


def service_action(name, action):
    if action not in ('start', 'stop', 'restart'):
        return {'ok': False, 'message': 'invalid action'}
    try:
        res = subprocess.run(['systemctl', '--no-ask-password', action, name],
                             capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as e:
        return {'ok': False, 'message': str(e)}
    if res.returncode != 0:
        msg = (res.stderr or res.stdout).strip()
        if 'Access denied' in msg or 'Interactive authentication required' in msg:
            msg += ' (install the polkit rule 50-casparcg-monitor.rules)'
        return {'ok': False, 'message': msg}
    return {'ok': True, 'message': 'systemctl %s %s done' % (action, name)}


def _hex_ip(h):
    raw = bytes.fromhex(h)
    if len(raw) == 4:
        return socket.inet_ntop(socket.AF_INET, raw[::-1])
    # IPv6: four 32-bit words, each in host (little-endian) order
    raw = b''.join(raw[i:i + 4][::-1] for i in range(0, 16, 4))
    if raw[:12] == b'\0' * 10 + b'\xff\xff':
        return socket.inet_ntop(socket.AF_INET, raw[12:])  # IPv4-mapped
    return socket.inet_ntop(socket.AF_INET6, raw)


def tcp_clients(port):
    """Remote (ip, port) of ESTABLISHED TCP connections to local `port`, from /proc/net/tcp{,6}."""
    out = []
    for path in ('/proc/net/tcp', '/proc/net/tcp6'):
        try:
            lines = open(path).read().splitlines()[1:]
        except OSError:
            continue
        for line in lines:
            f = line.split()
            if len(f) < 4 or f[3] != '01':  # 01 = ESTABLISHED
                continue
            lip, lport = f[1].split(':')
            if int(lport, 16) != port:
                continue
            rip, rport = f[2].split(':')
            out.append((_hex_ip(rip), int(rport, 16)))
    return out


def amcp_command(host, port, command, timeout=5.0, idle=0.25):
    """Send one AMCP command on its own connection and return the raw response text.

    Response framing varies (200: lines until an empty line, 201: one data block whose XML
    uses bare \\n, 400: status line plus echo), so after the status line arrives we read
    until the server goes quiet for `idle` seconds."""
    start = time.time()
    s = socket.create_connection((host, port), timeout=timeout)
    buf = b''
    try:
        s.sendall(command.encode('utf-8') + b'\r\n')
        while time.time() - start < timeout:
            s.settimeout(idle if b'\r\n' in buf else max(0.1, timeout - (time.time() - start)))
            try:
                chunk = s.recv(65536)
            except socket.timeout:
                if b'\r\n' in buf:
                    break
                continue
            if not chunk:
                break
            buf += chunk
            if buf.startswith(b'200 ') and buf.endswith(b'\r\n\r\n'):
                break
    finally:
        s.close()
    text = buf.decode('utf-8', 'replace').replace('\r\n', '\n').rstrip('\n')
    return {'ok': text[:1] == '2', 'code': text[:3] if text[:3].isdigit() else None,
            'response': text or '(no response within %.0fs)' % timeout}


# --------------------------------------------------------------------------- OSC / AMCP link

def _osc_pad(i):
    return (i + 4) & ~3


def _osc_str(b, i):
    end = b.index(b'\0', i)
    return b[i:end].decode('utf-8', 'replace'), _osc_pad(end)


def osc_parse(b, out):
    """Parse an OSC packet (message or bundle) into out[address] = [args]."""
    if b.startswith(b'#bundle\0'):
        i = 16
        while i + 4 <= len(b):
            n = struct.unpack('>i', b[i:i + 4])[0]
            osc_parse(b[i + 4:i + 4 + n], out)
            i += 4 + n
        return
    addr, i = _osc_str(b, 0)
    tags, i = _osc_str(b, i)
    args = []
    for t in tags[1:]:
        if t == 'i':
            args.append(struct.unpack('>i', b[i:i + 4])[0]); i += 4
        elif t == 'f':
            args.append(struct.unpack('>f', b[i:i + 4])[0]); i += 4
        elif t == 'h':
            args.append(struct.unpack('>q', b[i:i + 8])[0]); i += 8
        elif t == 'd':
            args.append(struct.unpack('>d', b[i:i + 8])[0]); i += 8
        elif t == 's':
            s, i = _osc_str(b, i); args.append(s)
        elif t in 'TF':
            args.append(t == 'T')
        elif t == 'N':
            args.append(None)
        elif t == 'b':
            n = struct.unpack('>i', b[i:i + 4])[0]
            i = _osc_pad(i + 4 + n - 1) if n else i + 4
            args.append('<blob %d>' % n)
        else:
            break  # unknown tag: stop parsing this message
    out[addr] = args


class CasparLink:
    """Keeps an AMCP TCP connection open (connection status, and CasparCG then sends OSC
    to this client's address on the OSC default port) and collects OSC messages."""

    def __init__(self, cfg):
        self.host = cfg['host']
        self.amcp_port = cfg['amcp_port']
        self.osc_port = cfg['osc_port']
        self.stale = cfg['osc_stale_seconds']
        self.lock = threading.Lock()
        self.osc = {}                      # address -> (args, timestamp)
        self.osc_last = 0.0
        self.osc_error = None
        self.amcp_connected = False
        self.amcp_since = None
        self.amcp_error = None
        self.amcp_local_port = None        # our own AMCP socket, to tell it apart in the client list
        self.version = None

    def start(self):
        threading.Thread(target=self._amcp_loop, daemon=True).start()
        threading.Thread(target=self._osc_loop, daemon=True).start()

    def _amcp_loop(self):
        while True:
            s = None
            try:
                s = socket.create_connection((self.host, self.amcp_port), timeout=2)
                s.settimeout(3)
                s.sendall(b'VERSION\r\n')       # one log line per (re)connect, no polling
                resp = s.recv(4096).decode('utf-8', 'replace').split('\r\n')
                with self.lock:
                    self.version = resp[1] if len(resp) > 1 and resp[0].startswith('201') else None
                    self.amcp_connected, self.amcp_since, self.amcp_error = True, time.time(), None
                    self.amcp_local_port = s.getsockname()[1]
                s.settimeout(1)
                while True:
                    try:
                        if not s.recv(4096):
                            raise ConnectionError('closed by server')
                    except socket.timeout:
                        continue
            except OSError as e:
                with self.lock:
                    if self.amcp_connected or self.amcp_error is None:
                        self.amcp_since = time.time()
                    self.amcp_connected, self.amcp_error = False, str(e) or e.__class__.__name__
                if s:
                    s.close()
                time.sleep(2)

    def _osc_loop(self):
        while True:
            try:
                u = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                u.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                u.bind(('0.0.0.0', self.osc_port))
                self.osc_error = None
            except OSError as e:
                self.osc_error = 'cannot bind UDP %d: %s' % (self.osc_port, e)
                time.sleep(5)
                continue
            while True:
                data = u.recv(65536)
                msgs = {}
                try:
                    osc_parse(data, msgs)
                except (ValueError, struct.error, IndexError):
                    continue
                now = time.time()
                with self.lock:
                    for k, v in msgs.items():
                        self.osc[k] = (v, now)
                    self.osc_last = now

    def snapshot(self):
        now = time.time()
        with self.lock:
            items = [(k, v) for k, (v, ts) in self.osc.items() if now - ts <= self.stale]
            # Forget addresses that disappeared long ago
            for k in [k for k, (_, ts) in self.osc.items() if now - ts > 60]:
                del self.osc[k]
            status = {
                'amcp_connected': self.amcp_connected,
                'amcp_since': self.amcp_since,
                'amcp_error': self.amcp_error,
                'version': self.version,
                'osc_live': now - self.osc_last <= self.stale,
                'osc_last': self.osc_last or None,
                'osc_error': self.osc_error,
            }
            own_port = self.amcp_local_port if self.amcp_connected else None
        status['amcp_clients'] = [
            {'addr': ip, 'port': rport, 'self': ip in ('127.0.0.1', '::1') and rport == own_port}
            for ip, rport in tcp_clients(self.amcp_port)]
        channels = {}
        for addr, args in items:
            p = addr.strip('/').split('/')
            if len(p) < 3 or p[0] != 'channel':
                continue
            val = args[0] if len(args) == 1 else args
            ch = channels.setdefault(p[1], {'format': None, 'consumers': {}, 'layers': {}})
            if p[2] == 'format':
                ch['format'] = val
            elif p[2] == 'output' and len(p) >= 6 and p[3] == 'port':
                ch['consumers'].setdefault(p[4], {})['/'.join(p[5:])] = val
            elif p[2] == 'stage' and len(p) >= 7 and p[3] == 'layer':
                layer = ch['layers'].setdefault(p[4], {'foreground': {}, 'background': {}})
                if p[5] in layer:
                    layer[p[5]]['/'.join(p[6:])] = val
        out = []
        for cid in sorted(channels, key=int):
            ch = channels[cid]
            consumers = []
            for port in sorted(ch['consumers'], key=lambda x: int(x) if x.isdigit() else 0):
                c = ch['consumers'][port]
                ctype = c.get('consumer', '?')
                details = {k.split('/', 1)[1] if k.startswith(ctype + '/') else k: v
                           for k, v in c.items() if k != 'consumer'}
                consumers.append({'port': port, 'type': ctype, 'details': details})
            layers = []
            for lid in sorted(ch['layers'], key=int):
                L = ch['layers'][lid]
                fg, bg = L['foreground'], L['background']
                if fg.get('producer', 'empty') == 'empty' and bg.get('producer', 'empty') == 'empty':
                    continue
                layers.append({'layer': lid, 'foreground': fg, 'background': bg})
            out.append({'channel': cid, 'format': ch['format'], 'consumers': consumers, 'layers': layers})
        status['channels'] = out
        return status


# --------------------------------------------------------------------------- log tail

def newest_log(log_dir):
    files = glob.glob(os.path.join(log_dir, '*.log'))
    return max(files, key=os.path.getmtime) if files else None


def tail_lines(path, n):
    with open(path, 'rb') as f:
        f.seek(0, os.SEEK_END)
        pos = f.tell()
        buf = b''
        while pos > 0 and buf.count(b'\n') <= n:
            step = min(65536, pos)
            pos -= step
            f.seek(pos)
            buf = f.read(step) + buf
    return buf.decode('utf-8', 'replace').splitlines()[-n:]


# --------------------------------------------------------------------------- sampler

class Sampler:
    def __init__(self, cfg):
        self.cfg = cfg
        self.lock = threading.Lock()
        self.data = {}
        self.cpu_info = cpu_static()
        self.btime = boot_time()
        self._prev_cpu = cpu_times()
        self._prev_net = {}
        self._prev_t = time.time()
        self._prev_proc = None             # (main_pid, ticks)
        self._addrs, self._addrs_t = {}, 0
        self.memwatch = MemWatch(cfg['memory_watch'])
        # Usage history for the graphs: (t, cpu %, [[gpu %, enc %, dec %], ...], {nic: [rx bps, tx bps]})
        self.history = collections.deque(
            maxlen=max(10, int(cfg['history_minutes'] * 60 / max(0.1, cfg['sample_interval']))))

    def start(self):
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        while True:
            t0 = time.time()
            try:
                snap = self._sample()
                point = (round(snap['time'], 2), snap['cpu']['usage'],
                         [[g.get('util'), g.get('enc'), g.get('dec')] for g in snap['gpus']],
                         {n['name']: [round(n['rx_bps']), round(n['tx_bps'])] for n in snap['network']})
                with self.lock:
                    self.data = snap
                    self.history.append(point)
            except Exception as e:  # keep sampling even if one source fails
                print('sampler error:', repr(e), file=sys.stderr)
            time.sleep(max(0.1, self.cfg['sample_interval'] - (time.time() - t0)))

    def _sample(self):
        now = time.time()
        dt = max(1e-3, now - self._prev_t)

        cur = cpu_times()
        usage = {}
        for name, (busy, total) in cur.items():
            pb, pt = self._prev_cpu.get(name, (busy, total))
            usage[name] = 100.0 * (busy - pb) / (total - pt) if total > pt else 0.0
        self._prev_cpu = cur
        per_core = [round(usage['cpu%d' % i], 1) for i in range(len(cur) - 1) if 'cpu%d' % i in usage]

        if now - self._addrs_t > 5:
            self._addrs, self._addrs_t = iface_addrs(), now
        nets = []
        prev_net = {}
        for name in link_up_ifaces():
            rx, tx = iface_bytes(name)
            prx, ptx = self._prev_net.get(name, (rx, tx))
            prev_net[name] = (rx, tx)
            speed = read('/sys/class/net/%s/speed' % name)
            info = self._addrs.get(name, {})
            nets.append({
                'name': name,
                'addrs': info.get('addrs', []),
                'mac': info.get('mac'),
                'link_mbps': int(speed) if speed and speed.lstrip('-').isdigit() and int(speed) > 0 else None,
                'rx_bps': max(0, (rx - prx) * 8 / dt),
                'tx_bps': max(0, (tx - ptx) * 8 / dt),
            })
        self._prev_net = prev_net

        gpus, gpu_error = gpu_stats()

        ccfg = self.cfg['casparcg']
        main, children = find_caspar(ccfg['process_name'])
        proc = None
        if main:
            pids = [main] + children
            ticks = sum(proc_cpu_ticks(p) for p in pids)
            cpu_pct = None
            if self._prev_proc and self._prev_proc[0] == main:
                cpu_pct = 100.0 * (ticks - self._prev_proc[1]) / CLK_TCK / dt
            self._prev_proc = (main, ticks)
            rss, pss = proc_mem(main)
            total_pss = pss + sum(proc_mem(p)[1] for p in children)
            started = self.btime + proc_start_ticks(main) / CLK_TCK
            proc = {
                'pid': main,
                'started': started,
                'age': now - started,
                'rss': rss,
                'pss': pss,
                'total_pss': total_pss,
                'helpers': len(children),
                'cpu_pct': cpu_pct,
                'threads': int(proc_stat_fields(main)[17]) if os.path.exists('/proc/%d' % main) else None,
                'mem_watch': self.memwatch.update(main, now),
            }
        else:
            self._prev_proc = None
            self.memwatch.update(None, now)

        self._prev_t = now
        uptime = float(read('/proc/uptime', '0 0').split()[0])
        return {
            'time': now,
            'hostname': socket.gethostname(),
            'cpu': dict(self.cpu_info, usage=round(usage.get('cpu', 0.0), 1), per_core=per_core,
                        load=os.getloadavg()),
            'memory': meminfo(),
            'system': {'uptime': uptime, 'boot': self.btime, 'kernel': platform.release()},
            'gpus': gpus,
            'gpu_error': gpu_error,
            'network': nets,
            'process': proc,
            'service': service_state(ccfg['service_name']),
        }

    def snapshot(self):
        with self.lock:
            return dict(self.data)

    def history_since(self, since):
        with self.lock:
            pts = [p for p in self.history if p[0] > since]
        return {'interval': self.cfg['sample_interval'], 'minutes': self.cfg['history_minutes'], 'points': pts}


# --------------------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    server_version = 'CasparMonitor/1.0'
    app = None  # set in main()

    def log_message(self, fmt, *args):
        pass  # the UI polls every second; keep stdout quiet

    def _send(self, code, body, ctype='application/json; charset=utf-8'):
        if not isinstance(body, bytes):
            body = json.dumps(body, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        url = urlparse(self.path)
        app = self.app
        if url.path in ('/', '/index.html'):
            with open(os.path.join(BASE_DIR, 'static', 'index.html'), 'rb') as f:
                return self._send(200, f.read(), 'text/html; charset=utf-8')
        if url.path == '/api/status':
            data = app['sampler'].snapshot()
            data['casparcg'] = app['link'].snapshot()
            data['preview'] = app['cfg']['preview']
            hs = parse_qs(url.query).get('hist_since', [None])[0]
            if hs is not None:      # graphs: piggyback the history points the page does not have yet
                try:
                    data['history'] = app['sampler'].history_since(float(hs))
                except ValueError:
                    pass
            return self._send(200, data)
        if url.path == '/api/history':
            try:
                since = float(parse_qs(url.query).get('since', ['0'])[0])
            except ValueError:
                since = 0.0
            return self._send(200, app['sampler'].history_since(since))
        if url.path == '/api/log':
            ccfg = app['cfg']['casparcg']
            path = newest_log(ccfg['log_dir'])
            if not path:
                return self._send(200, {'file': None, 'lines': [], 'key': None})
            st = os.stat(path)
            key = '%s:%d:%d' % (path, st.st_ino, st.st_size)
            if parse_qs(url.query).get('key', [None])[0] == key:
                return self._send(200, {'unchanged': True, 'key': key})
            return self._send(200, {'file': path, 'key': key, 'lines': tail_lines(path, ccfg['log_lines'])})
        self._send(404, {'error': 'not found'})

    def do_POST(self):
        path = urlparse(self.path).path
        if path not in ('/api/kill', '/api/service', '/api/amcp'):
            return self._send(404, {'error': 'not found'})
        length = int(self.headers.get('Content-Length') or 0)
        try:
            body = json.loads(self.rfile.read(length) or b'{}')
        except ValueError:
            body = {}
        ccfg = self.app['cfg']['casparcg']
        if path == '/api/amcp':
            cmd = str(body.get('command', '')).strip()
            if not cmd or len(cmd) > 8192 or '\r' in cmd or '\n' in cmd:
                return self._send(400, {'ok': False, 'response': 'enter a single-line AMCP command'})
            try:
                res = amcp_command(ccfg['host'], ccfg['amcp_port'], cmd)
            except OSError as e:
                res = {'ok': False, 'code': None, 'response': 'cannot connect to AMCP %s:%d: %s'
                       % (ccfg['host'], ccfg['amcp_port'], e)}
            print('amcp from %s: %s -> %s' % (self.client_address[0], cmd, res['response'].split('\n', 1)[0]),
                  flush=True)
            return self._send(200, res)
        if body.get('confirm') is not True:
            return self._send(400, {'ok': False, 'message': 'confirm required'})
        if path == '/api/kill':
            what = 'kill'
            res = kill_caspar(ccfg['process_name'], ccfg['kill_timeout'])
        else:
            what = 'service %s' % body.get('action')
            res = service_action(ccfg['service_name'], body.get('action'))
        print('%s requested from %s: %s' % (what, self.client_address[0], res['message']), flush=True)
        return self._send(200 if res['ok'] else 409, res)


def main():
    cfg_path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(BASE_DIR, 'config.json')
    cfg = load_config(cfg_path)
    sampler = Sampler(cfg)
    link = CasparLink(cfg['casparcg'])
    sampler.start()
    link.start()
    Handler.app = {'cfg': cfg, 'sampler': sampler, 'link': link}
    httpd = ThreadingHTTPServer((cfg['http']['bind'], cfg['http']['port']), Handler)
    httpd.daemon_threads = True
    print('CasparCG monitor on http://%s:%d (config %s)' % (cfg['http']['bind'], cfg['http']['port'], cfg_path),
          flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    main()
