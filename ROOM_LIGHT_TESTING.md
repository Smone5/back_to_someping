# Voxitale Home Assistant, Matter, and Raspberry Pi Setup

This guide documents the real smart-light setup used with this app.

Voxitale does not talk to Matter or Google Home directly. The app sends REST calls to **Home Assistant**, and Home Assistant controls the light entity that is backed by your Matter device.

## Deployed Path

`Voxitale on Google Cloud Run -> public HTTPS Home Assistant URL -> Home Assistant on Raspberry Pi -> Matter Server -> Matter lightbulb`

The public HTTPS Home Assistant URL can come from:

- Cloudflare Tunnel
- Nabu Casa
- another equivalent HTTPS tunnel/proxy you expose from the Pi

## Tech Stack

- Hardware: Raspberry Pi, 64-bit
- Home automation controller: Home Assistant in Docker
- Matter bridge/controller: Matter Server in Docker
- External access: Cloudflare Tunnel (`cloudflared`) or Nabu Casa
- App hosting: Voxitale frontend and backend on Google Cloud Run

## What This Repo Assumes

This repo does **not** provision Home Assistant, Matter Server, or Cloudflare Tunnel for you. Those run alongside your Raspberry Pi setup.

The app only needs three values in the light settings modal:

- Home Assistant URL
- Long-Lived Access Token
- Light Entity ID

Runtime behavior in this repo:

- Public `https://` Home Assistant URLs use the **backend relay** path
- Local/private Home Assistant URLs such as `http://192.168.x.x:8123` use the **browser-direct** path
- Matter stays behind Home Assistant; Voxitale never calls Matter Server directly

## Recommended Production Setup

### 1. Run Home Assistant and Matter on the Raspberry Pi

Your Raspberry Pi setup should have:

- a Home Assistant container
- a Matter Server container
- your Matter light paired into Home Assistant so it appears as a `light.*` entity

If you are using Docker Compose on the Pi, make sure the Home Assistant, Matter Server, and tunnel/proxy containers are all healthy before testing Voxitale.

### 2. Expose Home Assistant over HTTPS

Voxitale’s deployed cloud path needs a publicly reachable `https://` Home Assistant URL.

#### Option A: Cloudflare Tunnel

- Ensure the `cloudflared` container is running on the Pi
- In Cloudflare Zero Trust, go to `Networks -> Tunnels`
- Add a public hostname such as `ha.yourdomain.com`
- Point that hostname to your Home Assistant service, for example `http://homeassistant:8123`

If your Pi stack uses Docker Compose, this usually means the Cloudflare container and Home Assistant container share a Docker network.

#### Option B: Nabu Casa

- In Home Assistant, open `Settings -> Home Assistant Cloud`
- Enable `Remote Control`
- Use the generated `.ui.nabu.casa` URL as your public Home Assistant URL

### 3. Configure Home Assistant for proxy headers and browser compatibility

Edit `configuration.yaml` on the Raspberry Pi:

```yaml
http:
  # Required when Home Assistant is behind Cloudflare Tunnel or another reverse proxy.
  use_x_forwarded_for: true
  trusted_proxies:
    - 172.18.0.0/16

  # Recommended for local browser-applied mode and direct browser verification.
  # Add your real deployed Voxitale origin here.
  cors_allowed_origins:
    - http://localhost:3000
    - https://your-voxitale-origin.example
    # Example Cloud Run origin:
    # - https://storyteller-frontend-119014819686.us-central1.run.app
```

Notes:

- `trusted_proxies` must match the subnet used by your reverse proxy or tunnel container
- `172.18.0.0/16` is a common Docker bridge range, but yours may be different
- If Home Assistant returns `400 Bad Request`, verify the actual subnet with `docker network inspect` or `docker inspect`
- Restart Home Assistant after editing `configuration.yaml`

If you need the currently deployed Voxitale frontend origin for `cors_allowed_origins`, you can fetch it from Cloud Run:

```bash
gcloud run services describe storyteller-frontend \
  --region="$GOOGLE_CLOUD_LOCATION" \
  --project="$GOOGLE_CLOUD_PROJECT" \
  --format='value(status.url)'
```

Important distinction:

- For current Voxitale builds, public `https://` Home Assistant URLs are relayed through the backend, so Home Assistant browser CORS is not the primary requirement for deployed use
- `cors_allowed_origins` is still required for local/private browser-direct mode and is a safe compatibility setting to keep in place

### 4. Get the values Voxitale needs

#### Home Assistant URL

Use one of:

- your Cloudflare Tunnel URL, such as `https://ha.yourdomain.com`
- your Nabu Casa URL, such as `https://example.ui.nabu.casa`
- for local-only browser testing, a private URL such as `http://raspberrypi.local:8123` or `http://192.168.x.x:8123`

#### Long-Lived Access Token

In Home Assistant:

1. Open your user profile
2. Scroll to `Long-Lived Access Tokens`
3. Create a token for Voxitale
4. Copy it somewhere safe

#### Light Entity ID

In Home Assistant:

1. Open `Settings -> Devices & Services -> Entities`
2. Find your light
3. Copy the entity ID

Examples:

- `light.living_room_matter`
- `light.story_room`
- `light.demo_light`

### 5. Verify Home Assistant can control the light before using Voxitale

Test the state read:

```bash
curl \
  -H "Authorization: Bearer YOUR_LONG_LIVED_TOKEN" \
  https://YOUR_PUBLIC_HA_URL/api/states/light.YOUR_ENTITY_ID
```

Test turning the light on:

```bash
curl \
  -H "Authorization: Bearer YOUR_LONG_LIVED_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"entity_id":"light.YOUR_ENTITY_ID","brightness":180}' \
  https://YOUR_PUBLIC_HA_URL/api/services/light/turn_on
```

If you are doing local-only testing on the same network as the Pi, use your private URL instead of the public one.

### 6. Enter the values in Voxitale

Open the parent gate, click `Lights`, and enter:

- Home Assistant URL: your public `https://` URL for deployed cloud use
- Long-Lived Access Token: the Home Assistant token you created
- Light Entity ID: your Matter-backed light entity

Save the settings, then run `Test Light`.

## Matter-Specific Note

If your bulb is paired through Matter, Voxitale still only sees a normal Home Assistant light entity.

That means:

- the app does not need Matter credentials
- the app does not call Matter Server directly
- all Matter pairing and device health debugging still happens in Home Assistant and Matter Server

## Local and Developer Test Modes

### Option A: Local/private Raspberry Pi URL

Use a local URL such as `http://raspberrypi.local:8123` or `http://192.168.x.x:8123` when:

- your browser is on the same network as the Pi
- you are testing locally
- you want the browser-direct lighting path

For this mode:

- if the page is `https://`, the Home Assistant URL also needs to be `https://` or the browser will block mixed content
- your frontend origin must be listed in `http.cors_allowed_origins`

### Option B: Fastest local smoke test with the mock server

Start the mock Home Assistant server from the repo root:

```bash
python3 scripts/mock_home_assistant.py --token test-token --allow-origin http://localhost:3000
```

Then run the app locally and configure:

- Home Assistant URL: `http://127.0.0.1:8123`
- Long-Lived Access Token: `test-token`
- Light Entity ID: `light.story_room`

Verify the latest command:

```bash
curl http://127.0.0.1:8123/mock/state
```

### Option C: Raspberry Pi with Home Assistant `demo:` entities

If you want the full Raspberry Pi + Home Assistant path without touching a real bulb, enable the official [`demo:` integration](https://www.home-assistant.io/integrations/demo/) and use the resulting demo light entity.

## Troubleshooting

### Low voltage warning on the Raspberry Pi

If you see the lightning-bolt warning on the Pi, use a `5.1V 3A+` power supply. Low voltage can cause Matter Server, Home Assistant, or Cloudflare Tunnel to drop connections intermittently.

### `400 Bad Request` from Home Assistant behind a proxy

This usually means `trusted_proxies` does not match the Docker subnet used by your tunnel or reverse proxy container.

Check:

- `docker network inspect`
- `docker inspect`

Then update `trusted_proxies` and restart Home Assistant.

### Mixed content error

If the Voxitale page is loaded over `https://` but your Home Assistant URL is `http://`, the browser will block local browser-direct requests.

Fix:

- use an `https://` Home Assistant URL
- or do local testing entirely over `http://localhost:3000`

### `Test Light` fails

Check:

- the Home Assistant URL is correct
- the token is valid
- the entity ID is correct
- the light is available in Home Assistant
- Home Assistant is reachable from the cloud if you are using a public URL

If the failure mentions origin or relay access:

- verify the frontend origin is the deployed Voxitale frontend origin
- if you are doing local/private browser testing, make sure that origin is listed in `http.cors_allowed_origins`

### `401 Unauthorized` or `403 Forbidden`

Likely causes:

- the token is wrong
- the token was revoked
- the Home Assistant user does not have access to that entity

### Entity not found

The app can only control the exact entity ID you entered. Re-check the entity in `Settings -> Devices & Services -> Entities`.

## Recommended Use

For the most realistic production setup:

- Raspberry Pi
- Home Assistant in Docker
- Matter Server in Docker
- Cloudflare Tunnel or Nabu Casa
- a public `https://` Home Assistant URL entered into Voxitale

For the fastest safe smoke test:

- use the included mock Home Assistant server
