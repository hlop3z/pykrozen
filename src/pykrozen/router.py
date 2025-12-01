"""
High-performance Radix Tree Router - Fastify-inspired routing.

Optimized for minimal allocations and fast path matching.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

__all__ = ["RadixRouter", "RouteMatch", "RouteNode"]

# Node type constants
NODE_STATIC = 0
NODE_DYNAMIC = 1
NODE_WILDCARD = 2


class RouteNode:
    """A node in the radix tree with __slots__ for performance."""

    __slots__ = (
        "segment",
        "node_type",
        "param_name",
        "static_children",
        "dynamic_child",
        "wildcard_child",
        "handlers",
    )

    def __init__(
        self,
        segment: str = "",
        node_type: int = NODE_STATIC,
        param_name: str = "",
    ) -> None:
        self.segment = segment
        self.node_type = node_type
        self.param_name = param_name
        self.static_children: dict[str, RouteNode] = {}
        self.dynamic_child: RouteNode | None = None
        self.wildcard_child: RouteNode | None = None
        self.handlers: dict[str, Callable[..., Any]] = {}


class RouteMatch:
    """Result of a successful route match."""

    __slots__ = ("handler", "params", "matched")

    def __init__(self) -> None:
        self.handler: Callable[..., Any] | None = None
        self.params: dict[str, str] = {}
        self.matched: bool = False

    def reset(self) -> None:
        """Reset match state for reuse."""
        self.handler = None
        self.params.clear()
        self.matched = False


class RadixRouter:
    """High-performance radix tree router."""

    __slots__ = ("_trees", "_routes", "_match_cache", "_compiled")

    def __init__(self) -> None:
        self._trees: dict[str, RouteNode] = {}
        self._routes: list[tuple[str, str, Callable[..., Any]]] = []
        self._match_cache: RouteMatch = RouteMatch()
        self._compiled: bool = False

    def add(self, method: str, path: str, handler: Callable[..., Any]) -> RadixRouter:
        """Add a route to the router."""
        method = method.upper()
        self._routes.append((method, path, handler))
        self._compiled = False

        # Get or create root node
        root = self._trees.get(method)
        if root is None:
            root = RouteNode()
            self._trees[method] = root

        # Split path
        if path == "/":
            segments: list[str] = []
        else:
            path_stripped = path[1:] if path[0:1] == "/" else path
            segments = path_stripped.split("/") if path_stripped else []

        current = root

        for segment in segments:
            first_char = segment[0:1]
            if first_char == ":":
                param_name = segment[1:]
                if current.dynamic_child is not None:
                    if current.dynamic_child.param_name != param_name:
                        raise ValueError(
                            f"Conflicting parameter names at {path}: "
                            f"'{current.dynamic_child.param_name}' vs '{param_name}'"
                        )
                    current = current.dynamic_child
                else:
                    new_node = RouteNode(segment, NODE_DYNAMIC, param_name)
                    current.dynamic_child = new_node
                    current = new_node

            elif first_char == "*":
                param_name = segment[1:] if len(segment) > 1 else ""
                if current.wildcard_child is not None:
                    if current.wildcard_child.param_name != param_name:
                        raise ValueError(
                            f"Conflicting wildcard names at {path}: "
                            f"'{current.wildcard_child.param_name}' vs '{param_name}'"
                        )
                    current = current.wildcard_child
                else:
                    new_node = RouteNode(segment, NODE_WILDCARD, param_name)
                    current.wildcard_child = new_node
                    current = new_node
            else:
                child = current.static_children.get(segment)
                if child is not None:
                    current = child
                else:
                    new_node = RouteNode(segment, NODE_STATIC)
                    current.static_children[segment] = new_node
                    current = new_node

        if method in current.handlers:
            raise ValueError(f"Duplicate route: {method} {path}")
        current.handlers[method] = handler

        return self

    def compile(self) -> RadixRouter:
        """Mark routes as compiled."""
        self._compiled = True
        return self

    def _match_node(
        self,
        node: RouteNode,
        segments: list[str],
        idx: int,
        num_segments: int,
        params: dict[str, str],
        method: str,
    ) -> Callable[..., Any] | None:
        """Match path segments starting from idx. Returns handler or None."""
        # Base case: consumed all segments
        if idx >= num_segments:
            return node.handlers.get(method)

        segment = segments[idx]
        next_idx = idx + 1

        # Priority 1: Static match
        static_child = node.static_children.get(segment)
        if static_child is not None:
            result = self._match_node(
                static_child, segments, next_idx, num_segments, params, method
            )
            if result is not None:
                return result

        # Priority 2: Dynamic match
        dyn = node.dynamic_child
        if dyn is not None:
            param_name = dyn.param_name
            params[param_name] = segment
            result = self._match_node(
                dyn, segments, next_idx, num_segments, params, method
            )
            if result is not None:
                return result
            del params[param_name]  # Backtrack

        # Priority 3: Wildcard match
        wc = node.wildcard_child
        if wc is not None:
            if wc.param_name:
                params[wc.param_name] = "/".join(segments[idx:])
            return wc.handlers.get(method)

        return None

    def match_new(self, method: str, path: str) -> RouteMatch:
        """Match with a new RouteMatch object (thread-safe)."""
        match = RouteMatch()

        # Fast method lookup
        root = self._trees.get(method)
        if root is None:
            method_upper = method.upper()
            if method_upper != method:
                root = self._trees.get(method_upper)
            if root is None:
                return match
            method = method_upper

        # Fast path for root
        if path == "/" or not path:
            handler = root.handlers.get(method)
            if handler is not None:
                match.handler = handler
                match.matched = True
            return match

        # Split path
        path_stripped = path[1:] if path[0:1] == "/" else path
        if not path_stripped:
            handler = root.handlers.get(method)
            if handler is not None:
                match.handler = handler
                match.matched = True
            return match

        segments = path_stripped.split("/")

        # Match using recursive function
        handler = self._match_node(
            root, segments, 0, len(segments), match.params, method
        )
        if handler is not None:
            match.handler = handler
            match.matched = True

        return match

    def match(self, method: str, path: str) -> RouteMatch:
        """Match using cached RouteMatch (not thread-safe but faster)."""
        match = self._match_cache
        match.reset()

        root = self._trees.get(method)
        if root is None:
            method_upper = method.upper()
            if method_upper != method:
                root = self._trees.get(method_upper)
            if root is None:
                return match
            method = method_upper

        if path == "/" or not path:
            handler = root.handlers.get(method)
            if handler is not None:
                match.handler = handler
                match.matched = True
            return match

        path_stripped = path[1:] if path[0:1] == "/" else path
        if not path_stripped:
            handler = root.handlers.get(method)
            if handler is not None:
                match.handler = handler
                match.matched = True
            return match

        segments = path_stripped.split("/")

        handler = self._match_node(
            root, segments, 0, len(segments), match.params, method
        )
        if handler is not None:
            match.handler = handler
            match.matched = True

        return match

    def get_routes(self) -> list[tuple[str, str]]:
        """Get all registered routes."""
        return [(method, path) for method, path, _ in self._routes]

    def _print_node(self, node: RouteNode, prefix: str, is_last: bool) -> None:
        """Recursively print a node and its children."""
        connector = "`-- " if is_last else "|-- "
        if node.segment:
            type_indicator = ""
            if node.node_type == NODE_DYNAMIC:
                type_indicator = " (param)"
            elif node.node_type == NODE_WILDCARD:
                type_indicator = " (wildcard)"
            handlers_str = (
                f" [{', '.join(node.handlers.keys())}]" if node.handlers else ""
            )
            print(f"{prefix}{connector}{node.segment}{type_indicator}{handlers_str}")
        else:
            handlers_str = (
                f" [{', '.join(node.handlers.keys())}]" if node.handlers else ""
            )
            print(f"{prefix}{connector}(root){handlers_str}")

        children: list[tuple[str, RouteNode]] = []
        for seg, child in sorted(node.static_children.items()):
            children.append((f"static:{seg}", child))
        if node.dynamic_child:
            children.append(("dynamic", node.dynamic_child))
        if node.wildcard_child:
            children.append(("wildcard", node.wildcard_child))

        child_prefix = prefix + ("    " if is_last else "|   ")
        for i, (_, child) in enumerate(children):
            self._print_node(child, child_prefix, i == len(children) - 1)

    def print_tree(self, method: str | None = None) -> None:
        """Print the routing tree for debugging."""
        methods = [method] if method else list(self._trees.keys())
        for m in methods:
            if m not in self._trees:
                continue
            print(f"\n=== {m} Routes ===")
            self._print_node(self._trees[m], "", True)


def create_router() -> RadixRouter:
    """Create a new radix router instance."""
    return RadixRouter()
