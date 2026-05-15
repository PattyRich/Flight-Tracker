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

TOKEN_URL    = "https://auth.opensky-network.org/auth/realms/opensky-network/protocol/openid-connect/token"
API_URL      = "https://opensky-network.org/api/states/all"
ADSBDB_AC    = "https://api.adsbdb.com/v0/aircraft/{icao}"
ADSBDB_CS    = "https://api.adsbdb.com/v0/callsign/{callsign}"

app = Flask(__name__)

# ── Shared state ──────────────────────────────────────────
state = {
    "plane":        None,
    "credits":      "?",
    "last_updated": None,
    "error":        None,
}
lock = threading.Lock()

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

# ── Helpers ───────────────────────────────────────────────
def haversine(lat1, lon1, lat2, lon2):
    R = 3958.8
    lat1, lon1, lat2, lon2 = map(math.radians, [lat1, lon1, lat2, lon2])
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    a    = math.sin(dlat/2)**2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon/2)**2
    return R * 2 * math.asin(math.sqrt(a))

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
    """Format an airport dict into 'Name (IATA)' string."""
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

# ── adsbdb: aircraft lookup by ICAO24 ────────────────────
def fetch_aircraft(icao24):
    """Returns aircraft details: model, manufacturer, operator, photo."""
    try:
        r = requests.get(ADSBDB_AC.format(icao=icao24.lower()), timeout=8)
        if r.status_code != 200:
            return {}
        aircraft = r.json().get("response", {}).get("aircraft") or {}
        return {
            "model":         aircraft.get("type"),
            "manufacturer":  aircraft.get("manufacturer"),
            "operator":      aircraft.get("registered_owner"),
            "registration":  aircraft.get("registration"),
            "photo_url":     aircraft.get("url_photo"),
            "photo_thumb":   aircraft.get("url_photo_thumbnail"),
        }
    except Exception as e:
        print(f"[adsbdb aircraft] error: {e}")
        return {}

# ── adsbdb: route lookup by callsign ─────────────────────
def fetch_route(callsign):
    """Returns origin/destination from callsign endpoint."""
    if not callsign or callsign.strip() in ("", "N/A"):
        return {}
    try:
        r = requests.get(ADSBDB_CS.format(callsign=callsign.strip()), timeout=8)
        if r.status_code != 200:
            print(f"[adsbdb route] {callsign} → HTTP {r.status_code}")
            return {}
        flightroute = r.json().get("response", {}).get("flightroute") or {}

        airline = flightroute.get("airline") or {}
        origin  = flightroute.get("origin") or {}
        dest    = flightroute.get("destination") or {}

        return {
            "airline":     airline.get("name"),
            "origin":      fmt_airport(origin),
            "destination": fmt_airport(dest),
        }
    except Exception as e:
        print(f"[adsbdb route] error: {e}")
        return {}

# ── Background polling thread ─────────────────────────────
def poll_loop():
    last_icao     = None
    last_callsign = None
    cached_ac     = {}
    cached_route  = {}

    while True:
        try:
            plane, dist, credits = fetch_closest()

            if plane:
                icao24   = plane[0]
                callsign = (plane[1] or "").strip()

                # New aircraft → re-fetch aircraft details
                if icao24 != last_icao:
                    print(f"[tracker] new aircraft: {icao24} / {callsign}")
                    cached_ac    = fetch_aircraft(icao24)
                    last_icao    = icao24
                    # Reset route cache so it re-fetches for new plane
                    cached_route  = {}
                    last_callsign = None

                # New callsign → re-fetch route
                if callsign and callsign != last_callsign:
                    print(f"[tracker] fetching route for callsign: {callsign}")
                    cached_route  = fetch_route(callsign)
                    last_callsign = callsign

                altitude  = meters_to_feet(plane[7])
                speed     = ms_to_mph(plane[9])
                heading   = plane[10]
                vert_rate = plane[11]
                on_ground = plane[8]

                # Operator: prefer airline name from route, fall back to aircraft owner
                operator = (
                    cached_route.get("airline")
                    or cached_ac.get("operator")
                    or plane[2]
                    or "N/A"
                )

                payload = {
                    "callsign":     callsign or "N/A",
                    "country":      plane[2] or "N/A",
                    "icao24":       icao24,
                    "registration": cached_ac.get("registration"),
                    "on_ground":    on_ground,
                    "status":       "On Ground" if on_ground else "In Flight",
                    "distance":     round(dist, 1),
                    "altitude_ft":  altitude,
                    "speed_mph":    speed,
                    "heading_deg":  round(heading) if heading else None,
                    "heading_dir":  bearing_to_compass(heading),
                    "trend":        vertical_trend(vert_rate),
                    "lat":          plane[6],
                    "lon":          plane[5],
                    # aircraft
                    "operator":     operator,
                    "model":        cached_ac.get("model"),
                    "manufacturer": cached_ac.get("manufacturer"),
                    "photo_url":    cached_ac.get("photo_url"),
                    "photo_thumb":  cached_ac.get("photo_thumb"),
                    # route
                    "origin":       cached_route.get("origin"),
                    "destination":  cached_route.get("destination"),
                }

                with lock:
                    state["plane"]        = payload
                    state["credits"]      = credits
                    state["last_updated"] = datetime.now().strftime("%H:%M:%S")
                    state["error"]        = None
            else:
                with lock:
                    state["plane"]        = None
                    state["credits"]      = credits
                    state["last_updated"] = datetime.now().strftime("%H:%M:%S")

        except Exception as e:
            print(f"[tracker] error: {e}")
            with lock:
                state["error"] = str(e)

        time.sleep(POLL_INTERVAL)

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
    })

@app.route("/api/plane")
def api_plane():
    with lock:
        return jsonify({
            "plane":        state["plane"],
            "credits":      state["credits"],
            "last_updated": state["last_updated"],
            "error":        state["error"],
        })

# ── Entry point ───────────────────────────────────────────
if __name__ == "__main__":
    t = threading.Thread(target=poll_loop, daemon=True)
    t.start()
    print(f"Flight tracker running at http://0.0.0.0:5000")
    print(f"Location : {HOME_CITY} ({HOME_LAT}, {HOME_LON})")
    print(f"Interval : {POLL_INTERVAL}s")
    app.run(host="0.0.0.0", port=5000, debug=False)
