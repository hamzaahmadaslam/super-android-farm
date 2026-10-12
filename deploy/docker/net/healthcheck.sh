#!/bin/sh
# healthcheck.sh           Docker HEALTHCHECK: exit 0 = healthy
# healthcheck.sh report    everything the farm wants to know, in one `docker exec`
#
# Healthy means all three: the supervisor is alive, the mode's network is ready (for a VPN:
# the tunnel is up) and the firewall rules are really loaded. The last point is checked, not
# assumed: a sidecar that "should" have a kill switch but does not is the worst failure here.
set -u
RUN=${SF_RUN:-/run/sf}

kv() { sed -n "s/^$1=//p" "$RUN/status" 2>/dev/null | head -n 1; }

if [ "${1:-}" = report ]; then
    echo "== status"
    cat "$RUN/status" 2>/dev/null
    echo "== check"
    if /sf/killswitch.sh check 2>/dev/null; then echo "killswitch_check=ok"; else echo "killswitch_check=failed"; fi
    if [ -e "$RUN/heartbeat" ]; then echo "heartbeat_age=$(($(date +%s) - $(stat -c %Y "$RUN/heartbeat")))"; fi
    echo "now=$(date +%s)"
    echo "== rules4"
    iptables -S 2>&1
    echo "== rules6"
    ip6tables -S 2>&1
    echo "== ipv6"
    cat /proc/net/if_inet6 2>/dev/null
    echo "== pins"
    cat "$RUN/pins" 2>/dev/null
    echo "== log"
    tail -n "${2:-120}" "$RUN/openvpn.log" 2>/dev/null
    exit 0
fi

[ -e "$RUN/status" ] || { echo "no status yet"; exit 1; }
if [ -e "$RUN/heartbeat" ]; then
    age=$(($(date +%s) - $(stat -c %Y "$RUN/heartbeat")))
    [ "$age" -lt 60 ] || { echo "supervisor stalled (${age}s)"; exit 1; }
fi
/sf/killswitch.sh check || { echo "firewall rules are not in place"; exit 1; }
[ "$(kv ready)" = 1 ] || { echo "state: $(kv state) $(kv last_error)"; exit 1; }
echo "ok: $(kv state)"
exit 0
