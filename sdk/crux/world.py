from __future__ import annotations

import os
import warnings

from .client import CruxClient
from .types import BeliefInvalidatedWarning, BeliefNode, InvalidationDiff


class WorldModel:
    def __init__(self, run_id: str | None = None, socket_path: str = "/tmp/crux.sock"):
        self._client = CruxClient(socket_path)
        self._client.connect()

        if run_id is None:
            run_id = os.urandom(8).hex()
        self.run_id = run_id
        self._step = 0
        # Track observed (belief_type, identity_key) for to_context enumeration
        self._observed_keys: list[tuple[str, dict]] = []

        try:
            resp = self._client.send({"type": "run_start", "run_id": self.run_id})
        except Exception:
            self._client.close()
            raise
        if "error" in resp:
            self._client.close()
            raise RuntimeError(f"run_start failed: {resp['error']}")

    def observe(
        self,
        action: str,
        inputs: dict,
        result: dict,
        status: str = "success",
        step: int | None = None,
    ) -> tuple[InvalidationDiff | None, str]:
        """Observe an action. Returns (diff_or_none, belief_id)."""
        if step is None:
            self._step += 1
            step = self._step
        else:
            self._step = step

        # Map action to belief_type and track identity key for to_context
        action_to_type = {
            "read_file": ("file", "path"),
            "http_get": ("http_endpoint", "url"),
            "shell": ("shell_output", "command"),
            "env_read": ("env_var", "key"),
        }
        if action in action_to_type:
            btype, key_field = action_to_type[action]
            filters = {key_field: inputs.get(key_field, "")}
            entry = (btype, filters)
            if entry not in self._observed_keys:
                self._observed_keys.append(entry)

        resp = self._client.send({
            "type": "observe",
            "action": action,
            "inputs": inputs,
            "result": result,
            "status": status,
            "step": step,
        })

        if "error" in resp:
            raise RuntimeError(f"observe failed: {resp['error']}")

        belief_id = resp.get("belief_id", "")
        diff = InvalidationDiff.from_dict(resp.get("diff"))
        if not diff.is_empty():
            warnings.warn(BeliefInvalidatedWarning(diff))
            return diff, belief_id
        return None, belief_id

    def query(self, belief_type: str, **filters) -> BeliefNode | None:
        resp = self._client.send({
            "type": "query",
            "belief_type": belief_type,
            "filters": filters,
        })
        if "error" in resp:
            raise RuntimeError(f"query failed: {resp['error']}")
        return BeliefNode.from_dict(resp.get("belief"))

    def diff(self) -> InvalidationDiff:
        resp = self._client.send({"type": "diff"})
        if "error" in resp:
            raise RuntimeError(f"diff failed: {resp['error']}")
        return InvalidationDiff.from_dict(resp.get("diff"))

    def snapshot(self) -> str:
        resp = self._client.send({"type": "snapshot"})
        if "error" in resp:
            raise RuntimeError(f"snapshot failed: {resp['error']}")
        return resp.get("snapshot_id", "")

    def branch(self) -> str:
        resp = self._client.send({"type": "branch"})
        if "error" in resp:
            raise RuntimeError(f"branch failed: {resp['error']}")
        return resp["branch_id"]

    def rollback(self, branch_id: str) -> None:
        resp = self._client.send({"type": "rollback", "branch_id": branch_id})
        if "error" in resp:
            raise RuntimeError(f"rollback failed: {resp['error']}")

    def add_edge(self, from_id: str, to_id: str, weight: float = 0.9, fields: list[str] | None = None) -> None:
        msg = {
            "type": "add_edge",
            "from": from_id,
            "to": to_id,
            "weight": weight,
        }
        if fields is not None:
            msg["fields"] = fields
        resp = self._client.send(msg)
        if "error" in resp:
            raise RuntimeError(f"add_edge failed: {resp['error']}")

    def at(self, step: int) -> HistoricalWorldModel:
        return HistoricalWorldModel(self._client, step)

    def to_context(self) -> str:
        """Serialize current world model state into a compact string for context injection."""
        sections: dict[str, list[str]] = {}
        pending_count = 0

        for btype, filters in self._observed_keys:
            belief = self.query(btype, **filters)
            if belief is None:
                continue

            section_name = {
                "file": "FILES",
                "http_endpoint": "HTTP",
                "env_var": "ENV",
                "shell_output": "SHELL",
            }.get(btype, btype.upper())

            if section_name not in sections:
                sections[section_name] = []

            identity = list(filters.values())[0]
            p = belief.payload

            # Type-specific compact summary
            if btype == "file":
                summary = p.get("hash", "?")[:8]
            elif btype == "http_endpoint":
                summary = f"{p.get('status_code', '?')}"
            elif btype == "env_var":
                summary = "set" if p.get("present") else "unset"
            elif btype == "shell_output":
                summary = f"exit:{p.get('exit_code', '?')}"
            else:
                summary = ""

            # Status flag — only annotate non-valid
            if belief.status == "uncertain":
                flag = f"  !! conf={belief.confidence:.2f}"
                pending_count += 1
            elif belief.status == "invalidated":
                flag = "  !! STALE"
                pending_count += 1
            else:
                flag = ""

            line = f"  {identity:<28s} {summary}{flag}"
            sections[section_name].append(line)

        lines = [f"WORLD MODEL [step {self._step}] [run:{self.run_id[:8]}]"]

        for section_name, entries in sections.items():
            lines.append(section_name)
            lines.extend(entries)

        if pending_count > 0:
            lines.append(f"!! {pending_count} belief(s) need re-observation")

        return "\n".join(lines)

    def close(self, reason: str = "complete") -> None:
        try:
            self._client.send({
                "type": "run_end",
                "run_id": self.run_id,
                "reason": reason,
            })
        except Exception:
            pass  # best-effort; socket cleanup below is what matters
        finally:
            self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class HistoricalWorldModel:
    """Read-only view of the world model at a specific step. Supports .query() only."""

    def __init__(self, client: CruxClient, step: int):
        self._client = client
        self._step = step

    def query(self, belief_type: str, **filters) -> BeliefNode | None:
        resp = self._client.send({
            "type": "query_at",
            "step": self._step,
            "belief_type": belief_type,
            "filters": filters,
        })
        if "error" in resp:
            raise RuntimeError(f"query_at failed: {resp['error']}")
        return BeliefNode.from_dict(resp.get("belief"))
