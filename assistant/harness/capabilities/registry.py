"""⑥ 层注册表：Action 名称到 Manifest 与 Handler 的唯一映射。"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Protocol

from harness.capabilities.models import CapabilityRequest, CapabilityResult, ActionManifest


class CapabilityRegistryError(ValueError):
    pass


class CapabilityHandler(Protocol):
    def execute(self, request: CapabilityRequest) -> CapabilityResult: ...


class CapabilityRegistry:
    def __init__(self, entries: Iterable[tuple[ActionManifest, CapabilityHandler]] = ()):
        self._manifests: dict[str, ActionManifest] = {}
        self._handlers: dict[str, CapabilityHandler] = {}
        for manifest, handler in entries:
            self.register(manifest, handler)

    def register(self, manifest: ActionManifest, handler: CapabilityHandler) -> None:
        if manifest.id in self._manifests:
            raise CapabilityRegistryError(f"重复 action id: {manifest.id}")
        self._manifests[manifest.id] = manifest
        self._handlers[manifest.id] = handler

    def get_manifest(self, action_id: str) -> ActionManifest:
        try:
            return self._manifests[action_id]
        except KeyError as exc:
            raise KeyError(action_id) from exc

    def get_handler(self, action_id: str) -> CapabilityHandler:
        try:
            return self._handlers[action_id]
        except KeyError as exc:
            raise KeyError(action_id) from exc

    def action_ids(self) -> frozenset[str]:
        return frozenset(self._manifests)

    def schemas_for(self, action_ids: Iterable[str]) -> list[dict]:
        schemas: list[dict] = []
        for action_id in action_ids:
            manifest = self.get_manifest(action_id)
            schemas.append({
                "name": manifest.id,
                "description": manifest.description or f"Registered action: {manifest.id}",
                "input_schema": dict(manifest.input_schema),
            })
        return schemas
