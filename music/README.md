# Oversite Music Node (Lavalink)

One shared audio server that **every** bot dials into for music. Deploy it once;
turn music on for any bot by flipping the dashboard "Music Add-On" toggle and
pointing that bot at this node. No music code lives in the bots — they're thin
clients (wavelink). The bot never touches audio, so there's **no PyNaCl / ffmpeg /
YouTube bot-check** anywhere.

**Sources:** Deezer (optional, env-gated — the most reliable real-audio source
from datacenter hosts as of Aug 2026), SoundCloud (native, zero credentials, but
currently spotty: datacenter edge-blocks plus an open track-404 bug), direct HTTP
streams for radio (never blocked), and Spotify/Apple Music **link** resolution
(mirrored to Deezer→SoundCloud audio). YouTube is intentionally off — it is the
maintenance treadmill.

## Enabling Deezer (recommended for reliable playback)

Deezer authenticates with a login cookie, not your server's IP — so it streams
fine from Railway. Heads-up first: LavaSrc's Deezer playback decrypts Deezer's
streams, which violates Deezer's ToS — for a commercial product that's a
business/legal call, not just a technical one. If you enable it, use a burner
account. Set these on the node service:

| Variable | Value |
|---|---|
| `DEEZER_ENABLED` | `true` |
| `DEEZER_ARL` | the `arl` cookie from deezer.com — log in (burner account), DevTools → Application → Cookies → copy `arl`. Re-paste every few months when the session dies. |
| `DEEZER_MASTER_KEY` | Deezer's track master decryption key. Not distributed in this repo; it circulates publicly in open-source Deezer downloader projects (deemix-family repos). |

With Deezer on, plain searches and Spotify/Apple links resolve by exact ISRC
match on Deezer — i.e. the real studio recordings. A free account streams
MP3_128 (more than enough for Discord voice).

---

## Deploy to Railway (one time)

1. **New service → Deploy from repo**, point it at this repo and set the service's
   **Root Directory** to `music/`. Railway will build the `Dockerfile` here.
2. Set these **Variables** on the service:

   | Variable | Value |
   |---|---|
   | `LAVALINK_SERVER_PASSWORD` | a long random string (this is the node's only auth) |
   | `_JAVA_OPTIONS` | `-Xmx512M` (raise to `-Xmx1G`+ for heavy multi-guild load) |
   | `SPOTIFY_CLIENT_ID` | *(optional)* only if anonymous Spotify stops working |
   | `SPOTIFY_CLIENT_SECRET` | *(optional)* pair with the id above |

   > `PORT` is injected by Railway automatically — do **not** set it. Lavalink
   > binds it, and Railway routes the service's public HTTPS domain to it.
3. Give the service **≥ 1 GB RAM** (Lavalink is a JVM). Enable a **public domain**
   under Settings → Networking. You'll get something like
   `https://oversite-music.up.railway.app`.
4. Deploy. In the logs you should see Lavalink start and
   `Lavalink is ready to accept connections`.

> Prefer Railway **private networking** if the bots run in the same project — then
> the node is never publicly reachable and bots connect over the internal host.
> Public HTTPS is fine too; traffic is TLS-terminated by Railway's edge.

## Point a bot at the node

On the **bot's** Railway service (not this one), set:

| Variable | Value |
|---|---|
| `LAVALINK_URI` | `https://oversite-music.up.railway.app` (the node's public URL; wavelink derives `wss://` from it) |
| `LAVALINK_PASSWORD` | the **same** value as `LAVALINK_SERVER_PASSWORD` above |

Then enable **Music Add-On** for that bot in the dashboard and save. On next
config load the bot connects to the node and registers `/play /skip /stop /pause
/resume /volume /loop /queue /nowplaying /radio`. Every bot uses the same two
variables and the same node — that's the "link once, reuse everywhere" part.

## How it plays

- **`/play <song name>`** → searches SoundCloud (`scsearch`).
- **`/play <SoundCloud link>`** → plays it directly.
- **`/play <Spotify/Apple link or playlist>`** → LavaSrc reads the metadata and
  plays the matching audio from SoundCloud (by exact ISRC first, then by search).
- **`/radio [genre]`** → a continuous SomaFM stream via Lavalink's `http` source.

## Maintenance

- **SoundCloud**: essentially none — it auto-fetches its own client id. (Watch: in
  late 2025 SoundCloud began 403-ing *some* datacenter/VPN IP ranges; if searches
  start failing, that's the cause — a fallback source or a different egress fixes
  it. It is **not** YouTube's per-request bot wall.)
- **Spotify links**: the anonymous path can drift; if Spotify links stop resolving,
  create a free Spotify developer app and set `SPOTIFY_CLIENT_ID/SECRET`. SoundCloud
  playback is unaffected either way.
- **Versions**: `application.yml` pins LavaSrc `4.8.3` and the image is
  `lavalink:4-alpine`. Bump deliberately, not automatically.
