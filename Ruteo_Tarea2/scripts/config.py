# scripts/config.py
import os

DB_HOST = os.getenv("DB_HOST", "localhost")
DB_PORT = os.getenv("DB_PORT", "5432")
DB_USER = os.getenv("DB_USER", "postgres")
DB_PASS = os.getenv("DB_PASS", "postgres")
DB_NAME = os.getenv("DB_NAME", "red_chile")

DB_URL = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
ADMIN_DB_URL = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/postgres"

# Tolerancia para pgr_createTopology:
# 0.001 grados ≈ 111 m en latitud (~90 m en longitud en Chile central).
# Suficiente para unir extremos digitalizados con leve desfase sin colapsar tramos distintos.
TOPOLOGY_TOLERANCE = 0.001

# Distancia máxima (km) para asociar un nodo clave de referencia al vértice topológico más cercano
MAX_SNAP_DIST_KM = 50.0

# Parámetros de simulación Monte Carlo (Criterios 3.0 y 5.0)
# Con N = 10.000, el error estándar acotado es SE <= 0.5 / sqrt(10000) = 0.005 (margen < ±0.01 al 95%)
MONTE_CARLO_ITERATIONS = 10000
MONTE_CARLO_SEED = 42

# Parámetros del modelo probabilístico (Criterio 4.1)
# Si & Midorikawa (1999): d = -0.02 para terremotos de subducción interplaca (tipo 27F)
FAULT_TYPE_D = -0.02
# ALA (2001): K1 = 0.5 para ductos flexibles de PVC/HDPE (tritubos de fibra óptica enterrada)
ALA_K1_PVC = 0.5

# Criterio 1.0: Nodos clave de la Internet chilena (ubicación, tipo y rol)
# Roles: Genera (origen de contenido / inyección internacional), Intercambia (IXP/PIT / enrutamiento troncal),
# Recibe (consumo y distribución regional)
NODOS_CLAVE = [
    {
        "id": 1,
        "nombre": "Arica (Aterrizaje Submarino SAm-1 / SAC)",
        "categoria": "Aterrizaje cable submarino / Troncal norte",
        "rol": "Genera / Intercambia / Recibe",
        "lat": -18.4783,
        "lon": -70.3126,
        "fuente": "Estudio Resiliencia Internet Chilena (NIC Chile) / Subtel",
    },
    {
        "id": 2,
        "nombre": "Iquique (Nodo Troncal Norte Grande)",
        "categoria": "Nodo troncal regional",
        "rol": "Intercambia / Recibe",
        "lat": -20.2307,
        "lon": -70.1357,
        "fuente": "Estudio Resiliencia Internet Chilena (NIC Chile)",
    },
    {
        "id": 3,
        "nombre": "Antofagasta (Hub Troncal Norte)",
        "categoria": "Nodo troncal regional",
        "rol": "Intercambia / Recibe",
        "lat": -23.6509,
        "lon": -70.3975,
        "fuente": "Estudio Resiliencia Internet Chilena (NIC Chile)",
    },
    {
        "id": 4,
        "nombre": "La Serena / Coquimbo (Nodo Troncal Centro-Norte)",
        "categoria": "Nodo troncal regional",
        "rol": "Intercambia / Recibe",
        "lat": -29.9027,
        "lon": -71.2519,
        "fuente": "Estudio Resiliencia Internet Chilena (NIC Chile)",
    },
    {
        "id": 5,
        "nombre": "Valparaíso / Concón (Aterrizajes Submarinos Internacionales)",
        "categoria": "Aterrizaje cable submarino (SAC, SAm-1, Curie, Mistral)",
        "rol": "Genera / Intercambia / Recibe",
        "lat": -33.0246,
        "lon": -71.5518,
        "fuente": "Estudio Resiliencia Internet Chilena (NIC Chile) / TeleGeography",
    },
    {
        "id": 6,
        "nombre": "Santiago (PIT Chile / IXP y Data Centers Principales)",
        "categoria": "IXP / PIT y Data Centers Tier III/IV",
        "rol": "Genera / Intercambia / Recibe",
        "lat": -33.4489,
        "lon": -70.6693,
        "fuente": "Estudio Resiliencia Internet Chilena (NIC Chile) / PIT Chile",
    },
    {
        "id": 7,
        "nombre": "Talca (Nodo Troncal Maule)",
        "categoria": "Nodo troncal regional",
        "rol": "Intercambia / Recibe",
        "lat": -35.4264,
        "lon": -71.6554,
        "fuente": "Estudio Resiliencia Internet Chilena (NIC Chile)",
    },
    {
        "id": 8,
        "nombre": "Concepción (Hub Troncal Biobío)",
        "categoria": "Nodo troncal regional / PIT regional",
        "rol": "Intercambia / Recibe",
        "lat": -36.8201,
        "lon": -73.0444,
        "fuente": "Estudio Resiliencia Internet Chilena (NIC Chile)",
    },
    {
        "id": 9,
        "nombre": "Temuco (Nodo Troncal Araucanía)",
        "categoria": "Nodo troncal regional",
        "rol": "Intercambia / Recibe",
        "lat": -38.7359,
        "lon": -72.5904,
        "fuente": "Estudio Resiliencia Internet Chilena (NIC Chile)",
    },
    {
        "id": 10,
        "nombre": "Valdivia (Nodo Troncal Los Ríos)",
        "categoria": "Nodo troncal regional",
        "rol": "Intercambia / Recibe",
        "lat": -39.8142,
        "lon": -73.2459,
        "fuente": "Estudio Resiliencia Internet Chilena (NIC Chile)",
    },
    {
        "id": 11,
        "nombre": "Puerto Montt (Hub Sur / Cabecera Austral)",
        "categoria": "Nodo troncal / Cabecera FOA",
        "rol": "Intercambia / Recibe",
        "lat": -41.4693,
        "lon": -72.9424,
        "fuente": "Estudio Resiliencia Internet Chilena (NIC Chile) / Subtel",
    },
    {
        "id": 12,
        "nombre": "Coyhaique (Nodo Troncal Aysén)",
        "categoria": "Nodo troncal austral",
        "rol": "Recibe / Intercambia",
        "lat": -45.5752,
        "lon": -72.0662,
        "fuente": "Estudio Resiliencia Internet Chilena (NIC Chile)",
    },
    {
        "id": 13,
        "nombre": "Punta Arenas (Nodo Troncal Magallanes / FOA)",
        "categoria": "Nodo troncal austral",
        "rol": "Recibe / Intercambia",
        "lat": -53.1638,
        "lon": -70.9171,
        "fuente": "Estudio Resiliencia Internet Chilena (NIC Chile) / Subtel",
    },
]