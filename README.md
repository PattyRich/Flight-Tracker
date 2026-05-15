# ✈ AIRWATCH // LOCAL

A self-hosted flight tracker dashboard that runs on a Raspberry Pi and displays the closest aircraft to your location in real time. Built with Flask, OpenSky Network, and adsbdb.

---

## What it does

- Finds the closest aircraft within ~170 miles every 60 seconds
- Shows callsign, airline, aircraft model, altitude, speed, heading
- Shows origin → destination route when available
- Displays a photo of the specific aircraft when available
- Shows a directional arrow indicating where the plane is **relative to your position** and which way your screen faces
- Runs as a local web server — open in any browser on your network

---

## Hardware

- Raspberry Pi 3 B+ (or any Pi with WiFi)
- MicroSD card (16GB minimum, 32GB recommended)
- Any HDMI monitor (connected via Mini HDMI cable for Pi 3 B+)
- Micro-USB 5V 2.4A+ power supply

---

## Setup

### 1. Flash the SD card

Download [Raspberry Pi Imager](https://www.raspberrypi.com/software/) and flash **Raspberry Pi OS Lite (64-bit)**.

In the imager settings (⚙️ icon before writing):
- Enable SSH
- Set username and password
- Set your WiFi SSID and password
- Set hostname (e.g. `pi3`)

### 2. SSH into the Pi

```bash
ssh youruser@pi3.local
```

### 3. Clone or copy the project

```bash
mkdir ~/Flight-Tracker && cd ~/Flight-Tracker
# copy files over via scp or git clone
```

### 4. Install dependencies

```bash
pip3 install -r requirements.txt --break-system-packages
```

### 5. Create your `.env` file

```bash
create .env file
```

```env
CLIENTID=your_opensky_client_id
CLIENTSECRET=your_opensky_client_secret

HOME_LAT=30.2672
HOME_LON=-97.7431
HOME_CITY=Austin, TX

RADIUS_DEG=2.5
POLL_INTERVAL=60

# Direction the viewer is facing looking at the monitor
# Accepts: NORTH, NE, EAST, SE, SOUTH, SW, WEST, NW — or degrees (0-360)
VIEWER_FACES=WEST 

### 6. Run it

```bash
python3 server.py
```

### 7. Open the dashboard

From any device on the same network:
```
http://pi3.local:5000
```

From the Pi itself in kiosk mode (fullscreen, no browser chrome):
```bash
chromium-browser --kiosk http://localhost:5000
```

---

## Getting OpenSky credentials

1. Create a free account at [opensky-network.org](https://opensky-network.org)
2. Go to your account → API → Create Client Credentials
3. Copy the Client ID and Client Secret into your `.env`

Free accounts get **4,000 API credits per day**. With a 60s poll interval and a small bounding box, you'll use ~1,440 credits/day — well within budget.

---

## VIEWER_FACES explained

The `VIEWER_FACES` value tells the dashboard which compass direction **you (the viewer) are facing** when looking at the screen.

The 3D arrow in the sidebar then shows where the plane is **from your perspective** — if the plane is directly in front of you (in the direction you are facing), the arrow points up. If it's behind you, the arrow points down.

Examples:
| VIEWER_FACES | Plane is {direction} of you. | Arrow points |
|---|---|---|
| NORTH | North | Up (Ahead) |
| NORTH | South | Down (Behind) |
| WEST | North | Right |
| EAST | North | Left |
| SOUTH | East | Left | 

---

## File structure

```
Flight-Tracker/
  server.py          ← Flask server + background polling thread
  requirements.txt   ← Python dependencies
  .env               ← Your credentials and config (never commit this)
  templates/
    index.html       ← Dashboard UI
  README.md
```

---

## APIs used

| API | Purpose | Cost |
|---|---|---|
| [OpenSky Network](https://opensky-network.org) | Live flight positions | Free (4000 credits/day) |
| [adsbdb.com](https://api.adsbdb.com) | Aircraft details, route, photos | Free, no key needed |

---

## Tips

- Run the server on boot by adding it to `/etc/rc.local` or as a `systemd` service
- Point your monitor's browser at `http://localhost:5000` in kiosk mode for a clean fullscreen display
- Adjust `RADIUS_DEG` in `.env` to widen or narrow the search area (larger = more credits per call)
- If a plane's route shows `---`, adsbdb doesn't have route data for that callsign — common for private/military flights
