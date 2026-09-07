"""
Jeux de données pour le DVRP :
- get_real_algiers_dataset() : cas métier réel (livraison de produits frais/périssables à Alger)
- generate_solomon_benchmark() : jeu synthétique inspiré des instances Solomon (VRPTW dynamique)

Toutes les commandes partagent EXACTEMENT les mêmes colonnes, pour que app.py
puisse les traiter indifféremment sans planter sur une colonne manquante.
"""

import numpy as np
import pandas as pd

# Dépôt central : Zone Industrielle Oued Smar, Alger
DEPOT_ALGIERS = (36.7025, 3.1612)

COLUMNS = ["id", "client", "lat", "lon", "demand_kg", "temp_max",
           "time_window", "release_time", "priority", "type"]


def get_real_algiers_dataset() -> tuple[tuple[float, float], pd.DataFrame]:
    """Retourne (coordonnées du dépôt, DataFrame des commandes)."""
    data = [
        # --- Commandes statiques (connues dès le début de la tournée, t = 0) ---
        {"id": "CMD-001", "client": "Supermarché Uno Bab Ezzouar", "lat": 36.7225, "lon": 3.1812,
         "demand_kg": 150, "temp_max": 4.0, "time_window": "08:30-11:00",
         "release_time": 0, "priority": "NORMALE", "type": "STATIC"},
        {"id": "CMD-002", "client": "Hypermarché Ardis", "lat": 36.7350, "lon": 3.1320,
         "demand_kg": 300, "temp_max": 4.0, "time_window": "08:30-12:00",
         "release_time": 0, "priority": "NORMALE", "type": "STATIC"},
        {"id": "CMD-003", "client": "Pharmacie Centrale Mustafa", "lat": 36.7610, "lon": 3.0580,
         "demand_kg": 45, "temp_max": 8.0, "time_window": "09:00-13:00",
         "release_time": 0, "priority": "NORMALE", "type": "STATIC"},
        {"id": "CMD-004", "client": "Hôtel El Aurassi (Kouba)", "lat": 36.7290, "lon": 3.0850,
         "demand_kg": 120, "temp_max": 6.0, "time_window": "09:00-14:00",
         "release_time": 0, "priority": "NORMALE", "type": "STATIC"},
        {"id": "CMD-005", "client": "Magasin Prima Chéraga", "lat": 36.7680, "lon": 2.9560,
         "demand_kg": 80, "temp_max": 6.0, "time_window": "09:30-14:00",
         "release_time": 0, "priority": "NORMALE", "type": "STATIC"},

        # --- Commandes dynamiques (apparaissent en cours de journée) ---
        {"id": "CMD-DYN-01", "client": "Restaurant Le Lagon (Sidi Fredj)", "lat": 36.7560, "lon": 2.8480,
         "demand_kg": 60, "temp_max": 4.0, "time_window": "11:00-15:00",
         "release_time": 45, "priority": "URGENTE", "type": "DYNAMIC"},
        {"id": "CMD-DYN-02", "client": "Clinique Chahids (Bir Mourad Raïs)", "lat": 36.7380, "lon": 3.0510,
         "demand_kg": 35, "temp_max": 4.0, "time_window": "10:00-16:00",
         "release_time": 90, "priority": "URGENTE", "type": "DYNAMIC"},
        {"id": "CMD-DYN-03", "client": "Superette Zéralda", "lat": 36.6780, "lon": 2.8410,
         "demand_kg": 110, "temp_max": 8.0, "time_window": "12:00-17:00",
         "release_time": 120, "priority": "HAUTE", "type": "DYNAMIC"},
    ]
    return DEPOT_ALGIERS, pd.DataFrame(data, columns=COLUMNS)


def generate_solomon_benchmark(num_clients: int = 25, degree_of_dynamism: float = 0.4,
                                seed: int = 101) -> tuple[tuple[float, float], pd.DataFrame]:
    """Génère un jeu synthétique type Solomon VRPTW adapté au DVRP autour d'Alger."""
    rng = np.random.default_rng(seed)
    num_dynamic = int(num_clients * degree_of_dynamism)

    rows = []
    for i in range(num_clients):
        is_dynamic = i < num_dynamic
        rows.append({
            "id": f"BM-{i + 1:03d}",
            "client": f"Point de livraison {i + 1}",
            "lat": DEPOT_ALGIERS[0] + rng.uniform(-0.08, 0.08),
            "lon": DEPOT_ALGIERS[1] + rng.uniform(-0.08, 0.08),
            "demand_kg": int(rng.integers(20, 150)),
            "temp_max": float(rng.choice([4.0, 8.0, 15.0])),
            "time_window": "09:00-17:00",
            "release_time": int(rng.integers(30, 240)) if is_dynamic else 0,
            "priority": str(rng.choice(["NORMALE", "HAUTE", "URGENTE"], p=[0.6, 0.3, 0.1])),
            "type": "DYNAMIC" if is_dynamic else "STATIC",
        })
    return DEPOT_ALGIERS, pd.DataFrame(rows, columns=COLUMNS)
