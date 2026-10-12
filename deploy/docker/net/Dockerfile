# Super Farm network sidecar: owns one Android's network namespace.
#
# Everything is installed at build time. An earlier design installed packages at every
# container start, which was slow, needed unprotected internet before the VPN was up and
# broke whenever a mirror was unreachable.
ARG ALPINE_VERSION=3.24.2
FROM alpine:${ALPINE_VERSION}

# wireguard-tools-wg is the `wg` binary alone. The interface is set up with plain `ip`
# commands by the entrypoint, so wg-quick (and the bash + resolvconf it drags in) stays out.
# iptables here is the nftables-backed build: it needs only the kernel modules Docker itself
# needs, and Android's own legacy iptables calls in the shared namespace cannot touch it.
RUN apk add --no-cache \
        openvpn wireguard-tools-wg \
        iptables ip6tables iproute2 \
        curl bind-tools tini \
    && mkdir -p /sf /run/sf \
    && chmod 0700 /run/sf

# Permissions are set in a step of their own: the copy instruction's chmod flag needs
# BuildKit, and plenty of hosts still build with the classic builder.
COPY entrypoint.sh killswitch.sh routesync.sh healthcheck.sh /sf/
RUN chmod 0755 /sf/*.sh

# Changes whenever a file of the build context changes; the farm compares it with the
# checkout to decide whether the image is stale.
ARG SF_CONTEXT_HASH=dev
LABEL io.superfarm.role="net-image" \
      io.superfarm.context="${SF_CONTEXT_HASH}"

ENV SF_MODE=direct \
    SF_KILLSWITCH=1

# `unhealthy` means: tunnel not up, supervisor stalled, or the kill switch is not in place.
HEALTHCHECK --interval=30s --timeout=8s --start-period=30s --retries=2 CMD ["/sf/healthcheck.sh"]

# tini reaps the short-lived helpers (dig, ip, hook scripts) and forwards SIGTERM so a
# `docker stop` ends the tunnel cleanly instead of waiting for the kill timeout.
ENTRYPOINT ["/sbin/tini", "-g", "--", "/sf/entrypoint.sh"]
