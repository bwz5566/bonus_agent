"""Measure resolution of validation problems, rather than successful tool responses."""
from dataclasses import dataclass, field


@dataclass
class ProgressWatch:
    previous: set[str] = field(default_factory=set)
    stagnant: int = 0

    def observe(self, blockers):
        current = set(blockers)
        if not current or not self.previous or self.previous - current:
            self.stagnant = 0
        else:
            self.stagnant += 1
        self.previous = current
        return self.stagnant
