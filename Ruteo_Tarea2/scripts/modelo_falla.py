# scripts/modelo_falla.py
import math
from sqlalchemy import text
from scripts.config import FAULT_TYPE_D, ALA_K1_PVC


def calcular_probabilidad_enlace(dist_km: float, largo_km: float, mw: float, depth_km: float) -> tuple[float, float]:
    """
    Calcula PGV_600 (cm/s) según Si & Midorikawa (1999) para subducción interplaca
    y probabilidad de falla P_f en [0, 1) según ALA (2001) + proceso de Poisson espacial.
    """
    # 1. Atenuación PGV_600 (cm/s) - Si & Midorikawa (1999)
    termino_fuente = 0.0028 * (10 ** (0.50 * mw))
    log10_pgv = (
        0.58 * mw
        + 0.0038 * depth_km
        + FAULT_TYPE_D
        - 1.29
        - math.log10(max(dist_km, 0.0) + termino_fuente)
        - 0.002 * max(dist_km, 0.0)
    )
    pgv_cm_s = 10 ** log10_pgv

    # 2. Tasa de reparación por km - ALA (2001)
    # RR = K1 * 0.00187 * PGV(in/s) fallas / 1000 ft
    # Factor conversión: 0.00187 / (0.3048 km * 2.54 cm/in) = 0.00241542
    rr_km = ALA_K1_PVC * (0.00187 / (0.3048 * 2.54)) * pgv_cm_s

    # 3. Probabilidad de al menos una falla en el enlace de longitud L (km)
    largo_eff = max(largo_km, 0.001)
    prob_falla = 1.0 - math.exp(-rr_km * largo_eff)
    prob_falla = min(max(prob_falla, 0.0), 1.0)
    return pgv_cm_s, prob_falla


def actualizar_probabilidades_en_bd(engine) -> dict:
    """
    Calcula distancias y longitudes métricas en PostGIS (geography -> metros -> km),
    aplica el modelo probabilístico a cada enlace y actualiza la tabla 'enlaces'.
    """
    with engine.connect() as conn:
        sismo = conn.execute(
            text("SELECT mag, depth_km, title FROM terremoto LIMIT 1;")
        ).fetchone()
        if not sismo:
            raise RuntimeError("No se encontró ningún registro en la tabla 'terremoto'.")
        mw, depth_km, title = float(sismo[0]), float(sismo[1]), sismo[2]

        filas = conn.execute(
            text("""
                SELECT
                    e.id,
                    e.nombre,
                    ST_Distance(e.geom::geography, t.geom::geography) / 1000.0 AS dist_km,
                    ST_Length(e.geom::geography) / 1000.0 AS largo_km
                FROM enlaces e
                CROSS JOIN (SELECT geom FROM terremoto LIMIT 1) t
                ORDER BY e.id;
            """)
        ).fetchall()

    updates = []
    for eid, nombre, dist_km, largo_km in filas:
        pgv, prob = calcular_probabilidad_enlace(dist_km, largo_km, mw, depth_km)
        updates.append({
            "id": eid,
            "nombre": nombre,
            "dist_km": round(dist_km, 3),
            "largo_km": round(largo_km, 3),
            "pgv_cm_s": round(pgv, 4),
            "prob_falla": round(prob, 6),
        })

    with engine.begin() as conn:
        conn.execute(
            text("""
                UPDATE enlaces
                SET dist_epicentro_km = :dist_km,
                    largo_km = :largo_km,
                    pgv_cm_s = :pgv_cm_s,
                    prob_falla = :prob_falla
                WHERE id = :id;
            """),
            updates,
        )

    ordenados = sorted(updates, key=lambda x: x["prob_falla"], reverse=True)
    return {
        "sismo": title,
        "total_enlaces": len(updates),
        "mas_afectados": ordenados[:5],
        "menos_afectados": ordenados[-5:][::-1],
    }