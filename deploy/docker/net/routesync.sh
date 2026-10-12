#!/bin/sh
# Keeps the routing of the shared network namespace the way the sidecar needs it.
#
#   routesync.sh once     one pass
#   routesync.sh loop     a pass every 2 seconds, forever
#
# Why it exists: the Android in this namespace runs its own network daemon. It routes by
# policy rules into a per-interface table (number = 1000 + interface index of eth0), removes
# rules it did not create and re-fills that table whenever it reconfigures the interface.
# Routes the tunnel needs therefore have to be put back continuously:
#
#   - rule  100: lookup main      so the sidecar's own programs (VPN client, lookups) are
#                                 never caught by Android's final "unreachable" rule
#   - main table                  default route, pinned VPN server routes, our own tunnel routes
#   - Android's table(s)          a copy of every main route except the default one: tunnel
#                                 routes, the bridge subnet and the server pins
#   - /etc/resolv.conf            the configured resolvers
#
# The default route of Android's table is left alone on purpose. When the tunnel is down,
# traffic follows it to the uplink, where the kill switch drops it.
set -u

RUN=${SF_RUN:-/run/sf}
# shellcheck disable=SC1091
. "$RUN/env"

WG_MARK=51820
WG_TABLE=51820
LAN_RANGES="10.0.0.0/8 172.16.0.0/12 192.168.0.0/16"

log() { printf '[sf-net] routes: %s\n' "$*"; }

# Flags `ip route show` prints but `ip route replace` does not accept.
clean() { sed -e 's/ linkdown//g; s/ dead//g; s/ offload//g; s/ trap//g; s/ rt_[a-z_]*//g; s/[[:space:]]*$//'; }

# Tables Android may use: one per ethernet-like interface, 1000 + ifindex. The index is read,
# never assumed: it is 2 on a fresh container but differs once a second network is attached.
android_tables() {
    for d in /sys/class/net/eth*; do
        [ -e "$d/ifindex" ] || continue
        echo $((1000 + $(cat "$d/ifindex")))
    done
}

# Routes this sidecar owns in the main table (the VPN client adds its own for OpenVPN).
wanted_main() {
    if [ -n "$WAN_GW" ] && [ -n "$WAN_IF" ]; then
        if [ -s "$RUN/pins" ]; then
            while read -r _proto ip _port _host; do
                # A server on the directly attached network needs no host route (and one
                # through the gateway would be a detour); everything else gets pinned.
                [ -n "$(ip -4 route show table main match "$ip" dev "$WAN_IF" scope link 2>/dev/null)" ] && continue
                echo "$ip via $WAN_GW dev $WAN_IF"
            done <"$RUN/pins" | sort -u
        fi
        if [ "$SF_ALLOW_LAN" = 1 ]; then
            case "$SF_MODE" in vpn|gateway)
                # Without these the two half-default tunnel routes would swallow LAN addresses.
                for r in $LAN_RANGES; do echo "$r via $WAN_GW dev $WAN_IF"; done ;;
            esac
        fi
    fi
    if [ -s "$RUN/uplink" ]; then
        # "<dev> [gateway]" written by the entrypoint for tunnels it sets up itself.
        read -r up_dev up_gw <"$RUN/uplink"
        if [ -e "/sys/class/net/$up_dev" ]; then
            for half in 0.0.0.0/1 128.0.0.0/1; do
                if [ -n "${up_gw:-}" ]; then echo "$half via $up_gw dev $up_dev"; else echo "$half dev $up_dev"; fi
            done
        fi
    fi
}

snapshot() {
    ip -4 rule show 2>/dev/null
    ip -4 route show table main 2>/dev/null
    for t in $(android_tables); do
        echo "== $t"
        ip -4 route show table "$t" 2>/dev/null
    done
    cat "$RUN/env" "$RUN/pins" "$RUN/uplink" /etc/resolv.conf 2>/dev/null
}

apply_route() {  # apply_route <table> <route words...>
    table=$1; shift
    # shellcheck disable=SC2068
    if ip -4 route replace $@ table "$table" 2>"$RUN/route.err"; then
        log "table $table: $*"
    else
        failed=1
        echo "table $table: cannot add '$*': $(tr '\n' ' ' <"$RUN/route.err")" >>"$RUN/routes.fail.new"
    fi
}

once() {
    # Read again on every pass: the entrypoint rewrites this file when interfaces change.
    # shellcheck disable=SC1091
    . "$RUN/env"
    before=$(snapshot)
    # Nothing changed since the last clean pass: do nothing, log nothing.
    if [ -e "$RUN/routes.snap" ] && [ "$before" = "$(cat "$RUN/routes.snap")" ]; then
        return 0
    fi
    failed=0
    : >"$RUN/routes.fail.new"

    rules=$(ip -4 rule show 2>/dev/null)
    if ! printf '%s\n' "$rules" | grep -q '^100:[[:space:]]*from all lookup main'; then
        ip -4 rule add pref 100 lookup main 2>/dev/null && log "rule 100 (lookup main) restored"
    fi
    if [ -e /sys/class/net/wg0 ] && [ -n "$WAN_GW" ]; then
        # WireGuard marks its own encrypted packets; the mark sends them out of the uplink
        # instead of back into the tunnel.
        ip -4 route replace default via "$WAN_GW" dev "$WAN_IF" table "$WG_TABLE" 2>/dev/null
        if ! printf '%s\n' "$rules" | grep -q "^90:.*lookup $WG_TABLE"; then
            ip -4 rule add pref 90 fwmark "$WG_MARK" lookup "$WG_TABLE" 2>/dev/null && log "rule 90 (WireGuard transport) restored"
        fi
    fi

    main=$(ip -4 route show table main 2>/dev/null | clean)
    if [ -n "$WAN_GW" ] && [ -n "$WAN_IF" ] && ! printf '%s\n' "$main" | grep -q '^default '; then
        apply_route main default via "$WAN_GW" dev "$WAN_IF"
    fi
    wanted_main | while read -r line; do
        [ -n "$line" ] || continue
        printf '%s\n' "$main" | grep -qxF -- "$line" || echo "$line"
    done >"$RUN/routes.todo"
    while read -r line; do
        apply_route main "$line"
    done <"$RUN/routes.todo"

    main=$(ip -4 route show table main 2>/dev/null | clean | grep -v '^default ')
    for t in $(android_tables); do
        have=$(ip -4 route show table "$t" 2>/dev/null | clean)
        printf '%s\n' "$main" | while read -r line; do
            [ -n "$line" ] || continue
            printf '%s\n' "$have" | grep -qxF -- "$line" || echo "$line"
        done >"$RUN/routes.todo"
        while read -r line; do
            apply_route "$t" "$line"
        done <"$RUN/routes.todo"
    done

    want_resolv=$(
        _old=$IFS; IFS=,
        # shellcheck disable=SC2086
        set -- $SF_DNS
        IFS=$_old
        for d in "$@"; do echo "nameserver $d"; done
        echo "options timeout:2 attempts:2"
    )
    if [ -n "$SF_DNS" ] && [ "$want_resolv" != "$(cat /etc/resolv.conf 2>/dev/null)" ]; then
        # Written in place: Docker bind-mounts this file, so it cannot be replaced by rename.
        printf '%s\n' "$want_resolv" >/etc/resolv.conf && log "resolv.conf set to $SF_DNS"
    fi

    # Remember the result only when everything applied, so a failed route is retried on the
    # next pass. The same failure is reported once, not every two seconds.
    if [ "$failed" = 0 ]; then
        snapshot >"$RUN/routes.snap"
        rm -f "$RUN/routes.fail"
    else
        rm -f "$RUN/routes.snap"
        if ! cmp -s "$RUN/routes.fail.new" "$RUN/routes.fail" 2>/dev/null; then
            while read -r line; do log "$line"; done <"$RUN/routes.fail.new"
            cp "$RUN/routes.fail.new" "$RUN/routes.fail"
        fi
    fi
    return 0
}

case "${1:-}" in
    once) once ;;
    loop)
        while :; do
            once
            # Short enough that Android wiping a route is repaired before apps notice,
            # cheap enough (a handful of `ip` calls) for a hundred sidecars.
            sleep 2 &
            wait $!
        done ;;
    *) echo "usage: routesync.sh once|loop" >&2; exit 2 ;;
esac
