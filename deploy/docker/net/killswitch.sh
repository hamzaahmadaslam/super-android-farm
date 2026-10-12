#!/bin/sh
# Firewall of the sidecar's network namespace (which the Android shares).
#
#   killswitch.sh init                    load the complete default-deny rule set
#   killswitch.sh dns open|close          open/close name lookups on the uplink interface
#   killswitch.sh pins <file>             replace the set of allowed VPN servers in one step
#                                         (lines: <proto> <ip> <port> [name])
#   killswitch.sh check                   exit 0 when the rule set is in place
#   killswitch.sh print                   show the rule set `init` would load, without loading it
#
# Why it exists: with a plain default route next to the tunnel routes, traffic leaves with
# the real address whenever the tunnel is down (before the first connect, during every
# reconnect). So the default is DROP and it is loaded before anything else runs.
#
# Only matches that Docker itself depends on are used (interface, address, tcp/udp port,
# conntrack, MASQUERADE). On hosts where kernel module auto-loading is off, anything else
# (mark, owner, REJECT, LOG, TCPMSS) may simply not be there.
set -u

RUN=${SF_RUN:-/run/sf}
# Written by the entrypoint from validated values only; never from the VPN server or a profile.
# shellcheck disable=SC1091
. "$RUN/env"

log() { printf '[sf-net] killswitch: %s\n' "$*"; }

# Destinations that are "your LAN" rather than the internet. 169.254/16 is included because
# cloud metadata services live there; 100.64/10 because mesh VPNs on the host use it.
LAN_RANGES="10.0.0.0/8 172.16.0.0/12 192.168.0.0/16 169.254.0.0/16 100.64.0.0/10"

enforced() {
    # `none` means no outbound at all, whatever the kill switch setting says.
    [ "$SF_KILLSWITCH" = 1 ] || [ "$SF_MODE" = none ]
}

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

dns_servers() {
    _old=$IFS; IFS=,
    # shellcheck disable=SC2086
    set -- $SF_DNS
    IFS=$_old
    for _d in "$@"; do
        is_ipv4 "$_d" && printf '%s\n' "$_d"
    done
}

rules_v4() {
    echo "*filter"
    # INPUT is closed too. ADB has no authentication, and on a shared Docker bridge every
    # other container (including other Androids) could otherwise connect to port 5555 here.
    echo ":INPUT DROP [0:0]"
    echo ":FORWARD DROP [0:0]"
    echo ":OUTPUT DROP [0:0]"
    echo ":SF-PIN - [0:0]"
    echo ":SF-DNS - [0:0]"

    echo "-A INPUT -i lo -j ACCEPT"
    echo "-A INPUT -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT"
    if [ -n "$WAN_IF" ]; then
        if [ "$SF_ADB_FROM" = gateway ] && [ -n "$WAN_GW" ]; then
            # A port published on a loopback address reaches the container from the bridge
            # gateway address (userland proxy or hairpin NAT); nothing else may connect.
            echo "-A INPUT -i $WAN_IF -p tcp -s $WAN_GW --dport $SF_ADB_PORT -j ACCEPT"
        else
            echo "-A INPUT -i $WAN_IF -p tcp --dport $SF_ADB_PORT -j ACCEPT"
        fi
    fi

    echo "-A OUTPUT -o lo -j ACCEPT"
    if [ -n "$WAN_IF" ]; then
        # Answers of an ADB session only. This replaces the old "allow the whole bridge
        # subnet" rule, which also let an Android reach the host and its neighbours.
        echo "-A OUTPUT -o $WAN_IF -p tcp --sport $SF_ADB_PORT -m conntrack --ctstate ESTABLISHED -j ACCEPT"
    fi

    case "$SF_MODE" in
    direct)
        [ -n "$WAN_IF" ] || { echo "COMMIT"; return 0; }
        if [ "$SF_ALLOW_LAN" != 1 ]; then
            # A private resolver configured on purpose stays usable.
            for d in $(dns_servers); do
                echo "-A OUTPUT -o $WAN_IF -p udp -d $d --dport 53 -j ACCEPT"
                echo "-A OUTPUT -o $WAN_IF -p tcp -d $d --dport 53 -j ACCEPT"
            done
            for r in $LAN_RANGES; do
                echo "-A OUTPUT -o $WAN_IF -d $r -j DROP"
            done
        fi
        echo "-A OUTPUT -o $WAN_IF -j ACCEPT"
        ;;
    none)
        ;;
    vpn|gwserver)
        echo "-A OUTPUT -o $TUN_MATCH -j ACCEPT"
        if [ -n "$WAN_IF" ]; then
            echo "-A OUTPUT -o $WAN_IF -j SF-PIN"
            echo "-A OUTPUT -o $WAN_IF -j SF-DNS"
            if [ "$SF_ALLOW_LAN" = 1 ] && [ "$SF_MODE" = vpn ]; then
                for r in $LAN_RANGES; do
                    echo "-A OUTPUT -o $WAN_IF -d $r -j ACCEPT"
                done
            fi
        fi
        if [ "$SF_MODE" = gwserver ] && [ -n "$GW_IF" ]; then
            echo "-A INPUT -i $GW_IF -p icmp -j ACCEPT"
            echo "-A OUTPUT -o $GW_IF -j ACCEPT"
            # Members may leave through the tunnel and nowhere else. This holds even with
            # the kill switch setting off: a VPN gateway that forwards around its VPN would
            # be a leak by definition.
            echo "-A FORWARD -i $GW_IF -o $TUN_MATCH -j ACCEPT"
            echo "-A FORWARD -i $TUN_MATCH -o $GW_IF -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT"
        fi
        enforced || echo "-A OUTPUT -j ACCEPT"
        ;;
    gateway)
        # The gateway network has no address on the host and no NAT: the only router on it
        # is the gateway container, so allowing that interface allows "via the gateway".
        [ -n "$GW_IF" ] && echo "-A OUTPUT -o $GW_IF -j ACCEPT"
        enforced || echo "-A OUTPUT -j ACCEPT"
        ;;
    esac
    echo "COMMIT"
}

rules_v6() {
    echo "*filter"
    echo ":INPUT DROP [0:0]"
    echo ":FORWARD DROP [0:0]"
    echo ":OUTPUT DROP [0:0]"
    echo "-A INPUT -i lo -j ACCEPT"
    echo "-A OUTPUT -o lo -j ACCEPT"
    if [ "$SF_IPV6" = 1 ]; then
        echo "-A INPUT -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT"
        case "$SF_MODE:${WAN_IF:+wan}" in
        direct:wan)
            # Neighbour discovery and router advertisements, or IPv6 never comes up.
            echo "-A INPUT -i $WAN_IF -p ipv6-icmp -j ACCEPT"
            echo "-A OUTPUT -o $WAN_IF -j ACCEPT"
            ;;
        vpn:*)
            # IPv6 only inside the tunnel; the uplink stays closed for it.
            echo "-A OUTPUT -o $TUN_MATCH -j ACCEPT"
            enforced || echo "-A OUTPUT -j ACCEPT"
            ;;
        esac
    fi
    echo "COMMIT"
}

ipv6_absent() {
    # Only a kernel without IPv6 counts. The disable_ipv6 switch does not: Android can turn it
    # back on (see init), and "no addresses yet" cannot be told from procfs, whose files
    # always report size 0.
    [ ! -e /proc/net/if_inet6 ]
}

init() {
    # One atomic load per address family: there is no moment with half a rule set.
    if ! rules_v4 | iptables-restore 2>"$RUN/iptables.err"; then
        log "ERROR: cannot load IPv4 rules: $(tr '\n' ' ' <"$RUN/iptables.err")"
        log "On the host, load the firewall modules by name and start this Android again:"
        log "  sudo modprobe -a nf_tables nft_compat xt_conntrack xt_tcpudp xt_MASQUERADE"
        # Fail closed: still try to shut the door before giving up.
        iptables -P OUTPUT DROP 2>/dev/null
        iptables -P FORWARD DROP 2>/dev/null
        return 1
    fi
    # IPv6 is filtered even when it is switched off by sysctl: Android's network daemon can
    # write the per-interface disable_ipv6 switch of this namespace and turn it back on.
    if ! rules_v6 | ip6tables-restore 2>"$RUN/ip6tables.err"; then
        if ipv6_absent; then
            log "IPv6 rules not loaded ($(tr '\n' ' ' <"$RUN/ip6tables.err")); this kernel has no IPv6, continuing"
        else
            log "ERROR: cannot load IPv6 rules and this kernel has IPv6: $(tr '\n' ' ' <"$RUN/ip6tables.err")"
            log "Fix on the host: sudo modprobe -a nf_tables nft_compat   (or boot the host with ipv6.disable=1)"
            return 1
        fi
    fi
    if [ "$SF_MODE" = gwserver ]; then
        # Appended, not restored: Docker keeps its own chains in this namespace's nat table
        # (embedded DNS) and a table restore would wipe them.
        iptables -t nat -C POSTROUTING -o "$TUN_MATCH" -j MASQUERADE 2>/dev/null \
            || iptables -t nat -A POSTROUTING -o "$TUN_MATCH" -j MASQUERADE \
            || { log "ERROR: cannot add the NAT rule (xt_MASQUERADE missing on the host?)"; return 1; }
    fi
    if enforced; then log "active (mode $SF_MODE, uplink ${WAN_IF:-none})"; else log "OFF by configuration; inbound guard only"; fi
    return 0
}

dns() {
    [ -n "$WAN_IF" ] || return 0
    iptables -F SF-DNS || return 1
    [ "$1" = open ] || return 0
    for d in $(dns_servers); do
        iptables -A SF-DNS -p udp -d "$d" --dport 53 -j ACCEPT || return 1
        iptables -A SF-DNS -p tcp -d "$d" --dport 53 -j ACCEPT || return 1
    done
}

pins() {
    # One transaction: the old set is replaced by the new one with no gap in between.
    # A user chain named in a --noflush restore is emptied before its new rules are added.
    {
        echo "*filter"
        echo ":SF-PIN - [0:0]"
        while read -r proto ip port _name; do
            case "$proto" in tcp|udp) ;; *) continue ;; esac
            is_ipv4 "$ip" || continue
            case "$port" in ''|*[!0-9]*) continue ;; esac
            echo "-A SF-PIN -p $proto -d $ip --dport $port -j ACCEPT"
        done <"$1"
        echo "COMMIT"
    } | iptables-restore --noflush
}

check() {
    # The first line of `iptables -S <chain>` is the chain policy.
    out=$(iptables -S OUTPUT 2>/dev/null | head -n 1)
    fwd=$(iptables -S FORWARD 2>/dev/null | head -n 1)
    [ "$out" = "-P OUTPUT DROP" ] && [ "$fwd" = "-P FORWARD DROP" ] || return 1
    if enforced && iptables -S OUTPUT 2>/dev/null | grep -qx -- '-A OUTPUT -j ACCEPT'; then
        return 1
    fi
    if ! ipv6_absent; then
        [ "$(ip6tables -S OUTPUT 2>/dev/null | head -n 1)" = "-P OUTPUT DROP" ] || return 1
    fi
    return 0
}

case "${1:-}" in
    init)  init ;;
    dns)   dns "${2:-close}" ;;
    pins)  [ $# -eq 2 ] && [ -r "$2" ] || { log "usage: pins <file>"; exit 2; }; pins "$2" ;;
    check) check ;;
    print) echo "# IPv4"; rules_v4; echo "# IPv6"; rules_v6 ;;
    *)     echo "usage: killswitch.sh init | dns open|close | pins <file> | check | print" >&2; exit 2 ;;
esac
