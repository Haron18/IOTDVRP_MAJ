"""
Gestion des événements dynamiques du DVRP.

Correctif important : la version d'origine appelait
    np.random.choice(events_pool, p=[...])
sur une LISTE DE DICTIONNAIRES. np.random.choice exige un tableau de valeurs
scalaires (nombres, strings...) — cet appel plantait systématiquement
(ValueError: a must be 1-dimensional). On tire maintenant un INDEX pondéré,
puis on va chercher le dict correspondant : c'est la façon correcte de faire.

Amélioration par rapport à la version précédente : les événements ne se
contentent plus d'être JOURNALISÉS (affichage d'un message dans le log) — ils
MODIFIENT réellement l'état de la simulation (commandes, véhicules, distances)
via `apply_event_effect`, ce qui force une vraie réoptimisation OR-Tools au
prochain calcul, et pas seulement un message informatif.

Les codes d'événements sont désormais entièrement en français (NOUVELLE_COMMANDE,
EMBOUTEILLAGE, PANNE_VEHICULE...) au lieu de codes anglais (NEW_ORDER,
TRAFFIC_JAM, BREAKDOWN...), pour rester cohérent avec le reste de l'interface.
"""

import time as _time

import numpy as np

EVENTS_POOL = [
    {"type": "Aucun", "level": "INFO", "desc": "Fonctionnement normal"},
    {"type": "Nouvelle demande", "level": "GLOBAL", "desc": "Nouveau client prioritaire apparu !"},
    {"type": "Accident / Embouteillage", "level": "LOCAL", "desc": "Route bloquée sur l'itinéraire"},
    {"type": "Alerte Température", "level": "URGENT", "desc": "Rupture de chaîne du froid"},
    {"type": "Sortie Geofence + Arrêt", "level": "ALERTE", "desc": "Véhicule hors zone et à l'arrêt !"},
    {"type": "Panne Véhicule", "level": "GLOBAL", "desc": "Panne critique sur un véhicule"},
]
_WEIGHTS = [0.5, 0.15, 0.15, 0.1, 0.05, 0.05]


def trigger_random_event() -> dict:
    """Tire un événement aléatoire pondéré (corrige le bug np.random.choice sur des dicts)."""
    idx = int(np.random.choice(len(EVENTS_POOL), p=_WEIGHTS))
    return EVENTS_POOL[idx]


# Matrice de décision : quel type d'événement terrain déclenche quelle action DVRP.
# Complète la version d'origine qui laissait certains cas (SORTIE_ZONE, GPS_PERDU,
# GPS_MISE_A_JOUR) non gérés et provoquait donc une action "AUCUNE" par défaut silencieuse.
EVENT_RULES = {
    "NOUVELLE_COMMANDE":     {"action": "REOPTIM_GLOBALE", "alert_level": "ÉLEVÉ",
                               "message": "Nouvelle commande : réoptimisation globale des tournées (OR-Tools)."},
    "ANNULATION_COMMANDE":   {"action": "REOPTIM_LOCALE",  "alert_level": "MOYEN",
                               "message": "Commande annulée : suppression de l'arrêt et recalcul de l'itinéraire."},
    "MODIFICATION_QUANTITE": {"action": "REOPTIM_LOCALE",  "alert_level": "MOYEN",
                               "message": "Quantité modifiée : vérification de la capacité du véhicule."},
    "EMBOUTEILLAGE":         {"action": "REOPTIM_LOCALE",  "alert_level": "MOYEN",
                               "message": "Embouteillage : calcul d'un itinéraire alternatif."},
    "ROUTE_FERMEE":          {"action": "REOPTIM_GLOBALE", "alert_level": "ÉLEVÉ",
                               "message": "Route fermée : re-planification élargie des tournées."},
    "PANNE_VEHICULE":        {"action": "REOPTIM_GLOBALE", "alert_level": "ÉLEVÉ",
                               "message": "Panne véhicule : redistribution des clients restants."},
    "ALERTE_TEMPERATURE":    {"action": "REOPTIM_LOCALE",  "alert_level": "URGENT",
                               "message": "Alerte température : livraison passée en priorité absolue."},
    "SORTIE_ZONE":           {"action": "ALERTE_SEULE",    "alert_level": "AVERTISSEMENT",
                               "message": "Sortie de zone autorisée : alerte superviseur."},
    "CLIENT_ABSENT":         {"action": "REOPTIM_LOCALE",  "alert_level": "MOYEN",
                               "message": "Client absent : passage au suivant, réordonnancement de la tournée."},
    "GPS_PERDU":             {"action": "AUCUNE",          "alert_level": "AVERTISSEMENT",
                               "message": "Signal GPS perdu : dernière position connue conservée."},
    "GPS_MISE_A_JOUR":       {"action": "AUCUNE",          "alert_level": "INFO",
                               "message": "Mise à jour GPS standard."},
}


def process_dynamic_event(event_type: str, event_data: dict | None = None) -> dict:
    """Évalue l'importance d'un événement et détermine l'action DVRP requise."""
    event_data = event_data or {}
    rule = EVENT_RULES.get(event_type, {"action": "AUCUNE", "alert_level": "INFO", "message": "Événement inconnu."})
    decision = dict(rule)
    decision["event_type"] = event_type
    decision["vehicle_id"] = event_data.get("vehicle_id", "N/A")
    return decision


def generate_random_order(depot_coords: tuple[float, float], sim_time: int) -> dict:
    """Crée une nouvelle commande urgente aléatoire (pour l'événement NOUVELLE_COMMANDE),
    quelque part autour du dépôt, apparaissant à l'instant courant de la simulation.
    """
    order_id = f"CMD-EVT-{int(_time.time() * 1000) % 1_000_000}"
    return {
        "id": order_id,
        "client": f"Client urgent {order_id[-4:]}",
        "lat": depot_coords[0] + float(np.random.uniform(-0.08, 0.08)),
        "lon": depot_coords[1] + float(np.random.uniform(-0.08, 0.08)),
        "demand_kg": int(np.random.randint(20, 150)),
        "temp_max": 4.0,
        "time_window": "dès que possible",
        "release_time": sim_time,
        "priority": "URGENTE",
        "type": "DYNAMIC",
    }


def apply_event_effect(event_type: str, session_state, depot_coords: tuple[float, float],
                        sim_time: int, candidate_ids: list[str],
                        manual_target: str | None = None) -> str | None:
    """Applique l'effet CONCRET d'un événement sur l'état de la simulation
    (`session_state`), pour qu'il soit réellement pris en compte au prochain
    calcul OR-Tools — au lieu d'être seulement journalisé.

    `candidate_ids` = identifiants de commandes actuellement connues, dans
    lesquels piocher pour les événements qui ciblent une commande existante
    (annulation, absence client, alerte température).

    `manual_target` = identifiant choisi explicitement par l'utilisateur (ex. commande
    à annuler sélectionnée dans l'interface). S'il est fourni ET valide (présent dans
    `candidate_ids`), il est utilisé à la place du tirage aléatoire — utile pour
    ANNULATION_COMMANDE, où l'utilisateur choisit lui-même la commande à retirer
    (uniquement parmi celles pas encore livrées, filtrées en amont côté interface).

    Retourne un court complément d'information à afficher dans le log, ou None.
    """
    if event_type == "NOUVELLE_COMMANDE":
        new_order = generate_random_order(depot_coords, sim_time)
        session_state.extra_orders.append(new_order)
        return f"commande {new_order['id']} créée ({new_order['demand_kg']} kg)"

    if event_type in ("ANNULATION_COMMANDE", "CLIENT_ABSENT") and candidate_ids:
        if manual_target and manual_target in candidate_ids:
            target = manual_target
        else:
            target = str(np.random.choice(candidate_ids))
        session_state.cancelled_ids.add(target)
        return f"commande {target} retirée de la tournée"

    if event_type == "PANNE_VEHICULE":
        session_state.vehicle_breakdown_count += 1
        return "un véhicule est retiré de la flotte disponible"

    if event_type in ("EMBOUTEILLAGE", "ROUTE_FERMEE"):
        factor = 1.4 if event_type == "EMBOUTEILLAGE" else 1.9
        session_state.traffic_penalty = max(session_state.traffic_penalty, factor)
        return f"distances pénalisées x{factor} jusqu'à réinitialisation"

    if event_type == "ALERTE_TEMPERATURE" and candidate_ids:
        target = str(np.random.choice(candidate_ids))
        session_state.priority_overrides[target] = "URGENTE"
        return f"commande {target} passée en priorité URGENTE"

    # SORTIE_ZONE, GPS_PERDU, GPS_MISE_A_JOUR, ou type inconnu : alerte seule,
    # aucune donnée n'est modifiée.
    return None
