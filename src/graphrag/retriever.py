"""
Graph Retriever - Graph-based traversal and relationship discovery

Provides intelligent schema navigation using the graph structure
of foreign key relationships and semantic similarity.
"""

import contextlib
import logging
from collections import defaultdict, deque
from collections.abc import Callable
from itertools import pairwise
from typing import Any

import networkx as nx

from .identity import Resolution, choose, display_name, qualified

logger = logging.getLogger(__name__)


class GraphRetriever:
    """Graph-based retrieval for schema navigation."""

    def __init__(self) -> None:
        """Initialize the graph retriever."""
        self.graph = nx.DiGraph()
        self._tables_info: dict[str, dict[str, Any]] = {}
        # Which tables each schema reported, kept apart from _tables_info.
        # Nodes are keyed by bare name, so a table of the same name in a second
        # schema overwrites the first one's record and the fact that the first
        # schema also holds it would be lost -- which is how a rediscovery of
        # one schema came to delete another's table. Membership is recorded per
        # schema so that question can still be answered.
        self._schema_tables: dict[str | None, set[str]] = {}
        # Bare table name -> the identities that carry it. Resolution is on the
        # path of every join lookup, and walking all the nodes to find what a
        # name could mean cost three times the lookup itself at 60 tables.
        self._by_name: dict[str, set[str]] = {}
        # Bumped by every method that changes the graph, so the undirected
        # snapshot below can tell whether it is still current.
        self._generation = 0
        self._undirected: tuple[int, nx.Graph] | None = None

    def tables_of_schema(self, schema_name: str | None) -> set[str]:
        """Which tables *schema_name* reported the last time it was discovered.

        Args:
            schema_name: The schema to ask about.

        Returns:
            The table names it reported.
        """
        return set(self._schema_tables.get(schema_name, set()))

    def tables_claimed_elsewhere(
        self, schema_name: str | None, table_names: set[str]
    ) -> set[str]:
        """Which of *table_names* another schema also reported.

        A node is one bare name shared by every schema that has a table of that
        name, so removing it on one schema's behalf would take the others' with
        it.

        Args:
            schema_name: The schema asking.
            table_names: Names it no longer reports.

        Returns:
            The subset another schema still claims.
        """
        claimed: set[str] = set()
        for other, tables in self._schema_tables.items():
            if other == schema_name:
                continue
            claimed |= table_names & tables
        return claimed

    def remove_tables(self, table_names: set[str] | list[str]) -> int:
        """Remove tables, and every relationship that touched them.

        A table dropped from the database kept its node and its edges, so join
        paths were still offered through a table SQL can no longer name.

        Args:
            table_names: Tables to remove.

        Returns:
            How many nodes were removed.
        """
        removed = 0
        for name in table_names:
            self._tables_info.pop(name, None)
            for tables in self._schema_tables.values():
                tables.discard(name)
            bare = display_name(name)
            if bare in self._by_name:
                self._by_name[bare].discard(name)
                if not self._by_name[bare]:
                    del self._by_name[bare]
            if name in self.graph:
                self.graph.remove_node(name)  # takes its edges with it
                removed += 1
        if removed:
            self._graph_changed()
            logger.info(f"Removed {removed} dropped table(s) from the graph")
        return removed

    def resolve_name(self, name: str, current_schema: str | None = None) -> Resolution:
        """Which table a caller means, given a bare or qualified name.

        Args:
            name: The name as it was written.
            current_schema: The session's schema, used to break a tie.

        Returns:
            The resolution, which reports ambiguity rather than guessing.
        """
        return choose(
            name,
            name in self.graph,
            list(self._by_name.get(name, ())),
            current_schema,
        )

    def identity_for(self, name: str, current_schema: str | None = None) -> str:
        """The node *name* refers to, or *name* itself when nothing matches.

        Returning the name unchanged keeps every caller's "not in the graph"
        branch working exactly as it did.

        Args:
            name: The name as it was written.
            current_schema: The session's schema, used to break a tie.

        Returns:
            An identity in the graph, or the name as given.
        """
        return self.resolve_name(name, current_schema).identity or name

    def _add_node(self, table: dict[str, Any]) -> str:
        """Add or update one table's node, keyed by its identity.

        Args:
            table: The table's metadata, as discovery reported it.

        Returns:
            The identity the node is keyed by.
        """
        schema = table.get("schema")
        identity = qualified(schema, table["name"])
        self._tables_info[identity] = table
        self._by_name.setdefault(table["name"], set()).add(identity)
        self.graph.add_node(
            identity,
            node_type="table",
            # Kept on the node so every reader -- join paths, community
            # summaries, visualization -- can show the name a person asked
            # about while the key underneath stays unambiguous.
            table=table["name"],
            schema=schema,
            column_count=len(table.get("columns", [])),
            has_comment=bool(table.get("comment")),
            comment=table.get("comment", ""),
        )
        return identity

    def _fk_target(self, fk: dict[str, Any], from_schema: str | None) -> str | None:
        """Which node a foreign key points at.

        A constraint names its target by bare name, sometimes with a schema of
        its own. Resolved explicitly rather than by assuming the bare name is a
        node: within the referencing table's schema first, which is what a
        database means by an unqualified reference, then a unique match
        anywhere, and nothing at all when two schemas both have that name.

        Args:
            fk: The foreign key as discovery reported it.
            from_schema: Schema of the table the key belongs to.

        Returns:
            The target's identity, or None if it is not in the graph or the
            name is ambiguous.
        """
        target = fk.get("referenced_table")
        if not target:
            return None
        schema = fk.get("referenced_schema") or from_schema
        within = qualified(schema, target)
        if within in self.graph:
            return within
        found = self.resolve_name(target, current_schema=schema)
        if found.ambiguous:
            logger.debug(
                f"Foreign key to '{target}' is ambiguous "
                f"({', '.join(found.candidates)}); leaving it unresolved"
            )
        return found.identity

    def _add_foreign_keys(self, table: dict[str, Any]) -> int:
        """Add the edges for one table's foreign keys.

        Args:
            table: The table's metadata.

        Returns:
            How many edges were new.
        """
        schema = table.get("schema")
        source = qualified(schema, table["name"])
        added = 0
        for fk in table.get("foreign_keys", []):
            target = self._fk_target(fk, schema)
            if target is None:
                continue
            if not self.graph.has_edge(source, target):
                added += 1
            self.graph.add_edge(
                source,
                target,
                edge_type="foreign_key",
                column=fk["column"],
                referenced_column=fk["referenced_column"],
            )
        return added

    def _graph_changed(self) -> None:
        """Record that the graph is no longer what the snapshot was taken from."""
        self._generation += 1
        self._undirected = None

    def _undirected_snapshot(self) -> nx.Graph:
        """An undirected copy of the graph, reused until the graph changes.

        Join lookups need an undirected reading, because a path may cross a
        foreign key against its direction. Converting copies every node and
        edge, which was paid per lookup: roughly 1ms on 400 tables and 2.6ms on
        1,000, against 0.03ms and 0.06ms for reusing one.

        A copy rather than ``to_undirected(as_view=True)``: a view reflects
        later changes to the graph it was taken from, which is the opposite of
        what a snapshot keyed to a generation means. Callers read it and must
        not modify it.

        Returns:
            The undirected form of the current graph.
        """
        cached = self._undirected
        if cached is not None and cached[0] == self._generation:
            return cached[1]
        snapshot = self.graph.to_undirected()
        self._undirected = (self._generation, snapshot)
        return snapshot

    def build_graph(self, tables_info: list[dict[str, Any]]) -> None:
        """
        Build graph from schema information.

        Args:
            tables_info: List of table metadata with columns and foreign keys
        """
        # Before and after: a build that raises midway must not leave a
        # snapshot of the graph as it was, still matching the generation.
        self._graph_changed()
        self.graph.clear()
        self._tables_info = {}
        # A rebuild replaces everything, membership and the name index included.
        self._schema_tables = {}
        self._by_name = {}

        # Add nodes (tables)
        for table in tables_info:
            identity = self._add_node(table)
            self._schema_tables.setdefault(table.get("schema"), set()).add(identity)

        # Add edges (foreign key relationships)
        for table in tables_info:
            self._add_foreign_keys(table)

        self._graph_changed()
        logger.info(
            f"Built graph with {self.graph.number_of_nodes()} nodes "
            f"and {self.graph.number_of_edges()} edges"
        )

    def add_to_graph(self, tables_info: list[dict[str, Any]]) -> None:
        """Add tables and relationships to the existing graph (accumulative).

        Unlike build_graph(), this does NOT clear the graph first.
        Duplicate nodes are updated, new edges are added.

        Args:
            tables_info: List of table metadata with columns and foreign keys
        """
        self._graph_changed()
        added_nodes = 0
        added_edges = 0

        # A schema in this batch reports its whole table set, so its membership
        # is replaced rather than added to: that is what makes a table missing
        # from a rediscovery a dropped table rather than an unmentioned one.
        for schema in {table.get("schema") for table in tables_info}:
            self._schema_tables[schema] = set()

        for table in tables_info:
            identity = qualified(table.get("schema"), table["name"])
            if identity not in self.graph:
                added_nodes += 1
            self._add_node(table)
            self._schema_tables.setdefault(table.get("schema"), set()).add(identity)

        # A table's foreign keys are replaced, not merged. Rediscovery is what
        # happens after a schema changes, and a constraint dropped there used
        # to keep its edge forever -- so join paths were still offered through
        # a relationship the database no longer has. Only edges this discovery
        # is responsible for are removed: foreign keys leaving the tables in
        # this batch. Edges from tables in other schemas, and anything not
        # recorded as a foreign key, are left alone.
        # Identity carries the schema, so this is now exactly the tables this
        # discovery reported: a table of the same name in another schema is a
        # different node and keeps its own relationships. The guard that used
        # to be needed -- keep the edges of a name last seen under a different
        # schema -- is gone with the collision it worked around.
        removed_edges = 0
        for table in tables_info:
            identity = qualified(table.get("schema"), table["name"])
            if identity not in self.graph:
                continue
            stale = [
                (identity, referenced)
                for _, referenced, data in self.graph.out_edges(identity, data=True)
                if data.get("edge_type") == "foreign_key"
            ]
            self.graph.remove_edges_from(stale)
            removed_edges += len(stale)

        for table in tables_info:
            added_edges += self._add_foreign_keys(table)

        self._graph_changed()
        logger.info(
            f"Added to graph: +{added_nodes} nodes, +{added_edges} edges "
            f"(-{removed_edges} replaced foreign keys; total: "
            f"{self.graph.number_of_nodes()} nodes, "
            f"{self.graph.number_of_edges()} edges)"
        )

    def _joins_along(self, path: list[str]) -> list[dict[str, Any]]:
        """Join specifications for a path of table names, in path order."""
        joins = []
        for left_table, right_table in pairwise(path):
            # Edge data is the FK relationship, stored in the FK's direction.
            if self.graph.has_edge(left_table, right_table):
                edge_data = self.graph[left_table][right_table]
                joins.append(
                    {
                        "from_table": left_table,
                        "to_table": right_table,
                        "from_column": edge_data["column"],
                        "to_column": edge_data["referenced_column"],
                        "join_type": "INNER",
                    }
                )
            elif self.graph.has_edge(right_table, left_table):
                edge_data = self.graph[right_table][left_table]
                joins.append(
                    {
                        "from_table": left_table,
                        "to_table": right_table,
                        "from_column": edge_data["referenced_column"],
                        "to_column": edge_data["column"],
                        "join_type": "INNER",
                    }
                )
        return joins

    def find_alternative_join_paths(
        self,
        from_table: str,
        to_table: str,
        chosen: list[dict[str, Any]],
        limit: int = 5,
    ) -> list[list[dict[str, Any]]]:
        """Other join paths exactly as short as the one ``find_join_path`` chose.

        ``find_join_path`` returns *a* shortest path. When several routes are
        equally short -- an order reaching a region through its customer or
        through its warehouse -- it picks one silently, and the two generally
        answer different questions. This reports the ones it did not pick, so
        the choice can be made knowingly.

        Two foreign keys between the *same* pair of tables are not seen here:
        the graph keeps one edge per table pair.

        Args:
            from_table: Source table.
            to_table: Target table.
            chosen: The joins ``find_join_path`` returned.
            limit: Most alternatives to report.

        Returns:
            Join specifications of each other shortest path; empty when the
            chosen path is the only one of its length.
        """
        from_table = self.identity_for(from_table)
        to_table = self.identity_for(to_table)
        if from_table not in self.graph or to_table not in self.graph:
            return []
        chosen_tables = [from_table, *(join["to_table"] for join in chosen)]
        alternatives: list[list[dict[str, Any]]] = []
        try:
            undirected = self._undirected_snapshot()
            for path in nx.all_shortest_paths(
                undirected, source=from_table, target=to_table
            ):
                if len(path) != len(chosen_tables) or path == chosen_tables:
                    continue
                alternatives.append(self._joins_along(path))
                if len(alternatives) >= limit:
                    break
        except nx.NetworkXNoPath:
            return []
        return alternatives

    def find_join_path(
        self, from_table: str, to_table: str, max_hops: int = 12
    ) -> list[dict[str, Any]] | None:
        """
        Find join path between two tables.

        Args:
            from_table: Source table
            to_table: Target table
            max_hops: Maximum number of joins allowed (default: 12)

        Returns:
            List of join specifications or None if no path exists
        """
        from_table = self.identity_for(from_table)
        to_table = self.identity_for(to_table)
        if from_table not in self.graph or to_table not in self.graph:
            return None

        try:
            # Use bidirectional search (considers both directions)
            # Also try undirected view to find mixed-direction paths (e.g., A → B ← C)
            path_forward = None
            path_backward = None
            path_undirected = None

            with contextlib.suppress(nx.NetworkXNoPath):
                path_forward = nx.shortest_path(
                    self.graph, source=from_table, target=to_table
                )

            with contextlib.suppress(nx.NetworkXNoPath):
                path_backward = nx.shortest_path(
                    self.graph, source=to_table, target=from_table
                )

            # Try undirected view for mixed-direction paths
            try:
                undirected_graph = self._undirected_snapshot()
                path_undirected = nx.shortest_path(
                    undirected_graph, source=from_table, target=to_table
                )
            except nx.NetworkXNoPath:
                pass

            # Choose shortest path among all options
            candidates = []
            if path_forward:
                candidates.append(path_forward)
            if path_backward:
                candidates.append(list(reversed(path_backward)))
            if path_undirected:
                candidates.append(path_undirected)

            if not candidates:
                return None

            # Pick shortest path
            path = min(candidates, key=len)

            if len(path) - 1 > max_hops:
                logger.warning(
                    f"Path from {from_table} to {to_table} requires {len(path) - 1} hops (max: {max_hops})"
                )
                return None

            return self._joins_along(path)

        except Exception as e:
            logger.error(f"Error finding join path: {e}")
            return None

    def get_related_tables(
        self, table_name: str, max_distance: int = 1, direction: str = "both"
    ) -> dict[int, list[str]]:
        """
        Get tables related to a given table.

        Args:
            table_name: Source table
            max_distance: Maximum graph distance (hops)
            direction: "outgoing" (FK from table), "incoming" (FK to table), "both"

        Returns:
            Dictionary with lists of related tables by distance
        """
        table_name = self.identity_for(table_name)
        if table_name not in self.graph:
            return {}

        related: defaultdict[int, list[str]] = defaultdict(list)

        if direction in ["outgoing", "both"]:
            # Tables this table references (FK from)
            for target in self.graph.successors(table_name):
                related[1].append(target)

            # Multi-hop outgoing
            if max_distance > 1:
                visited = {table_name}
                queue = deque([(table_name, 0)])

                while queue:
                    current, dist = queue.popleft()

                    if dist >= max_distance:
                        continue

                    for neighbor in self.graph.successors(current):
                        if neighbor not in visited:
                            visited.add(neighbor)
                            related[dist + 1].append(neighbor)
                            queue.append((neighbor, dist + 1))

        if direction in ["incoming", "both"]:
            # Tables that reference this table (FK to)
            for source in self.graph.predecessors(table_name):
                if source not in related[1]:  # Avoid duplicates if "both"
                    related[1].append(source)

            # Multi-hop incoming
            if max_distance > 1:
                visited = {table_name}
                queue = deque([(table_name, 0)])

                while queue:
                    current, dist = queue.popleft()

                    if dist >= max_distance:
                        continue

                    for neighbor in self.graph.predecessors(current):
                        if neighbor not in visited:
                            visited.add(neighbor)
                            if neighbor not in related[dist + 1]:
                                related[dist + 1].append(neighbor)
                            queue.append((neighbor, dist + 1))

        return dict(related)

    def _directed_closure(
        self,
        table_name: str,
        max_hops: int | None,
        neighbor_fn: Callable[[str], Any],
    ) -> dict[str, Any]:
        """Cycle-safe directed transitive closure from a table.

        Edges in the FK DiGraph point finer grain -> coarser grain (a table to
        the table it references). Following ``successors`` walks the many-to-one
        (dimension) direction; following ``predecessors`` walks the one-to-many
        (measure) direction.

        Args:
            table_name: Anchor table
            max_hops: Maximum hops (None = unbounded full closure)
            neighbor_fn: ``self.graph.successors`` or ``self.graph.predecessors``

        Returns:
            Dict with ``exists``, ordered ``tables`` list, and ``by_hop`` mapping.
        """
        table_name = self.identity_for(table_name)
        if table_name not in self.graph:
            return {"exists": False, "tables": [], "by_hop": {}}

        visited = {table_name}
        order: list[str] = []
        by_hop: dict[int, list[str]] = {}
        queue = deque([(table_name, 0)])

        while queue:
            current, dist = queue.popleft()
            if max_hops is not None and dist >= max_hops:
                continue
            for neighbor in neighbor_fn(current):
                if neighbor not in visited:
                    visited.add(neighbor)  # cycle-safe: each node enqueued once
                    order.append(neighbor)
                    by_hop.setdefault(dist + 1, []).append(neighbor)
                    queue.append((neighbor, dist + 1))

        return {"exists": True, "tables": order, "by_hop": by_hop}

    def reachable_from(
        self, table_name: str, max_hops: int | None = None
    ) -> dict[str, Any]:
        """Dimension-capable tables for a query anchored on ``table_name``.

        Directed many-to-one closure (``successors``): coarser-grain tables that
        can be joined from the anchor without row multiplication (every hop is
        functional), so their columns are safe to GROUP BY / filter on.
        """
        return self._directed_closure(table_name, max_hops, self.graph.successors)

    def measurable_from(
        self, table_name: str, max_hops: int | None = None
    ) -> dict[str, Any]:
        """Measure-capable tables for a query anchored on ``table_name``.

        Directed one-to-many closure (``predecessors``): finer-grain tables that
        fan out the anchor, so their values can only be aggregated into measures
        (SUM/COUNT/...), never used as dimensions at this grain.
        """
        return self._directed_closure(table_name, max_hops, self.graph.predecessors)

    def detect_fan_traps(self, tables: list[str]) -> list[dict[str, Any]]:
        """
        Detect potential fan-trap scenarios in a set of tables.

        Args:
            tables: List of table names

        Returns:
            List of fan-trap warnings
        """
        warnings = []

        for name in tables:
            table = self.identity_for(name)
            if table not in self.graph:
                continue

            # Count outgoing FKs
            outgoing_fks = list(self.graph.successors(table))

            if len(outgoing_fks) > 1:
                warnings.append(
                    {
                        "bridge_table": table,
                        "referenced_tables": outgoing_fks,
                        "warning": f"Table '{table}' connects to multiple tables - potential fan-trap",
                        "recommendation": "Use separate CTEs or UNION approach if aggregating across these relationships",
                    }
                )

        return warnings

    def get_table_metadata(self, table_name: str) -> dict[str, Any] | None:
        """
        Get full metadata for a table.

        Args:
            table_name: Table name

        Returns:
            Table metadata dictionary or None
        """
        return self._tables_info.get(self.identity_for(table_name))

    def get_graph_summary(self) -> dict[str, Any]:
        """
        Get summary statistics of the schema graph.

        Returns:
            Summary dictionary
        """
        # Find central tables (high degree centrality)
        centrality = nx.degree_centrality(self._undirected_snapshot())
        top_central = sorted(centrality.items(), key=lambda x: x[1], reverse=True)[:5]

        # Find hub tables (many outgoing FKs)
        out_degrees = dict(self.graph.out_degree())
        top_hubs = sorted(out_degrees.items(), key=lambda x: x[1], reverse=True)[:5]

        # Find reference tables (many incoming FKs)
        in_degrees = dict(self.graph.in_degree())
        top_references = sorted(in_degrees.items(), key=lambda x: x[1], reverse=True)[
            :5
        ]

        return {
            "total_tables": self.graph.number_of_nodes(),
            "total_relationships": self.graph.number_of_edges(),
            "top_central_tables": [
                {"table": t, "centrality": c} for t, c in top_central
            ],
            "top_hub_tables": [
                {"table": t, "outgoing_fks": d} for t, d in top_hubs if d > 0
            ],
            "top_reference_tables": [
                {"table": t, "incoming_fks": d} for t, d in top_references if d > 0
            ],
            "avg_connections_per_table": sum(dict(self.graph.degree()).values())
            / max(self.graph.number_of_nodes(), 1),
        }

    def load_graph(self, tables_info: list[dict[str, Any]]) -> bool:
        """Rebuild graph from previously saved tables_info.

        This is equivalent to build_graph() but named distinctly to indicate
        it's for restore-from-disk scenarios.

        Args:
            tables_info: List of table metadata dicts (from saved state)

        Returns:
            True if graph was rebuilt successfully
        """
        if not tables_info:
            return False
        self.build_graph(tables_info)
        return True

    def export_graph_for_visualization(self) -> dict[str, Any]:
        """
        Export graph in format suitable for visualization.

        Returns:
            Dictionary with nodes and edges
        """
        nodes = [
            {
                "id": node[0],
                # The label is the table's own name; the id carries its schema,
                # so two tables of the same name are two nodes that read alike.
                "label": node[1].get("table") or display_name(node[0]),
                "schema": node[1].get("schema"),
                "type": "table",
                "column_count": node[1].get("column_count", 0),
            }
            for node in self.graph.nodes(data=True)
        ]

        edges = [
            {
                "from": edge[0],
                "to": edge[1],
                "label": f"{edge[2].get('column')} → {edge[2].get('referenced_column')}",
                "type": "foreign_key",
            }
            for edge in self.graph.edges(data=True)
        ]

        return {"nodes": nodes, "edges": edges}
