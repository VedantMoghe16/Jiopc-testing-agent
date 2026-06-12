#!/usr/bin/env bash
# build-deb.sh — build dist/jiopc-testing-agent_<version>_all.deb
#
# Intended to run on Linux (Ubuntu 24.04) where dpkg-deb is available.
# On macOS (dev machine) dpkg-deb is normally absent: the script still
# stages and VALIDATES the full package layout under packaging/debroot/,
# then prints a clear message and exits 0 instead of building the .deb.
#
# Usage:  bash packaging/build-deb.sh
# Output: dist/jiopc-testing-agent_<version>_all.deb   (Linux)
#         packaging/debroot/                            (staging, both OSes)
#
# Package layout (SPEC §Packaging):
#   /opt/jiopc-testing-agent/   code (src/), prompts/, default YAML, shims, docs
#   /usr/bin/jiopc-agent        launcher (prefers /opt/.../venv python)
#   /usr/bin/jiopc-agent-analyse
#   DEBIAN/{control,postinst,prerm}
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/.." >/dev/null 2>&1 && pwd)"
PKG_NAME=jiopc-testing-agent
APP_HOME=opt/${PKG_NAME}
DEBROOT="${SCRIPT_DIR}/debroot"
DIST_DIR="${REPO_ROOT}/dist"

# ---------------------------------------------------------------- version ---
VERSION="$(sed -n 's/^version = "\(.*\)"/\1/p' "${REPO_ROOT}/pyproject.toml" | head -n1)"
if [[ -z "${VERSION}" ]]; then
    echo "ERROR: could not read version from pyproject.toml" >&2
    exit 1
fi
echo "Building ${PKG_NAME} ${VERSION}" >&2

# ------------------------------------------------------------------ stage ---
rm -rf "${DEBROOT}"
mkdir -p "${DEBROOT}/DEBIAN" \
         "${DEBROOT}/${APP_HOME}" \
         "${DEBROOT}/usr/bin"

# Required payload — missing any of these is a hard error.
required=(
    "src/jiopc_agent"
    "jiopc-agent.yaml"
)
for rel in "${required[@]}"; do
    if [[ ! -e "${REPO_ROOT}/${rel}" ]]; then
        echo "ERROR: required payload missing: ${rel}" >&2
        exit 1
    fi
done

mkdir -p "${DEBROOT}/${APP_HOME}/src"
cp -R "${REPO_ROOT}/src/jiopc_agent" "${DEBROOT}/${APP_HOME}/src/"
cp "${REPO_ROOT}/jiopc-agent.yaml" "${DEBROOT}/${APP_HOME}/"

# Optional payload — warn but keep going (repo may be mid-build in dev).
optional=(
    "jiopc_agent.py"
    "analyse.py"
    "prompts"
    "README.md"
    "INSTALL.md"
    "design.md"
)
for rel in "${optional[@]}"; do
    if [[ -e "${REPO_ROOT}/${rel}" ]]; then
        cp -R "${REPO_ROOT}/${rel}" "${DEBROOT}/${APP_HOME}/"
    else
        echo "WARN: optional payload missing, skipping: ${rel}" >&2
    fi
done

# Never ship caches.
find "${DEBROOT}/${APP_HOME}" -type d -name __pycache__ -prune -exec rm -rf {} +

# ------------------------------------------------------------- launchers ---
# Both launchers prefer the postinst-created venv python (has Playwright),
# falling back to system python3 (Parts B/C only need pyyaml+psutil from apt).
cat > "${DEBROOT}/usr/bin/jiopc-agent" <<'LAUNCHER'
#!/bin/sh
# jiopc-agent — launcher for the JioPC testing agent.
# Prefers the Playwright venv python if postinst managed to create it.
set -eu
APP_HOME=/opt/jiopc-testing-agent
PY="$APP_HOME/venv/bin/python"
[ -x "$PY" ] || PY="$(command -v python3)"
PYTHONPATH="$APP_HOME/src${PYTHONPATH:+:$PYTHONPATH}"
export PYTHONPATH
# Default to the shipped config when the caller did not pass --config.
case " $* " in
    *" --config"*)
        exec "$PY" -m jiopc_agent "$@"
        ;;
    *)
        exec "$PY" -m jiopc_agent --config "$APP_HOME/jiopc-agent.yaml" "$@"
        ;;
esac
LAUNCHER

cat > "${DEBROOT}/usr/bin/jiopc-agent-analyse" <<'LAUNCHER'
#!/bin/sh
# jiopc-agent-analyse — post-run LLM analysis of a test_run_*.log.
# Needs LLM_BASE_URL / LLM_MODEL (LLM_API_KEY optional for local Ollama).
set -eu
APP_HOME=/opt/jiopc-testing-agent
PY="$APP_HOME/venv/bin/python"
[ -x "$PY" ] || PY="$(command -v python3)"
PYTHONPATH="$APP_HOME/src${PYTHONPATH:+:$PYTHONPATH}"
export PYTHONPATH
exec "$PY" -m jiopc_agent.analyse_cli "$@"
LAUNCHER

chmod 0755 "${DEBROOT}/usr/bin/jiopc-agent" "${DEBROOT}/usr/bin/jiopc-agent-analyse"

# -------------------------------------------------------- DEBIAN scripts ---
sed "s/@VERSION@/${VERSION}/" "${SCRIPT_DIR}/deb/control" > "${DEBROOT}/DEBIAN/control"
cp "${SCRIPT_DIR}/deb/postinst" "${DEBROOT}/DEBIAN/postinst"
cp "${SCRIPT_DIR}/deb/prerm" "${DEBROOT}/DEBIAN/prerm"
chmod 0755 "${DEBROOT}/DEBIAN/postinst" "${DEBROOT}/DEBIAN/prerm"

# Normalise payload permissions: dirs 755, files 644, keep launchers 755.
find "${DEBROOT}/${APP_HOME}" -type d -exec chmod 0755 {} +
find "${DEBROOT}/${APP_HOME}" -type f -exec chmod 0644 {} +

# --------------------------------------------------------------- validate ---
echo "Validating staging layout..." >&2
checks=(
    "DEBIAN/control"
    "DEBIAN/postinst"
    "DEBIAN/prerm"
    "${APP_HOME}/src/jiopc_agent/__init__.py"
    "${APP_HOME}/src/jiopc_agent/cli.py"
    "${APP_HOME}/jiopc-agent.yaml"
    "usr/bin/jiopc-agent"
    "usr/bin/jiopc-agent-analyse"
)
fail=0
for rel in "${checks[@]}"; do
    if [[ -e "${DEBROOT}/${rel}" ]]; then
        echo "  ok  ${rel}" >&2
    else
        echo "  MISSING  ${rel}" >&2
        fail=1
    fi
done
# Maintainer scripts and launchers must be executable, POSIX-sh clean.
for s in DEBIAN/postinst DEBIAN/prerm usr/bin/jiopc-agent usr/bin/jiopc-agent-analyse; do
    [[ -x "${DEBROOT}/${s}" ]] || { echo "  NOT EXECUTABLE  ${s}" >&2; fail=1; }
    sh -n "${DEBROOT}/${s}" || { echo "  SYNTAX ERROR  ${s}" >&2; fail=1; }
done
if [[ ${fail} -ne 0 ]]; then
    echo "ERROR: staging validation failed" >&2
    exit 1
fi
echo "Staging layout OK: ${DEBROOT}" >&2

# ------------------------------------------------------------------ build ---
if ! command -v dpkg-deb >/dev/null 2>&1; then
    echo "" >&2
    echo "dpkg-deb not found (expected on macOS dev machines)." >&2
    echo "Staging layout validated; run this script on Ubuntu 24.04 to produce" >&2
    echo "  dist/${PKG_NAME}_${VERSION}_all.deb" >&2
    exit 0
fi

mkdir -p "${DIST_DIR}"
DEB_PATH="${DIST_DIR}/${PKG_NAME}_${VERSION}_all.deb"
dpkg-deb --build --root-owner-group "${DEBROOT}" "${DEB_PATH}"
echo "Built ${DEB_PATH}" >&2
dpkg-deb --info "${DEB_PATH}" >&2
