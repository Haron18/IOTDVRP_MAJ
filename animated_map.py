"""
animated_map.py — Carte interactive Leaflet avec animation fluide des camions
CÔTÉ NAVIGATEUR (JavaScript), sans rafraîchissement de la page Streamlit.

Le dépôt, les commandes et le tracé des tournées sont dessinés une seule fois ; la
position de chaque camion en mouvement est ensuite interpolée en continu par le
navigateur (requestAnimationFrame), selon sa vitesse réelle et le facteur
d'accélération du temps choisis dans l'interface — ce qui donne un mouvement fluide,
façon "scène animée", au lieu d'un saut de position à chaque rafraîchissement Streamlit.

Le rafraîchissement Streamlit (auto-refresh existant) continue de faire progresser
l'état "officiel" (km parcourus, commandes livrées, KPI...) toutes les quelques
secondes ; l'animation JS comble visuellement l'intervalle entre deux rafraîchissements
en utilisant la même vitesse, donc sans décalage perceptible au moment du resynch.
"""
from math import asin, cos, radians, sin, sqrt


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    dlat = radians(lat2 - lat1)
    dlon = radians(lon2 - lon1)
    a = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2) ** 2
    return 2 * r * asin(sqrt(a))


def render_animated_map_html(depot_coords, orders, trucks, height: int = 520) -> str:
    """
    depot_coords : (lat, lon)
    orders : liste de dicts {id, client, lat, lon, demand_kg, temp_max, time_window, priority}
    trucks : liste de dicts, un par camion PHYSIQUE, avec :
      - label ("V1", "V2", ...), color (nom de couleur CSS)
      - used (bool) : a une tournée assignée par le solveur
      - shape : tracé complet [[lat,lon], ...] de toute la rotation (si used)
      - trip_shapes : liste de tracés (un par trajet) pour le dessin des polylines
      - total_km, traveled_km, speed_kmh
      - animate (bool) : True si la simulation temps réel (auto-refresh) est active
      - sim_minutes_per_real_second : facteur d'accélération courant
      - status_label : texte affiché dans l'info-bulle
    """
    import json

    priority_color = {"URGENTE": "red", "HAUTE": "orange", "NORMALE": "blue"}
    orders_js = [
        {
            "lat": o["lat"], "lon": o["lon"], "id": o["id"], "client": o["client"],
            "demand_kg": o["demand_kg"], "temp_max": o["temp_max"],
            "time_window": o["time_window"],
            "color": priority_color.get(o["priority"], "blue"),
        }
        for o in orders
    ]

    trucks_js = []
    for t in trucks:
        shape = t.get("shape") or []
        cum_km = [0.0]
        for i in range(1, len(shape)):
            lat1, lon1 = shape[i - 1]
            lat2, lon2 = shape[i]
            cum_km.append(cum_km[-1] + _haversine_km(lat1, lon1, lat2, lon2))
        trucks_js.append({
            "label": t["label"], "color": t["color"], "used": bool(t.get("used")),
            "shape": shape, "cumKm": cum_km, "tripShapes": t.get("trip_shapes", []),
            "totalKm": t.get("total_km", 0.0), "traveledKm": t.get("traveled_km", 0.0),
            "speedKmh": t.get("speed_kmh", 30), "animate": bool(t.get("animate")),
            "simMinPerRealSec": t.get("sim_minutes_per_real_second", 0.0),
            "statusLabel": t.get("status_label", ""),
        })

    data_json = json.dumps({"depot": list(depot_coords), "orders": orders_js, "trucks": trucks_js})

    return f"""
<div id="dvrp-map" style="width:100%; height:{height}px; border-radius:8px;"></div>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" />
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>
  .truck-icon {{ font-size: 22px; line-height: 22px; text-align: center;
                 filter: drop-shadow(0 0 2px rgba(0,0,0,.6)); }}
  .depot-icon {{ font-size: 22px; line-height: 22px; text-align: center; }}
</style>
<script>
(function () {{
  const DATA = {data_json};
  const map = L.map('dvrp-map').setView(DATA.depot, 11);
  L.tileLayer('https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png', {{
      attribution: '&copy; OpenStreetMap contributors', maxZoom: 19,
  }}).addTo(map);

  L.marker(DATA.depot, {{
      icon: L.divIcon({{className: 'depot-icon', html: '🏠', iconSize: [24, 24]}}),
  }}).addTo(map).bindPopup('<b>Dépôt Central — Oued Smar</b>');

  DATA.orders.forEach(o => {{
      L.circleMarker([o.lat, o.lon], {{
          radius: 8, color: o.color, fillColor: o.color, fillOpacity: 0.85, weight: 2,
      }}).addTo(map).bindPopup(
          `<b>${{o.client}}</b><br>Charge: ${{o.demand_kg}} kg<br>` +
          `Temp. max: ${{o.temp_max}}°C<br>Fenêtre: ${{o.time_window}}`
      ).bindTooltip(o.id);
  }});

  function interpolate(shape, cumKm, targetKm) {{
      if (!shape || shape.length === 0) return null;
      if (targetKm <= 0) return shape[0];
      const total = cumKm[cumKm.length - 1];
      if (targetKm >= total) return shape[shape.length - 1];
      for (let i = 1; i < cumKm.length; i++) {{
          if (cumKm[i] >= targetKm) {{
              const segLen = cumKm[i] - cumKm[i - 1];
              const ratio = segLen === 0 ? 0 : (targetKm - cumKm[i - 1]) / segLen;
              return [
                  shape[i - 1][0] + (shape[i][0] - shape[i - 1][0]) * ratio,
                  shape[i - 1][1] + (shape[i][1] - shape[i - 1][1]) * ratio,
              ];
          }}
      }}
      return shape[shape.length - 1];
  }}

  const truckMarkers = [];
  DATA.trucks.forEach(t => {{
      if (t.used && t.shape.length) {{
          t.tripShapes.forEach(path => {{
              L.polyline(path, {{color: t.color, weight: 4, opacity: 0.85}}).addTo(map);
          }});
      }}
      const startPos = (t.used && t.shape.length)
          ? interpolate(t.shape, t.cumKm, t.traveledKm) : DATA.depot;
      const marker = L.marker(startPos, {{
          icon: L.divIcon({{className: 'truck-icon', html: '🚚', iconSize: [24, 24]}}),
      }}).addTo(map).bindTooltip(`${{t.label}} — ${{t.statusLabel}}`);
      truckMarkers.push({{marker, t}});
  }});

  // Animation fluide côté navigateur : chaque camion avance en continu (60 images/s)
  // à sa vitesse réelle x facteur d'accélération, sans dépendre d'un rerun Streamlit.
  const startTime = performance.now();
  function animate(now) {{
      const elapsedSec = (now - startTime) / 1000;
      truckMarkers.forEach(({{marker, t}}) => {{
          if (t.used && t.animate && t.shape.length) {{
              const simMinutesElapsed = elapsedSec * t.simMinPerRealSec;
              const traveledKm = Math.min(
                  t.totalKm, t.traveledKm + t.speedKmh * (simMinutesElapsed / 60)
              );
              const pos = interpolate(t.shape, t.cumKm, traveledKm);
              if (pos) marker.setLatLng(pos);
          }}
      }});
      requestAnimationFrame(animate);
  }}
  requestAnimationFrame(animate);
}})();
</script>
"""
