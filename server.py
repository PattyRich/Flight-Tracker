import os
import time
import math
import threading
import requests
from datetime import datetime
from dotenv import load_dotenv
from flask import Flask, jsonify, render_template

load_dotenv()

# ── Config ────────────────────────────────────────────────
CLIENT_ID     = os.getenv("CLIENTID")
CLIENT_SECRET = os.getenv("CLIENTSECRET")
HOME_LAT      = float(os.getenv("HOME_LAT",   30.2672))
HOME_LON      = float(os.getenv("HOME_LON",  -97.7431))
HOME_CITY     = os.getenv("HOME_CITY",        "Austin, TX")
RADIUS_DEG    = float(os.getenv("RADIUS_DEG", 2.5))
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", 60))
CACHE_TTL     = 45  # seconds — serve cached data to any browser within this window

# VIEWER_FACES: direction the viewer faces when looking at the screen
_VIEWER_FACES_RAW = os.getenv("VIEWER_FACES", "NORTH")
_CARDINAL = {
    "N": 0,   "NORTH": 0,
    "NE": 45, "NORTHEAST": 45,
    "E": 90,  "EAST": 90,
    "SE": 135,"SOUTHEAST": 135,
    "S": 180, "SOUTH": 180,
    "SW": 225,"SOUTHWEST": 225,
    "W": 270, "WEST": 270,
    "NW": 315,"NORTHWEST": 315,
}
try:
    VIEWER_FACES = _CARDINAL.get(_VIEWER_FACES_RAW.upper(), None)
    if VIEWER_FACES is None:
        VIEWER_FACES = float(_VIEWER_FACES_RAW)
except ValueError:
    print(f"[config] invalid VIEWER_FACES '{_VIEWER_FACES_RAW}', defaulting to 0 (North)")
    VIEWER_FACES = 0

TOKEN_URL = "https://auth.opensky-network.org/auth/realms/opensky-network/protocol/openid-connect/token"
API_URL   = "https://opensky-network.org/api/states/all"
ADSBDB_AC = "https://api.adsbdb.com/v0/aircraft/{icao}"
ADSBDB_CS = "https://api.adsbdb.com/v0/callsign/{callsign}"

app = Flask(__name__)

# ── Token Manager ─────────────────────────────────────────
class TokenManager:
    def __init__(self):
        self.token      = None
        self.expires_at = 0

    def get_token(self):
        if self.token and time.time() < self.expires_at:
            return self.token
        return self._refresh()

    def _refresh(self):
        r = requests.post(TOKEN_URL, data={
            "grant_type":    "client_credentials",
            "client_id":     CLIENT_ID,
            "client_secret": CLIENT_SECRET,
        })
        r.raise_for_status()
        data            = r.json()
        self.token      = data["access_token"]
        self.expires_at = time.time() + data.get("expires_in", 1800) - 30
        return self.token

    def headers(self):
        return {"Authorization": f"Bearer {self.get_token()}"}

tokens = TokenManager()

# ── On-demand cache ───────────────────────────────────────
# Holds the last fetch result and enough context to avoid redundant
# aircraft / route lookups when the plane hasn't changed.
cache = {
    "payload":        None,   # full dict returned to the frontend
    "fetched_at":     0,      # epoch seconds of last successful fetch
    "last_icao":      None,
    "last_callsign":  None,
    "cached_ac":      {},
    "cached_route":   {},
    "credits":        "?",
    "last_updated":   None,
    "error":          None,
}
cache_lock = threading.Lock()

# ── Helpers ───────────────────────────────────────────────
def haversine(lat1, lon1, lat2, lon2):
    R = 3958.8
    lat1, lon1, lat2, lon2 = map(math.radians, [lat1, lon1, lat2, lon2])
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    a    = math.sin(dlat/2)**2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon/2)**2
    return R * 2 * math.asin(math.sqrt(a))

def bearing_to_plane(lat1, lon1, lat2, lon2):
    """True compass bearing (0-360) from home to the plane's position."""
    lat1, lon1, lat2, lon2 = map(math.radians, [lat1, lon1, lat2, lon2])
    dlon = lon2 - lon1
    x = math.sin(dlon) * math.cos(lat2)
    y = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(dlon)
    return (math.degrees(math.atan2(x, y)) + 360) % 360

def bearing_to_compass(deg):
    if deg is None:
        return "N/A"
    dirs = ["N","NE","E","SE","S","SW","W","NW"]
    return dirs[round(deg / 45) % 8]

def ms_to_mph(ms):
    return round(ms * 2.237) if ms else None

def meters_to_feet(m):
    return round(m * 3.281) if m else None

def vertical_trend(vr):
    if vr is None: return {"label": "Level",      "symbol": "→"}
    if vr > 1:     return {"label": "Climbing",   "symbol": "↑"}
    if vr < -1:    return {"label": "Descending", "symbol": "↓"}
    return                {"label": "Level",      "symbol": "→"}

def fmt_airport(airport):
    if not airport:
        return None
    name = airport.get("name") or airport.get("municipality") or None
    iata = airport.get("iata_code") or airport.get("icao_code") or None
    if name and iata:
        return f"{name} ({iata})"
    return name or iata or None

# ── OpenSky fetch ─────────────────────────────────────────
def fetch_closest():
    params = {
        "lamin": HOME_LAT - RADIUS_DEG,
        "lomin": HOME_LON - RADIUS_DEG,
        "lamax": HOME_LAT + RADIUS_DEG,
        "lomax": HOME_LON + RADIUS_DEG,
        "extended": 1,
    }
    r         = requests.get(API_URL, headers=tokens.headers(), params=params, timeout=10)
    remaining = r.headers.get("X-Rate-Limit-Remaining", "?")

    if r.status_code == 429:
        return None, None, remaining

    r.raise_for_status()
    states    = r.json().get("states") or []
    best      = None
    best_dist = float("inf")

    for s in states:
        lat, lon = s[6], s[5]
        if lat is None or lon is None:
            continue
        dist = haversine(HOME_LAT, HOME_LON, lat, lon)
        if dist < best_dist:
            best_dist = dist
            best      = s

    return (best, best_dist, remaining) if best else (None, None, remaining)

# ── adsbdb: aircraft lookup ───────────────────────────────
def fetch_aircraft(icao24):
    try:
        r = requests.get(ADSBDB_AC.format(icao=icao24.lower()), timeout=8)
        if r.status_code != 200:
            return {}
        aircraft = r.json().get("response", {}).get("aircraft") or {}
        return {
            "model":        aircraft.get("type"),
            "manufacturer": aircraft.get("manufacturer"),
            "operator":     aircraft.get("registered_owner"),
            "registration": aircraft.get("registration"),
            "photo_url":    aircraft.get("url_photo"),
            "photo_thumb":  aircraft.get("url_photo_thumbnail"),
        }
    except Exception as e:
        print(f"[adsbdb aircraft] error: {e}")
        return {}

# ── adsbdb: route lookup ──────────────────────────────────
def fetch_route(callsign):
    if not callsign or callsign.strip() in ("", "N/A"):
        return {}
    try:
        r = requests.get(ADSBDB_CS.format(callsign=callsign.strip()), timeout=8)
        if r.status_code != 200:
            print(f"[adsbdb route] {callsign} → HTTP {r.status_code}")
            return {}
        flightroute = r.json().get("response", {}).get("flightroute") or {}
        airline     = flightroute.get("airline") or {}
        origin      = flightroute.get("origin") or {}
        dest        = flightroute.get("destination") or {}
        return {
            "airline":     airline.get("name"),
            "origin":      fmt_airport(origin),
            "destination": fmt_airport(dest),
        }
    except Exception as e:
        print(f"[adsbdb route] error: {e}")
        return {}

# ── On-demand fetch (called from /api/plane) ──────────────
def refresh_cache():
    """Fetch fresh data from OpenSky + adsbdb and update the cache in-place."""
    try:
        plane, dist, credits = fetch_closest()

        if plane:
            icao24   = plane[0]
            callsign = (plane[1] or "").strip()

            if icao24 != cache["last_icao"]:
                print(f"[tracker] new aircraft: {icao24} / {callsign}")
                cache["cached_ac"]    = fetch_aircraft(icao24)
                cache["last_icao"]    = icao24
                cache["cached_route"] = {}
                cache["last_callsign"] = None

            if callsign and callsign != cache["last_callsign"]:
                print(f"[tracker] fetching route for: {callsign}")
                cache["cached_route"]  = fetch_route(callsign)
                cache["last_callsign"] = callsign

            plane_lat = plane[6]
            plane_lon = plane[5]
            altitude  = meters_to_feet(plane[7])
            speed     = ms_to_mph(plane[9])
            heading   = plane[10]
            vert_rate = plane[11]
            on_ground = plane[8]

            origin      = cache["cached_route"].get("origin")
            destination = cache["cached_route"].get("destination")
            home_city_short = HOME_CITY.split(",")[0].strip().upper()
            if altitude is not None and altitude < 5000:
                route_mentions_home = any(
                    home_city_short in (s or "").upper()
                    for s in [origin, destination]
                )
                if not route_mentions_home:
                    origin      = None
                    destination = None

            look_bearing = None
            rel_bearing  = None
            if plane_lat is not None and plane_lon is not None:
                look_bearing = bearing_to_plane(HOME_LAT, HOME_LON, plane_lat, plane_lon)
                rel_bearing  = (look_bearing - VIEWER_FACES) % 360

            operator = (
                cache["cached_route"].get("airline")
                or cache["cached_ac"].get("operator")
                or plane[2]
                or "N/A"
            )

            cache["payload"] = {
                "callsign":     callsign or "N/A",
                "country":      plane[2] or "N/A",
                "icao24":       icao24,
                "registration": cache["cached_ac"].get("registration"),
                "on_ground":    on_ground,
                "status":       "On Ground" if on_ground else "In Flight",
                "distance":     round(dist, 1),
                "altitude_ft":  altitude,
                "speed_mph":    speed,
                "heading_deg":  round(heading) if heading else None,
                "heading_dir":  bearing_to_compass(heading),
                "look_bearing": round(look_bearing) if look_bearing is not None else None,
                "look_dir":     bearing_to_compass(look_bearing),
                "rel_bearing":  round(rel_bearing) if rel_bearing is not None else None,
                "trend":        vertical_trend(vert_rate),
                "lat":          plane_lat,
                "lon":          plane_lon,
                "operator":     operator,
                "model":        cache["cached_ac"].get("model"),
                "manufacturer": cache["cached_ac"].get("manufacturer"),
                "photo_url":    cache["cached_ac"].get("photo_url"),
                "photo_thumb":  cache["cached_ac"].get("photo_thumb"),
                "origin":       origin,
                "destination":  destination,
            }
        else:
            cache["payload"] = None

        cache["credits"]      = credits
        cache["last_updated"] = datetime.now().strftime("%H:%M:%S")
        cache["error"]        = None

    except Exception as e:
        print(f"[tracker] error: {e}")
        cache["error"] = str(e)

    cache["fetched_at"] = time.time()

# ── Flask routes ──────────────────────────────────────────
@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/config")
def api_config():
    return jsonify({
        "city":          HOME_CITY,
        "lat":           HOME_LAT,
        "lon":           HOME_LON,
        "poll_interval": POLL_INTERVAL,
        "VIEWER_FACES":  VIEWER_FACES,
    })

@app.route("/api/plane")
def api_plane():
    with cache_lock:
        age = time.time() - cache["fetched_at"]
        if age >= CACHE_TTL:
            # Cache is stale — fetch fresh data before responding
            print(f"[cache] stale ({age:.0f}s old), refreshing …")
            refresh_cache()
        else:
            print(f"[cache] hit ({age:.0f}s old, TTL={CACHE_TTL}s)")

        return jsonify({
            "plane":        cache["payload"],
            "credits":      cache["credits"],
            "last_updated": cache["last_updated"],
            "error":        cache["error"],
        })

# ── Entry point ───────────────────────────────────────────
if __name__ == "__main__":
    print(f"Flight tracker running at http://0.0.0.0:5000")
    print(f"Location : {HOME_CITY} ({HOME_LAT}, {HOME_LON})")
    print(f"Interval : {POLL_INTERVAL}s (frontend-driven)")
    print(f"Cache TTL: {CACHE_TTL}s")
    print(f"Viewer   : faces {_VIEWER_FACES_RAW} ({VIEWER_FACES}°)")
    app.run(host="0.0.0.0", port=5000, debug=False)