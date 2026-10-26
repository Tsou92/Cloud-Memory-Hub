"""Directional closure over explicit, project-scoped stored provenance only."""

import re
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Literal

from pydantic import ConfigDict, Field, field_validator

from cloud_memory_hub.governance_journal import GovernanceError
from cloud_memory_hub.read_contract import ScopeRequest


class PreviewRequest(ScopeRequest):
    model_config = ConfigDict(revalidate_instances="always")
    action: Literal["forget", "purge"]
    record_ids: tuple[str, ...] = Field(min_length=1, max_length=100)
    offset: int = Field(default=0, strict=True, ge=0, le=20_000)
    limit: int = Field(default=20, strict=True, ge=1, le=50)
    expected_view: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @field_validator("record_ids")
    @classmethod
    def identifiers(cls, values):
        if len(set(values)) != len(values) or any(
            re.fullmatch(r"[A-Za-z0-9_:-]{1,200}", value) is None for value in values
        ):
            raise ValueError("invalid_selection")
        return tuple(sorted(values))


@dataclass(frozen=True)
class RelationNode:
    record_id: str
    kind: Literal["memory", "raw"]
    content: str = field(repr=False)
    layer: str
    status: str
    parents: frozenset[str] = frozenset()
    versions: frozenset[str] = frozenset()


def plan_relations(nodes: dict[str, RelationNode], selected: tuple[str, ...]) -> dict:
    if not selected or any(identity not in nodes for identity in selected):
        raise GovernanceError("governance_selection_not_found")
    versions, children = defaultdict(set), defaultdict(set)
    for identity, node in nodes.items():
        for version in node.versions:
            if version in nodes and nodes[version].kind != node.kind:
                raise GovernanceError("governance_version_kind_conflict")
            versions[identity].add(version)
            versions[version].add(identity)
        for parent in node.parents:
            children[parent].add(identity)
    reasons = defaultdict(set)
    roots = set()
    queue = deque((identity, "selected") for identity in selected)

    def check(identity):
        if identity not in nodes:
            # Do not resolve foreign content by ID or silently omit dangling links.
            raise GovernanceError("governance_reference_unresolved")

    while queue:
        identity, reason = queue.popleft()
        check(identity)
        reasons[identity].add(reason)
        if identity in roots:
            continue
        roots.add(identity)
        queue.extend((value, "evolution") for value in sorted(versions[identity]))
        queue.extend((value, "source") for value in sorted(nodes[identity].parents))
    # Descendants are tainted by a chosen source. Do not traverse their other
    # parents, which may contain independent user data outside the chosen roots.
    included = set()
    queue = deque(sorted(roots))
    while queue:
        identity = queue.popleft()
        check(identity)
        if identity in included:
            continue
        included.add(identity)
        for parent in nodes[identity].parents:
            check(parent)
        for reason, neighbors in (
            ("derived", children[identity]),
            ("evolution", versions[identity]),
        ):
            for neighbor in sorted(neighbors):
                check(neighbor)
                reasons[neighbor].add(reason)
                queue.append(neighbor)
    return {
        "record_ids": tuple(sorted(included)),
        "roots": tuple(sorted(roots)),
        "reasons": {identity: tuple(sorted(reasons[identity])) for identity in sorted(included)},
        "unresolved_origins": tuple(
            sorted(
                identity
                for identity in included
                if nodes[identity].kind == "memory" and not nodes[identity].parents
            )
        ),
    }
