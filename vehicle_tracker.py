"""
Simulation LOCALE du tracking des véhicules le long de leur(s) tournée(s).

Complète la télémétrie MQTT (qui simule des messages GPS/IoT génériques, sans lien
direct avec les tournées calculées) : ici, la position affichée sur la carte suit
précisément le tracé réel (OSRM) de la rotation complète de chaque camion, y compris
lorsqu'il effectue plusieurs trajets successifs (retour au dépôt, rechargement,
nouveau départ) — c'est ce qui permet de "voir" concrètement les allers-retours
quand la capacité d'un camion est insuffisante pour tout livrer en un seul trajet.

Aucune connexion réseau n'est nécessaire : la position est calculée par
interpolation géométrique sur les tracés déjà obtenus via OSRM.
"""

from __future__ import annotations

import math


def _haversine_km(p1: tuple[float, float], p2: tuple[float, float]) -> float:
    """Distance à vol d'oiseau (km) entre deux points (lat, lon)."""
    lat1, lon1 = p1
    lat2, lon2 = p2
    r = 6371.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def route_length_km(shape_coords: list[list[float]]) -> float:
    """Longueur totale d'un tracé, donné comme une liste de points [lat, lon]."""
    if len(shape_coords) < 2:
        return 0.0
    return sum(
        _haversine_km(tuple(shape_coords[i]), tuple(shape_coords[i + 1]))
        for i in range(len(shape_coords) - 1)
    )


def interpolate_position(shape_coords: list[list[float]], progress: float) -> list[float]:
    """Position [lat, lon] à `progress` (0.0 = départ, 1.0 = fin de la rotation complète,
    tous trajets confondus) le long du tracé.
    """
    if not shape_coords:
        return [0.0, 0.0]
    if len(shape_coords) == 1 or progress <= 0:
        return shape_coords[0]
    if progress >= 1:
        return shape_coords[-1]

    total = route_length_km(shape_coords)
    if total == 0:
        return shape_coords[0]

    target = progress * total
    covered = 0.0
    for i in range(len(shape_coords) - 1):
        seg = _haversine_km(tuple(shape_coords[i]), tuple(shape_coords[i + 1]))
        if covered + seg >= target:
            if seg == 0:
                return shape_coords[i]
            frac = (target - covered) / seg
            lat = shape_coords[i][0] + frac * (shape_coords[i + 1][0] - shape_coords[i][0])
            lon = shape_coords[i][1] + frac * (shape_coords[i + 1][1] - shape_coords[i][1])
            return [lat, lon]
        covered += seg
    return shape_coords[-1]


def current_trip_index(trip_lengths_km: list[float], progress: float) -> int:
    """Étant donné les longueurs (km) de chaque trajet d'une rotation, et une progression
    globale (0.0 à 1.0) sur l'ensemble de la rotation, renvoie l'index (0-based) du trajet
    actuellement parcouru — utile pour afficher « Trajet 2/3 » dans le popup du marqueur.
    """
    total = sum(trip_lengths_km)
    if total == 0 or not trip_lengths_km:
        return 0
    target = progress * total
    covered = 0.0
    for i, length in enumerate(trip_lengths_km):
        covered += length
        if target <= covered:
            return i
    return len(trip_lengths_km) - 1
