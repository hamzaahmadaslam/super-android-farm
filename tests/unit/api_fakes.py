"""A Farm wired to fakes, and a client for it, shared by the API unit tests.

The instance manager is the real one on core_fakes' Docker and ADB stand-ins, so the API is
exercised against the modules' real behaviour without a Docker daemon or a device.
"""
from __future__ import annotations

from contextlib import contextmanager

from core_fakes import Farm as CoreFarm
from starlette.testclient import TestClient

from superfarm.api.app import create_app
from superfarm.api.auth import read_token
from superfarm.config import save_config
from superfarm.farm import Farm

BASE = "http://127.0.0.1"
WS = "ws://127.0.0.1"          # the test client names its host `testserver` for sockets unless told otherwise


def make_farm(tmp_path, config: dict | None = None) -> tuple[Farm, CoreFarm]:
    core = CoreFarm(tmp_path, config)
    save_config(core.config, core.paths)
    farm = Farm(core.paths.home)
    assert farm.config == core.config
    core.mgr.reload_config(farm.config)         # one config object, as in a farm that built its own manager
    for name, component in (("docker", core.docker), ("store", core.store), ("tasks", core.tasks),
                            ("instances", core.mgr)):
        farm.use(name, component)
    return farm, core


class Api:
    """What a test needs: the farm, the fakes behind it, and clients with and without a token."""

    def __init__(self, tmp_path, config: dict | None = None, *, web_dir=None):
        self.farm, self.core = make_farm(tmp_path, config)
        self.app = create_app(self.farm, web_dir=web_dir if web_dir is not None else tmp_path / "no-panel")
        self.token = read_token(self.farm.paths)
        self.anonymous = TestClient(self.app, base_url=BASE, raise_server_exceptions=False)
        self.client = TestClient(self.app, base_url=BASE, raise_server_exceptions=False,
                                 headers={"Authorization": f"Bearer {self.token}"})

    def wait(self, response, timeout: float = 10.0) -> dict:
        """Follow a 202 to the finished task."""
        assert response.status_code == 202, response.text
        task = self.farm.tasks.get(response.json()["task"]["id"])
        self.farm.tasks.wait(task, timeout)
        assert not task.active, "task did not finish"
        return task.to_dict()

    def audit_lines(self) -> list[dict]:
        import json
        path = self.farm.paths.logs / "audit.log"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def close(self) -> None:
        self.app.state.audit.close()
        self.farm.close()


@contextmanager
def api(tmp_path, config: dict | None = None, **kw):
    a = Api(tmp_path, config, **kw)
    try:
        yield a
    finally:
        a.close()
