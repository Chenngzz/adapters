#!/usr/bin/env bash
# Start the product the way the upstream runtime/product.sh does: the app
# listens on container port 3000 and is reachable at 127.0.0.1:13200. Upstream
# publishes 13200 with Docker; here a local TCP forwarder plays that role.
# Readiness is checked by the [environment.healthcheck] in task.toml.
if [ "${SWEEPER_AUTOSTART:-1}" = "1" ]; then
  socat TCP-LISTEN:13200,bind=127.0.0.1,fork,reuseaddr TCP:127.0.0.1:3000 \
    >/tmp/sweeper-forward.log 2>&1 &
  setsid bash -lc 'cd /workspace/product && HOST=0.0.0.0 PORT=3000 BROWSER=none product-yarn start >/tmp/product-dev-server.log 2>&1' \
    </dev/null >/dev/null 2>&1 &
fi
exec "$@"
