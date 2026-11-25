# scripts/preparar_main_sql.py
import json
from pathlib import Path
import geopandas as gpd
import numpy as np
from shapely import STRtree, force_2d
from shapely.geometry import LineString
from shapely.ops import unary_union
from sqlalchemy import create_engine, text

from scripts.config import (
    ADMIN_DB_URL,
    DB_NAME,
    DB_URL,
    MAX_SNAP_DIST_KM,
    NODOS_CLAVE,
    TOPOLOGY_TOLERANCE,
)
from scripts.modelo_falla import actualizar_probabilidades_en_bd


def sql_str(val) -> str:
    if val is None:
        return "NULL"
    return "'" + str(val).replace("'", "''") + "'"


def extraer_linestrings(geom) -> list[LineString]:
    if geom is None or geom.is_empty:
        return []
    if geom.geom_type == "LineString":
        return [geom] if len(geom.coords) >= 2 and geom.length > 0 else []
    if geom.geom_type in ("MultiLineString", "GeometryCollection"):
        res = []
        for g in geom.geoms:
            res.extend(extraer_linestrings(g))
        return res
    return []


def asegurar_base_de_datos():
    admin_engine = create_engine(ADMIN_DB_URL, isolation_level="AUTOCOMMIT")
    with admin_engine.connect() as conn:
        existe = conn.execute(
            text("SELECT 1 FROM pg_database WHERE datname = :name"),
            {"name": DB_NAME},
        ).scalar()
        if not existe:
            conn.execute(text(f'CREATE DATABASE "{DB_NAME}";'))
            print(f"Base de datos '{DB_NAME}' creada.")
    admin_engine.dispose()


def clasificar_archivo(stem: str) -> tuple[str, bool]:
    s = stem.lower()
    if "fibra" in s:
        return "FibraOptica", False
    if "sin_postacion" in s or "camino" in s:
        return "CaminoSinPostacion", True
    if "postacion" in s:
        return "PostacionElectrica", True
    if "ferry" in s:
        return "TrayectoFerry", True
    return stem, True


def cargar_lineas_desde_carpeta(carpeta_data: Path) -> list[tuple[LineString, str, bool]]:
    shps = sorted(carpeta_data.glob("*.shp"))
    if not shps:
        raise FileNotFoundError("No se encontró ningún archivo .shp dentro de data/.")

    capas = []
    todas_crudas = []
    for shp_path in shps:
        print(f"Leyendo Shapefile: {shp_path}")
        gdf = gpd.read_file(shp_path)
        print(f"  -> CRS: {gdf.crs} | Registros: {len(gdf)} | Geometrías: {gdf.geom_type.unique().tolist()}")
        gdf = gdf.set_crs(epsg=4326) if gdf.crs is None else gdf.to_crs(epsg=4326)
        gdf = gdf.explode(index_parts=False).reset_index(drop=True)

        etiqueta, es_sint = clasificar_archivo(shp_path.stem)
        lineas_capa = []
        for geom in gdf.geometry:
            if geom is not None and not geom.is_empty:
                lineas_capa.extend(extraer_linestrings(force_2d(geom)))

        if lineas_capa:
            capas.append((etiqueta, es_sint, lineas_capa, STRtree(lineas_capa)))
            todas_crudas.extend(lineas_capa)

    if not todas_crudas:
        raise RuntimeError("No se encontraron geometrías LineString válidas en data/.")

    # Priorizar FibraOptica (es_sintetico=False) ante eventuales solapes
    capas.sort(key=lambda c: c[1])

    noded = unary_union(todas_crudas)
    lineas_noded = extraer_linestrings(noded)
    print(f"Segmentos tras noding (cruces/T-junctions): {len(todas_crudas)} -> {len(lineas_noded)}")

    resultado = []
    for ls in lineas_noded:
        mid = ls.interpolate(0.5, normalized=True)
        etiqueta_final, sint_final = "Complementario", True
        for etiqueta, es_sint, geoms_capa, tree in capas:
            idx_cercano = int(tree.nearest(mid))
            if geoms_capa[idx_cercano].distance(mid) < 1e-6:
                etiqueta_final, sint_final = etiqueta, es_sint
                break
        resultado.append((ls, etiqueta_final, sint_final))

    return resultado


def calcular_puentes_mst(conn) -> list[tuple[int, int, tuple[float, float], tuple[float, float], float]]:
    """
    Calcula el Árbol de Expansión Mínima (Prim) entre componentes conexos usando
    coordenadas métricas UTM 19S (EPSG:32719) para conectar la red con la mínima
    extensión posible de enlaces sintéticos.
    """
    rows = conn.execute(text("""
        SELECT
            c.node AS vid,
            c.component AS comp_id,
            ST_X(ST_Transform(v.the_geom, 32719)) AS x_m,
            ST_Y(ST_Transform(v.the_geom, 32719)) AS y_m,
            ST_X(v.the_geom) AS lon,
            ST_Y(v.the_geom) AS lat
        FROM pgr_connectedComponents(
            'SELECT id, source, target, 1.0 AS cost FROM enlaces WHERE source IS NOT NULL AND target IS NOT NULL'
        ) c
        JOIN enlaces_vertices_pgr v ON v.id = c.node
        ORDER BY c.node;
    """)).fetchall()

    vids = np.array([r[0] for r in rows], dtype=int)
    comps = np.array([r[1] for r in rows], dtype=int)
    coords_m = np.array([[r[2], r[3]] for r in rows], dtype=float)
    coords_ll = [(float(r[4]), float(r[5])) for r in rows]

    unique_comps, counts = np.unique(comps, return_counts=True)
    if len(unique_comps) <= 1:
        return []

    comp_inicial = unique_comps[np.argmax(counts)]
    in_tree = comps == comp_inicial
    n_v = len(vids)
    best_dist = np.full(n_v, np.inf)
    best_src = np.full(n_v, -1, dtype=int)

    def actualizar_distancias(nuevos_idx):
        fuera_idx = np.where(~in_tree)[0]
        if len(fuera_idx) == 0:
            return
        for u in nuevos_idx:
            dx = coords_m[fuera_idx, 0] - coords_m[u, 0]
            dy = coords_m[fuera_idx, 1] - coords_m[u, 1]
            d = np.hypot(dx, dy)
            mejora = d < best_dist[fuera_idx]
            idx_mejora = fuera_idx[mejora]
            best_dist[idx_mejora] = d[mejora]
            best_src[idx_mejora] = u

    actualizar_distancias(np.where(in_tree)[0])
    puentes = []

    MAX_MST_BRIDGE_KM = 500.0  # Excluye territorios insulares oceánicos (ej. Rapa Nui a ~3.500 km)

    while not np.all(in_tree):
        fuera_idx = np.where(~in_tree)[0]
        v_local = int(np.argmin(best_dist[fuera_idx]))
        v_idx = int(fuera_idx[v_local])
        u_idx = int(best_src[v_idx])
        dist_km = float(best_dist[v_idx]) / 1000.0

        # Si el componente más cercano restante supera 500 km (ej. Rapa Nui), detener el puenteo
        if dist_km > MAX_MST_BRIDGE_KM:
            print(f"[Puentes sintéticos MST] Detenido: componente insular restante a {dist_km:.1f} km (> {MAX_MST_BRIDGE_KM} km).")
            break

        if coords_ll[u_idx] != coords_ll[v_idx]:
            puentes.append((
                int(vids[u_idx]),
                int(vids[v_idx]),
                coords_ll[u_idx],
                coords_ll[v_idx],
                dist_km,
            ))

        nueva_comp = comps[v_idx]
        nuevos_idx = np.where(comps == nueva_comp)[0]
        in_tree[nuevos_idx] = True
        actualizar_distancias(nuevos_idx)
    return puentes


def construir_y_exportar_main_sql(
    ruta_sismo: str = "data/query.json",
    salida_sql: str = "main.sql",
):
    asegurar_base_de_datos()
    engine = create_engine(DB_URL)

    lineas_saip = cargar_lineas_desde_carpeta(Path("data"))

    with engine.begin() as conn:
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS postgis;"))
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS pgrouting;"))
        conn.execute(text("DROP TABLE IF EXISTS enlaces_vertices_pgr CASCADE;"))
        conn.execute(text("DROP TABLE IF EXISTS enlaces CASCADE;"))
        conn.execute(text("DROP TABLE IF EXISTS terremoto CASCADE;"))
        conn.execute(text("DROP TABLE IF EXISTS nodos_clave_ref CASCADE;"))

        conn.execute(text("""
            CREATE TABLE enlaces (
                id SERIAL PRIMARY KEY,
                nombre TEXT,
                es_sintetico BOOLEAN DEFAULT FALSE,
                source INTEGER,
                target INTEGER,
                dist_epicentro_km DOUBLE PRECISION DEFAULT 0.0,
                largo_km DOUBLE PRECISION DEFAULT 0.0,
                pgv_cm_s DOUBLE PRECISION DEFAULT 0.0,
                prob_falla DOUBLE PRECISION DEFAULT 0.0,
                geom GEOMETRY(LineString, 4326)
            );
            CREATE INDEX idx_enlaces_geom ON enlaces USING GIST (geom);

            CREATE TABLE terremoto (
                id SERIAL PRIMARY KEY,
                event_id TEXT,
                title TEXT,
                place TEXT,
                mag DOUBLE PRECISION,
                depth_km DOUBLE PRECISION,
                geom GEOMETRY(Point, 4326)
            );

            CREATE TABLE nodos_clave_ref (
                id INTEGER PRIMARY KEY,
                nombre TEXT,
                categoria TEXT,
                rol TEXT,
                fuente TEXT,
                vertice_id INTEGER,
                dist_snap_km DOUBLE PRECISION,
                geom GEOMETRY(Point, 4326)
            );
        """))

        # 1. Insertar enlaces de las capas SAIP (solo FibraOptica lleva es_sintetico = FALSE)
        for idx, (ls, etiqueta, es_sint) in enumerate(lineas_saip, start=1):
            conn.execute(
                text("""
                    INSERT INTO enlaces (id, nombre, es_sintetico, prob_falla, geom)
                    VALUES (:id, :nombre, :sint, 0.0, ST_GeomFromText(:wkt, 4326));
                """),
                {"id": idx, "nombre": f"{etiqueta}_{idx}", "sint": es_sint, "wkt": ls.wkt},
            )

        # 2. Topología inicial SOLO con datos SAIP (para reportar diagnóstico crudo en el informe)
        conn.execute(
            text(f"SELECT pgr_createTopology('enlaces', {TOPOLOGY_TOLERANCE}, 'geom', 'id', 'source', 'target', 'true', true);")
        )
        comp_saip = conn.execute(text("""
            SELECT COUNT(DISTINCT component)
            FROM pgr_connectedComponents(
                'SELECT id, source, target, 1.0 AS cost FROM enlaces WHERE source IS NOT NULL AND target IS NOT NULL'
            );
        """)).scalar()
        km_saip = conn.execute(text(
            "SELECT ROUND((SUM(ST_Length(geom::geography)) / 1000.0)::numeric, 2) FROM enlaces;"
        )).scalar()
        print(f"[Diagnóstico SAIP crudo] Enlaces: {len(lineas_saip)} | Extensión: {km_saip} km | Componentes conexos: {comp_saip}")

        # 3. Agregar puentes sintéticos MST (es_sintetico = TRUE) para cerrar brechas SAIP
        puentes = calcular_puentes_mst(conn)
        next_id = len(lineas_saip) + 1
        for k, (u_vid, v_vid, ll_u, ll_v, dist_km) in enumerate(puentes, start=1):
            wkt_puente = LineString([ll_u, ll_v]).wkt
            conn.execute(
                text("""
                    INSERT INTO enlaces (id, nombre, es_sintetico, prob_falla, geom)
                    VALUES (:id, :nombre, TRUE, 0.0, ST_GeomFromText(:wkt, 4326));
                """),
                {
                    "id": next_id,
                    "nombre": f"PuenteMST_{k}_v{u_vid}_v{v_vid}",
                    "wkt": wkt_puente,
                },
            )
            next_id += 1

        # 4. Reconstruir topología unificada (SAIP + enlaces sintéticos)
        res_topo = conn.execute(
            text(f"SELECT pgr_createTopology('enlaces', {TOPOLOGY_TOLERANCE}, 'geom', 'id', 'source', 'target', 'true', true);")
        ).scalar()
        res_anal = conn.execute(
            text(f"SELECT pgr_analyzeGraph('enlaces', {TOPOLOGY_TOLERANCE}, 'geom', 'id', 'source', 'target');")
        ).scalar()
        comp_final = conn.execute(text("""
            SELECT COUNT(DISTINCT component)
            FROM pgr_connectedComponents(
                'SELECT id, source, target, 1.0 AS cost FROM enlaces WHERE source IS NOT NULL AND target IS NOT NULL'
            );
        """)).scalar()
        km_sint = conn.execute(text(
            "SELECT ROUND(COALESCE(SUM(ST_Length(geom::geography)) / 1000.0, 0)::numeric, 2) FROM enlaces WHERE nombre LIKE 'PuenteMST_%';"
        )).scalar()
        print(f"[Puentes sintéticos MST] Agregados: {len(puentes)} ({km_sint} km) | Componentes finales: {comp_final}")
        print(f"pgr_createTopology: {res_topo} | pgr_analyzeGraph: {res_anal}")

        # 5. Insertar terremoto 8.8 desde data/query.json
        sismo_raw = json.loads(Path(ruta_sismo).read_text(encoding="utf-8"))
        feat = sismo_raw["features"][0]
        lon, lat, depth_km = feat["geometry"]["coordinates"]
        props = feat["properties"]
        conn.execute(
            text("""
                INSERT INTO terremoto (event_id, title, place, mag, depth_km, geom)
                VALUES (:eid, :title, :place, :mag, :depth, ST_SetSRID(ST_MakePoint(:lon, :lat), 4326));
            """),
            {
                "eid": feat["id"],
                "title": props["title"],
                "place": props["place"],
                "mag": float(props["mag"]),
                "depth": float(depth_km),
                "lon": float(lon),
                "lat": float(lat),
            },
        )

        # 6. Insertar nodos clave y asociarlos a su vértice más cercano
        for nk in NODOS_CLAVE:
            conn.execute(
                text("""
                    INSERT INTO nodos_clave_ref (id, nombre, categoria, rol, fuente, geom)
                    VALUES (:id, :nombre, :cat, :rol, :fuente, ST_SetSRID(ST_MakePoint(:lon, :lat), 4326));
                """),
                {
                    "id": nk["id"],
                    "nombre": nk["nombre"],
                    "cat": nk["categoria"],
                    "rol": nk["rol"],
                    "fuente": nk["fuente"],
                    "lon": nk["lon"],
                    "lat": nk["lat"],
                },
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

    # 7. Calcular probabilidades de falla y exportar main.sql autocontenido en UTF-8
    res_prob = actualizar_probabilidades_en_bd(engine)
    print(f"Probabilidades calculadas para {res_prob['total_enlaces']} enlaces.")

    lineas = [
        "-- =============================================================================",
        "-- main.sql - Infraestructura Internet Chile + Sismo 27F + Probabilidades de Falla",
        "-- Autocontenido: carga directamente sobre una base de datos PostgreSQL vacía.",
        "-- =============================================================================",
        "CREATE EXTENSION IF NOT EXISTS postgis;",
        "CREATE EXTENSION IF NOT EXISTS pgrouting;",
        "",
        "DROP TABLE IF EXISTS enlaces_vertices_pgr CASCADE;",
        "DROP TABLE IF EXISTS enlaces CASCADE;",
        "DROP TABLE IF EXISTS terremoto CASCADE;",
        "DROP TABLE IF EXISTS nodos_clave_ref CASCADE;",
        "",
        "CREATE TABLE enlaces (",
        "    id SERIAL PRIMARY KEY,",
        "    nombre TEXT,",
        "    es_sintetico BOOLEAN DEFAULT FALSE,",
        "    source INTEGER,",
        "    target INTEGER,",
        "    dist_epicentro_km DOUBLE PRECISION DEFAULT 0.0,",
        "    largo_km DOUBLE PRECISION DEFAULT 0.0,",
        "    pgv_cm_s DOUBLE PRECISION DEFAULT 0.0,",
        "    prob_falla DOUBLE PRECISION DEFAULT 0.0,",
        "    geom GEOMETRY(LineString, 4326)",
        ");",
        "CREATE INDEX idx_enlaces_geom ON enlaces USING GIST (geom);",
        "",
        "CREATE TABLE terremoto (",
        "    id SERIAL PRIMARY KEY,",
        "    event_id TEXT,",
        "    title TEXT,",
        "    place TEXT,",
        "    mag DOUBLE PRECISION,",
        "    depth_km DOUBLE PRECISION,",
        "    geom GEOMETRY(Point, 4326)",
        ");",
        "",
        "CREATE TABLE nodos_clave_ref (",
        "    id INTEGER PRIMARY KEY,",
        "    nombre TEXT,",
        "    categoria TEXT,",
        "    rol TEXT,",
        "    fuente TEXT,",
        "    vertice_id INTEGER,",
        "    dist_snap_km DOUBLE PRECISION,",
        "    geom GEOMETRY(Point, 4326)",
        ");",
        "",
        "BEGIN;",
    ]

    with engine.connect() as conn:
        enlaces_db = conn.execute(text("""
            SELECT id, nombre, es_sintetico, source, target,
                   dist_epicentro_km, largo_km, pgv_cm_s, prob_falla, ST_AsText(geom)
            FROM enlaces ORDER BY id;
        """)).fetchall()

        for eid, nombre, sint, src, tgt, dist_km, largo_km, pgv, p_falla, wkt in enlaces_db:
            lineas.append(
                f"INSERT INTO enlaces (id, nombre, es_sintetico, source, target, dist_epicentro_km, largo_km, pgv_cm_s, prob_falla, geom) "
                f"VALUES ({eid}, {sql_str(nombre)}, {'TRUE' if sint else 'FALSE'}, "
                f"{src if src is not None else 'NULL'}, {tgt if tgt is not None else 'NULL'}, "
                f"{dist_km:.3f}, {largo_km:.3f}, {pgv:.4f}, {p_falla:.6f}, ST_GeomFromText({sql_str(wkt)}, 4326));"
            )

        sismos_db = conn.execute(text("""
            SELECT event_id, title, place, mag, depth_km, ST_AsText(geom)
            FROM terremoto;
        """)).fetchall()
        for eid, title, place, mag, depth, wkt in sismos_db:
            lineas.append(
                f"INSERT INTO terremoto (event_id, title, place, mag, depth_km, geom) "
                f"VALUES ({sql_str(eid)}, {sql_str(title)}, {sql_str(place)}, {mag}, {depth}, ST_GeomFromText({sql_str(wkt)}, 4326));"
            )

        nodos_db = conn.execute(text("""
            SELECT id, nombre, categoria, rol, fuente, vertice_id, dist_snap_km, ST_AsText(geom)
            FROM nodos_clave_ref ORDER BY id;
        """)).fetchall()
        for nid, nombre, cat, rol, fuente, vid, dsnap, wkt in nodos_db:
            lineas.append(
                f"INSERT INTO nodos_clave_ref (id, nombre, categoria, rol, fuente, vertice_id, dist_snap_km, geom) "
                f"VALUES ({nid}, {sql_str(nombre)}, {sql_str(cat)}, {sql_str(rol)}, {sql_str(fuente)}, "
                f"{vid if vid is not None else 'NULL'}, {dsnap if dsnap is not None else 'NULL'}, "
                f"ST_GeomFromText({sql_str(wkt)}, 4326));"
            )

        total_km = conn.execute(text(
            "SELECT ROUND((SUM(ST_Length(geom::geography)) / 1000.0)::numeric, 2) FROM enlaces;"
        )).scalar()
        n_vertices = conn.execute(text("SELECT COUNT(*) FROM enlaces_vertices_pgr;")).scalar()

        desglose = conn.execute(text("""
            SELECT
                split_part(nombre, '_', 1) AS capa,
                es_sintetico,
                COUNT(*) AS n_tramos,
                ROUND((SUM(ST_Length(geom::geography)) / 1000.0)::numeric, 2) AS km
            FROM enlaces
            GROUP BY 1, 2
            ORDER BY es_sintetico, km DESC;
        """)).fetchall()

    lineas.extend([
        "COMMIT;",
        "",
        f"SELECT pgr_createTopology('enlaces', {TOPOLOGY_TOLERANCE}, 'geom', 'id', 'source', 'target', 'true', true);",
        f"SELECT pgr_analyzeGraph('enlaces', {TOPOLOGY_TOLERANCE}, 'geom', 'id', 'source', 'target');",
        "",
    ])

    Path(salida_sql).write_text("\n".join(lineas), encoding="utf-8")

    print("\n[Desglose por capa para el informe]")
    for capa, sint, n_tramos, km in desglose:
        tipo = "Sintético/Complementario" if sint else "Fibra óptica confirmada"
        print(f"  - {capa:20s} ({tipo}): {n_tramos:4d} tramos | {km} km")

    print(f"\n[OK] '{salida_sql}' regenerado exitosamente.")
    print(f"     Total enlaces: {len(enlaces_db)} (SAIP: {len(lineas_saip)}, Puentes MST: {len(puentes)}) | Vértices: {n_vertices} | Extensión total: {total_km} km")


if __name__ == "__main__":
    construir_y_exportar_main_sql()