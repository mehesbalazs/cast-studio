#!/bin/bash
# Dupla kattintással indítható a Finderből. A mappa bárhová másolható:
# minden beállítás a data/ almappában marad, semmit nem ír rajta kívülre.
cd "$(dirname "$0")" || exit 1

PY="$(command -v python3 2>/dev/null)"
[ -x "$PY" ] || PY=/usr/bin/python3
if [ ! -x "$PY" ]; then
  echo "Nem találok python3-at. Telepítsd, vagy futtasd: python3 server.py"
  read -r -p "Nyomj Entert a bezáráshoz…" _
  exit 1
fi

exec "$PY" -u server.py "$@"
