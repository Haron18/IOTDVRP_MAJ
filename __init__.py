"""
dvrp_map_component — Composant Streamlit personnalisé (React) : carte des tournées
DVRP avec animation fluide des camions, côté navigateur.

C'est un VRAI composant Streamlit (protocole officiel `components.declare_component`),
pas un simple `components.html()` : le frontend (dans frontend/build/) est une app
React compilée avec esbuild, qui communique avec Python via le protocole
bidirectionnel standard de Streamlit (`Streamlit.setComponentValue`, `args`, etc.).

Le bundle est PRÉ-COMPILÉ (frontend/build/bundle.js + bundle.css) et committé dans le
repo : Streamlit Cloud sert ces fichiers statiques tels quels, sans étape de build
npm côté serveur — aucune configuration supplémentaire nécessaire au déploiement.

Pour modifier le composant : éditez frontend/src/index.jsx, puis
`cd frontend && npm install && npm run build` avant de commit/push.
"""
import os

import streamlit.components.v1 as components

_BUILD_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "frontend", "build")

_dvrp_map_component = components.declare_component("dvrp_map", path=_BUILD_DIR)


def dvrp_map(depot_coords, orders, trucks, height: int = 520, key: str | None = None):
    """
    depot_coords : (lat, lon)
    orders : liste de dicts {id, client, lat, lon, demand_kg, temp_max, time_window, priority}
    trucks : liste de dicts, un par camion physique, avec :
      label, color, used (bool), shape ([[lat,lon],...]), trip_shapes (liste de tracés),
      total_km, traveled_km, speed_kmh, animate (bool), sim_minutes_per_real_second,
      status_label
    """
    return _dvrp_map_component(
        depot=list(depot_coords),
        orders=orders,
        trucks=trucks,
        height=height,
        key=key,
        default=None,
    )
