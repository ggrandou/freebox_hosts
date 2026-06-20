# freebox-hosts

Daemon that maintains a dnsmasq `hosts(5)` file from Freebox LAN data and reloads dnsmasq automatically on every change.

## How it works

- **REST polling** every 5 minutes to fetch the full list of LAN hosts.
- **WebSocket** for near-instant detection of reachability changes (`lan_host_l3addr_reachable`).
- The hosts file is only rewritten when its content actually changes; dnsmasq then receives a `SIGHUP`.
- On any communication error the existing file is left unchanged.

Hosts inactive for more than 7 days (configurable) are removed. IPv6 link-local addresses and subnet addresses (host-part = 0) are excluded; GUA and ULA addresses are included.

## Installation

Requires Python 3.11+.

```sh
pip install git+https://github.com/ggrandou/freebox-hosts.git
```

Or in development mode:

```sh
. ./setup_env.sh
```

## Usage

```
freebox-hosts [options]

Options:
  -f, --hosts-file PATH       Path to the hosts file (default: /tmp/hosts/freebox.hosts)
  -p, --poll-interval SECS    Full refresh interval in seconds (default: 300)
  -l, --log-file PATH         Write logs to this file instead of stderr
  -v, --verbose               Enable debug logging
  -1, --once                  Generate the hosts file once and exit
      --inactive-threshold H  Keep inactive hosts seen within the last H hours (default: 168)
```

Pass `--hosts-file -` to write to stdout (useful for testing).

## First run

On the first launch the daemon requests authorization on the Freebox screen. Follow the instructions printed to stderr, then restart.

## Deployment on OpenWrt

Clone the repository and install dependencies into a local venv:

```sh
cd /opt
git clone https://github.com/ggrandou/freebox-hosts.git freebox_hosts
cd freebox_hosts
./setup_env.sh
```

Install and enable the init script:

```sh
cp openwrt/freebox-hosts /etc/init.d/freebox-hosts
chmod +x /etc/init.d/freebox-hosts
service freebox-hosts enable
service freebox-hosts start
```

The init script points to `/opt/freebox_hosts/.venv/bin/freebox-hosts`. It uses procd with `respawn` to automatically restart the daemon on failure.

## Dependencies

- [`python-freebox`](https://github.com/ggrandou/python-freebox) — Freebox API client
- [`text-unidecode`](https://pypi.org/project/text-unidecode/) — hostname normalization
- [`websockets`](https://pypi.org/project/websockets/) ≥ 12

## License

GPL-3.0-or-later
