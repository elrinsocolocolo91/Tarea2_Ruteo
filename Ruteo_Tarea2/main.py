
from pathlib import Path
import psycopg2
import uvicorn
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from sqlalchemy import create_engine, text

from scripts.config import (
    ADMIN_DB_URL,
    DB_HOST,
    DB_NAME,
    DB_PASS,
    DB_PORT,
    DB_URL,
    DB_USER,
    MAX_SNAP_DIST_KM,
    MONTE_CARLO_ITERATIONS,
    MONTE_CARLO_SEED,
    TOPOLOGY_TOLERANCE,
)
from scripts.modelo_falla import actualizar_probabilidades_en_bd
from scripts.attr import calcular_attr_operacion, calcular_attr_post_sismo

engine = create_engine(DB_URL)
app = FastAPI(title="Resiliencia Infraestructura Internet Chile - Terremoto 8.8 (27F)")

# Cache en memoria con los resultados exactos calculados al iniciar main.py
RESULTADOS_PIPELINE = {}


def asegurar_y_cargar_bd():
    """Paso 1: Crea la BD si no existe y carga main.sql desde cero."""
    admin_engine = create_engine(ADMIN_DB_URL, isolation_level="AUTOCOMMIT")
    with admin_engine.connect() as conn:
        existe = conn.execute(
            text("SELECT 1 FROM pg_database WHERE datname = :name"),
            {"name": DB_NAME},
        ).scalar()
        if not existe:
            conn.execute(text(f'CREATE DATABASE "{DB_NAME}";'))
            print(f"[1/5] Base de datos '{DB_NAME}' creada automáticamente.")
        else:
            print(f"[1/5] Base de datos '{DB_NAME}' detectada.")
    admin_engine.dispose()

    ruta_sql = Path("main.sql")
    if not ruta_sql.exists():
        raise FileNotFoundError(
            "No existe 'main.sql' en la raíz. Ejecuta primero: python -m scripts.preparar_main_sql"
        )

    sql_contenido = ruta_sql.read_text(encoding="utf-8")
    conn_raw = psycopg2.connect(
        host=DB_HOST, port=DB_PORT, user=DB_USER, password=DB_PASS, dbname=DB_NAME
    )
    conn_raw.autocommit = True
    try:
        with conn_raw.cursor() as cur:
            cur.execute(sql_contenido)
    finally:
        conn_raw.close()
    print("[2/5] Archivo main.sql cargado exitosamente en PostgreSQL/PostGIS.")


def ejecutar_pipeline_completo() -> dict:
    """Ejecuta de extremo a extremo todo el flujo requerido por la pauta."""
    asegurar_y_cargar_bd()

    # Paso 2: Asegurar topología, prob_falla = 0 por defecto y mapeo de nodos clave
    with engine.begin() as conn:
        conn.execute(text("UPDATE enlaces SET prob_falla = 0.0;"))
        conn.execute(
            text(f"SELECT pgr_createTopology('enlaces', {TOPOLOGY_TOLERANCE}, 'geom', 'id', 'source', 'target', 'true', true);")
        )
        conn.execute(
            text(f"SELECT pgr_analyzeGraph('enlaces', {TOPOLOGY_TOLERANCE}, 'geom', 'id', 'source', 'target');")
        )
        conn.execute(
            text(f"""
                UPDATE nodos_clave_ref k
                SET
                    vertice_id = sub.vid,
                    dist_snap_km = ROUND((sub.dist_m / 1000.0)::numeric, 3)
                FROM (
                    SELECT
                        nk.id AS kid,
                        v.id AS vid,
                        ST_Distance(nk.geom::geography, v.the_geom::geography) AS dist_m
                    FROM nodos_clave_ref nk
                    CROSS JOIN LATERAL (
                        SELECT id, the_geom
                        FROM enlaces_vertices_pgr
                        ORDER BY the_geom <-> nk.geom
                        LIMIT 1
                    ) v
                ) sub
                WHERE k.id = sub.kid
                  AND (sub.dist_m / 1000.0) <= {MAX_SNAP_DIST_KM};
            """)
        )
        resumen_topo = conn.execute(text("""
            SELECT
                (SELECT COUNT(*) FROM enlaces) AS total_enlaces,
                (SELECT COUNT(*) FROM enlaces WHERE es_sintetico = TRUE) AS enlaces_sinteticos,
                (SELECT COUNT(*) FROM enlaces_vertices_pgr) AS total_vertices,
                (SELECT ROUND((SUM(ST_Length(geom::geography)) / 1000.0)::numeric, 2) FROM enlaces) AS total_km,
                (SELECT COUNT(DISTINCT component) FROM pgr_connectedComponents(
                    'SELECT id, source, target, 1.0 AS cost FROM enlaces WHERE source IS NOT NULL AND target IS NOT NULL'
                )) AS total_componentes;
        """)).mappings().first()

    # Paso 3: ATTR en operación (Antes del terremoto)
    res_antes = calcular_attr_operacion(engine)
    print(f"[3/5] Topología verificada ({resumen_topo['total_enlaces']} enlaces, "
          f"{resumen_topo['total_vertices']} vértices, {resumen_topo['total_km']} km, "
          f"{resumen_topo['total_componentes']} componentes conexos).")
    print(f"      ATTR en operación (Antes): {res_antes['attr_operacion']:.4f}")
    if res_antes["nodos_clave_desconectados"]:
        print(f"      Nodos clave desconectados en operación: {res_antes['nodos_clave_desconectados']}")

    # Paso 4: Modelo probabilístico de falla (Si & Midorikawa 1999 + ALA 2001)
    res_prob = actualizar_probabilidades_en_bd(engine)
    print(f"[4/5] Probabilidades de falla calculadas para {res_prob['total_enlaces']} enlaces ({res_prob['sismo']}).")
    print("      Top 5 enlaces MÁS afectados:")
    for e in res_prob["mas_afectados"]:
        print(f"        - ID {e['id']} ({e['nombre']}): dist={e['dist_km']} km, L={e['largo_km']} km, "
              f"PGV={e['pgv_cm_s']} cm/s, P_f={e['prob_falla']:.4f}")
    print("      Top 5 enlaces MENOS afectados:")
    for e in res_prob["menos_afectados"]:
        print(f"        - ID {e['id']} ({e['nombre']}): dist={e['dist_km']} km, L={e['largo_km']} km, "
              f"PGV={e['pgv_cm_s']} cm/s, P_f={e['prob_falla']:.6f}")

    # Paso 5: ATTR Post-Terremoto (Monte Carlo)
    res_despues = calcular_attr_post_sismo(
        engine, simulaciones=MONTE_CARLO_ITERATIONS, seed=MONTE_CARLO_SEED
    )
    antes = res_antes["attr_operacion"]
    despues = res_despues["attr_post_media"]
    diff_abs = antes - despues
    caida_pct = (diff_abs / antes * 100.0) if antes > 0 else 0.0

    print(f"[5/5] ATTR Post-Terremoto (N={res_despues['simulaciones']}, seed={res_despues['seed']}): "
          f"{despues:.4f} ± {res_despues['attr_post_std']:.4f}")
    print(f"      Comparación -> Diferencia: -{diff_abs:.4f} | Caída porcentual: -{caida_pct:.2f}%")
    print("      Conectividad hacia Santiago por nodo clave (post-sismo):")
    for nc in res_despues["conectividad_por_nodo_clave"]:
        print(f"        - {nc['nodo']}: {nc['prob_conectado_santiago'] * 100:.2f}%")

    RESULTADOS_PIPELINE.clear()
    RESULTADOS_PIPELINE.update({
        "topologia": dict(resumen_topo),
        "antes": res_antes,
        "probabilidades": res_prob,
        "despues": res_despues,
        "comparacion": {
            "diferencia_absoluta": round(diff_abs, 6),
            "caida_porcentual": round(caida_pct, 2),
        },
    })
    return RESULTADOS_PIPELINE


@app.get("/api/mapa")
def obtener_mapa():
    with engine.connect() as conn:
        enlaces = conn.execute(text("""
            SELECT json_build_object(
                'type', 'FeatureCollection',
                'features', COALESCE(json_agg(
                    json_build_object(
                        'type', 'Feature',
                        'geometry', ST_AsGeoJSON(geom)::json,
                        'properties', json_build_object(
                            'id', id,
                            'nombre', nombre,
                            'es_sintetico', es_sintetico,
                            'dist_km', COALESCE(dist_epicentro_km, 0),
                            'largo_km', COALESCE(largo_km, 0),
                            'pgv_cm_s', COALESCE(pgv_cm_s, 0),
                            'prob_falla', COALESCE(prob_falla, 0)
                        )
                    )
                ), '[]'::json)
            ) FROM enlaces;
        """)).scalar()

        nodos_clave = conn.execute(text("""
            SELECT json_build_object(
                'type', 'FeatureCollection',
                'features', COALESCE(json_agg(
                    json_build_object(
                        'type', 'Feature',
                        'geometry', ST_AsGeoJSON(geom)::json,
                        'properties', json_build_object(
                            'id', id,
                            'nombre', nombre,
                            'categoria', categoria,
                            'rol', rol,
                            'vertice_id', vertice_id,
                            'dist_snap_km', dist_snap_km
                        )
                    )
                ), '[]'::json)
            ) FROM nodos_clave_ref;
        """)).scalar()

        sismo = conn.execute(text("""
            SELECT json_build_object(
                'type', 'Feature',
                'geometry', ST_AsGeoJSON(geom)::json,
                'properties', json_build_object(
                    'event_id', event_id,
                    'title', title,
                    'place', place,
                    'mag', mag,
                    'depth_km', depth_km
                )
            ) FROM terremoto LIMIT 1;
        """)).scalar()

    return {"enlaces": enlaces, "nodos_clave": nodos_clave, "terremoto": sismo}


@app.get("/api/resultados")
def obtener_resultados():
    if not RESULTADOS_PIPELINE:
        return ejecutar_pipeline_completo()
    return RESULTADOS_PIPELINE


@app.get("/", response_class=HTMLResponse)
def index():
    return Path("static/index.html").read_text(encoding="utf-8")


if __name__ == "__main__":
    print("=== INICIANDO PIPELINE DE RESILIENCIA INTERNET CHILE (27F) ===")
    ejecutar_pipeline_completo()
    print("\n=== LEVANTANDO VISUALIZADOR LEAFLET EN http://127.0.0.1:8000 ===")
    uvicorn.run(app, host="127.0.0.1", port=8000)