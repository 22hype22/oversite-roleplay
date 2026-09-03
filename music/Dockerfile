# Shared Oversite music node = stock Lavalink v4 with our application.yml baked in.
# Pinned to a real tag (not :latest) so a surprise upstream change can't break
# the node on its own.
FROM ghcr.io/lavalink-devs/lavalink:4-alpine

# Our config. Secrets/ports come from env vars referenced inside it, so this file
# carries no credentials.
COPY application.yml /opt/Lavalink/application.yml

# Lavalink reads PORT from the env (Railway injects it). Heap is set via
# _JAVA_OPTIONS in the Railway service env (e.g. -Xmx512M).
EXPOSE 2333
