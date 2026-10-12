"""Networking: VPN profiles, credentials (write-only), shared gateways, per-Android status."""
from __future__ import annotations

import re
from typing import Literal

from fastapi import APIRouter, Depends, Request
from pydantic import Field
from starlette.concurrency import run_in_threadpool

from ...config import GatewayConfig
from ...net import gateway as gateway_mod
from ...net import public_ip as net_public_ip
from ...net import status as net_status
from ...net import vpn
from ..audit import note
from ..auth import require
from ..common import Body, farm_of, instance_id
from ..uploads import receive_upload

router = APIRouter(dependencies=[Depends(require)])

MAX_PROFILE_BYTES = 1024 * 1024       # a VPN profile is a few kB of text
_CRED_FILE = re.compile(r"^vpn-([A-Za-z0-9_.\-]{1,64})\.auth$")


# ----------------------------------------------------------------------------- profiles

@router.get("/api/net/profiles")
def profiles(request: Request) -> dict:
    return {"profiles": vpn.list_profiles(farm_of(request).paths)}


@router.post("/api/net/profiles", status_code=201)
async def profile_upload(request: Request) -> dict:
    farm = farm_of(request)
    upload = await receive_upload(request, farm, max_bytes=MAX_PROFILE_BYTES, default_name="")
    try:
        data = await run_in_threadpool(upload.path.read_bytes)
        name = await run_in_threadpool(vpn.import_profile, farm.paths, upload.filename, data)
    finally:
        upload.discard()
    note(request, name=name)
    return {"name": name}


@router.delete("/api/net/profiles/{name}")
def profile_delete(name: str, request: Request) -> dict:
    vpn.delete_profile(farm_of(request).paths, name)
    note(request, name=name)
    return {"deleted": name}


# -------------------------------------------------------------------------- credentials

class Credentials(Body):
    username: str = Field(..., min_length=1, max_length=256)
    password: str = Field(..., min_length=1, max_length=1024)


@router.get("/api/net/credentials")
def credentials(request: Request) -> dict:
    """Names only. There is no way to read a stored username or password back."""
    paths = farm_of(request).paths
    names = []
    if paths.secrets.is_dir():
        for entry in sorted(paths.secrets.iterdir()):
            m = _CRED_FILE.match(entry.name)
            if m and vpn.has_credentials(paths, m.group(1)):
                names.append(m.group(1))
    return {"credentials": [{"name": n} for n in names]}


@router.put("/api/net/credentials/{name}")
def credentials_set(name: str, body: Credentials, request: Request) -> dict:
    vpn.set_credentials(farm_of(request).paths, name, body.username, body.password)
    note(request, name=name)
    return {"name": name, "stored": True}


@router.delete("/api/net/credentials/{name}")
def credentials_delete(name: str, request: Request) -> dict:
    if not vpn.delete_credentials(farm_of(request).paths, name):
        raise KeyError(f"no credentials named {name[:64]!r}")
    note(request, name=name)
    return {"deleted": name}


# ----------------------------------------------------------------------------- gateways

class Gateway(Body):
    provider: Literal["openvpn", "wireguard"] = "openvpn"
    profile: str = Field(..., min_length=1, max_length=200)
    credentials: str = Field("default", max_length=64)
    subnet: str = Field("", max_length=43)


def _gateway(request: Request, name: str) -> str:
    gateway_mod.check_name(name)
    if name not in farm_of(request).config.network.gateways:
        raise KeyError(f"no gateway named {name!r}")
    return name


@router.get("/api/net/gateways")
def gateways(request: Request) -> dict:
    farm = farm_of(request)
    out = []
    for name, cfg in sorted(farm.config.network.gateways.items()):
        try:
            live = gateway_mod.status(farm.docker, farm.config, name)
        except Exception as e:  # noqa: BLE001 - one gateway's probe must not hide the others
            live = {"name": name, "state": "unknown", "detail": f"{type(e).__name__}: {e}"[:200]}
        out.append({**live, "config": cfg.model_dump(mode="json")})
    return {"gateways": out}


@router.put("/api/net/gateways/{name}")
def gateway_put(name: str, body: Gateway, request: Request) -> dict:
    farm = farm_of(request)
    gateway_mod.check_name(name)
    if body.subnet:
        gateway_mod.check_subnet(body.subnet)
    vpn.load_profile(farm.paths, body.profile)        # 404 now rather than a gateway that cannot start
    entry = GatewayConfig(**body.model_dump())
    farm.set_config({"network": {"gateways": {name: entry.model_dump(mode="json")}}})
    note(request, name=name)
    return {"name": name, "config": entry.model_dump(mode="json"),
            "note": "saved; `up` (or starting an Android that uses it) applies it"}


@router.delete("/api/net/gateways/{name}")
def gateway_delete(name: str, request: Request, force: bool = False) -> dict:
    farm = farm_of(request)
    name = _gateway(request, name)
    note(request, name=name, force=force or None)
    users = []
    for i, spec in farm.store.list().items():
        mode = spec.network.mode or farm.config.network.mode
        chosen = spec.network.gateway if spec.network.gateway is not None else farm.config.network.gateway
        if mode == "gateway" and chosen == name:
            users.append(i)
    if users:
        raise ValueError(f"gateway {name!r} is still the network of Android(s) {', '.join(map(str, users[:10]))}; "
                         "change their network first")
    removed = gateway_mod.remove(farm.docker, farm.config, name, force=force)
    farm.set_config({"network": {"gateways": {name: None}}})       # null removes the entry (merge patch)
    return {"deleted": name, "removed": removed.get("removed", [])}


@router.post("/api/net/gateways/{name}/{action}")
def gateway_action(name: str, action: Literal["up", "down"], request: Request, force: bool = False) -> dict:
    farm = farm_of(request)
    name = _gateway(request, name)
    note(request, name=name, action=action, force=force or None)
    if action == "up":
        return gateway_mod.ensure(farm.docker, farm.config, farm.paths, name)
    return gateway_mod.remove(farm.docker, farm.config, name, force=force)


# ------------------------------------------------------------------- per-Android status

@router.get("/api/instances/{id}/net")
def instance_net(id: str, request: Request, refresh: bool = False) -> dict:
    """Mode, tunnel state, kill switch and the public address. `refresh` asks the address
    again from inside the Android's network (a few seconds) instead of showing the last known one."""
    farm = farm_of(request)
    r = farm.instances.resolved(instance_id(farm, id))
    out = net_status(farm.docker, r)
    if refresh:
        out["public_ip"] = net_public_ip(farm.docker, r, url=farm.config.network.vpn.public_ip_url)
    return out
