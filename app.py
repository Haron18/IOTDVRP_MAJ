"""
Système Intelligent de Tracking GPS & DVRP Dynamique — Alger
==============================================================

Corrections apportées par rapport à la version générée initialement :

1. BUG BLOQUANT : `np.random.choice(events_pool, p=...)` sur une liste de dicts
   plantait toujours. → corrigé dans event_handler.py (tirage par index).
2. BUG DE SÉCURITÉ DES THREADS : le callback MQTT écrivait directement dans
   st.session_state depuis un thread réseau. → corrigé via une queue.Queue
   thread-safe, vidée uniquement depuis le thread principal (mqtt_manager.py).
3. BUG D'ALIGNEMENT D'INDEX : après avoir filtré `active_orders` par le temps
   écoulé, l'ancien index du DataFrame ne correspondait plus aux positions
   utilisées par OR-Tools (`route[i]`). → corrigé avec `.reset_index(drop=True)`.
4. MANQUE : aucune contrainte de capacité — un camion pouvait se voir assigner
   un poids illimité. → ajout d'une vraie contrainte de capacité (kg).
5. PERFORMANCE : chaque interaction relançait des appels réseau OSRM.
   → mise en cache (st.cache_data, TTL 5 min) dans dvrp_engine.py.
6. ROBUSTESSE : gestion du cas où OR-Tools ne trouve aucune solution
   (capacité totale insuffisante) au lieu de faire planter l'affichage.
7. MQTT désormais optionnel (case à cocher) plutôt que connecté d'office à
   chaque rechargement de page, ce qui évitait d'accumuler des connexions.
8. ÉVÉNEMENTS RÉELLEMENT ACTIFS : la version précédente se contentait de
   JOURNALISER les événements dynamiques (message informatif, sans effet sur
   les données). Ils modifient désormais vraiment l'état de la simulation
   (event_handler.apply_event_effect) — nouvelle commande, annulation, panne
   véhicule, pénalité de trafic, priorité relevée — ce qui déclenche une vraie
   réoptimisation OR-Tools au calcul suivant, au lieu d'un simple message.
9. ROBUSTESSE SUPPLÉMENTAIRE : bouton de réinitialisation des événements
   cumulés, résumé des effets actifs visible en permanence dans la barre
   latérale, et prise en compte du nombre de véhicules réellement disponibles
   (après pannes) dans les indicateurs et les messages d'erreur.
10. ROTATIONS MULTIPLES (capacité insuffisante) : un camion peut désormais
    effectuer plusieurs allers-retours au dépôt si la capacité totale ne
    suffit pas en un seul passage (nombre de rotations calculé automatiquement).
    Modélisé via des véhicules « virtuels » (camion x trajets max) pour
    OR-Tools, regroupés ensuite par camion physique (dvrp_engine.py :
    group_multi_trip_routes). Le temps de rechargement au dépôt entre deux
    rotations n'est pas modélisé (supposé instantané).
11. TRACKING SIMULÉ (vehicle_tracker.py) : position de chaque camion calculée
    localement par interpolation sur le tracé réel de sa rotation complète
    (tous trajets et retours au dépôt inclus), pour visualiser concrètement
    les allers-retours quand plusieurs rotations sont nécessaires. Complète
    la télémétrie MQTT, qui simule des messages génériques sans lien direct
    avec les tournées calculées.
"""

import math
import time
from datetime import datetime

import folium
import numpy as np
import pandas as pd
import streamlit as st
from streamlit_folium import st_folium

from benchmark_loader import COLUMNS, get_real_algiers_dataset, generate_solomon_benchmark
from dvrp_engine import (
    get_osrm_distance_matrix,
    get_osrm_route_shape,
    group_multi_trip_routes,
    naive_baseline_routes,
    route_distance,
    solve_dvrp_ortools,
)
from event_handler import apply_event_effect, process_dynamic_event, trigger_random_event
from mqtt_manager import MQTTBridge
from vehicle_tracker import current_trip_index, interpolate_position

st.set_page_config(page_title="DVRP Logistique & Tracking Alger", page_icon="🚚", layout="wide")

# ----------------------------------------------------------------------------
# 1. ÉTAT DE SESSION
# ----------------------------------------------------------------------------
DEFAULTS = {
    "logs": [],
    "mqtt_events": [],
    "mqtt_bridge": None,
    "extra_orders": [],       # commandes ajoutées par des événements NOUVELLE_COMMANDE
    "cancelled_ids": set(),   # commandes retirées (ANNULATION_COMMANDE / CLIENT_ABSENT)
    "priority_overrides": {}, # id -> priorité forcée (ALERTE_TEMPERATURE)
    "vehicle_breakdown_count": 0,  # nb de camions mis hors service (PANNE_VEHICULE)
    "traffic_penalty": 1.0,   # multiplicateur appliqué à la matrice de distances
    "truck_progress_km": {},  # distance déjà parcourue (km) par chaque camion sur sa rotation
    "delivered_ids": set(),   # commandes déjà livrées (calculé à partir du tracking)
}
for key, default in DEFAULTS.items():
    if key not in st.session_state:
        st.session_state[key] = default


def log_event(message: str) -> None:
    st.session_state.logs.insert(0, f"[{datetime.now().strftime('%H:%M:%S')}] {message}")
    st.session_state.logs = st.session_state.logs[:30]


# ----------------------------------------------------------------------------
# 2. EN-TÊTE
# ----------------------------------------------------------------------------
st.title("🚚 Tracking en temps réel pour l'optimisation logistique : cas les tournées dynamiques DVRP")
st.caption("Alger — livraison de produits frais / express — OSRM + Google OR-Tools + MQTT")
st.markdown("---")

# ----------------------------------------------------------------------------
# 3. BARRE LATÉRALE — DONNÉES
# ----------------------------------------------------------------------------
st.sidebar.header("📁 Jeu de données")
dataset_choice = st.sidebar.selectbox(
    "Source de données",
    ["Cas réel : livraisons Grand Alger", "Benchmark synthétique (type Solomon)"],
)

if dataset_choice.startswith("Cas réel"):
    depot_coords, df_orders = get_real_algiers_dataset()
else:
    depot_coords, df_orders = generate_solomon_benchmark()

st.sidebar.header("🕹 Contrôle de la simulation")
num_vehicles = st.sidebar.slider("Camions frigorifiques disponibles", 1, 4, 2)
vehicle_capacity = st.sidebar.slider("Capacité par camion (kg)", 100, 1000, 400, step=50)
sim_time = st.sidebar.slider(
    "Temps écoulé dans la journée (min)", 0, 480, 45, step=15,
    help="Jusqu'à 8h de journée simulée. Une commande n'apparaît que si son "
         "heure d'apparition (release_time) est inférieure ou égale à cette valeur.",
)
st.sidebar.caption(f"⏱️ {sim_time // 60}h{sim_time % 60:02d} écoulées depuis le début de la tournée")
st.sidebar.caption(
    "🔁 Rotations : calculées automatiquement selon la demande totale et la capacité "
    "disponible (pas de réglage manuel nécessaire)."
)

# ----------------------------------------------------------------------------
# 4. BARRE LATÉRALE — ÉVÉNEMENTS DYNAMIQUES (désormais réellement actifs)
# ----------------------------------------------------------------------------
st.sidebar.markdown("---")
st.sidebar.header("🚨 Événements dynamiques")
st.sidebar.caption("Chaque événement modifie réellement les données et relance l'optimisation.")

manual_event_type = st.sidebar.selectbox(
    "Déclencher un événement",
    ["AUCUN", "NOUVELLE_COMMANDE", "ANNULATION_COMMANDE", "EMBOUTEILLAGE", "ROUTE_FERMEE",
     "PANNE_VEHICULE", "ALERTE_TEMPERATURE", "SORTIE_ZONE", "CLIENT_ABSENT"],
)

manual_cancel_target = None
if manual_event_type == "ANNULATION_COMMANDE":
    known_ids_now = pd.concat(
        [df_orders["id"], pd.Series([o["id"] for o in st.session_state.extra_orders], dtype=str)]
    )
    # Seules les commandes pas encore livrées (ni déjà annulées) peuvent être choisies —
    # "delivered_ids" est recalculé à chaque affichage de la carte à partir de la
    # progression réelle des camions sur leur tournée.
    cancellable_ids = [
        i for i in known_ids_now
        if i not in st.session_state.cancelled_ids and i not in st.session_state.delivered_ids
    ]
    if cancellable_ids:
        manual_cancel_target = st.sidebar.selectbox(
            "Commande à annuler (non encore livrée)", cancellable_ids,
        )
    else:
        st.sidebar.caption("Aucune commande annulable : toutes livrées ou déjà annulées.")

if st.sidebar.button("⚠️ Appliquer l'événement") and manual_event_type != "AUCUN":
    decision = process_dynamic_event(manual_event_type, {"vehicle_id": "V1"})
    known_ids = pd.concat(
        [df_orders["id"], pd.Series([o["id"] for o in st.session_state.extra_orders], dtype=str)]
    )
    candidate_ids = [i for i in known_ids if i not in st.session_state.cancelled_ids]
    detail = apply_event_effect(
        manual_event_type, st.session_state, depot_coords, sim_time, candidate_ids,
        manual_target=manual_cancel_target,
    )
    message = decision["message"] + (f" — {detail}" if detail else "")
    log_event(f"{manual_event_type} → {decision['action']} ({message})")
    st.rerun()

if st.sidebar.button("🔄 Réinitialiser les événements"):
    st.session_state.extra_orders = []
    st.session_state.cancelled_ids = set()
    st.session_state.priority_overrides = {}
    st.session_state.vehicle_breakdown_count = 0
    st.session_state.traffic_penalty = 1.0
    log_event("Réinitialisation manuelle des événements dynamiques.")
    st.rerun()

active_effects = []
if st.session_state.vehicle_breakdown_count:
    active_effects.append(f"🚧 {st.session_state.vehicle_breakdown_count} véhicule(s) en panne")
if st.session_state.traffic_penalty > 1.0:
    active_effects.append(f"🚦 trafic dégradé (x{st.session_state.traffic_penalty:.1f})")
if st.session_state.cancelled_ids:
    active_effects.append(f"❌ {len(st.session_state.cancelled_ids)} commande(s) retirée(s)")
if st.session_state.extra_orders:
    active_effects.append(f"🆕 {len(st.session_state.extra_orders)} commande(s) ajoutée(s)")
if active_effects:
    st.sidebar.caption(" · ".join(active_effects))

# ----------------------------------------------------------------------------
# 5. BARRE LATÉRALE — MQTT (télémétrie temps réel)
# ----------------------------------------------------------------------------
st.sidebar.markdown("---")
st.sidebar.header("📡 MQTT (télémétrie temps réel)")
st.sidebar.caption("Broker public de démonstration — non sécurisé, à usage pédagogique uniquement.")
mqtt_enabled = st.sidebar.checkbox("Activer la connexion MQTT", value=False)

if mqtt_enabled and st.session_state.mqtt_bridge is None:
    bridge = MQTTBridge()
    if bridge.start():
        st.session_state.mqtt_bridge = bridge
        st.sidebar.success("Connecté au broker MQTT.")
    else:
        st.sidebar.error("Connexion MQTT impossible (réseau indisponible).")
elif not mqtt_enabled and st.session_state.mqtt_bridge is not None:
    st.session_state.mqtt_bridge.stop()
    st.session_state.mqtt_bridge = None

if st.session_state.mqtt_bridge is not None:
    # On vide la queue thread-safe UNIQUEMENT ici, dans le thread principal Streamlit.
    new_events = st.session_state.mqtt_bridge.drain_events()
    st.session_state.mqtt_events.extend(new_events)
    st.session_state.mqtt_events = st.session_state.mqtt_events[-50:]

    st.sidebar.subheader("Envoyer une commande")
    target_v = st.sidebar.selectbox("Véhicule cible", [f"V{i + 1}" for i in range(num_vehicles)])
    cmd_type = st.sidebar.selectbox("Commande", ["MODIFIER_TOURNEE", "ARRETER_ALERTE", "RECALCULER_ITINERAIRE"])
    if st.sidebar.button("Envoyer la commande MQTT"):
        st.session_state.mqtt_bridge.send_command(target_v, {"command": cmd_type, "timestamp": time.time()})
        st.sidebar.info(f"Commande {cmd_type} envoyée à {target_v}")

st.sidebar.markdown("---")
st.sidebar.header("📍 Tracking simulé des camions")
st.sidebar.caption(
    "Position GPS calculée localement à partir d'une vitesse moyenne réaliste, sur le "
    "tracé réel de la rotation complète de chaque camion — indépendant de MQTT."
)
truck_speed_kmh = st.sidebar.slider(
    "Vitesse moyenne des camions (km/h)", 15, 60, 30,
    help="Vitesse commerciale en zone urbaine (arrêts, feux, trafic inclus). "
         "Détermine le temps réel nécessaire pour parcourir chaque tournée.",
)
time_accel_options = {
    "Temps réel (1h simulée = 60 min réelles)": 60,
    "Rapide (1h simulée = 10 min réelles)": 10,
    "Très rapide (1h simulée = 5 min réelles)": 5,
    "Ultra rapide (1h simulée = 1 min réelle)": 1,
}
time_accel_label = st.sidebar.selectbox(
    "Vitesse d'accélération du temps", list(time_accel_options.keys()), index=2,
)
real_minutes_per_sim_hour = time_accel_options[time_accel_label]
sim_minutes_per_real_second = 60 / (real_minutes_per_sim_hour * 60)

col_track1, col_track2 = st.sidebar.columns(2)
manual_advance_clicked = col_track1.button("➡️ +10 min simulées")
if col_track2.button("🔄 Réinitialiser tracking"):
    st.session_state.truck_progress_km = {}

st.sidebar.markdown("---")
auto_run = st.sidebar.toggle("▶️ Simulation temps réel (auto-refresh)", value=False)
sim_speed = st.sidebar.slider("Fréquence de rafraîchissement (sec)", 2, 10, 3)

# Combien de "minutes simulées" doivent s'écouler pour le tracking à ce tour de boucle :
# soit un pas fixe (bouton manuel), soit dérivé de la fréquence de rafraîchissement et du
# facteur d'accélération choisi (auto-refresh) — c'est ce qui donne un mouvement à vitesse
# CONSTANTE et réaliste, au lieu d'un pas arbitraire par rafraîchissement.
sim_minutes_elapsed_this_tick = 0.0
if manual_advance_clicked:
    sim_minutes_elapsed_this_tick = 10.0
elif auto_run:
    sim_minutes_elapsed_this_tick = sim_speed * sim_minutes_per_real_second

# ----------------------------------------------------------------------------
# 6. APPLICATION DES EFFETS DES ÉVÉNEMENTS SUR LE JEU DE DONNÉES
# ----------------------------------------------------------------------------
if st.session_state.extra_orders:
    new_ids = {o["id"] for o in st.session_state.extra_orders}
    df_orders = pd.concat(
        [df_orders, pd.DataFrame(st.session_state.extra_orders, columns=COLUMNS)],
        ignore_index=True,
    )
else:
    new_ids = set()

# Statut affiché dans le tableau (n'affecte pas le calcul des tournées) :
# une commande annulée n'est plus retirée du jeu de données, elle est simplement
# exclue du calcul de tournée plus bas tout en restant visible avec son état.
df_orders["status"] = df_orders["id"].apply(
    lambda i: "❌ Retirée" if i in st.session_state.cancelled_ids
    else ("🆕 Nouvelle" if i in new_ids else "✅ Normale")
)

if st.session_state.priority_overrides:
    df_orders = df_orders.copy()
    df_orders["priority"] = df_orders.apply(
        lambda r: st.session_state.priority_overrides.get(r["id"], r["priority"]), axis=1
    )

# Commandes "visibles" à l'instant t de la simulation (release_time <= sim_time),
# pour l'affichage : inclut les commandes annulées (marquées "❌ Retirée").
display_orders = df_orders[df_orders["release_time"] <= sim_time].reset_index(drop=True)

# Commandes réellement routables (exclut les commandes annulées) : c'est CE sous-ensemble
# qui sert au calcul des tournées (coords_list / demands / index des routes OR-Tools).
active_orders = display_orders[
    ~display_orders["id"].isin(st.session_state.cancelled_ids)
].reset_index(drop=True)

if len(active_orders) == 0:
    st.info("Aucune commande active à cet instant — avancez le curseur « Temps écoulé ».")
    st.stop()

effective_vehicles = max(1, num_vehicles - st.session_state.vehicle_breakdown_count)
if effective_vehicles < num_vehicles:
    st.warning(
        f"🚧 {st.session_state.vehicle_breakdown_count} camion(s) en panne : "
        f"{effective_vehicles}/{num_vehicles} véhicule(s) réellement disponibles."
    )

coords_list = [depot_coords] + list(zip(active_orders["lat"], active_orders["lon"]))
demands = [0] + active_orders["demand_kg"].astype(int).tolist()

# Nombre de rotations par camion calculé automatiquement à partir de la demande totale
# et de la capacité réellement disponible (plus besoin de régler un curseur manuel).
# +1 rotation de marge : laisse au solveur une capacité légèrement excédentaire pour
# répartir les tournées efficacement (sinon, avec le compte pile, il peut n'exister
# aucune répartition valide même quand la capacité totale suffit tout juste).
total_demand = int(active_orders["demand_kg"].sum())
max_trips_per_vehicle = max(
    1, math.ceil(total_demand / (effective_vehicles * vehicle_capacity)) + 1
)

# Le solveur reçoit des véhicules "virtuels" (camion physique x trajets max autorisés),
# tous de même capacité : ça lui permet de répartir une commande sur plusieurs rotations
# d'un même camion si la capacité en un seul passage ne suffit pas.
virtual_vehicle_count = effective_vehicles * max_trips_per_vehicle
vehicle_capacities = [vehicle_capacity] * virtual_vehicle_count

# ----------------------------------------------------------------------------
# 7. CALCUL DVRP (OSRM + OR-Tools, avec capacité, rotations et pénalité de trafic)
# ----------------------------------------------------------------------------
with st.spinner("Calcul des distances et optimisation des tournées..."):
    raw_dist_matrix = get_osrm_distance_matrix(tuple(coords_list))  # distances réelles (km affichés)
    solver_dist_matrix = raw_dist_matrix
    if st.session_state.traffic_penalty > 1.0:
        # La pénalité de trafic influence UNIQUEMENT la décision d'OR-Tools (pour qu'il évite
        # la zone concernée) ; les distances affichées restent les vraies distances physiques.
        solver_dist_matrix = (np.array(raw_dist_matrix) * st.session_state.traffic_penalty).tolist()
    virtual_routes = solve_dvrp_ortools(solver_dist_matrix, demands, vehicle_capacities)

total_capacity = vehicle_capacity * virtual_vehicle_count
if not virtual_routes:
    st.error(
        f"⚠️ Aucune tournée réalisable : {total_demand} kg de commandes pour "
        f"{total_capacity} kg de capacité totale disponible ({effective_vehicles} véhicule(s) "
        f"x {max_trips_per_vehicle} rotation(s), calculées automatiquement). "
        f"Augmentez le nombre de véhicules ou leur capacité, ou réinitialisez les événements."
    )
    st.stop()

# Regroupe les tournées virtuelles en rotations successives par camion physique.
truck_trips = group_multi_trip_routes(virtual_routes, effective_vehicles)
optimized_routes = [route for trips in truck_trips.values() for route in trips]  # pour les KPI globaux
total_trips = len(optimized_routes)
multi_trip_trucks = sum(1 for trips in truck_trips.values() if len(trips) > 1)

# ----------------------------------------------------------------------------
# 7bis. PREUVE DE L'OPTIMISATION : distance réelle vs référence non optimisée
# ----------------------------------------------------------------------------
# On mesure la distance physique (raw_dist_matrix, sans la pénalité de trafic qui ne sert
# qu'à orienter le solveur) des tournées OR-Tools, et on la compare à une tournée « naïve »
# qui affecte les commandes dans leur ordre d'apparition, sans aucune optimisation.
optimized_distance_km = sum(route_distance(r, raw_dist_matrix) for r in optimized_routes) / 1000
baseline_routes = naive_baseline_routes(demands, vehicle_capacities)
baseline_distance_km = sum(route_distance(r, raw_dist_matrix) for r in baseline_routes) / 1000
gain_km = baseline_distance_km - optimized_distance_km
gain_pct = (gain_km / baseline_distance_km * 100) if baseline_distance_km > 0 else 0

# ----------------------------------------------------------------------------
# 8. INDICATEURS CLÉS (KPI)
# ----------------------------------------------------------------------------
k1, k2, k3, k4, k5 = st.columns(5)
k1.metric("📋 Commandes visibles", f"{len(active_orders)} / {len(df_orders)}")
k2.metric("📦 Quantité totale", f"{total_demand} kg", delta=f"capacité totale {total_capacity} kg")
k3.metric("🚛 Camion disponible", f"{len(truck_trips)} / {num_vehicles}")
k4.metric("🔁 Trajets effectués", f"{total_trips}",
          delta=f"{multi_trip_trucks} en rotation multiple" if multi_trip_trucks else None)
k5.metric("⏱️ Temps de simulation", f"{sim_time // 60}h{sim_time % 60:02d}")

if multi_trip_trucks:
    st.info(
        f"🔁 {multi_trip_trucks} camion(s) doivent effectuer **plusieurs rotations** "
        f"(retour au dépôt puis nouveau départ) pour livrer toutes les commandes : "
        f"la capacité en un seul passage est insuffisante avec les paramètres actuels."
    )

st.markdown("##### 📏 Preuve d'optimisation — distance réellement parcourue")
d1, d2, d3 = st.columns(3)
d1.metric("Distance après l'optimisation (OR-Tools)", f"{optimized_distance_km:.1f} km")
d2.metric("Distance avant l'optimisation", f"{baseline_distance_km:.1f} km")
d3.metric("Gain apporté par l'optimisation", f"-{gain_pct:.0f} %", delta=f"-{gain_km:.1f} km", delta_color="normal")
st.caption(
    "La « référence » affecte les commandes aux véhicules dans leur ordre d'apparition, "
    "sans aucune optimisation de séquence ni de répartition. La différence avec la colonne "
    "de gauche mesure ce qu'OR-Tools apporte réellement (même contrainte de capacité, "
    "mêmes distances routières réelles OSRM pour les deux)."
)

if st.session_state.logs:
    st.info(f"Dernier événement : {st.session_state.logs[0]}")

st.markdown("---")

# ----------------------------------------------------------------------------
# 9. CARTE & DÉTAILS
# ----------------------------------------------------------------------------
col_map, col_details = st.columns([2, 1])

with col_map:
    st.subheader("🗺 Carte des tournées optimisées (réseau routier réel OSRM)")
    m = folium.Map(location=depot_coords, zoom_start=11, tiles="OpenStreetMap")

    folium.Marker(
        depot_coords,
        popup="<b>Dépôt Central — Oued Smar</b>",
        icon=folium.Icon(color="black", icon="home", prefix="fa"),
    ).add_to(m)

    priority_color = {"URGENTE": "red", "HAUTE": "orange", "NORMALE": "blue"}
    for _, row in active_orders.iterrows():
        folium.Marker(
            [row["lat"], row["lon"]],
            popup=(f"<b>{row['client']}</b><br>Charge: {row['demand_kg']} kg<br>"
                   f"Temp. max: {row['temp_max']}°C<br>Fenêtre: {row['time_window']}"),
            tooltip=row["id"],
            icon=folium.Icon(color=priority_color.get(row["priority"], "blue"),
                              icon="shopping-cart", prefix="fa"),
        ).add_to(m)

    route_colors = ["blue", "green", "purple", "orange", "darkred", "cadetblue"]
    dash_patterns = [None, "8,8", "2,6"]  # trajet 1 = trait plein, trajet 2 = tirets, trajet 3 = pointillés

    # full_shapes[p] = tracé combiné de TOUTE la rotation du camion physique p (tous
    # trajets mis bout à bout), utilisé ensuite pour le tracking simulé.
    full_shapes: dict[int, list[list[float]]] = {}
    trip_lengths: dict[int, list[float]] = {}

    for p_idx, trips in truck_trips.items():
        color = route_colors[p_idx % len(route_colors)]
        full_shapes[p_idx] = []
        trip_lengths[p_idx] = []
        for t_idx, route in enumerate(trips):
            dash = dash_patterns[t_idx % len(dash_patterns)]
            trip_shape: list[list[float]] = []
            for i in range(len(route) - 1):
                p1 = coords_list[route[i]]
                p2 = coords_list[route[i + 1]]
                path = get_osrm_route_shape(tuple(p1), tuple(p2))
                trip_shape.extend(path)
                folium.PolyLine(
                    path, color=color, weight=4, opacity=0.85, dash_array=dash,
                    tooltip=f"Camion V{p_idx + 1} — Trajet {t_idx + 1}/{len(trips)}",
                ).add_to(m)
            full_shapes[p_idx].extend(trip_shape)
            trip_lengths[p_idx].append(route_distance(route, raw_dist_matrix) / 1000)

    # Tracking simulé : position de TOUS les camions physiques (0 à effective_vehicles-1),
    # pas seulement ceux ayant une tournée assignée par le solveur. En effet, OR-Tools
    # minimise la distance totale sans coût fixe par véhicule : si la capacité d'un seul
    # camion suffit à tout livrer, il n'utilisera QUE ce camion (comportement correct pour
    # l'optimisation, mais qui faisait disparaître les autres camions de la carte). On
    # affiche donc désormais aussi les camions inutilisés, immobiles au dépôt.
    #
    # On mémorise la distance RÉELLEMENT parcourue (km), pas une fraction (%) : après une
    # réoptimisation (ex. annulation d'une commande non livrée → OR-Tools recalcule des
    # tournées de longueur différente), le camion reprend exactement où il en était
    # (mêmes km déjà roulés) au lieu de "sauter" en avant ou en arrière sur la nouvelle
    # tournée, ce qu'un pourcentage recalculé sur une distance totale changée aurait fait.
    truck_gps_status = []
    delivered_ids: set[str] = set()  # commandes déjà livrées, tous camions confondus
    for p_idx in range(effective_vehicles):
        shape = full_shapes.get(p_idx, [])

        if not shape:
            # Camion non utilisé pour cette tournée : reste visible, immobile au dépôt.
            folium.Marker(
                depot_coords,
                popup=f"<b>Camion V{p_idx + 1}</b><br>🅿️ Au dépôt (non utilisé pour cette tournée)",
                tooltip=f"V{p_idx + 1} — Au dépôt",
                icon=folium.Icon(color=route_colors[p_idx % len(route_colors)], icon="truck", prefix="fa"),
            ).add_to(m)
            truck_gps_status.append({
                "Camion": f"V{p_idx + 1}",
                "Statut": "🅿️ Au dépôt (non utilisé)",
                "Latitude": round(depot_coords[0], 5),
                "Longitude": round(depot_coords[1], 5),
                "Avancement": "—",
                "Temps restant (min sim.)": "—",
            })
            continue

        total_km = sum(trip_lengths[p_idx])
        traveled_km = st.session_state.truck_progress_km.get(p_idx, 0.0)
        if sim_minutes_elapsed_this_tick > 0:
            traveled_km += truck_speed_kmh * (sim_minutes_elapsed_this_tick / 60)
        traveled_km = min(traveled_km, total_km)
        st.session_state.truck_progress_km[p_idx] = traveled_km

        new_progress = (traveled_km / total_km) if total_km > 0 else 0.0
        total_duration_min = (total_km / truck_speed_kmh * 60) if truck_speed_kmh > 0 else 0

        # Marque comme "livrée" toute commande dont l'arrêt est dépassé par la distance
        # déjà parcourue par ce camion sur sa rotation — sert à ne proposer à l'annulation
        # manuelle (voir barre latérale) que les commandes pas encore livrées.
        cum_km = 0.0
        for route in truck_trips[p_idx]:
            for i in range(len(route) - 1):
                cum_km += raw_dist_matrix[route[i]][route[i + 1]] / 1000
                node = route[i + 1]
                if node != 0 and cum_km <= traveled_km:
                    delivered_ids.add(active_orders.iloc[node - 1]["id"])

        if not shape:
            continue
        pos = interpolate_position(shape, new_progress)
        trips = truck_trips[p_idx]
        t_idx = current_trip_index(trip_lengths[p_idx], new_progress)
        remaining_min = max(0.0, total_duration_min * (1 - new_progress))
        status_label = "✅ Livraison terminée" if new_progress >= 1.0 else f"Trajet {t_idx + 1}/{len(trips)} en cours"

        folium.Marker(
            pos,
            popup=(f"<b>Camion V{p_idx + 1}</b><br>{status_label}<br>"
                   f"Avancement rotation : {new_progress * 100:.0f}%<br>"
                   f"Position GPS : {pos[0]:.5f}, {pos[1]:.5f}<br>"
                   f"Temps restant estimé : {remaining_min:.0f} min simulées"),
            tooltip=f"V{p_idx + 1} — {status_label}",
            icon=folium.Icon(color=route_colors[p_idx % len(route_colors)], icon="truck", prefix="fa"),
        ).add_to(m)
        truck_gps_status.append({
            "Camion": f"V{p_idx + 1}",
            "Statut": status_label,
            "Latitude": round(pos[0], 5),
            "Longitude": round(pos[1], 5),
            "Avancement": f"{new_progress * 100:.0f}%",
            "Temps restant (min sim.)": round(remaining_min),
        })

    st.session_state.delivered_ids = delivered_ids

    st_folium(m, width="100%", height=520, key="dvrp_map")

    st.caption(
        f"🕒 Accélération : {time_accel_label.lower()} · 🚚 Vitesse moyenne assumée : "
        f"{truck_speed_kmh} km/h — le temps de trajet de chaque camion est calculé à "
        f"partir de sa distance réelle et de cette vitesse."
    )

with col_details:
    st.subheader("📦 Commandes actives")
    st.dataframe(
        display_orders[["id", "client", "demand_kg", "priority", "type", "status"]].rename(
            columns={"status": "État"}
        ),
        hide_index=True,
        use_container_width=True,
        height=260,
    )

    st.subheader("🗺️ Séquence de l'itinéraire construite")
    for p_idx, trips in truck_trips.items():
        truck_total_km = sum(route_distance(r, raw_dist_matrix) for r in trips) / 1000
        truck_total_load = sum(
            active_orders.iloc[node - 1]["demand_kg"] for r in trips for node in r if node != 0
        )
        header = f"**Camion V{p_idx + 1}**"
        if len(trips) > 1:
            header += f" — 🔁 {len(trips)} rotations — {truck_total_load} kg au total — {truck_total_km:.1f} km"
        else:
            header += f" — {truck_total_load}/{vehicle_capacity} kg — {truck_total_km:.1f} km"
        st.markdown(header)
        for t_idx, route in enumerate(trips):
            stops = [active_orders.iloc[node - 1]["client"] for node in route if node != 0]
            load = sum(active_orders.iloc[node - 1]["demand_kg"] for node in route if node != 0)
            trip_km = route_distance(route, raw_dist_matrix) / 1000
            prefix = f"Trajet {t_idx + 1}/{len(trips)} " if len(trips) > 1 else ""
            st.caption(
                f"{prefix}({load}/{vehicle_capacity} kg, {trip_km:.1f} km) : "
                + " → ".join(["Dépôt"] + stops + ["Dépôt"])
            )

    st.subheader("📍 Suivi GPS des camions (simulé)")
    if truck_gps_status:
        st.dataframe(pd.DataFrame(truck_gps_status), hide_index=True, use_container_width=True)
        st.caption(
            "Coordonnées calculées localement (lat/lon) à partir de la vitesse moyenne "
            "assumée et du temps simulé écoulé — pas une remontée GPS physique."
        )
    else:
        st.caption("Aucun camion en mouvement à afficher.")

    st.subheader("📡 Télémétrie MQTT")
    if st.session_state.mqtt_bridge is None:
        st.caption("MQTT désactivé — cochez la case dans la barre latérale pour l'activer.")
    elif st.session_state.mqtt_events:
        for event in reversed(st.session_state.mqtt_events[-5:]):
            st.json(event, expanded=False)
    else:
        st.caption("En attente de télémétrie sur `fleet/telemetry/+`...")

    st.subheader("📋 Journal des évènements")
    st.text_area("Historique", value="\n".join(st.session_state.logs[:10]), height=150, label_visibility="collapsed")

# ----------------------------------------------------------------------------
# 10. BOUCLE DE SIMULATION TEMPS RÉEL
# ----------------------------------------------------------------------------
if auto_run:
    event = trigger_random_event()
    if event["type"] != "Aucun":
        log_event(f"{event['type']} — {event['desc']}")
    time.sleep(sim_speed)
    st.rerun()
