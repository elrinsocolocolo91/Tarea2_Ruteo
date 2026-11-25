# scripts/attr.py
import random
import statistics
import networkx as nx
from sqlalchemy import text
from scripts.config import MONTE_CARLO_ITERATIONS, MONTE_CARLO_SEED


def calcular_attr_grafo(G: nx.Graph, ids_nodos_clave: list[str]) -> float:
    """
    ATTR = (1 / C(n, 2)) * sum_{i < j} Z_{ij}
    donde Z_{ij} = 1 si existe camino entre los nodos clave i y j (Boettcher et al., 2018).
    Se evalúa eficientemente sobre componentes conexos: sum_r C(c_r, 2) / C(n, 2).
    """
    n = len(ids_nodos_clave)
    if n < 2:
        return 1.0

    conjunto_clave = set(ids_nodos_clave)
    total_pares = n * (n - 1) / 2.0
    pares_conectados = 0.0

    for comp in nx.connected_components(G):
        c = len(comp & conjunto_clave)
        if c >= 2:
            pares_conectados += c * (c - 1) / 2.0

    return pares_conectados / total_pares


def cargar_topologia_y_claves(engine):
    with engine.connect() as conn:
        enlaces = conn.execute(
            text("""
                SELECT id, source, target, COALESCE(prob_falla, 0.0), es_sintetico
                FROM enlaces
                WHERE source IS NOT NULL AND target IS NOT NULL;
            """)
        ).fetchall()

        nodos_clave = conn.execute(
            text("""
                SELECT id, nombre, rol, vertice_id, dist_snap_km
                FROM nodos_clave_ref
                ORDER BY id;
            """)
        ).fetchall()

    return enlaces, nodos_clave


def _construir_grafo_base(nodos_clave):
    """
    Representa cada nodo clave como un nodo lógico 'K_<id>' unido sin falla
    a su vértice topológico 'V_<vertice_id>' (si fue asociado dentro del umbral).
    Si vertice_id es NULL, queda como nodo aislado y penaliza correctamente el ATTR.
    """
    G = nx.Graph()
    ids_clave = []
    mapa_nombres = {}

    for nid, nombre, rol, vid, dist_snap in nodos_clave:
        k_id = f"K_{nid}"
        ids_clave.append(k_id)
        mapa_nombres[k_id] = nombre
        G.add_node(k_id)
        if vid is not None:
            v_id = f"V_{vid}"
            G.add_edge(k_id, v_id)

    return G, ids_clave, mapa_nombres


def calcular_attr_operacion(engine) -> dict:
    """Criterio 3.1: ATTR en operación normal (prob_falla = 0) y diagnóstico de conectividad."""
    enlaces, nodos_clave = cargar_topologia_y_claves(engine)
    G, ids_clave, mapa_nombres = _construir_grafo_base(nodos_clave)

    for _, u, v, _, _ in enlaces:
        G.add_edge(f"V_{u}", f"V_{v}")

    attr_val = calcular_attr_grafo(G, ids_clave)

    # Identificar componente principal y nodos clave desconectados
    componentes_clave = []
    conjunto_clave = set(ids_clave)
    for comp in nx.connected_components(G):
        claves_en_comp = sorted(comp & conjunto_clave)
        if claves_en_comp:
            componentes_clave.append(claves_en_comp)

    componentes_clave.sort(key=len, reverse=True)
    principal = set(componentes_clave[0]) if componentes_clave else set()
    desconectados = [mapa_nombres[k] for k in ids_clave if k not in principal]

    return {
        "attr_operacion": round(attr_val, 6),
        "total_nodos_clave": len(ids_clave),
        "componentes_con_nodos_clave": len(componentes_clave),
        "nodos_clave_en_componente_principal": len(principal),
        "nodos_clave_desconectados": desconectados,
    }


def calcular_attr_post_sismo(
    engine,
    simulaciones: int = MONTE_CARLO_ITERATIONS,
    seed: int = MONTE_CARLO_SEED,
) -> dict:
    """Criterio 5.0: Simulación Monte Carlo de ATTR tras el terremoto 8.8."""
    enlaces, nodos_clave = cargar_topologia_y_claves(engine)
    G_base, ids_clave, mapa_nombres = _construir_grafo_base(nodos_clave)
    rng = random.Random(seed)

    # Usamos Santiago (id=6) como nodo de referencia para medir aislamiento por nodo clave
    ref_santiago = "K_6" if "K_6" in mapa_nombres else ids_clave[0]
    veces_conectado_a_santiago = {k: 0 for k in ids_clave}

    resultados = []
    for _ in range(simulaciones):
        G = G_base.copy()
        for _, u, v, p_falla, _ in enlaces:
            if rng.random() >= p_falla:
                G.add_edge(f"V_{u}", f"V_{v}")

        resultados.append(calcular_attr_grafo(G, ids_clave))

        # Registrar qué nodos clave mantienen ruta hacia Santiago
        comp_santiago = nx.node_connected_component(G, ref_santiago)
        for k in ids_clave:
            if k in comp_santiago:
                veces_conectado_a_santiago[k] += 1

    media = statistics.mean(resultados)
    desv = statistics.stdev(resultados) if len(resultados) > 1 else 0.0

    conectividad_nodos = [
        {
            "nodo": mapa_nombres[k],
            "prob_conectado_santiago": round(veces_conectado_a_santiago[k] / simulaciones, 4),
        }
        for k in ids_clave
    ]
    conectividad_nodos.sort(key=lambda x: x["prob_conectado_santiago"])

    return {
        "attr_post_media": round(media, 6),
        "attr_post_std": round(desv, 6),
        "simulaciones": simulaciones,
        "seed": seed,
        "conectividad_por_nodo_clave": conectividad_nodos,
    }