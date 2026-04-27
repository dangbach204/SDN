"""
adaptive_routing.py
Utility functions for congestion-aware path selection on the Mininet triangle topology.
"""

import math
from typing import Dict, List, Optional, Tuple

# Deterministic uplink mapping from mininet/topo.py
# s1-eth1 <-> s2-eth1
# s2-eth2 <-> s3-eth1
# s1-eth2 <-> s3-eth2
PORT_BY_NEIGHBOR = {
    (1, 2): 1,
    (1, 3): 2,
    (2, 1): 1,
    (2, 3): 2,
    (3, 2): 1,
    (3, 1): 2,
}

NEIGHBOR_BY_PORT = {(sw, port): nb for (sw, nb), port in PORT_BY_NEIGHBOR.items()}
UPLINK_PORTS = set(NEIGHBOR_BY_PORT.keys())
NODES = [1, 2, 3]


def is_uplink_port(dpid: int, port_no: int) -> bool:
    return (dpid, port_no) in UPLINK_PORTS


def capacity_mbps(dpid: int, port_no: int) -> float:
    return 100.0 if is_uplink_port(dpid, port_no) else 50.0


def mac_to_switch(mac: Optional[str]) -> Optional[int]:
    if not mac or ":" not in mac:
        return None

    try:
        last = int(mac.strip().split(":")[-1], 16)
    except Exception:
        return None

    if 1 <= last <= 4:
        return 1
    if 5 <= last <= 8:
        return 2
    if 9 <= last <= 12:
        return 3
    return None


def build_link_weights(
    util_by_port: Dict[Tuple[int, int], float],
    penalize_port: Optional[Tuple[int, int]] = None,
) -> Dict[Tuple[int, int], float]:
    """
    Build directional edge weights from per-port utilization percentages.
    Higher utilization means higher weight.
    """
    weights: Dict[Tuple[int, int], float] = {}

    for (a, b), port_a in PORT_BY_NEIGHBOR.items():
        if a > b:
            continue

        port_b = PORT_BY_NEIGHBOR[(b, a)]
        util_a = max(0.0, float(util_by_port.get((a, port_a), 0.0)))
        util_b = max(0.0, float(util_by_port.get((b, port_b), 0.0)))
        edge_util = max(util_a, util_b)

        # Base shortest-path weight + congestion penalty.
        weight = 1.0 + (edge_util / 100.0) * 4.0
        if edge_util >= 95.0:
            weight += 8.0

        if penalize_port in {(a, port_a), (b, port_b)}:
            weight += 12.0

        weights[(a, b)] = weight
        weights[(b, a)] = weight

    return weights


def shortest_path(
    src: int,
    dst: int,
    weights: Dict[Tuple[int, int], float],
) -> List[int]:
    if src == dst:
        return [src]

    dist = {n: math.inf for n in NODES}
    prev: Dict[int, Optional[int]] = {n: None for n in NODES}
    visited = set()
    dist[src] = 0.0

    while len(visited) < len(NODES):
        cur = None
        cur_dist = math.inf
        for node in NODES:
            if node in visited:
                continue
            if dist[node] < cur_dist:
                cur = node
                cur_dist = dist[node]

        if cur is None or cur_dist == math.inf:
            break

        if cur == dst:
            break

        visited.add(cur)

        for nb in NODES:
            if nb == cur:
                continue
            edge_w = weights.get((cur, nb))
            if edge_w is None:
                continue
            cand = dist[cur] + edge_w
            if cand < dist[nb]:
                dist[nb] = cand
                prev[nb] = cur

    if dist[dst] == math.inf:
        return []

    path = [dst]
    while path[-1] != src:
        p = prev[path[-1]]
        if p is None:
            return []
        path.append(p)
    path.reverse()
    return path


def ecmp_next_ports(
    src: int,
    dst: int,
    weights: Dict[Tuple[int, int], float],
    epsilon: float = 0.05,
) -> List[int]:
    """Return candidate equal-cost next-hop ports for future ECMP support."""
    if src == dst:
        return []

    candidate_costs: List[Tuple[float, int]] = []
    for (sw, port), nb in NEIGHBOR_BY_PORT.items():
        if sw != src:
            continue
        tail_path = shortest_path(nb, dst, weights)
        if not tail_path:
            continue
        total_cost = weights.get((src, nb), math.inf)
        for i in range(len(tail_path) - 1):
            total_cost += weights.get((tail_path[i], tail_path[i + 1]), math.inf)
        if total_cost < math.inf:
            candidate_costs.append((total_cost, port))

    if not candidate_costs:
        return []

    best = min(c for c, _ in candidate_costs)
    return [p for c, p in candidate_costs if abs(c - best) <= epsilon]


def choose_reroute_port(
    dpid: int,
    congested_port: int,
    util_by_port: Dict[Tuple[int, int], float],
    dst_switch: Optional[int] = None,
) -> Tuple[Optional[int], List[int], str]:
    """
    Choose an output uplink port to avoid a congested link.

    Returns:
    - output port (or None)
    - selected path as switch list
    - reasoning string
    """
    if dpid not in NODES:
        return None, [], f"Unknown switch s{dpid}"

    weights = build_link_weights(util_by_port, penalize_port=(dpid, congested_port))

    if dst_switch and dst_switch in NODES and dst_switch != dpid:
        path = shortest_path(dpid, dst_switch, weights)
        if len(path) >= 2:
            next_hop = path[1]
            out_port = PORT_BY_NEIGHBOR.get((dpid, next_hop))
            if out_port is not None and out_port != congested_port:
                return out_port, path, f"Shortest path to s{dst_switch} avoids congested port {congested_port}"

    candidates: List[Tuple[float, int, int]] = []
    for (sw, port), nb in NEIGHBOR_BY_PORT.items():
        if sw != dpid or port == congested_port:
            continue
        w = weights.get((sw, nb), math.inf)
        if w < math.inf:
            candidates.append((w, port, nb))

    if not candidates:
        return None, [], f"No alternate uplink from s{dpid} excluding port {congested_port}"

    candidates.sort(key=lambda x: x[0])
    _, best_port, best_nb = candidates[0]
    return best_port, [dpid, best_nb], f"Lowest-weight alternate uplink selected from s{dpid}"
