"""Iterative ancestry and every best common ancestor of two commit-ish roots."""

from collections import deque

from .errors import InvalidObject
from .revisions import RevisionLimits, _Context, _resolve, check_history_profile


def _graph(context, roots):
    graph = {};colors = {};stack = [(oid, False) for oid in reversed(roots)]
    while stack:
        oid, complete = stack.pop()
        if complete:
            colors[oid] = 2;continue
        color = colors.get(oid, 0)
        if color == 2:continue
        if color == 1:raise InvalidObject('cycle in commit ancestry')
        context.step();colors[oid] = 1
        _, commit = context.commit(oid, peel=False)
        parents = tuple(dict.fromkeys(commit.parents));graph[oid] = parents
        stack.append((oid, True))
        stack.extend((parent, False) for parent in reversed(parents))
    return graph


def _prepare(repo, left, right, limits):
    check_history_profile(repo)
    context = _Context(repo, limits or RevisionLimits())
    a, _ = context.commit(_resolve(context, left));b, _ = context.commit(_resolve(context, right))
    return context, a, b, _graph(context, (a, b))


def is_ancestor(repo, ancestor, descendant, *, limits=None):
    """Return a verified predicate; corruption/limits never mean False."""
    context, a, b, graph = _prepare(repo, ancestor, descendant, limits)
    stack = [b];seen = set()
    while stack:
        oid = stack.pop()
        if oid in seen:continue
        context.step();seen.add(oid)
        if oid == a:return True
        stack.extend(graph[oid])
    return False


def merge_bases(repo, left, right, *, limits=None):
    """Return all two-root best common ancestors, sorted by full object ID."""
    context, a, b, graph = _prepare(repo, left, right, limits)
    # Topological propagation goes from descendants to parents. A common node
    # makes every ancestor stale, including through non-common intermediate nodes.
    children = {oid:0 for oid in graph}
    for parents in graph.values():
        for parent in parents:children[parent] += 1
    queue = deque(sorted((oid for oid, count in children.items() if not count), key=lambda oid:oid.hex))
    masks = {a:1};masks[b] = masks.get(b, 0) | 2
    stale = set();result = []
    while queue:
        context.step();oid = queue.popleft();mask = masks.get(oid, 0);common = mask == 3
        if common and oid not in stale:result.append(oid)
        for parent in graph[oid]:
            masks[parent] = masks.get(parent, 0) | mask
            if common or oid in stale:stale.add(parent)
            children[parent] -= 1
            if not children[parent]:queue.append(parent)
    return tuple(sorted(result, key=lambda oid:oid.hex))
