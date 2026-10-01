#!/usr/bin/env bash
# Opens the opportunities dashboard (macOS / Linux). Starts Ollama first if needed.
cd "$(dirname "$0")"

if ! curl -s --max-time 2 http://127.0.0.1:11434/api/version >/dev/null; then
  echo "Starting Ollama..."
  if [ -d "/Applications/Ollama.app" ]; then
    open -a Ollama
  else
    (ollama serve >/dev/null 2>&1 &)
  fi
  for i in $(seq 1 15); do
    curl -s --max-time 2 http://127.0.0.1:11434/api/version >/dev/null && break
    sleep 1
  done
fi

exec uv run vs serve
