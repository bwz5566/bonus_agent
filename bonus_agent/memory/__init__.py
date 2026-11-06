"""Three durable memory layers. Values are references, never rule authority."""
import time
from bonus_agent.infra.storage import fingerprint


class Memory:
    def __init__(self, state):
        self.state = state

    def put(self, layer, owner_id, key, value, *, summary="", ttl=None):
        if layer not in {"working", "episodic", "long_term"}:
            raise ValueError("Unknown memory layer")
        id = fingerprint([layer, owner_id, key])
        item = {"id": id, "layer": layer, "owner_id": owner_id, "key": key,
                "value_ref": self.state.objects.put_json(value), "summary": str(summary)[:500],
                "expires": time.time() + ttl if ttl is not None else None}
        self.state.store.put("memory_items", self.state.scope, id, item)
        return item

    def items(self, layer, owner_id):
        return [r for r in self.state.store.list("memory_items", self.state.scope)
                if r["layer"] == layer and r["owner_id"] == owner_id
                and (r["expires"] is None or r["expires"] > time.time())]

    def get(self, layer, owner_id, key):
        for r in self.items(layer, owner_id):
            if r["key"] == key:
                return self.state.objects.get_json(r["value_ref"])
        return None

    def working(self, session_id):
        return MemoryLayer(self, "working", session_id)

    def episodic(self, case_id):
        return MemoryLayer(self, "episodic", case_id)

    def long_term(self, tenant_id):
        return MemoryLayer(self, "long_term", tenant_id)


class MemoryLayer:
    def __init__(self, memory, layer, owner):
        self.memory, self.layer, self.owner = memory, layer, owner

    def put(self, key, value, **kwargs):
        return self.memory.put(self.layer, self.owner, key, value, **kwargs)

    def get(self, key):
        return self.memory.get(self.layer, self.owner, key)
