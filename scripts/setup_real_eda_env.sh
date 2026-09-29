#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TOOL_ROOT="${TOOL_ROOT:-$ROOT_DIR/.tools}"
OPENROAD_SRC_DIR="${OPENROAD_SRC_DIR:-$TOOL_ROOT/OpenROAD}"
OPENROAD_INSTALL_DIR="${OPENROAD_INSTALL_DIR:-$TOOL_ROOT/OpenROAD/install}"
VENV_DIR="${VENV_DIR:-$ROOT_DIR/.venv}"
PREFER_APT="${PREFER_APT:-1}"

require_root() {
  if [[ ${EUID:-$(id -u)} -ne 0 ]]; then
    echo "This setup script must run as root (or via sudo)."
    echo "Example: sudo bash scripts/setup_real_eda_env.sh"
    exit 1
  fi
}

ensure_apt_packages() {
  export DEBIAN_FRONTEND=noninteractive
  apt-get update
  apt-get install -y --no-install-recommends \
    ca-certificates curl git build-essential cmake ninja-build pkg-config \
    python3 python3-venv python3-dev python3-tk tcl-dev tk-dev \
    bison flex swig libreadline-dev libffi-dev libxml2-dev libzstd-dev \
    iverilog verilator yosys gtkwave netgen magic imagemagick \
    libgtk-3-0 libgl1-mesa-dev libqt5svg5-dev \
    libboost-all-dev libgmp-dev libmpfr-dev libmpc-dev \
    libopenblas-dev
}

ensure_openroad() {
  mkdir -p "$TOOL_ROOT"

  if [[ ! -d "$OPENROAD_SRC_DIR/.git" ]]; then
    git clone --depth 1 --recursive https://github.com/The-OpenROAD-Project/OpenROAD.git "$OPENROAD_SRC_DIR"
  else
    git -C "$OPENROAD_SRC_DIR" pull --ff-only || true
    git -C "$OPENROAD_SRC_DIR" submodule update --init --recursive || true
  fi

  if [[ ! -x "$OPENROAD_INSTALL_DIR/bin/openroad" ]]; then
    cmake -S "$OPENROAD_SRC_DIR" -B "$OPENROAD_SRC_DIR/build" \
      -G Ninja \
      -DCMAKE_BUILD_TYPE=Release \
      -DCMAKE_INSTALL_PREFIX="$OPENROAD_INSTALL_DIR"
    cmake --build "$OPENROAD_SRC_DIR/build" --target openroad -j"$(nproc)"
    cmake --install "$OPENROAD_SRC_DIR/build"
  fi

  OPENROAD_BIN="${OPENROAD_BIN:-$OPENROAD_INSTALL_DIR/bin/openroad}"
  if [[ ! -x "$OPENROAD_BIN" ]]; then
    OPENROAD_BIN="$OPENROAD_SRC_DIR/build/bin/openroad"
  fi

  if [[ ! -x "$OPENROAD_BIN" ]]; then
    echo "OpenROAD was not built successfully: $OPENROAD_BIN"
    exit 1
  fi
}

ensure_python_env() {
  if [[ ! -d "$VENV_DIR" ]]; then
    python3 -m venv "$VENV_DIR"
  fi

  "$VENV_DIR/bin/python" -m pip install --upgrade pip setuptools wheel
  "$VENV_DIR/bin/pip" install -r "$ROOT_DIR/requirements.txt"
  "$VENV_DIR/bin/pip" install volare
}

ensure_volare_pdk() {
  if [[ ! -x "$VENV_DIR/bin/volare" ]]; then
    echo "Volare is not available in $VENV_DIR; skipping Sky130 bootstrap."
    return 0
  fi

  "$VENV_DIR/bin/volare" --help >/dev/null 2>&1 || return 0

  # volare may need the user to fetch the Sky130 family explicitly. If that fails,
  # we still keep the script idempotent and leave the user with a clear error.
  if ! "$VENV_DIR/bin/volare" --family sky130 --help >/dev/null 2>&1; then
    echo "volare is installed but its Sky130 bootstrap command is not available in this environment."
    echo "You can still continue by installing the PDK manually and then rerunning this script."
    return 0
  fi

  "$VENV_DIR/bin/volare" --family sky130 --install || true
}

find_first() {
  local pattern="$1"
  find "$ROOT_DIR" -path "$pattern" 2>/dev/null | head -n 1 || true
}

write_env_file() {
  local openroad_bin="${OPENROAD_BIN:-$OPENROAD_INSTALL_DIR/bin/openroad}"
  local sky130_root="$(find_first '*/.volare/*/sky130*' || true)"
  local techlef="$(find_first '*/sky130A/libs.ref/sky130_fd_sc_hd/techlef/*.tlef' || true)"
  local celllef="$(find_first '*/sky130A/libs.ref/sky130_fd_sc_hd/lef/*.lef' || true)"
  local cellgds="$(find_first '*/sky130A/libs.ref/sky130_fd_sc_hd/gds/*.gds' || true)"
  local gds_map="$(find_first '*/sky130A/libs.tech/klayout/tech/*.map' || true)"
  local klayout_lyt="$(find_first '*/sky130A/libs.tech/klayout/tech/*.lyt' || true)"
  local klayout_lyp="$(find_first '*/sky130A/libs.tech/klayout/tech/*.lyp' || true)"
  local magic_dir="$(find_first '*/sky130A/libs.tech/magic' || true)"
  local netgen_setup="$(find_first '*/sky130A/libs.tech/netgen/sky130A_setup.tcl' || true)"

  if [[ -z "$techlef" ]]; then
    techlef="/absolute/path/to/technology.lef"
  fi
  if [[ -z "$celllef" ]]; then
    celllef="/absolute/path/to/standard-cell.lef"
  fi
  if [[ -z "$cellgds" ]]; then
    cellgds="/absolute/path/to/standard-cell.gds"
  fi
  if [[ -z "$gds_map" ]]; then
    gds_map="/absolute/path/to/gds-layer.map"
  fi
  if [[ -z "$klayout_lyt" ]]; then
    klayout_lyt="/absolute/path/to/sky130A.lyt"
  fi
  if [[ -z "$klayout_lyp" ]]; then
    klayout_lyp="/absolute/path/to/sky130A.lyp"
  fi
  if [[ -z "$magic_dir" ]]; then
    magic_dir="/absolute/path/to/sky130A/libs.tech/magic"
  fi
  if [[ -z "$netgen_setup" ]]; then
    netgen_setup="/absolute/path/to/sky130A/libs.tech/netgen/sky130A_setup.tcl"
  fi

  cat > "$ROOT_DIR/.env" <<EOF
OLLAMA_BASE_URL=http://localhost:11434
PLANNER_MODEL=llama3.1:latest
CODER_MODEL=qwen2.5-coder:7b
REVIEWER_MODEL=llama3.1:latest
LLM_TIMEOUT=900
MOCK_EDA=false
MAX_RETRIES=3
WORKSPACE_ROOT=./runs

OPENSTA_LIBERTY_PATH=/absolute/path/to/standard-cell.lib
OPENROAD_TECH_LEF_PATH=$techlef
OPENROAD_CELL_LEF_PATH=$celllef
OPENROAD_CELL_GDS_PATH=$cellgds
OPENROAD_GDS_MAP_PATH=$gds_map
OPENROAD_SDC_PATH=
OPENROAD_CLOCK_BUFFER=sky130_fd_sc_hd__clkbuf_1
OPENROAD_CONGESTION_ITERATIONS=10
OPENROAD_PATH=$openroad_bin
KLAYOUT_TECH_FILE=$klayout_lyt
KLAYOUT_LAYER_PROPERTIES=$klayout_lyp
KLAYOUT_STREAM_OUT_SCRIPT=/absolute/path/to/openlane/stream_out.py
KLAYOUT_CONTAINER_IMAGE=docker.io/efabless/openlane:latest
KLAYOUT_CONTAINER_RUNTIME=podman
MAGIC_TECH_PATH=$magic_dir
LVS_REFERENCE_SPICE=/absolute/path/to/reference.spice
LVS_EXTRACTED_SPICE=/absolute/path/to/extracted.spice
NETGEN_SETUP_PATH=$netgen_setup

NEO4J_URI=neo4j://localhost:7687
NEO4J_USERNAME=neo4j
NEO4J_PASSWORD=change-me
NEO4J_DATABASE=neo4j

RAG_VECTOR_ENABLED=true
QDRANT_URL=http://127.0.0.1:6333
QDRANT_COLLECTION=eda_experience
RAG_EMBEDDING_MODEL=BAAI/bge-small-en-v1.5
YOSYS_GENERATE_GRAPH=true
LVS_ENABLED=false
EOF

  echo "Updated $ROOT_DIR/.env with a real-toolchain configuration."
}

main() {
  require_root
  ensure_apt_packages
  ensure_openroad
  ensure_python_env
  ensure_volare_pdk
  write_env_file

  echo
  echo "Real EDA bootstrap complete."
  echo "Next commands:"
  echo "  source $VENV_DIR/bin/activate"
  echo "  uvicorn backend.main:app --reload --reload-dir backend --port 8000"
  echo "  # then trigger a prompt such as: Create a D flip-flop and run the complete EDA flow."
  echo
  echo "If OpenROAD still does not launch, run:"
  echo "  $OPENROAD_BIN -help | head"
  echo "and update the paths in .env if your machine installs tools to a different prefix."
}

main "$@"
