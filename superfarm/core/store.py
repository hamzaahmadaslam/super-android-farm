"""Instance specs and their runtime state on disk.

    instances/<id>/instance.yaml   the spec (what the operator asked for)
    instances/<id>/state.json      what the farm decided or observed: chosen VPN profile,
                                   renderer last booted with, trim marker, last error
"""
from __future__ import annotations

import shutil
import threading
from typing import Any

from ..paths import Paths
from ..util.files import locked, read_json, read_yaml, write_json, write_yaml
from .models import MAX_INSTANCE_ID, InstanceSpec, check_id


class Store:
    def __init__(self, paths: Paths):
        self.paths = paths
        self._lock = threading.RLock()

    def _spec_file(self, instance_id: int):
        return self.paths.instance(instance_id) / "instance.yaml"

    def _state_file(self, instance_id: int):
        return self.paths.instance(instance_id) / "state.json"

    # ------------------------------------------------------------------- specs
    def ids(self) -> list[int]:
        if not self.paths.instances.is_dir():
            return []
        out = []
        for d in self.paths.instances.iterdir():
            if d.name.isdigit() and (d / "instance.yaml").is_file():
                out.append(int(d.name))
        return sorted(out)

    def list(self) -> dict[int, InstanceSpec]:
        out: dict[int, InstanceSpec] = {}
        for i in self.ids():
            try:
                out[i] = self.get(i)
            except (KeyError, ValueError):
                continue   # an unreadable spec must not hide the rest of the farm
        return out

    def exists(self, instance_id: int) -> bool:
        return self._spec_file(check_id(instance_id)).is_file()

    def get(self, instance_id: int) -> InstanceSpec:
        instance_id = check_id(instance_id)
        data = read_yaml(self._spec_file(instance_id))
        if data is None:
            raise KeyError(f"no instance {instance_id}")
        return InstanceSpec.model_validate({**data, "id": instance_id})

    def save(self, spec: InstanceSpec) -> InstanceSpec:
        with self._lock, locked(self._spec_file(spec.id)):
            write_yaml(self._spec_file(spec.id), spec.model_dump(mode="json", exclude_defaults=False))
        return spec

    def delete(self, instance_id: int) -> None:
        with self._lock:
            shutil.rmtree(self.paths.instance(check_id(instance_id)), ignore_errors=True)

    def next_id(self, start: int = 1) -> int:
        used = set(self.ids())
        for n in range(start, MAX_INSTANCE_ID + 1):
            if n not in used:
                return n
        raise RuntimeError("no free instance id")

    # ------------------------------------------------------------------- state
    def state(self, instance_id: int) -> dict:
        return read_json(self._state_file(check_id(instance_id)), {}) or {}

    def state_get(self, instance_id: int, key: str, default: Any = None) -> Any:
        return self.state(instance_id).get(key, default)

    def state_set(self, instance_id: int, key: str, value: Any) -> None:
        f = self._state_file(check_id(instance_id))
        with self._lock, locked(f):
            data = read_json(f, {}) or {}
            if value is None:
                data.pop(key, None)
            else:
                data[key] = value
            write_json(f, data)
