#!/bin/sh
PROJECT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
exec "$PROJECT_DIR/start.sh"
