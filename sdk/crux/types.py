from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class UncertainBelief:
    id: str
    was: float
    now: float


@dataclass
class InvalidationDiff:
    invalidated: list[str] = field(default_factory=list)
    uncertain: list[UncertainBelief] = field(default_factory=list)
    action_required: str = ""

    @classmethod
    def from_dict(cls, d: dict | None) -> InvalidationDiff:
        if not d:
            return cls()
        return cls(
            invalidated=d.get("invalidated", []),
            uncertain=[
                UncertainBelief(id=u["id"], was=u["was"], now=u["now"])
                for u in d.get("uncertain", [])
            ],
            action_required=d.get("action_required", ""),
        )

    def is_empty(self) -> bool:
        return not self.invalidated and not self.uncertain


@dataclass
class BeliefNode:
    id: str
    belief_type: str
    payload: dict
    confidence: float
    source_obs: str
    status: str
    created_at: int
    updated_at: int

    @classmethod
    def from_dict(cls, d: dict | None) -> BeliefNode | None:
        if not d:
            return None
        return cls(
            id=d["id"],
            belief_type=d["belief_type"],
            payload=d["payload"],
            confidence=d["confidence"],
            source_obs=d["source_obs"],
            status=d["status"],
            created_at=d["created_at"],
            updated_at=d["updated_at"],
        )


class BeliefInvalidatedWarning(UserWarning):
    """Raised when an observation triggers invalidation of dependent beliefs."""

    def __init__(self, diff: InvalidationDiff):
        self.diff = diff
        super().__init__(diff.action_required)
