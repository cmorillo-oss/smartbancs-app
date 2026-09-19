"""Construye el árbol de bloqueos a partir de las sesiones de pg_stat_activity. Función PURA (sin BD).

LECCIÓN APRENDIDA (encontrada reproduciendo el incidente real, no en teoría): cuando N sesiones
hacen cola por el mismo bloqueo, `pg_blocking_pids(pid)` de cada una devuelve al culpable Y a TODAS las
sesiones que tiene delante en la cola. El grafo "quién bloquea a quién" es entonces DENSO (un DAG
completo). Una primera versión recorría ese grafo recursivamente repitiendo el subárbol por cada camino
posible: el número de caminos crece exponencialmente (con 40 sesiones ~2^40) y el endpoint consumió
2.5 GB de RAM y paralizó la API justo durante el incidente que debía ayudar a diagnosticar.

SOLUCIÓN: un ÁRBOL DE EXPANSIÓN. Cada sesión aparece UNA sola vez, colgada de su bloqueador más
cercano a la raíz. Coste lineal en el número de sesiones, sin importar cuán densa sea la cola.
"""


def build_tree(sessions: list[dict]) -> dict:
    """`sessions`: dicts con al menos `pid` y `blocked_by` (lista de pids). Devuelve raíces, ciclos y resumen."""
    by_pid = {s["pid"]: s for s in sessions}
    blockers = {s["pid"]: [b for b in s["blocked_by"] if b in by_pid] for s in sessions}

    # Profundidad = distancia a una sesión NO bloqueada (0 = raíz = culpable). Se relaja por capas:
    # una sesión bloqueada por la raíz queda en profundidad 1 aunque además esté detrás de otras en la cola.
    depth = {pid: 0 for pid, bl in blockers.items() if not bl}
    changed = True
    while changed:
        changed = False
        for pid, bl in blockers.items():
            known = [depth[b] for b in bl if b in depth]
            if known and (pid not in depth or depth[pid] > min(known) + 1):
                depth[pid] = min(known) + 1
                changed = True

    # Padre = el bloqueador de menor profundidad (empate: el pid menor, para un resultado estable).
    children: dict[int, list[int]] = {}
    for pid, bl in blockers.items():
        if pid in depth and bl:
            parent = min((b for b in bl if b in depth), key=lambda b: (depth[b], b))
            children.setdefault(parent, []).append(pid)

    def node(pid: int) -> dict:
        s = by_pid[pid]
        kids = [node(c) for c in sorted(children.get(pid, []))]
        return {
            "pid": pid, "application_name": s.get("application_name"), "state": s.get("state"),
            "transaction_seconds": s.get("transaction_seconds"), "query": s.get("query"),
            "blocked_sessions": sum(1 + k["blocked_sessions"] for k in kids), "blocks": kids,
        }

    # Sesiones sin profundidad = están en un ciclo (deadlock en curso): no tienen "raíz" que las explique.
    cycle = sorted(pid for pid in blockers if pid not in depth)
    roots = [pid for pid, d in depth.items() if d == 0 and pid in children]
    tree = sorted((node(r) for r in roots), key=lambda n: -n["blocked_sessions"])
    culprit = tree[0] if tree else None
    if culprit:
        summary = (f"pid {culprit['pid']} ({culprit['application_name']}, {culprit['state']}) bloquea a "
                   f"{culprit['blocked_sessions']} sesión(es); su última consulta: {culprit['query']}")
    elif cycle:
        summary = f"deadlock en curso entre las sesiones {cycle} (PostgreSQL lo resolverá abortando una)"
    else:
        summary = "sin bloqueos"
    return {"roots": len(tree), "tree": tree, "cycle_sessions": cycle, "summary": summary}
