#!/bin/sh
# Super Farm network sidecar: entrypoint and supervisor.
#
# Driven entirely by environment variables; profiles and credentials are bind-mounted
# read-only and never appear in the environment or in the log.
#
#   SF_MODE                 direct | vpn | gateway | none | gwserver
#   SF_VPN_PROVIDER         openvpn | wireguard                        (vpn, gwserver)
#   SF_KILLSWITCH           1 = nothing leaves outside the tunnel       (default 1)
#   SF_ALLOW_LAN            1 = private (RFC 1918) addresses stay reachable
#   SF_DNS                  comma-separated IPv4 resolvers
#   SF_DNS_LEAK_PROTECTION  1 = name lookups only through the tunnel once the server is known
#   SF_IPV6                 1 = leave IPv6 usable
#   SF_GATEWAY_IP           address of the shared gateway              (gateway, gwserver)
#   SF_UPLINK_GW            bridge address of the uplink network       (gateway, gwserver)
#   SF_ADB_PORT             port the Android's ADB listens on           (default 5555)
#   SF_ADB_FROM             gateway = only the host may connect to ADB | any
#   SF_VPN_PING, SF_VPN_PING_RESTART, SF_VPN_CONNECT_RETRY_MAX        OpenVPN timers
#   SF_PROFILE_NAME         shown in the status file, nothing else
#   arguments               extra options appended to the openvpn command line
#
#   /sf/vpn/profile.ovpn    OpenVPN profile (already sanitised by the farm)
#   /sf/vpn/profile.conf    WireGuard profile
#   /sf/secrets/auth        username + password, two lines, mode 0600
#
# THE ORDER BELOW IS THE SECURITY PROPERTY. Do not reorder it.
#   1. default-deny firewall for IPv4 and IPv6
#   2. resolve the VPN server names while only DNS to the configured resolvers is open;
#      pin the addresses (firewall + route + /etc/hosts, so a reconnect needs no DNS)
#   3. close DNS on the uplink again
#   4. start the tunnel
#   5. route sync loop
# The container never exits because the tunnel failed: the Android shares this network
# namespace, and a restarted sidecar would leave it attached to a dead one. The tunnel is
# restarted in place instead, behind the firewall that stays up the whole time.
set -u

RUN=/run/sf
PROFILE_OVPN=/sf/vpn/profile.ovpn
PROFILE_WG=/sf/vpn/profile.conf
AUTH=/sf/secrets/auth
OVPN_LOG=$RUN/openvpn.log
WG_MARK=51820

log() { printf '[sf-net] %s\n' "$*"; }
now() { date +%s; }
kv() { sed -n "s/^$1=//p" "$2" 2>/dev/null | head -n 1; }

is_ipv4() {
    case "$1" in ''|*[!0-9.]*|.*|*.|*..*) return 1 ;; esac
    _old=$IFS; IFS=.
    # shellcheck disable=SC2086
    set -- $1
    IFS=$_old
    [ $# -eq 4 ] || return 1
    for _o in "$@"; do
        [ ${#_o} -le 3 ] && [ "$_o" -le 255 ] || return 1
    done
    return 0
}
is_host() {
    [ ${#1} -le 253 ] || return 1
    case "$1" in ''|*[!A-Za-z0-9.-]*|-*|.*|*..*) return 1 ;; esac
    return 0
}
is_num() { case "$1" in ''|*[!0-9]*) return 1 ;; esac; return 0; }
is_ifname() { case "$1" in ''|*[!A-Za-z0-9_.-]*) return 1 ;; esac; [ ${#1} -le 15 ]; }
flag() { case "${1:-}" in 1|true|yes|on) echo 1 ;; *) echo 0 ;; esac; }

# ---------------------------------------------------------------- VPN servers of a profile
# Prints "<proto> <host> <port>" for every server the profile can connect to. This reads the
# very file the VPN client will use, so what is pinned is what will be dialled.
# `entrypoint.sh list-remotes <provider> <file>` prints the same list for inspection and tests.
remotes() {  # remotes <openvpn|wireguard> <profile file>
    if [ "$1" = wireguard ]; then
        awk -F= '/^[ \t]*[Ee]ndpoint[ \t]*=/ {
            v = $2; gsub(/[ \t\r]/, "", v); n = split(v, p, ":")
            if (n == 2) print "udp", p[1], p[2]
        }' "$2"
    else
        awk '
            function norm(p) { p = tolower(p); if (p ~ /^tcp/) return "tcp"; if (p ~ /^udp/) return "udp"; return "" }
            { sub(/\r$/, "") }
            /^[ \t]*[#;]/ { next }
            $1 == "proto" { g = norm($2); if (g != "") protos[g] = 1 }
            $1 == "port" || $1 == "rport" { gport = $2 }
            $1 == "remote" { n++; host[n] = $2; port[n] = $3; pr[n] = norm($4) }
            END {
                np = 0; for (p in protos) np++
                if (np == 0) protos["udp"] = 1
                for (i = 1; i <= n; i++) {
                    prt = port[i]; if (prt == "") prt = gport; if (prt == "") prt = "1194"
                    if (pr[i] != "") print pr[i], host[i], prt
                    else for (p in protos) print p, host[i], prt
                }
            }' "$2"
    fi
}

if [ "${1:-}" = list-remotes ]; then
    [ $# -eq 3 ] && [ -r "$3" ] || { echo "usage: entrypoint.sh list-remotes openvpn|wireguard <profile>" >&2; exit 2; }
    remotes "$2" "$3"
    exit 0
fi

# ---------------------------------------------------------------- OpenVPN hook
# Called by openvpn (--route-up) once the tunnel carries traffic. The values come from the
# VPN server, so they are checked and stored as plain data; nothing ever sources this file.
if [ "${1:-}" = hook-route-up ]; then
    umask 077
    d=${dev:-}; case "$d" in tun*) is_ifname "$d" || d= ;; *) d= ;; esac
    a=${ifconfig_local:-}; is_ipv4 "$a" || a=
    g=${route_vpn_gateway:-}; is_ipv4 "$g" || g=
    r=${trusted_ip:-}; is_ipv4 "$r" || r=
    printf 'dev=%s\nip=%s\ngateway=%s\nremote=%s\nup=%s\n' "$d" "$a" "$g" "$r" "$(date +%s)" >"$RUN/tunnel.env.tmp" \
        && mv -f "$RUN/tunnel.env.tmp" "$RUN/tunnel.env"
    exit 0
fi

# ---------------------------------------------------------------- settings
SF_MODE=${SF_MODE:-direct}
case "$SF_MODE" in direct|vpn|gateway|none|gwserver) ;; *) log "ERROR: unknown SF_MODE '$SF_MODE' (direct|vpn|gateway|none|gwserver)"; exit 64 ;; esac
PROVIDER=
case "$SF_MODE" in vpn|gwserver)
    PROVIDER=${SF_VPN_PROVIDER:-openvpn}
    case "$PROVIDER" in openvpn|wireguard) ;; *) log "ERROR: unknown SF_VPN_PROVIDER '$PROVIDER' (openvpn|wireguard)"; exit 64 ;; esac ;;
esac
SF_KILLSWITCH=$(flag "${SF_KILLSWITCH:-1}")
SF_ALLOW_LAN=$(flag "${SF_ALLOW_LAN:-0}")
SF_DNS_LEAK_PROTECTION=$(flag "${SF_DNS_LEAK_PROTECTION:-1}")
SF_IPV6=$(flag "${SF_IPV6:-0}")
SF_ADB_PORT=${SF_ADB_PORT:-5555}; is_num "$SF_ADB_PORT" || SF_ADB_PORT=5555
case "${SF_ADB_FROM:-gateway}" in any) SF_ADB_FROM=any ;; *) SF_ADB_FROM=gateway ;; esac
SF_VPN_PING=${SF_VPN_PING:-10}; is_num "$SF_VPN_PING" || SF_VPN_PING=10
SF_VPN_PING_RESTART=${SF_VPN_PING_RESTART:-120}; is_num "$SF_VPN_PING_RESTART" || SF_VPN_PING_RESTART=120
SF_VPN_CONNECT_RETRY_MAX=${SF_VPN_CONNECT_RETRY_MAX:-5}; is_num "$SF_VPN_CONNECT_RETRY_MAX" || SF_VPN_CONNECT_RETRY_MAX=5
SF_GATEWAY_IP=${SF_GATEWAY_IP:-}
SF_UPLINK_GW=${SF_UPLINK_GW:-}; is_ipv4 "$SF_UPLINK_GW" || SF_UPLINK_GW=
PROFILE_NAME=$(printf '%s' "${SF_PROFILE_NAME:-}" | tr -cd 'A-Za-z0-9._-' | cut -c1-120)

DNS_LIST=
for _d in $(printf '%s' "${SF_DNS:-}" | tr ',' ' '); do
    if is_ipv4 "$_d"; then DNS_LIST="${DNS_LIST:+$DNS_LIST,}$_d"; else log "ignoring resolver '$_d': not an IPv4 address"; fi
done
SF_DNS=$DNS_LIST

case "$SF_MODE" in gateway|gwserver)
    is_ipv4 "$SF_GATEWAY_IP" || { log "ERROR: SF_MODE=$SF_MODE needs SF_GATEWAY_IP (an IPv4 address)"; exit 64; } ;;
esac

mkdir -p "$RUN" && chmod 0700 "$RUN"
umask 077
rm -f "$RUN"/tunnel.env "$RUN"/pins "$RUN"/uplink "$RUN"/routes.snap "$RUN"/status

# ---------------------------------------------------------------- status file
STARTED=$(now); STATE=starting; READY=0; SINCE=$STARTED; LAST_ERROR=
T_DEV=; T_IP=; KS_STATE=unknown; RESTARTS=0

write_status() {
    {
        echo "mode=$SF_MODE"
        echo "provider=$PROVIDER"
        echo "profile=$PROFILE_NAME"
        echo "state=$STATE"
        echo "ready=$READY"
        echo "since=$SINCE"
        echo "started=$STARTED"
        echo "tunnel_dev=$T_DEV"
        echo "tunnel_ip=$T_IP"
        echo "killswitch=$KS_STATE"
        echo "uplink=$WAN_IF"
        echo "gateway_if=$GW_IF"
        echo "restarts=$RESTARTS"
        echo "last_error=$LAST_ERROR"
    } >"$RUN/status.tmp" && mv -f "$RUN/status.tmp" "$RUN/status"
}

# set_state <state> <ready 0|1> [error text]; an empty error keeps the previous one, "-" clears it.
set_state() {
    _err=${3:-}
    [ "$_err" = "-" ] && { _err=; _clear=1; } || _clear=0
    _err=$(printf '%s' "$_err" | tr '\n\r' '  ' | cut -c1-300)
    _changed=0
    if [ "$1" != "$STATE" ]; then STATE=$1; SINCE=$(now); _changed=1; log "state: $STATE${_err:+ ($_err)}"; fi
    [ "$2" != "$READY" ] && { READY=$2; _changed=1; }
    if [ "$_clear" = 1 ] && [ -n "$LAST_ERROR" ]; then LAST_ERROR=; _changed=1; fi
    if [ -n "$_err" ] && [ "$_err" != "$LAST_ERROR" ]; then LAST_ERROR=$_err; _changed=1; fi
    [ "$_changed" = 1 ] && write_status
    return 0
}

# ---------------------------------------------------------------- interfaces
WAN_IF=; WAN_GW=; GW_IF=
case "$PROVIDER" in wireguard) TUN_MATCH=wg0 ;; *) TUN_MATCH=tun+ ;; esac

discover_wan() {
    # "default via 172.17.0.1 dev eth0"
    # shellcheck disable=SC2046
    set -- $(ip -4 route show default 2>/dev/null | head -n 1)
    while [ $# -gt 0 ]; do
        case "$1" in
            via) WAN_GW=${2:-} ;;
            dev) WAN_IF=${2:-} ;;
        esac
        shift
    done
    is_ipv4 "$WAN_GW" || WAN_GW=
    is_ifname "$WAN_IF" || WAN_IF=
}

# With two networks attached, which one Docker picked for the default route (and which one
# it called eth0) is not predictable. The uplink is therefore found by its bridge address.
discover_uplink() {
    [ -n "$SF_UPLINK_GW" ] || return 1
    _r=$(ip -4 -o route get "$SF_UPLINK_GW" 2>/dev/null | head -n 1)
    case "$_r" in ''|*" via "*) return 1 ;; esac
    _if=$(printf '%s\n' "$_r" | sed -n 's/.* dev \([^ ]*\).*/\1/p')
    is_ifname "$_if" && [ "$_if" != lo ] || return 1
    WAN_IF=$_if; WAN_GW=$SF_UPLINK_GW
    return 0
}

discover_gw_if() {
    GW_IF=
    case "$SF_MODE" in
    gateway)
        # On-link only: a route "via" something means the gateway network is not attached.
        _r=$(ip -4 -o route get "$SF_GATEWAY_IP" 2>/dev/null | head -n 1)
        case "$_r" in ''|*" via "*) return 1 ;; esac
        GW_IF=$(printf '%s\n' "$_r" | sed -n 's/.* dev \([^ ]*\).*/\1/p')
        ;;
    gwserver)
        GW_IF=$(ip -4 -o addr show 2>/dev/null | awk -v ip="$SF_GATEWAY_IP" '{ split($4, a, "/"); if (a[1] == ip) { print $2; exit } }')
        ;;
    esac
    is_ifname "$GW_IF" || { GW_IF=; return 1; }
    [ "$GW_IF" != lo ] || { GW_IF=; return 1; }
    return 0
}

rename_if() {  # rename_if <old> <new>; the IPv4 address survives, routes are put back afterwards
    [ "$1" = "$2" ] && return 0
    ip link set dev "$1" down && ip link set dev "$1" name "$2" && ip link set dev "$2" up
}

# Gives the two interfaces of a gateway member the names the rest of the system expects:
#   eth0   the uplink (published ADB port). Android configures "eth0" as its connection.
#   sfgw0  the gateway network. Android manages every interface called eth<N>, and would run
#          DHCP on a second one and strip its address. A name it does not know is left
#          alone, exactly like tun0 of a VPN. To Android a gateway looks like a tunnel.
# Called only after the firewall is loaded. While names change, rules that mention the old
# names match nothing, so the namespace is closed, not open.
normalise_ifaces() {
    [ -n "$GW_IF" ] || return 1
    if [ -n "$WAN_IF" ] && [ "$GW_IF" = "$WAN_IF" ]; then return 1; fi
    if [ "$GW_IF" != sfgw0 ]; then
        rename_if "$GW_IF" sfgw0 || return 1
        GW_IF=sfgw0
    fi
    if [ -n "$WAN_IF" ] && [ "$WAN_IF" != eth0 ]; then
        rename_if "$WAN_IF" eth0 || return 1
        WAN_IF=eth0
    fi
    # Docker may have put the default route on the gateway network, and a renamed interface
    # lost its routes when it went down.
    [ -n "$WAN_GW" ] && ip -4 route replace default via "$WAN_GW" dev "$WAN_IF" 2>/dev/null
    return 0
}

write_env() {
    # Only values validated above reach this file, which the helper scripts source.
    cat >"$RUN/env.tmp" <<EOF
SF_MODE='$SF_MODE'
SF_KILLSWITCH='$SF_KILLSWITCH'
SF_ALLOW_LAN='$SF_ALLOW_LAN'
SF_DNS='$SF_DNS'
SF_IPV6='$SF_IPV6'
SF_ADB_PORT='$SF_ADB_PORT'
SF_ADB_FROM='$SF_ADB_FROM'
WAN_IF='$WAN_IF'
WAN_GW='$WAN_GW'
GW_IF='$GW_IF'
TUN_MATCH='$TUN_MATCH'
EOF
    mv -f "$RUN/env.tmp" "$RUN/env"
}

killswitch_up() {
    if /sf/killswitch.sh init; then
        if [ "$SF_KILLSWITCH" = 1 ] || [ "$SF_MODE" = none ]; then KS_STATE=active; else KS_STATE=off; fi
        return 0
    fi
    KS_STATE=broken
    return 1
}

# ---------------------------------------------------------------- VPN servers
resolve() {
    if is_ipv4 "$1"; then echo "$1"; return 0; fi
    is_host "$1" || return 1
    for _d in $(printf '%s' "$SF_DNS" | tr ',' ' '); do
        _ips=$(dig +short +time=2 +tries=2 A "$1" "@$_d" 2>/dev/null | while read -r _ip; do is_ipv4 "$_ip" && echo "$_ip"; done)
        [ -n "$_ips" ] && { echo "$_ips"; return 0; }
    done
    return 1
}

# Steps 2 and 3 of the start order. Returns 1 when no server address is known afterwards.
pin_servers() {
    if [ "$PROVIDER" = wireguard ]; then _list=$(remotes wireguard "$PROFILE_WG"); else _list=$(remotes openvpn "$PROFILE_OVPN"); fi
    if [ -z "$_list" ]; then
        log "ERROR: the profile names no server (no 'remote' / 'Endpoint' line)"
        return 1
    fi
    _names=0
    for _h in $(printf '%s\n' "$_list" | awk '{ print $2 }' | sort -u); do is_ipv4 "$_h" || _names=1; done
    [ "$_names" = 1 ] && /sf/killswitch.sh dns open

    : >"$RUN/pins.new"; : >"$RUN/hosts.new"; _failed=
    printf '%s\n' "$_list" | sort -u >"$RUN/remotes"
    while read -r _proto _host _port; do
        case "$_proto" in tcp|udp) ;; *) continue ;; esac
        is_num "$_port" || continue
        if _addrs=$(resolve "$_host"); then
            for _ip in $_addrs; do
                echo "$_proto $_ip $_port $_host" >>"$RUN/pins.new"
                is_ipv4 "$_host" || echo "$_ip $_host" >>"$RUN/hosts.new"
            done
        else
            _failed="${_failed:+$_failed }$_host"
        fi
    done <"$RUN/remotes"

    # With leak protection the window closes whether or not the lookups worked. Without it
    # (old behaviour, weaker) lookups to the configured resolvers stay possible on the uplink.
    if [ "$SF_DNS_LEAK_PROTECTION" = 1 ]; then /sf/killswitch.sh dns close; else /sf/killswitch.sh dns open; fi

    [ -n "$_failed" ] && log "could not resolve: $_failed"
    if [ ! -s "$RUN/pins.new" ]; then
        # Keep the addresses of the previous round: an outage of the resolver must not
        # take a working pin set away.
        [ -s "$RUN/pins" ] && return 0
        return 1
    fi
    sort -u "$RUN/pins.new" >"$RUN/pins.tmp" && mv -f "$RUN/pins.tmp" "$RUN/pins"
    /sf/killswitch.sh pins "$RUN/pins" || return 1
    # The names now resolve from /etc/hosts, so the VPN client never needs DNS again.
    # Written in place because Docker bind-mounts the file.
    { grep -v ' # sf-pin$' /etc/hosts; sort -u "$RUN/hosts.new" | sed 's/$/ # sf-pin/'; } >"$RUN/hosts.tmp"
    cat "$RUN/hosts.tmp" >/etc/hosts
    /sf/routesync.sh once
    log "pinned $(wc -l <"$RUN/pins") VPN server address(es)"
    return 0
}

# ---------------------------------------------------------------- OpenVPN
CHILD=; NEXT_TRY=0; DELAY=5; EVER_UP=0; NEED_PIN=1; WG_UP=0; TICK=0

start_openvpn() {
    : >"$OVPN_LOG"
    rm -f "$RUN/tunnel.env"
    # --pull-filter ignore ping-restart + our own --ping/--ping-restart: servers push short
    #   restart timers that tore tunnels down under load; ours are tolerant and predictable.
    # --connect-retry 2 / --connect-retry-max: fail over quickly, then exit so the supervisor
    #   can resolve the names again.
    # --script-security 2 + --route-up: the only script that ever runs is our own hook.
    # --redirect-gateway def1: everything goes through the tunnel even if the profile forgot it.
    # --disable-dco: kernel offload changes the device type and timer behaviour from one host
    #   kernel to the next; the userspace tunnel behaves the same everywhere.
    # --dns-updown disable: OpenVPN would otherwise rewrite resolv.conf with pushed resolvers.
    set -- --config "$PROFILE_OVPN" \
        --auth-nocache \
        --script-security 2 --route-up "/sf/entrypoint.sh hook-route-up" \
        --pull-filter ignore "ping-restart" \
        --ping "$SF_VPN_PING" --ping-restart "$SF_VPN_PING_RESTART" \
        --connect-retry 2 --connect-retry-max "$SF_VPN_CONNECT_RETRY_MAX" \
        --redirect-gateway def1 \
        --disable-dco --dns-updown disable \
        --log-append "$OVPN_LOG" "$@"
    [ -f "$AUTH" ] && set -- "$@" --auth-user-pass "$AUTH"
    if [ "$SF_IPV6" != 1 ]; then
        set -- "$@" --pull-filter ignore "ifconfig-ipv6" --pull-filter ignore "route-ipv6"
    fi
    openvpn "$@" &
    CHILD=$!
    log "openvpn started (pid $CHILD)"
}

retry_later() {  # retry_later [seconds]; without an argument: 5, 10, 20, 40, 60, 60, ...
    if [ -n "${1:-}" ]; then NEXT_TRY=$(($(now) + $1)); return 0; fi
    NEXT_TRY=$(($(now) + DELAY))
    DELAY=$((DELAY * 2)); [ "$DELAY" -gt 60 ] && DELAY=60
    return 0
}

tick_openvpn() {
    if [ -n "$CHILD" ] && ! kill -0 "$CHILD" 2>/dev/null; then
        wait "$CHILD"; _code=$?; CHILD=
        RESTARTS=$((RESTARTS + 1)); NEED_PIN=1; T_DEV=; T_IP=
        rm -f "$RUN/tunnel.env"
        if grep -q 'AUTH_FAILED' "$OVPN_LOG" 2>/dev/null; then
            # Retrying wrong credentials every few seconds gets accounts locked.
            set_state auth_failed 0 "the VPN server rejected the username or password; set them again and restart this Android"
            retry_later 300
        else
            _last=$(grep -v '^$' "$OVPN_LOG" 2>/dev/null | tail -n 1 | cut -c1-200)
            if [ "$EVER_UP" = 1 ]; then _s=reconnecting; else _s=connecting; fi
            set_state "$_s" 0 "openvpn exited with code $_code: $_last"
            retry_later
        fi
        write_status
        return 0
    fi
    if [ -z "$CHILD" ]; then
        [ "$(now)" -ge "$NEXT_TRY" ] || return 0
        if grep -q '^[[:space:]]*auth-user-pass[[:space:]]*$' "$PROFILE_OVPN" && [ ! -f "$AUTH" ]; then
            # openvpn would ask on a terminal that does not exist and exit, over and over.
            set_state auth_failed 0 "this profile needs a username and password, and none is stored for it"
            retry_later 300
            return 0
        fi
        if [ "$NEED_PIN" = 1 ]; then
            if pin_servers; then NEED_PIN=0; else
                set_state error 0 "cannot find the VPN server: no address for the names in the profile (resolvers: ${SF_DNS:-none})"
                retry_later
                return 0
            fi
        fi
        if [ "$EVER_UP" = 1 ]; then set_state reconnecting 0; else set_state connecting 0; fi
        start_openvpn "$@"
        return 0
    fi
    _dev=$(kv dev "$RUN/tunnel.env")
    if [ -n "$_dev" ] && [ -e "/sys/class/net/$_dev" ]; then
        _ip=$(kv ip "$RUN/tunnel.env")
        if [ "$_dev" != "$T_DEV" ] || [ "$_ip" != "$T_IP" ]; then T_DEV=$_dev; T_IP=$_ip; write_status; fi
        EVER_UP=1
        set_state connected 1 -
        # A tunnel that has held for a minute earns a fast retry next time.
        [ $(($(now) - SINCE)) -gt 60 ] && DELAY=5
    elif [ "$STATE" = connected ]; then
        T_DEV=; T_IP=
        set_state reconnecting 0 "the tunnel interface went away"
    fi
    return 0
}

# ---------------------------------------------------------------- WireGuard
# Set up with plain `ip` and `wg` (no wg-quick): the same steps, minus everything in
# wg-quick that would run commands from the profile or rewrite DNS behind our back.
start_wireguard() {
    ip link del wg0 2>/dev/null
    if ! ip link add wg0 type wireguard 2>"$RUN/wg.err"; then
        set_state error 0 "this kernel has no WireGuard support ($(tr '\n' ' ' <"$RUN/wg.err")). On the host run: sudo modprobe wireguard  and add 'wireguard' to /etc/modules-load.d/ so it loads at boot"
        return 1
    fi
    # `wg setconf` only understands its own keys; Address/DNS/MTU belong to wg-quick.
    awk '
        { sub(/\r$/, "") }
        /^[ \t]*\[/ { print; next }
        /^[ \t]*(PrivateKey|ListenPort|PublicKey|PresharedKey|AllowedIPs|Endpoint|PersistentKeepalive)[ \t]*=/ { print }
    ' "$PROFILE_WG" >"$RUN/wg0.conf"
    if ! wg setconf wg0 "$RUN/wg0.conf" 2>"$RUN/wg.err"; then
        rm -f "$RUN/wg0.conf"
        set_state error 0 "WireGuard refused the profile: $(tr '\n' ' ' <"$RUN/wg.err")"
        ip link del wg0 2>/dev/null
        return 1
    fi
    rm -f "$RUN/wg0.conf"
    wg set wg0 fwmark "$WG_MARK"
    # Without a keepalive WireGuard stays silent until there is traffic, and "connected"
    # could not be told apart from "never tried".
    for _peer in $(wg show wg0 peers); do wg set wg0 peer "$_peer" persistent-keepalive 25; done
    _mtu=$(awk -F= '/^[ \t]*MTU[ \t]*=/ { v = $2; gsub(/[ \t\r]/, "", v); print v; exit }' "$PROFILE_WG")
    is_num "$_mtu" || _mtu=1420
    T_IP=
    for _a in $(awk -F= '/^[ \t]*Address[ \t]*=/ { v = $2; gsub(/[ \t\r]/, "", v); gsub(/,/, " ", v); print v }' "$PROFILE_WG"); do
        case "$_a" in
            *:*) [ "$SF_IPV6" = 1 ] && ip -6 addr add "$_a" dev wg0 2>/dev/null ;;
            *)   is_ipv4 "${_a%/*}" && ip -4 addr add "$_a" dev wg0 && T_IP=${_a%/*} ;;
        esac
    done
    ip link set dev wg0 mtu "$_mtu" up
    echo "wg0" >"$RUN/uplink"
    /sf/routesync.sh once
    T_DEV=wg0; WG_UP=1
    log "wireguard interface up"
    return 0
}

tick_wireguard() {
    if [ "$WG_UP" != 1 ]; then
        [ "$(now)" -ge "$NEXT_TRY" ] || return 0
        if [ "$NEED_PIN" = 1 ]; then
            if pin_servers; then NEED_PIN=0; else
                set_state error 0 "cannot find the VPN server: no address for the Endpoint in the profile (resolvers: ${SF_DNS:-none})"
                retry_later 30; return 0
            fi
        fi
        set_state connecting 0
        start_wireguard || retry_later 60
        return 0
    fi
    # Asking the kernel every pass would be wasted work once the tunnel is steady.
    [ "$STATE" = connected ] && [ $((TICK % 5)) -ne 0 ] && return 0
    _hs=$(wg show wg0 latest-handshakes 2>/dev/null | awk '{ if ($2 > m) m = $2 } END { print m + 0 }')
    if [ "$_hs" -gt 0 ] && [ $(($(now) - _hs)) -lt 180 ]; then
        [ "$STATE" = connected ] || EVER_UP=1
        set_state connected 1 -
    else
        if [ "$EVER_UP" = 1 ]; then _s=reconnecting; else _s=connecting; fi
        set_state "$_s" 0 "no WireGuard handshake with the server yet"
        # The server may have moved: look the name up again every two minutes while down.
        if [ $((TICK % 60)) -eq 59 ] && pin_servers; then
            _ep=$(awk '{ print $2 ":" $3; exit }' "$RUN/pins")
            _peer=$(wg show wg0 peers | head -n 1)
            [ -n "$_ep" ] && [ -n "$_peer" ] && wg set wg0 peer "$_peer" endpoint "$_ep"
        fi
    fi
    return 0
}

# ---------------------------------------------------------------- shared gateway member
GW_READY=0
# Returns 0 once the gateway network is attached, named and allowed by the firewall.
gateway_ifaces_ready() {
    [ "$GW_READY" = 1 ] && return 0
    [ -n "$WAN_IF" ] || discover_uplink
    discover_gw_if || return 1
    if ! normalise_ifaces; then
        set_state error 0 "the gateway is on the same network as the uplink; a gateway network must be a separate Docker network"
        return 1
    fi
    write_env
    killswitch_up || { set_state error 0 "the firewall could not be loaded"; return 1; }
    [ "$SF_MODE" = gateway ] && echo "$GW_IF $SF_GATEWAY_IP" >"$RUN/uplink"
    rm -f "$RUN/routes.snap"
    /sf/routesync.sh once
    write_status
    log "interfaces: uplink ${WAN_IF:-none}, gateway network $GW_IF"
    GW_READY=1
    return 0
}

tick_gateway() {
    if ! gateway_ifaces_ready; then
        # The gateway network may be attached a moment after the container starts.
        [ "$STATE" = error ] || set_state connecting 0 "waiting for the gateway network ($SF_GATEWAY_IP is not on a directly attached network)"
        return 0
    fi
    [ "$STATE" = connected ] && [ $((TICK % 5)) -ne 0 ] && return 0
    if ping -c 1 -W 1 "$SF_GATEWAY_IP" >/dev/null 2>&1; then
        [ "$STATE" = connected ] || EVER_UP=1
        T_DEV=$GW_IF
        set_state connected 1 -
    else
        if [ "$EVER_UP" = 1 ]; then _s=reconnecting; else _s=connecting; fi
        set_state "$_s" 0 "gateway $SF_GATEWAY_IP does not answer; is the gateway container running?"
    fi
    return 0
}

# ---------------------------------------------------------------- start
ROUTESYNC=; TAILER=
shutdown() {
    log "stopping"
    [ -n "$CHILD" ] && kill "$CHILD" 2>/dev/null
    [ -n "$ROUTESYNC" ] && kill "$ROUTESYNC" 2>/dev/null
    [ -n "$TAILER" ] && kill "$TAILER" 2>/dev/null
    wait 2>/dev/null
    exit 0
}
trap shutdown TERM INT

case "$SF_MODE" in
    gateway|gwserver) discover_uplink || discover_wan ;;
    *)                discover_wan ;;
esac
discover_gw_if
write_env
write_status
log "mode $SF_MODE${PROVIDER:+ ($PROVIDER)}, uplink ${WAN_IF:-none}${WAN_GW:+ via $WAN_GW}, kill switch $SF_KILLSWITCH, resolvers ${SF_DNS:-none}"

# 1. Firewall first. Nothing below runs with an open uplink.
if ! killswitch_up; then
    set_state error 0 "the firewall could not be loaded, so the network stays closed; see this container's log for the modules to load on the host"
    # Stay up (closed) so the reason can be read, instead of crash-looping.
    while :; do sleep 60 & wait $!; done
fi
write_status
/sf/routesync.sh once

case "$SF_MODE" in
vpn|gwserver)
    case "$PROVIDER" in
        openvpn)   [ -r "$PROFILE_OVPN" ] || { set_state error 0 "no OpenVPN profile mounted at $PROFILE_OVPN"; PROVIDER=missing; } ;;
        wireguard) [ -r "$PROFILE_WG" ] || { set_state error 0 "no WireGuard profile mounted at $PROFILE_WG"; PROVIDER=missing; } ;;
    esac
    if [ "$SF_MODE" = gwserver ] && ! gateway_ifaces_ready; then
        [ "$STATE" = error ] || set_state error 0 "gateway address $SF_GATEWAY_IP is not configured on any interface of this container"
        PROVIDER=missing
    fi
    if [ "$PROVIDER" = openvpn ]; then
        : >"$OVPN_LOG"
        tail -n 0 -F "$OVPN_LOG" 2>/dev/null &
        TAILER=$!
    fi
    ;;
direct) set_state direct 1 ;;
none)   set_state isolated 1 ;;
esac

# Steps 2-4 (pin the servers, close DNS, start the tunnel) happen inside the first pass of
# the loop below; step 5, the route sync, runs beside it from here on. It is started first
# only so that the tunnel never waits for it; before a tunnel exists it has nothing to do.
/sf/routesync.sh loop &
ROUTESYNC=$!

while :; do
    TICK=$((TICK + 1))
    case "$SF_MODE:$PROVIDER" in
        vpn:openvpn|gwserver:openvpn)     tick_openvpn "$@" ;;
        vpn:wireguard|gwserver:wireguard) tick_wireguard ;;
        gateway:*)                        tick_gateway ;;
    esac
    kill -0 "$ROUTESYNC" 2>/dev/null || { log "route sync stopped; starting it again"; /sf/routesync.sh loop & ROUTESYNC=$!; }
    if [ $((TICK % 15)) -eq 0 ]; then
        # Verify, do not assume: a rule set that silently went missing is put back.
        if ! /sf/killswitch.sh check; then
            log "ERROR: firewall rules are not in place; loading them again"
            if killswitch_up; then
                [ -s "$RUN/pins" ] && /sf/killswitch.sh pins "$RUN/pins"
                [ "$SF_DNS_LEAK_PROTECTION" = 1 ] || case "$SF_MODE" in vpn|gwserver) /sf/killswitch.sh dns open ;; esac
            fi
            write_status
        fi
        # Keep the log small; openvpn appends, so cutting it in place is safe.
        if [ -f "$OVPN_LOG" ] && [ "$(wc -c <"$OVPN_LOG")" -gt 524288 ]; then
            tail -n 200 "$OVPN_LOG" >"$RUN/openvpn.log.tmp" && cat "$RUN/openvpn.log.tmp" >"$OVPN_LOG"
            rm -f "$RUN/openvpn.log.tmp"
        fi
    fi
    : >"$RUN/heartbeat"
    sleep 2 &
    wait $!
done
