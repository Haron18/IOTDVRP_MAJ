"""
Moteur de calcul du DVRP :
- get_osrm_distance_matrix : distances réelles routières (API publique OSRM), avec repli
  automatique sur une distance euclidienne si l'API est indisponible.
- get_osrm_route_shape : tracé réel de la route entre deux points (pour l'affichage carte).
- solve_dvrp_ortools : résolution du VRP avec capacité véhicule (Google OR-Tools).

Correctifs par rapport à la version d'origine :
- Ajout d'une VRAIE contrainte de capacité (les versions précédentes ignoraient le poids
  des commandes : un camion pouvait se voir assigner 10x sa capacité).
- Mise en cache (st.cache_data) des appels réseau pour éviter de re-solliciter OSRM à
  chaque interaction utilisateur (slider, etc.) → l'appli reste rapide et respecte le
  serveur public OSRM (gratuit mais limité en débit).
- Limite de temps de résolution pour ne jamais bloquer l'interface.
- Gestion propre du cas "pas de solution" (ex: capacité totale insuffisante).
"""

from __future__ import annotations

import numpy as np
import requests
import streamlit as st
from ortools.constraint_solver import pywrapcp, routing_enums_pb2

OSRM_BASE_URL = "http://router.project-osrm.org"


@st.cache_data(ttl=300, show_spinner=False)
def get_osrm_distance_matrix(coords: tuple[tuple[float, float], ...]) -> list[list[float]]:
    """Matrice de distances (en mètres) via l'API publique OSRM.
    `coords` est un tuple de (lat, lon) — un tuple pour être hashable par st.cache_data.
    Repli automatique sur une distance euclidienne approximative si l'API échoue.
    """
    coords_str = ";".join(f"{lon},{lat}" for lat, lon in coords)
    url = f"{OSRM_BASE_URL}/table/v1/driving/{coords_str}?annotations=distance"
    try:
        response = requests.get(url, timeout=6)
        response.raise_for_status()
        data = response.json()
        if data.get("code") == "Ok" and "distances" in data:
            return data["distances"]
    except Exception:
        pass  # on bascule sur le repli ci-dessous

    # Repli hors-ligne / API indisponible : distance euclidienne * 111 km par degré
    n = len(coords)
    matrix = np.zeros((n, n))
    pts = np.array(coords)
    for i in range(n):
        matrix[i] = np.linalg.norm(pts - pts[i], axis=1) * 111_000
    return matrix.tolist()


@st.cache_data(ttl=300, show_spinner=False)
def get_osrm_route_shape(coord1: tuple[float, float], coord2: tuple[float, float]) -> list[list[float]]:
    """Géométrie réelle de la route entre deux points (lat, lon) via OSRM.
    Repli sur une ligne droite si l'API échoue.
    """
    url = (f"{OSRM_BASE_URL}/route/v1/driving/"
           f"{coord1[1]},{coord1[0]};{coord2[1]},{coord2[0]}?overview=simplified&geometries=geojson")
    try:
        res = requests.get(url, timeout=4)
        res.raise_for_status()
        data = res.json()
        if data.get("code") == "Ok" and data.get("routes"):
            coords = data["routes"][0]["geometry"]["coordinates"]
            return [[lat, lon] for lon, lat in coords]
    except Exception:
        pass
    return [[coord1[0], coord1[1]], [coord2[0], coord2[1]]]


def route_distance(route: list[int], distance_matrix: list[list[float]]) -> float:
    """Distance totale (mètres) d'une tournée = somme des trajets consécutifs."""
    return sum(distance_matrix[route[i]][route[i + 1]] for i in range(len(route) - 1))


def naive_baseline_routes(
    demands: list[int], vehicle_capacities: list[int], depot_index: int = 0
) -> list[list[int]]:
    """Construit une tournée « naïve » de référence : chaque commande est affectée au
    premier véhicule disponible, dans l'ordre où elle apparaît dans les données —
    SANS aucune optimisation de séquence ni de répartition.

    Sert uniquement à mesurer, par comparaison, le gain réellement apporté par
    OR-Tools (affiché dans l'interface : distance optimisée vs distance de référence).
    """
    routes = [[depot_index] for _ in vehicle_capacities]
    loads = [0] * len(vehicle_capacities)
    v = 0
    for node in range(len(demands)):
        if node == depot_index:
            continue
        attempts = 0
        placed = False
        while attempts < len(vehicle_capacities):
            if loads[v] + demands[node] <= vehicle_capacities[v]:
                routes[v].append(node)
                loads[v] += demands[node]
                placed = True
                break
            v = (v + 1) % len(vehicle_capacities)
            attempts += 1
        if not placed:
            # Capacité dépassée sur tous les véhicules : on force l'ajout quand même,
            # car ceci n'est qu'une référence de comparaison, pas une vraie solution.
            routes[v].append(node)
            loads[v] += demands[node]
    for r in routes:
        r.append(depot_index)
    return routes


def group_multi_trip_routes(
    virtual_routes: list[list[int]], num_physical_vehicles: int, depot_index: int = 0
) -> dict[int, list[list[int]]]:
    """Regroupe des tournées « virtuelles » (issues du solveur) en rotations successives
    pour un nombre réduit de camions physiques.

    Principe : on résout le VRP avec `num_physical_vehicles * trajets_max` véhicules
    virtuels identiques (même capacité), ce qui autorise le solveur à répartir les
    commandes sur plusieurs allers-retours. On regroupe ensuite les tournées non vides
    par camion physique (index virtuel modulo nombre de camions physiques), dans l'ordre
    où elles doivent être effectuées (le camion revient au dépôt entre deux tournées).

    Hypothèse simplificatrice assumée : le temps de rechargement au dépôt entre deux
    rotations n'est pas modélisé (recharge supposée instantanée).

    Retourne {index_camion_physique: [tournée_1, tournée_2, ...]} — un camion sans
    rotation nécessaire n'apparaît pas dans le dictionnaire (reste au dépôt).
    """
    grouped: dict[int, list[list[int]]] = {}
    for v_idx, route in enumerate(virtual_routes):
        if len(route) <= 2:  # tournée vide : [depot, depot], rien à livrer
            continue
        physical_idx = v_idx % num_physical_vehicles
        grouped.setdefault(physical_idx, []).append(route)
    return grouped


def solve_dvrp_ortools(
    distance_matrix: list[list[float]],
    demands: list[int],
    vehicle_capacities: list[int],
    depot_index: int = 0,
    time_limit_s: int = 5,
) -> list[list[int]]:
    """Résolution du VRP avec contrainte de capacité.

    `demands[i]` = poids (kg) du point i (demands[depot_index] doit valoir 0).
    `vehicle_capacities[k]` = capacité max (kg) du véhicule k.
    Retourne une liste de tournées (chaque tournée = liste d'index dans distance_matrix),
    ou une liste vide si aucune solution n'a été trouvée (ex: capacité totale insuffisante).
    """
    num_vehicles = len(vehicle_capacities)
    manager = pywrapcp.RoutingIndexManager(len(distance_matrix), num_vehicles, depot_index)
    routing = pywrapcp.RoutingModel(manager)

    def distance_callback(from_index, to_index):
        from_node = manager.IndexToNode(from_index)
        to_node = manager.IndexToNode(to_index)
        return int(distance_matrix[from_node][to_node])

    transit_callback_index = routing.RegisterTransitCallback(distance_callback)
    routing.SetArcCostEvaluatorOfAllVehicles(transit_callback_index)

    def demand_callback(from_index):
        from_node = manager.IndexToNode(from_index)
        return int(demands[from_node])

    demand_callback_index = routing.RegisterUnaryTransitCallback(demand_callback)
    routing.AddDimensionWithVehicleCapacity(
        demand_callback_index,
        0,                      # pas de "slack" (marge) sur la capacité
        vehicle_capacities,     # capacité max par véhicule
        True,                   # le compteur repart de 0 au dépôt
        "Capacity",
    )

    search_parameters = pywrapcp.DefaultRoutingSearchParameters()
    search_parameters.first_solution_strategy = (
        routing_enums_pb2.FirstSolutionStrategy.PATH_CHEAPEST_ARC
    )
    search_parameters.local_search_metaheuristic = (
        routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
    )
    search_parameters.time_limit.FromSeconds(time_limit_s)

    solution = routing.SolveWithParameters(search_parameters)

    routes: list[list[int]] = []
    if solution:
        for vehicle_id in range(num_vehicles):
            index = routing.Start(vehicle_id)
            route = []
            while not routing.IsEnd(index):
                route.append(manager.IndexToNode(index))
                index = solution.Value(routing.NextVar(index))
            route.append(manager.IndexToNode(index))
            routes.append(route)
    return routes
