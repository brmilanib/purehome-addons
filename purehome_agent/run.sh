#!/usr/bin/with-contenv bashio
# shellcheck shell=bash
bashio::log.info "Iniciando PureHome Agent..."
cd /opt/purehome || exit 1
exec python3 -u -m agent
