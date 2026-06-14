#!/usr/bin/env bash
# Deploy a single stack ON THE SERVER.
# Usage:  bash scripts/deploy.sh <stack-name>
# Example: bash scripts/deploy.sh market-research
#
# Steps: decrypt SOPS secrets -> docker compose pull -> up -d -> prune dangling images.
# Requires: sops + age key at ~/.config/sops/age/keys.txt, docker compose v2.

set -euo pipefail

STACK="${1:-}"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STACK_DIR="${REPO_DIR}/stacks/${STACK}"

if [[ -z "${STACK}" ]]; then
  echo "ERROR: no stack given. Usage: bash scripts/deploy.sh <stack-name>" >&2
  echo "Available stacks:" >&2
  ls -1 "${REPO_DIR}/stacks" >&2
  exit 1
fi
if [[ ! -d "${STACK_DIR}" ]]; then
  echo "ERROR: stack '${STACK}' not found at ${STACK_DIR}" >&2
  exit 1
fi

cd "${STACK_DIR}"
echo "==> Deploying stack: ${STACK}"

# Decrypt secrets.env (SOPS) into a temporary, git-ignored .env that compose reads.
if [[ -f "secrets.env" ]]; then
  echo "==> Decrypting secrets.env -> .env"
  sops --decrypt secrets.env > .env
  chmod 600 .env
fi

echo "==> Validating compose"
docker compose config >/dev/null

echo "==> Pulling images"
docker compose pull

echo "==> Starting"
docker compose up -d --remove-orphans

echo "==> Pruning dangling images"
docker image prune -f >/dev/null || true

echo "==> Done: ${STACK}"
docker compose ps
