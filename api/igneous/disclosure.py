"""What an answer handed to an outside model may carry: the MCP server's tool results, and the research agent's.

A tool's result reaches the model its client runs, an outbound path for the user's records like a hosted model's
prompt (CO-1), so it follows the rule every prompt follows (igneous/llm.py):

- text (a record's title, body, excerpt, snippet, a quoted span, a model's claim read from it) only from records in
  the sources opted in to hosted models (`llm.allowed_sources`);
- a node's name only when a prompt may name it (`llm.nameable`), under the name that rule gives; a node's key (an
  address, a handle, a path) never;
- a count for everything else: how many records (by source) and how many nodes (by type) an answer left out, never
  which.

Ids, types, categories, dates and counts are the graph's structure, not text, and pass. A phone number inside an
opted-in record is that record's text and passes as it is; no contact's name is put in its place, since the name may
come from a source that isn't opted in. Callers pass `named={}` to the readers they share with the explorer for that
reason.
"""
from collections import Counter
from collections.abc import Callable, Iterable
from operator import itemgetter

from sqlalchemy.orm import Session

from igneous import llm


class Disclosure:
    """The rule for one answer, which also counts what the answer leaves out (`report`)."""

    def __init__(self, s: Session, sources: frozenset[str] | None = None):
        self.s = s
        self.sources = llm.allowed_sources() if sources is None else frozenset(sources)
        self._names: dict[int, str | None] = {}
        self._records: Counter[str] = Counter()
        self._nodes: Counter[str] = Counter()

    def readable(self, source: str | None) -> bool:
        """Whether text from a record of `source` may be disclosed."""
        return source is not None and source in self.sources

    def names(self, ids: Iterable[int | None]) -> dict[int, str]:
        """{node: the name it may be disclosed under}, for the nodes among `ids` that may be named."""
        wanted = {int(i) for i in ids if i is not None}
        todo = [i for i in wanted if i not in self._names]
        if todo:
            named = llm.nameable(self.s, todo, self.sources) if self.sources else {}
            self._names.update({i: named.get(i) for i in todo})
        return {i: name for i in wanted if (name := self._names[i]) is not None}

    def name(self, node_id: int | None) -> str | None:
        """The name `node_id` may be disclosed under, or None."""
        return self.names([node_id]).get(node_id) if node_id is not None else None

    def withhold_records(self, source: str | None, n: int = 1) -> None:
        """Count `n` records of `source` an answer leaves out."""
        if n > 0:
            self._records[source or "unknown"] += n

    def withhold_nodes(self, node_type: str | None, n: int = 1) -> None:
        """Count `n` nodes of `node_type` an answer leaves out."""
        if n > 0:
            self._nodes[node_type or "node"] += n

    def records(self, rows: Iterable[dict], source: Callable[[dict], str | None] = itemgetter("source")) -> list[dict]:
        """The rows (records, or anything read from one record) whose `source(row)` is readable; the rest counted."""
        kept = []
        for row in rows:
            if self.readable(source(row)):
                kept.append(row)
            else:
                self.withhold_records(source(row))
        return kept

    def nodes(self, rows: Iterable[dict], key: str = "node_id", kind: str = "type") -> list[dict]:
        """The rows whose node (`key`) may be named, each with `name` set to that name; the rest counted by `kind`."""
        rows = list(rows)
        named = self.names(row[key] for row in rows)
        kept = []
        for row in rows:
            if (name := named.get(row[key])) is None:
                self.withhold_nodes(row.get(kind))
            else:
                kept.append({**row, "name": name})
        return kept

    def report(self, answer: dict) -> dict:
        """`answer`, with `withheld` saying how many records (by source) and nodes (by type) it left out, if any."""
        withheld = {}
        if self._records:
            withheld["records"] = dict(sorted(self._records.items()))
        if self._nodes:
            withheld["nodes"] = dict(sorted(self._nodes.items()))
        if withheld:
            withheld["why"] = ("Only records in the sources opted in to hosted models (IGNEOUS_LLM_SOURCES) and the "
                               "people and things they name are shared with this client; these are counts of the rest.")
            answer["withheld"] = withheld
        return answer
