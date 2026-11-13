#!/usr/bin/env bash
# Alias → universal deploy
cd "$(dirname "$0")"
exec bash deploy.sh "$@"
