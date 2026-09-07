# 🚚 DVRP & Tracking GPS Temps Réel — Alger

Application Streamlit de suivi GPS et de ré-optimisation dynamique de tournées de
livraison (DVRP), avec calcul d'itinéraires réels (OSRM), optimisation exacte
(Google OR-Tools, avec contrainte de capacité), et télémétrie temps réel (MQTT).

## Installation locale

```bash
git clone <votre-repo>
cd dvrp-tracking
python -m venv venv
source venv/bin/activate      # Windows : venv\Scripts\activate
pip install -r requirements.txt
streamlit run app.py
```

## Rotations multiples (capacité insuffisante)

Le curseur **« Trajets max par camion (rotations) »** (1 à 3) permet de simuler
le cas où la capacité totale de la flotte ne suffit pas pour tout livrer en un
seul passage : chaque camion peut alors effectuer plusieurs allers-retours au
dépôt. Techniquement, le solveur reçoit des véhicules « virtuels » (camion
physique × trajets max), tous de même capacité, puis les tournées obtenues
sont regroupées par camion physique (`dvrp_engine.group_multi_trip_routes`).

⚠️ Hypothèse simplificatrice assumée : le temps de rechargement au dépôt entre
deux rotations n'est pas modélisé (supposé instantané).

## Tracking simulé des camions (coordonnées GPS réalistes)

En complément de MQTT, un module dédié (`vehicle_tracker.py`) calcule
**localement** (sans réseau) la position GPS de chaque camion, à partir d'un
vrai modèle physique — pas d'une formule arbitraire :

- une **vitesse moyenne** assumée (réglable, 15 à 60 km/h),
- la **distance réelle** de la rotation complète du camion (issue d'OSRM),
- un **facteur d'accélération du temps** explicite (ex. « 1h simulée =
  5 min réelles »), pour observer une rotation complète en quelques minutes
  au lieu d'attendre le temps réel.

Un tableau **« Suivi GPS des camions »** affiche, pour chaque camion, sa
latitude/longitude actuelle, son statut, son avancement en %, et le temps
restant estimé. Le bouton **« +10 min simulées »** avance manuellement d'un
pas fixe ; le mode auto-refresh fait avancer chaque camion en continu, à la
vitesse choisie. Le bouton « Réinitialiser tracking » remet tous les camions
à leur point de départ.

## Simuler des données GPS/IoT en direct (optionnel)

Dans un second terminal :

```bash
python simulate_iot_publisher.py
```

Puis, dans l'application, cochez « Activer la connexion MQTT » dans la barre latérale.

## Preuve d'optimisation

Sous les indicateurs clés, un bandeau **« Preuve d'optimisation »** compare la
distance réellement parcourue par les tournées calculées par OR-Tools à une
**tournée de référence non optimisée** (commandes affectées dans leur ordre
d'apparition, sans aucune logique de séquence ou de répartition). Le gain en km
et en % mesure concrètement ce qu'apporte l'optimisation — les deux distances
utilisent les mêmes distances routières réelles (OSRM) et la même contrainte
de capacité, seule la méthode d'affectation/séquencement diffère.

## Événements dynamiques

Le menu « Événements dynamiques » de la barre latérale permet de simuler un
imprévu terrain (nouvelle commande, embouteillage, panne, client absent...).
Chaque événement **modifie réellement les données de la simulation** (ajout ou
retrait d'une commande, pénalité sur les distances, véhicule mis hors service,
priorité relevée) et déclenche une vraie réoptimisation OR-Tools au prochain
calcul — ce n'est pas qu'un message dans le journal :

| Événement | Effet concret |
|---|---|
| `NOUVELLE_COMMANDE` | Ajoute une commande urgente aléatoire autour du dépôt |
| `ANNULATION_COMMANDE` / `CLIENT_ABSENT` | Retire une commande active tirée au hasard |
| `EMBOUTEILLAGE` | Multiplie toutes les distances par 1,4 |
| `ROUTE_FERMEE` | Multiplie toutes les distances par 1,9 |
| `PANNE_VEHICULE` | Retire un camion de la flotte disponible |
| `ALERTE_TEMPERATURE` | Fait passer une commande active en priorité `URGENTE` |
| `SORTIE_ZONE` | Alerte seule, aucune donnée modifiée |

Le bouton « Réinitialiser les événements » efface tous ces effets cumulés.

## Déploiement sur Streamlit Community Cloud

1. Poussez ce dossier sur un dépôt GitHub public (ou privé si vous avez un compte payant).
2. Allez sur https://share.streamlit.io
3. Connectez votre compte GitHub, sélectionnez le dépôt et pointez sur `app.py`.
4. Cliquez sur **Deploy**.

## Limites à connaître (usage démonstration / pédagogique)

- **OSRM public (`router.project-osrm.org`)** : serveur de démonstration gratuit,
  débit limité et sans garantie de disponibilité. Pour un usage en production,
  hébergez votre propre instance OSRM ou utilisez un service payant
  (OpenRouteService, Mapbox, Google Routes API...).
- **Broker MQTT public (`broker.hivemq.com`)** : accessible à tout le monde,
  sans authentification. Ne jamais y envoyer de données sensibles réelles.
  Pour un vrai projet, utilisez un broker privé (HiveMQ Cloud, EMQX, AWS IoT...).
- **Résolution OR-Tools** : limitée à 5 secondes de recherche pour rester
  réactive dans l'interface ; sur de très grosses instances (>200 clients),
  augmentez `time_limit_s` dans `dvrp_engine.py`.
