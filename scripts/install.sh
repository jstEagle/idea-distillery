#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_DIR="${ROOT_DIR}/.venv"
BIN_DIR="${HOME}/.local/bin"
DATA_DIR="${HOME}/.idea/distillery"
CONFIG_ENV="${DATA_DIR}/.env"

log() {
  printf '\033[32m%s\033[0m\n' "$*"
}

warn() {
  printf '\033[33m%s\033[0m\n' "$*" >&2
}

die() {
  printf '\033[31merror: %s\033[0m\n' "$*" >&2
  exit 1
}

find_python() {
  local candidate
  for candidate in python3.12 python3.11 python3.10 python3.9 python3; do
    if command -v "${candidate}" >/dev/null 2>&1; then
      if "${candidate}" - <<'PY' >/dev/null 2>&1
import sys
raise SystemExit(0 if sys.version_info >= (3, 9) else 1)
PY
      then
        command -v "${candidate}"
        return 0
      fi
    fi
  done
  return 1
}

upsert_env_value() {
  local file="$1"
  local key="$2"
  local value="$3"
  local tmp

  mkdir -p "$(dirname "${file}")"
  touch "${file}"
  chmod 600 "${file}"
  tmp="$(mktemp)"

  if grep -q "^${key}=" "${file}"; then
    awk -v key="${key}" -v value="${value}" '
      BEGIN { replaced = 0 }
      index($0, key "=") == 1 {
        print key "=" value
        replaced = 1
        next
      }
      { print }
      END {
        if (!replaced) {
          print key "=" value
        }
      }
    ' "${file}" > "${tmp}"
  else
    cat "${file}" > "${tmp}"
    printf '%s=%s\n' "${key}" "${value}" >> "${tmp}"
  fi

  mv "${tmp}" "${file}"
  chmod 600 "${file}"
}

has_openai_key() {
  if [ -n "${OPENAI_API_KEY:-}" ]; then
    return 0
  fi
  if [ -f "${CONFIG_ENV}" ] && grep -q '^OPENAI_API_KEY=' "${CONFIG_ENV}"; then
    return 0
  fi
  if [ -f "${HOME}/Documents/Programming/.env" ] && grep -q '^OPENAI_API_KEY=' "${HOME}/Documents/Programming/.env"; then
    return 0
  fi
  return 1
}

main() {
  cd "${ROOT_DIR}"

  local python_bin
  python_bin="$(find_python)" || die "Python 3.9+ is required."

  log "Using Python: ${python_bin}"
  "${python_bin}" -m venv "${VENV_DIR}"
  "${VENV_DIR}/bin/python" -m pip install --upgrade pip
  "${VENV_DIR}/bin/python" -m pip install -e ".[test]" >/dev/null 2>&1 || {
    "${VENV_DIR}/bin/python" -m pip install -e .
    "${VENV_DIR}/bin/python" -m pip install pytest
  }

  mkdir -p "${BIN_DIR}" "${DATA_DIR}/ideas" "${DATA_DIR}/scratch"
  ln -sf "${VENV_DIR}/bin/idea-distillery" "${BIN_DIR}/idea-distillery"
  ln -sf "${VENV_DIR}/bin/ida-distillery" "${BIN_DIR}/ida-distillery"

  if ! has_openai_key; then
    if [ -t 0 ]; then
      warn "OPENAI_API_KEY is not configured."
      printf 'Paste an OpenAI API key to save in %s, or press Enter to skip: ' "${CONFIG_ENV}"
      IFS= read -r -s api_key
      printf '\n'
      if [ -n "${api_key}" ]; then
        upsert_env_value "${CONFIG_ENV}" "OPENAI_API_KEY" "${api_key}"
        log "Saved OPENAI_API_KEY to ${CONFIG_ENV}"
      else
        warn "Skipped API key setup. Recording with --no-ai will work; AI transcription needs OPENAI_API_KEY."
      fi
    else
      warn "OPENAI_API_KEY is not configured. Set it in ${CONFIG_ENV} later."
    fi
  else
    log "OpenAI API key configuration found."
  fi

  if ! command -v pbcopy >/dev/null 2>&1; then
    warn "pbcopy was not found. Clipboard handoff prompts will be printed instead."
  fi

  "${VENV_DIR}/bin/python" -m pytest -q
  "${VENV_DIR}/bin/python" -m compileall -q src tests

  log "Installed Idea Distillery."
  log "Commands:"
  printf '  %s\n' "${BIN_DIR}/idea-distillery"
  printf '  %s\n' "${BIN_DIR}/ida-distillery"

  case ":${PATH}:" in
    *":${BIN_DIR}:"*) ;;
    *)
      warn "${BIN_DIR} is not currently on PATH."
      warn "Add this to your shell profile: export PATH=\"${BIN_DIR}:\$PATH\""
      ;;
  esac

  log "Try: ida-distillery devices"
}

main "$@"
