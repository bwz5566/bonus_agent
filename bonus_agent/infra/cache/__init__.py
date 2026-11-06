"""Disposable disk cache; cache hits never grant authority or scope."""
from dataclasses import asdict, dataclass
from pathlib import Path
import json

from bonus_agent.infra.storage import atomic_write, canonical, fingerprint


@dataclass(frozen=True)
class CacheKey:
    scenario: str
    rule_package_version: str
    input_fingerprint: str
    engine_version: str
    code_version: str
    numeric_profile: str
    plan_fingerprint: str = ""
    reference_fingerprint: str = ""
    decision_fingerprint: str = ""
    semantic_version: str = ""

    @property
    def digest(self):
        return fingerprint(asdict(self))


class ResultCache:
    def __init__(self, root: str | Path):
        self.root = Path(root)

    def _path(self, key, scope):
        return self.root / fingerprint(scope) / (key.digest + ".json")

    def put(self, key: CacheKey, scope: str, result):
        if not scope:
            raise ValueError("Scope required")
        atomic_write(self._path(key, scope), canonical({"key": asdict(key), "scope": scope,
                                                      "result": result, "checksum": fingerprint(result)}))

    def get(self, key: CacheKey, scope: str, *, authorized: bool):
        if not authorized or not scope:
            return None
        try:
            entry = json.loads(self._path(key, scope).read_bytes())
            if entry["scope"] == scope and entry["key"] == asdict(key) and fingerprint(entry["result"]) == entry["checksum"]:
                return entry["result"]
        except (OSError, ValueError, KeyError, TypeError):
            pass
        return None
