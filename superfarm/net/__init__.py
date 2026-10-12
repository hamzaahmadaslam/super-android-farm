"""Per-device networking: the sidecar container, VPN tunnels, shared gateways, kill switch.

    sidecar     image + `docker run` arguments of `<prefix>-net-<id>`
    vpn         profiles (sanitised), credentials (write-only), profile selection
    gateway     shared VPN gateways and their internal networks
    killswitch  firewall settings and verification of the live rules
    status      what a sidecar is doing; readiness; public address

The functions the instance manager needs are re-exported here. Note that this makes
`superfarm.net.status` the function; reach the module with
`importlib.import_module("superfarm.net.status")` or `from superfarm.net.status import ...`.
"""
from .sidecar import ensure_image, sidecar_run_args
from .status import public_ip, status, wait_ready

__all__ = ["ensure_image", "sidecar_run_args", "wait_ready", "status", "public_ip"]
