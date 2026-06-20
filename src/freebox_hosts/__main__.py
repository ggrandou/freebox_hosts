#!/usr/bin/env python3
"""Daemon maintaining a hosts(5) file from Freebox LAN data and reloading dnsmasq on changes.

Uses REST polling (default: every 5 minutes) combined with WebSocket events for near-instant
updates on host reachability changes.

On any communication error the current hosts file is preserved unchanged.

Usage:
    freebox_hosts
    freebox_hosts --hosts-file /tmp/hosts/freebox.hosts --verbose
"""

from __future__ import annotations

import argparse
import ipaddress
import logging
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time

import text_unidecode as unidecode

from freebox import CredentialStore, Freebox, LanHost

APP_ID             = 'freebox_hosts'
APP_NAME           = 'Freebox Hosts'
APP_VERSION        = '2.0'
HOSTS_FILE         = '/tmp/hosts/freebox.hosts'
POLL_INTERVAL      = 300  # seconds
INACTIVE_THRESHOLD = 168  # hours (7 days)

LAN_EVENTS = [
    'lan_host_l3addr_reachable',
]

log = logging.getLogger(__name__)


# ── hosts-file logic ──────────────────────────────────────────────────────────

def clean_hostname(name: str) -> str:
    name = unidecode.unidecode(name.lower())
    name = re.sub('[^a-z0-9-]', '-', name)
    name = re.sub('-+', '-', name)
    name = name.strip('-')
    name = re.sub('^([0-9])', r'host-\1', name)
    return name


# Names matching these patterns are considered generic/default and are suppressed
# when at least one non-default name is available for the same host.
DEFAULT_NAME_PATTERNS = [
    re.compile(r'^android-[0-9a-z]+$'),            # Android default hostnames
    re.compile(r'^host-[0-9a-f-]+$'),              # generic host-XXXXX names
    re.compile(r'^[0-9a-f]{2}(-[0-9a-f]{2}){5}'),   # cleaned MAC addresses (optional suffix)
]


def is_default_name(name: str) -> bool:
    return any(p.match(name) for p in DEFAULT_NAME_PATTERNS)


_NUMBERED_SUFFIX = re.compile(r'^(.+)-\d+$')


def filter_numbered_suffixes(names: list[str]) -> list[str]:
    """Remove 'xxxx-N' when 'xxxx' is also present in the same list."""
    name_set = set(names)
    return [n for n in names if not (m := _NUMBERED_SUFFIX.match(n)) or m.group(1) not in name_set]


def _ipv6_category(addr: str) -> str | None:
    """Return 'ula', 'gua', or None (link-local / zero host-part, skip)."""
    if addr.startswith('fe80:'):
        return None
    # Reject addresses whose host part (last 64 bits) is all zeros — these are
    # subnet/network addresses (e.g. 2a01:e0a:a31:56f0::) delegated to the LAN,
    # not valid unicast host addresses.
    if ipaddress.ip_address(addr).packed[8:] == b'\x00' * 8:
        return None
    if addr.startswith('fc') or addr.startswith('fd'):
        return 'ula'
    return 'gua'


def build_hosts_content(lanhost_list: list[LanHost], inactive_threshold: int = INACTIVE_THRESHOLD) -> str:
    now = int(time.time())
    threshold_secs = inactive_threshold * 3600

    # IPs currently active on any host — used to exclude reassigned addresses.
    active_addrs: set[str] = {
        c.addr
        for host in lanhost_list
        for c in host.l3connectivities
        if c.active
    }

    # Secondary names shared by more than one host are ambiguous: suppress them everywhere.
    primary_names = {clean_hostname(h.primary_name) for h in lanhost_list}
    secondary_counts: dict[str, int] = {}
    for host in lanhost_list:
        for n in host.names:
            cn = clean_hostname(n.name)
            secondary_counts[cn] = secondary_counts.get(cn, 0) + 1
    excluded_names = primary_names | {n for n, c in secondary_counts.items() if c > 1}

    hosts: list[str] = []

    for host in lanhost_list:
        try:
            primary_name   = clean_hostname(host.primary_name)
            host_reachable = any(c.reachable for c in host.l3connectivities)
            host_active    = any(c.active    for c in host.l3connectivities)

            addrs4: list[str] = []
            addrs6: list[str] = []

            if host_reachable or host_active:
                # Keep all currently active addresses.
                for c in host.l3connectivities:
                    if not c.active:
                        continue
                    if c.af == 'ipv4':
                        addrs4.append(c.addr)
                    elif c.af == 'ipv6' and _ipv6_category(c.addr) is not None:
                        addrs6.append(c.addr)
            else:
                # Inactive host: one best address per category, within threshold,
                # excluding IPs that have been reassigned to another active host.
                def best(candidates):
                    valid = [
                        c for c in candidates
                        if c.last_time_reachable
                        and (now - c.last_time_reachable) <= threshold_secs
                        and c.addr not in active_addrs
                    ]
                    if not valid:
                        return None
                    return max(valid, key=lambda c: c.last_time_reachable).addr

                ipv4 = [c for c in host.l3connectivities if c.af == 'ipv4']
                ipv6_gua = [c for c in host.l3connectivities if c.af == 'ipv6' and _ipv6_category(c.addr) == 'gua']
                ipv6_ula = [c for c in host.l3connectivities if c.af == 'ipv6' and _ipv6_category(c.addr) == 'ula']

                if (addr := best(ipv4)):
                    addrs4.append(addr)
                for addr in filter(None, [best(ipv6_gua), best(ipv6_ula)]):
                    addrs6.append(addr)

            if not (addrs4 or addrs6):
                continue

            names = sorted({
                clean_hostname(n.name)
                for n in host.names
                if clean_hostname(n.name) not in excluded_names
            })
            all_names = [primary_name] + names
            good_names = [n for n in all_names if not is_default_name(n)]
            display_names = filter_numbered_suffixes(good_names if good_names else all_names)
            hname = ' '.join(display_names if display_names else all_names)
            key = ipaddress.ip_address
            for addr in sorted(set(addrs4), key=key) + sorted(set(addrs6), key=key):
                hosts.append(f'{addr}\t{hname}')

        except (AttributeError, TypeError) as e:
            log.warning(f'skipping host with unexpected data: {e}')

    def ip_key(line: str):
        addr = ipaddress.ip_address(line.split('\t')[0])
        return (addr.version, addr)

    lines = ['# Freebox LAN hosts - hosts(5) format'] + sorted(set(hosts), key=ip_key)
    return '\n'.join(lines) + '\n'


def write_if_changed(content: str, path: str) -> bool:
    """Write content to path in-place if it differs from current. Returns True if written.

    Pass path='-' to write to stdout (always returns True, skips dnsmasq reload).

    dnsmasq watches /tmp/hosts/ with inotify and reads the file immediately
    on IN_CLOSE_WRITE. Permissions must be 0644 *before* the write so that
    inotify-triggered reads don't get EACCES.
    """
    if path == '-':
        sys.stdout.write(content)
        return True

    os.makedirs(os.path.dirname(path) or '.', mode=0o755, exist_ok=True)

    try:
        with open(path) as f:
            if f.read() == content:
                return False
        mode = os.stat(path).st_mode & 0o777
    except FileNotFoundError:
        mode = None

    if mode != 0o644:
        open(path, 'a').close()   # create if missing
        os.chmod(path, 0o644)

    with open(path, 'w') as f:
        f.write(content)

    return True


def reload_dnsmasq() -> None:
    try:
        result = subprocess.run(['pidof', '/usr/sbin/dnsmasq'], capture_output=True, text=True)
        pid_str = result.stdout.strip()
        if not pid_str:
            log.debug('dnsmasq not running, skipping SIGHUP')
            return
        os.kill(int(pid_str.split()[0]), signal.SIGHUP)
        log.info(f'Sent SIGHUP to dnsmasq (pid {pid_str})')
    except Exception as e:
        log.warning(f'Failed to reload dnsmasq: {e}')


# ── Daemon ────────────────────────────────────────────────────────────────────

class FreeboxHostsDaemon:
    def __init__(self, hosts_file: str, poll_interval: int, inactive_threshold: int) -> None:
        self.hosts_file         = hosts_file
        self.poll_interval      = poll_interval
        self.inactive_threshold = inactive_threshold
        self._fbx = Freebox(
            app_id=APP_ID,
            app_name=APP_NAME,
            app_version=APP_VERSION,
            device_name=socket.gethostname(),
            store=CredentialStore(APP_ID),
            on_pending=lambda msg: print(msg, file=sys.stderr),
        )
        self._refresh = threading.Event()
        self._stop    = threading.Event()

    # ── Hosts update ──────────────────────────────────────────────────────────

    def _fetch_and_update(self) -> None:
        """Fetch LAN hosts and update the hosts file if content changed."""
        hosts = self._fbx.lan.hosts('pub')
        content = build_hosts_content(hosts, self.inactive_threshold)
        if not write_if_changed(content, self.hosts_file):
            log.debug('Hosts file unchanged')
            return
        if self.hosts_file == '-':
            return
        log.info(f'Hosts file updated: {self.hosts_file}')
        reload_dnsmasq()

    # ── WebSocket ─────────────────────────────────────────────────────────────

    def _ws_manager(self) -> None:
        """Background thread: maintain WebSocket subscription, retry on disconnect."""
        retry_delay = 5
        while not self._stop.is_set():
            try:
                with self._fbx.events(LAN_EVENTS) as stream:
                    log.info('WebSocket connected, listening for LAN events')
                    retry_delay = 5
                    for notification in stream:
                        name = ''
                        if notification.result and isinstance(notification.result, dict):
                            name = notification.result.get('primary_name', '')
                        log.info(f'LAN event: {notification.event}' + (f' ({name})' if name else ''))
                        self._refresh.set()
                        if self._stop.is_set():
                            break
                if not self._stop.is_set():
                    log.debug('WebSocket disconnected, reconnecting…')
            except Exception as e:
                if self._stop.is_set():
                    break
                log.warning(f'WebSocket error: {e}, retrying in {retry_delay}s')
                self._stop.wait(timeout=retry_delay)
                retry_delay = min(retry_delay * 2, 120)

    # ── Main loop ─────────────────────────────────────────────────────────────

    def run(self, *, once: bool = False) -> None:
        # SIGTERM sets the stop flag; SIGINT is left as-is so KeyboardInterrupt propagates.
        signal.signal(signal.SIGTERM, lambda *_: (self._stop.set(), self._refresh.set()))

        self._fbx.open()

        try:
            self._fetch_and_update()
        except Exception as e:
            log.error(f'Fetch failed (keeping existing file): {e}')

        if once:
            self._fbx.close()
            return

        # Start WebSocket manager thread
        ws_thread = threading.Thread(target=self._ws_manager, daemon=True, name='ws-manager')
        ws_thread.start()

        log.info(f'Daemon running (poll every {self.poll_interval}s, hosts: {self.hosts_file})')

        try:
            while not self._stop.is_set():
                # Block until a WS event fires OR the poll interval expires
                self._refresh.wait(timeout=self.poll_interval)
                if self._stop.is_set():
                    break
                self._refresh.clear()

                try:
                    self._fetch_and_update()
                except Exception as e:
                    log.error(f'Fetch failed, keeping current file: {e}')
        except KeyboardInterrupt:
            pass
        finally:
            self._stop.set()
            self._fbx.close()
            log.info('Daemon stopped')


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(
        description='Freebox LAN hosts daemon — maintains a dnsmasq hosts(5) file',
    )
    ap.add_argument('--hosts-file', '-f', default=HOSTS_FILE,
                    help=f'Path to the hosts file (default: {HOSTS_FILE})')
    ap.add_argument('--poll-interval', '-p', type=int, default=POLL_INTERVAL,
                    help=f'Full refresh interval in seconds (default: {POLL_INTERVAL})')
    ap.add_argument('--log-file', '-l', metavar='PATH',
                    help='Write logs to this file instead of stderr')
    ap.add_argument('--verbose', '-v', action='store_true',
                    help='Enable debug logging')
    ap.add_argument('--once', '-1', action='store_true',
                    help='Generate the hosts file once and exit')
    ap.add_argument('--inactive-threshold', type=int, default=INACTIVE_THRESHOLD,
                    metavar='HOURS',
                    help=f'Keep inactive hosts seen within this many hours (default: {INACTIVE_THRESHOLD})')
    args = ap.parse_args()

    os.umask(0o022)  # ensure files are created as 0644, not 0600

    handler: logging.Handler
    if args.log_file:
        handler = logging.FileHandler(args.log_file)
    else:
        handler = logging.StreamHandler(sys.stderr)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format='%(asctime)s %(levelname)s %(name)s %(message)s',
        handlers=[handler],
    )

    FreeboxHostsDaemon(args.hosts_file, args.poll_interval, args.inactive_threshold).run(once=args.once)


if __name__ == '__main__':
    main()
