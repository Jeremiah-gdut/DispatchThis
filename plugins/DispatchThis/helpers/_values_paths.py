"""Transient CFG-edge correlation and complete-value evidence."""

from __future__ import annotations

from collections import deque

from ._values_contracts import CompleteValues, PathSource, ValueCase
from ._values_identity import entity_key, same_entity


class _PathCorrelation:
    def __init__(self, phis):
        self.phis = phis

    def phi_jobs(self, mapping, selections):
        selected = self._selection(mapping, selections)
        if selected is not None:
            return ((selected[2], selections),)
        jobs = []
        for candidate in mapping.cases:
            if not self._compatible(mapping, candidate, selections):
                continue
            jobs.append(
                (
                    candidate[2],
                    self._add_selection(selections, mapping.join_id, candidate[0]),
                )
            )
        return tuple(jobs)

    def _selection(self, mapping, selections):
        for join_id, edge_key in selections:
            if join_id == mapping.join_id:
                return mapping.by_edge.get(edge_key)
        return None

    def _add_selection(self, selections, join_id, edge_key):
        return tuple(
            sorted((*selections, (join_id, edge_key)), key=lambda item: item[0])
        )

    def _compatible(self, mapping, candidate, selections):
        for join_id, edge_key in selections:
            existing = self._mapping(join_id).by_edge[edge_key]
            if join_id == mapping.join_id:
                return edge_key == candidate[0]
            existing_edge = existing[1]
            candidate_edge = candidate[1]
            if not (
                self._reachable(
                    getattr(existing_edge, "target", None),
                    getattr(candidate_edge, "source", None),
                )
                or self._reachable(
                    getattr(candidate_edge, "target", None),
                    getattr(existing_edge, "source", None),
                )
            ):
                return False
        return True

    def _mapping(self, join_id):
        for mapping in self.phis.values():
            if mapping.join_id == join_id:
                return mapping
        raise LookupError("missing PHI mapping")

    def _reachable(self, start, goal):
        if start is None or goal is None:
            return False
        if same_entity(start, goal):
            return True
        queue = deque((start,))
        seen = {entity_key(start)}
        while queue:
            block = queue.popleft()
            for edge in tuple(getattr(block, "outgoing_edges", ()) or ()):
                target = getattr(edge, "target", None)
                if target is None:
                    continue
                if same_entity(target, goal):
                    return True
                key = entity_key(target)
                if key not in seen:
                    seen.add(key)
                    queue.append(target)
        return False


def complete_values(states, phis, graph):
    """Deduplicate values while retaining every selected raw CFG-edge source."""

    mappings = {mapping.join_id: mapping for mapping in phis.values()}
    values = {}
    for value, selections in states:
        sources = values.setdefault(value, {})
        if selections in sources:
            continue
        edges = tuple(
            mappings[join_id].by_edge[edge_key][1] for join_id, edge_key in selections
        )
        sources[selections] = PathSource(edges)
    cases = tuple(
        ValueCase(value, tuple(source.values()))
        for value, source in sorted(values.items())
    )
    return CompleteValues(
        tuple(value for value, _sources in sorted(values.items())), cases, graph
    )
