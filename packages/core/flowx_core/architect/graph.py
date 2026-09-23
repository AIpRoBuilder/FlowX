"""Graph class for loading and managing graph structure from JSON."""

import json
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Dict, List, Optional, Any, Iterable, Mapping, MutableMapping, Set, Tuple

from flowx_core.tools.file_tools import collect_session_state_keys_from_node_file
from flowx_core.tools.workflow_node_reference import canonical_meta_node_kind


@dataclass
class NodeMeta:
    """Metadata wrapper for a graph node."""
    
    name: str
    type: str
    desc: str
    meta_node_kind: str = ""
    ext_data: Optional[Dict[str, Any] | str] = None
    inputs_format: Dict[str, str] = field(default_factory=dict)
    enable: bool = True
    depends: List[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, node: Mapping[str, Any]) -> "NodeMeta":
        """Build a normalized ``NodeMeta`` from raw graph JSON data."""
        raw_inputs_format = node.get('inputs_format', {})
        inputs_format: Dict[str, str] = {}
        if isinstance(raw_inputs_format, Mapping):
            for key, value in raw_inputs_format.items():
                field_name = str(key).strip()
                field_type = str(value).strip()
                if field_name and field_type:
                    inputs_format[field_name] = field_type

        depends = node.get('depends', [])
        ext_data = node.get('ext_data')
        meta_node_kind = canonical_meta_node_kind(
            meta_node_kind=str(node.get('meta_node_kind') or node.get('metaNodeKind') or '').strip() or None,
            ext_data=ext_data,
        )

        return cls(
            name=str(node.get('name', '')),
            type=str(node.get('type', '')),
            desc=str(node.get('desc', '')),
            meta_node_kind=meta_node_kind,
            ext_data=ext_data,
            inputs_format=inputs_format,
            enable=bool(node.get('enable', True)),
            depends=list(depends) if isinstance(depends, list) else [],
        )
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert node metadata to dictionary."""
        return asdict(self)


class Graph:
    """Graph class that loads and manages graph structure from JSON."""
    
    def __init__(self, graph_json_path: str):
        """Initialize graph by loading from JSON file.
        
        Args:
            graph_json_path: Path to the graph JSON file.
            
        Raises:
            FileNotFoundError: If graph JSON file not found.
            json.JSONDecodeError: If JSON file is invalid.
        """
        self.graph_json_path = Path(graph_json_path)
        self.nodes: List[Dict[str, Any]] = []
        self.node_metas: Dict[str, NodeMeta] = {}
        self._load_graph()
    
    def _load_graph(self) -> None:
        """Load graph from JSON file and parse nodes."""
        if not self.graph_json_path.exists():
            raise FileNotFoundError(f"Graph JSON file not found: {self.graph_json_path}")
        
        try:
            with open(self.graph_json_path, 'r', encoding='utf-8') as f:
                graph_data = json.load(f)
        except json.JSONDecodeError as e:
            raise json.JSONDecodeError(f"Invalid JSON in {self.graph_json_path}: {e.msg}", e.doc, e.pos)
        
        # Extract nodes
        if 'nodes' not in graph_data:
            raise ValueError("Graph JSON must contain 'nodes' key")
        
        self.nodes = graph_data.get('nodes', [])
        self._build_node_metas()
    
    def _build_node_metas(self) -> None:
        """Build node metadata objects from nodes data."""
        self.node_metas.clear()
        
        for node in self.nodes:
            try:
                node_meta = NodeMeta.from_dict(node)
                self.node_metas[node_meta.name] = node_meta
            except Exception as e:
                raise ValueError(f"Failed to create NodeMeta from node {node}: {e}")
    
    def get_node_meta(self, node_name: str) -> Optional[NodeMeta]:
        """Get metadata for a specific node.
        
        Args:
            node_name: Name of the node.
            
        Returns:
            NodeMeta object if node exists, None otherwise.
        """
        return self.node_metas.get(node_name)
    
    def get_all_node_metas(self) -> Dict[str, NodeMeta]:
        """Get metadata for all nodes.
        
        Returns:
            Dictionary mapping node names to NodeMeta objects.
        """
        return self.node_metas.copy()
    
    def get_node_metas_as_dict(self) -> Dict[str, Dict[str, Any]]:
        """Get all node metadata as dictionary format.
        
        Returns:
            Dictionary where keys are node names and values are node attribute dicts.
        """
        return {name: meta.to_dict() for name, meta in self.node_metas.items()}
    
    def get_nodes_by_type(self, node_type: str) -> List[NodeMeta]:
        """Get all nodes of a specific type.
        
        Args:
            node_type: Type to filter by.
            
        Returns:
            List of NodeMeta objects with matching type.
        """
        return [meta for meta in self.node_metas.values() if meta.type == node_type]
    
    def get_enabled_nodes(self) -> List[NodeMeta]:
        """Get all enabled nodes.
        
        Returns:
            List of enabled NodeMeta objects.
        """
        return [meta for meta in self.node_metas.values() if meta.enable]
    
    def get_node_dependencies(self, node_name: str) -> List[str]:
        """Get dependencies for a specific node.
        
        Args:
            node_name: Name of the node.
            
        Returns:
            List of node names that this node depends on.
        """
        meta = self.get_node_meta(node_name)
        return meta.depends if meta else []
    
    def get_graph_size(self) -> int:
        """Get total number of nodes in the graph.
        
        Returns:
            Number of nodes.
        """
        return len(self.node_metas)

    def _get_root_nodes(self) -> List[str]:
        """Get root nodes (nodes with no in-graph dependencies).

        Returns:
            List of root node names in original graph order.
        """
        roots: List[str] = []
        for node in self.nodes:
            node_name = str(node.get("name", ""))
            if not node_name:
                continue
            depends = node.get("depends", []) or []
            in_graph_depends = [dep for dep in depends if dep in self.node_metas]
            if len(in_graph_depends) == 0:
                roots.append(node_name)
        return roots

    def _collect_ancestors(self, node_name: str) -> Set[str]:
        """Collect all ancestor nodes (including itself) by following dependencies.

        Args:
            node_name: Target node name.

        Returns:
            Set of node names that can reach ``node_name`` via dependency edges.
        """
        if node_name not in self.node_metas:
            return set()

        ancestors: Set[str] = set()
        stack: List[str] = [node_name]

        while stack:
            current = stack.pop()
            if current in ancestors:
                continue
            ancestors.add(current)

            for dep in self.get_node_dependencies(current):
                if dep in self.node_metas and dep not in ancestors:
                    stack.append(dep)

        return ancestors

    def get_ancestor_session_state_keys(self, node_name: str, include_current: bool = True) -> List[str]:
        """Get all ``session_state`` keys used by a node and its ancestors.

        This method uses ``_collect_ancestors`` to resolve relevant node names,
        loads corresponding node files from the graph JSON directory, and then
        extracts all string keys accessed on the ``session_state`` dict.

        Args:
            node_name: Target node name.
            include_current: Whether to include ``node_name`` itself when collecting
                keys. Defaults to ``True`` for backward compatibility.

        Returns:
            Sorted list of unique session_state keys from ancestor node files.
        """
        if node_name not in self.node_metas:
            return []

        ancestor_names = self._collect_ancestors(node_name)
        if not include_current:
            ancestor_names.discard(node_name)
        if not ancestor_names:
            return []

        node_dir = self.graph_json_path.resolve().parent
        all_keys: Set[str] = set()

        for ancestor_name in ancestor_names:
            node_file = node_dir / f"{ancestor_name}.py"
            all_keys.update(collect_session_state_keys_from_node_file(str(node_file), ancestor_name))

        return sorted(all_keys)

    def get_all_subgraph(self) -> Dict[str, Dict[str, List[Dict[str, Any]]]]:
        """Get root-to-node subgraphs for every node in JSON format.

        For each node in the graph, this returns a subgraph JSON payload that
        contains all ancestor nodes from graph root node(s) to the target node,
        including the target itself. Dependencies in each subgraph are filtered
        to only reference nodes included in that subgraph.

        Returns:
            A dictionary keyed by target node name where each value is a JSON-like
            dictionary of the shape ``{"nodes": [...]}``.
        """
        root_nodes = set(self._get_root_nodes())
        subgraphs: Dict[str, Dict[str, List[Dict[str, Any]]]] = {}

        for node in self.nodes:
            target_name = str(node.get("name", ""))
            if not target_name or target_name not in self.node_metas:
                continue

            ancestor_names = self._collect_ancestors(target_name)
            if not ancestor_names:
                subgraphs[target_name] = {"nodes": []}
                continue

            # Keep only nodes that are on root-to-target routes. For targets that
            # are roots themselves, keep the root node.
            if target_name in root_nodes:
                included_names = {target_name}
            else:
                included_names = {
                    name for name in ancestor_names
                    if name == target_name or name in root_nodes or len(self.get_node_dependencies(name)) > 0
                }

            subgraph_nodes: List[Dict[str, Any]] = []
            for original_node in self.nodes:
                original_name = str(original_node.get("name", ""))
                if original_name not in included_names:
                    continue

                copied_node = dict(original_node)
                depends = copied_node.get("depends", []) or []
                copied_node["depends"] = [dep for dep in depends if dep in included_names]
                subgraph_nodes.append(copied_node)

            subgraphs[target_name] = {"nodes": subgraph_nodes}

        return subgraphs
    
    def is_weakly_connected(self) -> bool:
        """Check if the graph is weakly connected.
        
        A directed graph is weakly connected if the underlying undirected graph
        is connected, meaning there is a path between every pair of nodes when
        treating all edges as bidirectional.
        
        Returns:
            True if the graph is weakly connected, False otherwise.
        """
        if not self.node_metas:
            return True
        
        # Build adjacency list treating graph as undirected
        adj: Dict[str, List[str]] = {name: [] for name in self.node_metas}
        
        for node_name, meta in self.node_metas.items():
            # Add edges from node to its dependencies
            for dep in meta.depends:
                if dep in adj:
                    adj[node_name].append(dep)
                    adj[dep].append(node_name)
        
        # DFS from first node to check if all nodes are reachable
        start_node = next(iter(self.node_metas))
        visited = set()
        stack = [start_node]
        
        while stack:
            node = stack.pop()
            if node in visited:
                continue
            visited.add(node)
            for neighbor in adj[node]:
                if neighbor not in visited:
                    stack.append(neighbor)
        
        # Graph is weakly connected if all nodes were visited
        return len(visited) == len(self.node_metas)
    
    def is_dag(self) -> Tuple[bool, Iterable[str]]:
        """Check whether a graph JSON definition is a DAG.

        Returns a tuple of ``(is_acyclic, cycle_path)``. ``cycle_path`` will be an
        ordered iterable of node names describing one detected cycle when
        ``is_acyclic`` is False; otherwise it is empty.
        """

        nodes = self.get_node_metas_as_dict()

        # Include referenced-but-undefined nodes to trace cycles across dependencies.
        all_nodes: Set[str] = set(nodes)
        for info in nodes.values():
            for dep in info.get("depends", []) or []:
                all_nodes.add(str(dep))

        visiting: Set[str] = set()
        visited: Set[str] = set()
        cycle: List[str] = []

        def dfs(node: str, stack: list[str]) -> bool:
            if node in visiting:
                # Capture the cycle path from the first occurrence of ``node``.
                idx = stack.index(node)
                cycle.extend(stack[idx:] + [node])
                return False

            if node in visited:
                return True

            visiting.add(node)
            next_stack = stack + [node]
            deps = nodes.get(node, {}).get("depends", []) or []
            for dep in deps:
                dep_name = str(dep)
                if not dfs(dep_name, next_stack):
                    return False

            visiting.remove(node)
            visited.add(node)
            return True

        for node_name in all_nodes:
            if node_name in visited:
                continue
            if not dfs(node_name, []):
                return False, cycle

        return True, []
    
    def get_topological_sorted_nodes(self) -> List[str]:
        """Get nodes in topological order using Kahn's algorithm.
        
        This performs a topological sort on the graph based on node dependencies.
        Nodes with no dependencies come first, followed by nodes whose dependencies
        have been satisfied.
        
        Returns:
            List of node names in topological order.
            
        Raises:
            ValueError: If the graph contains a cycle.
        """
        from collections import deque
        
        # Check if graph is a DAG
        is_acyclic, cycle_path = self.is_dag()
        if not is_acyclic:
            raise ValueError(f"Graph contains a cycle: {' -> '.join(cycle_path)}")
        
        # Build in-degree map and adjacency list
        in_degree: Dict[str, int] = {name: 0 for name in self.node_metas}
        adj_list: Dict[str, List[str]] = {name: [] for name in self.node_metas}
        
        # Calculate in-degrees and build adjacency list
        for node_name, meta in self.node_metas.items():
            for dep in meta.depends:
                if dep in self.node_metas:
                    adj_list[dep].append(node_name)
                    in_degree[node_name] += 1
        
        # Initialize queue with nodes that have no dependencies
        queue: deque = deque([name for name in self.node_metas if in_degree[name] == 0])
        sorted_nodes: List[str] = []
        
        # Process nodes in topological order
        while queue:
            node = queue.popleft()
            sorted_nodes.append(node)
            
            # Reduce in-degree for dependent nodes
            for dependent in adj_list[node]:
                in_degree[dependent] -= 1
                if in_degree[dependent] == 0:
                    queue.append(dependent)
        
        return sorted_nodes
    
    def __repr__(self) -> str:
        """String representation of the graph."""
        return (f"Graph(path={self.graph_json_path}, "
                f"nodes={self.get_graph_size()})")
