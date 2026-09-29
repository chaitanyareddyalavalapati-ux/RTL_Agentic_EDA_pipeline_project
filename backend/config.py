import glob
import os
from pathlib import Path
from dotenv import load_dotenv


def _first_existing(*paths):
    for path in paths:
        if not path:
            continue
        candidate = Path(path).expanduser()
        if candidate.exists():
            return str(candidate)
    return ''


def _glob_first(*patterns):
    for pattern in patterns:
        matches = sorted(glob.glob(pattern, recursive=True))
        if matches:
            return matches[0]
    return ''


PROJECT_ROOT = Path(__file__).resolve().parent.parent
SKY130_TEST_ROOT = PROJECT_ROOT / '.tools' / 'OpenROAD' / 'test' / 'sky130hd'


load_dotenv(Path(__file__).resolve().parent.parent/'.env')
OLLAMA_BASE_URL=os.getenv('OLLAMA_BASE_URL','http://localhost:11434')
PLANNER_MODEL=os.getenv('PLANNER_MODEL','llama3.1:latest')
CODER_MODEL=os.getenv('CODER_MODEL','qwen2.5-coder:7b')
REVIEWER_MODEL=os.getenv('REVIEWER_MODEL','llama3.1:latest')
LLM_TIMEOUT=float(os.getenv('LLM_TIMEOUT','900'))
# Default to the real toolchain described in the project README. Set MOCK_EDA=true
# only for quick smoke tests when the EDA toolchain is unavailable.
MOCK_EDA=os.getenv('MOCK_EDA','false').lower()=='true'
MAX_RETRIES=int(os.getenv('MAX_RETRIES','3'))
WORKSPACE_ROOT=Path(os.getenv('WORKSPACE_ROOT','./runs'))
DEFAULT_RAM_DEPTH=int(os.getenv('DEFAULT_RAM_DEPTH','16'))

# Optional Neo4j graph / GraphQL configuration
NEO4J_URI=os.getenv('NEO4J_URI','')
NEO4J_USERNAME=os.getenv('NEO4J_USERNAME',os.getenv('NEO4J_USER','neo4j'))
NEO4J_PASSWORD=os.getenv('NEO4J_PASSWORD','')
NEO4J_DATABASE=os.getenv('NEO4J_DATABASE','neo4j')

# Hybrid RAG / Qdrant configuration. SQLite remains the source of truth.
RAG_VECTOR_ENABLED=os.getenv('RAG_VECTOR_ENABLED','true').lower()=='true'
QDRANT_URL=os.getenv('QDRANT_URL','http://127.0.0.1:6333')
QDRANT_API_KEY=os.getenv('QDRANT_API_KEY','')
QDRANT_COLLECTION=os.getenv('QDRANT_COLLECTION','eda_experience')
RAG_EMBEDDING_MODEL=os.getenv('RAG_EMBEDDING_MODEL','BAAI/bge-small-en-v1.5')

# Real OpenSTA technology library. Override this for another PDK/library.
OPENSTA_LIBERTY_PATH = _first_existing(
    os.getenv('OPENSTA_LIBERTY_PATH'),
    '/home/mirafra/VLSI_final_project/libraries/sky130_fd_sc_hd__tt_025C_1v80.lib',
    '/home/mirafra/.volare/volare/sky130/versions/*/sky130A/libs.ref/sky130_fd_sc_hd/lib/sky130_fd_sc_hd__tt_025C_1v80.lib',
    '/home/mirafra/final_presentation_VLSI/rtl_agentic_eda_autorepair_rag_neo4j_graphql_v5/.volare/volare/sky130/versions/*/sky130A/libs.ref/sky130_fd_sc_hd/lib/sky130_fd_sc_hd__tt_025C_1v80.lib',
    str(SKY130_TEST_ROOT / 'sky130_fd_sc_hd__tt_025C_1v80.lib'),
)
SKY130_LEF_PATH = _first_existing(
    os.getenv('SKY130_LEF_PATH'),
    '/home/mirafra/OpenROAD/src/gpl/test/sky130hd.lef',
    '/home/mirafra/final_presentation_VLSI/rtl_agentic_eda_autorepair_rag_neo4j_graphql_v5/.tools/OpenROAD/test/sky130hd/sky130_fd_sc_hd.lef',
    '/home/mirafra/final_presentation_VLSI/rtl_agentic_eda_autorepair_rag_neo4j_graphql_v5/.volare/volare/sky130/versions/*/sky130A/libs.ref/sky130_fd_sc_hd/lef/sky130_fd_sc_hd.lef',
    str(SKY130_TEST_ROOT / 'sky130_fd_sc_hd.lef'),
)
OPENROAD_TECH_LEF_PATH = _first_existing(
    os.getenv('OPENROAD_TECH_LEF_PATH'),
    '/home/mirafra/.volare/**/sky130A/libs.ref/sky130_fd_sc_hd/techlef/*.tlef',
    '/home/mirafra/final_presentation_VLSI/rtl_agentic_eda_autorepair_rag_neo4j_graphql_v5/.volare/**/sky130A/libs.ref/sky130_fd_sc_hd/techlef/*.tlef',
    '/home/mirafra/final_presentation_VLSI/**/sky130_fd_sc_hd__nom.tlef',
    str(SKY130_TEST_ROOT / 'sky130_fd_sc_hd__nom.tlef'),
)
OPENROAD_CELL_LEF_PATH = _first_existing(
    os.getenv('OPENROAD_CELL_LEF_PATH'),
    SKY130_LEF_PATH,
    '/home/mirafra/.volare/volare/sky130/versions/*/sky130A/libs.ref/sky130_fd_sc_hd/lef/sky130_fd_sc_hd.lef',
    '/home/mirafra/final_presentation_VLSI/rtl_agentic_eda_autorepair_rag_neo4j_graphql_v5/.volare/volare/sky130/versions/*/sky130A/libs.ref/sky130_fd_sc_hd/lef/sky130_fd_sc_hd.lef',
    str(SKY130_TEST_ROOT / 'sky130_fd_sc_hd.lef'),
)
OPENROAD_CELL_GDS_PATH = os.getenv('OPENROAD_CELL_GDS_PATH') or _glob_first(
    '/home/mirafra/.volare/**/sky130A/libs.ref/sky130_fd_sc_hd/gds/*.gds',
    '/home/mirafra/final_presentation_VLSI/rtl_agentic_eda_autorepair_rag_neo4j_graphql_v5/.volare/**/sky130A/libs.ref/sky130_fd_sc_hd/gds/*.gds',
)
OPENROAD_GDS_MAP_PATH = os.getenv('OPENROAD_GDS_MAP_PATH') or _glob_first(
    '/home/mirafra/.volare/**/sky130A/libs.tech/klayout/tech/*.map',
    '/home/mirafra/final_presentation_VLSI/rtl_agentic_eda_autorepair_rag_neo4j_graphql_v5/.volare/**/sky130A/libs.tech/klayout/tech/*.map',
)
OPENROAD_SDC_PATH = os.getenv('OPENROAD_SDC_PATH', '')
OPENROAD_CLOCK_BUFFER = os.getenv('OPENROAD_CLOCK_BUFFER', 'sky130_fd_sc_hd__clkbuf_1')
OPENROAD_CONGESTION_ITERATIONS = int(os.getenv('OPENROAD_CONGESTION_ITERATIONS', '10'))
KLAYOUT_TECH_FILE = os.getenv('KLAYOUT_TECH_FILE') or _glob_first(
    '/home/mirafra/.volare/**/sky130A/libs.tech/klayout/tech/*.lyt',
    '/home/mirafra/final_presentation_VLSI/rtl_agentic_eda_autorepair_rag_neo4j_graphql_v5/.volare/**/sky130A/libs.tech/klayout/tech/*.lyt',
)
KLAYOUT_LAYER_PROPERTIES = os.getenv('KLAYOUT_LAYER_PROPERTIES') or _glob_first(
    '/home/mirafra/.volare/**/sky130A/libs.tech/klayout/tech/*.lyp',
    '/home/mirafra/final_presentation_VLSI/rtl_agentic_eda_autorepair_rag_neo4j_graphql_v5/.volare/**/sky130A/libs.tech/klayout/tech/*.lyp',
)
KLAYOUT_STREAM_OUT_SCRIPT = os.getenv('KLAYOUT_STREAM_OUT_SCRIPT') or _glob_first(
    '/home/mirafra/**/stream_out.py',
    '/opt/**/stream_out.py',
    '/usr/**/stream_out.py',
    '/home/mirafra/final_presentation_VLSI/**/stream_out.py',
    '/home/mirafra/final_presentation_VLSI/rtl_agentic_eda_autorepair_rag_neo4j_graphql_v5/.venv-pdk/**/stream_out.py',
)
KLAYOUT_CONTAINER_IMAGE = os.getenv('KLAYOUT_CONTAINER_IMAGE', 'docker.io/efabless/openlane:latest')
KLAYOUT_CONTAINER_RUNTIME = os.getenv('KLAYOUT_CONTAINER_RUNTIME', 'podman')
MAGIC_TECH_PATH = os.getenv('MAGIC_TECH_PATH') or _first_existing(
    '/home/mirafra/.volare/volare/sky130/versions/*/sky130A/libs.tech/magic',
    '/home/mirafra/final_presentation_VLSI/rtl_agentic_eda_autorepair_rag_neo4j_graphql_v5/.volare/volare/sky130/versions/*/sky130A/libs.tech/magic',
)
LVS_REFERENCE_SPICE = os.getenv('LVS_REFERENCE_SPICE', '')
LVS_EXTRACTED_SPICE = os.getenv('LVS_EXTRACTED_SPICE', '')
NETGEN_SETUP_PATH = os.getenv('NETGEN_SETUP_PATH') or _glob_first(
    '/home/mirafra/.volare/**/sky130A/libs.tech/netgen/sky130A_setup.tcl',
    '/home/mirafra/final_presentation_VLSI/rtl_agentic_eda_autorepair_rag_neo4j_graphql_v5/.volare/**/sky130A/libs.tech/netgen/sky130A_setup.tcl',
)
OPENROAD_PATH = os.getenv('OPENROAD_PATH') or _first_existing(
    '/usr/local/bin/openroad',
    '/usr/bin/openroad',
    '/opt/openroad/bin/openroad',
    '/home/mirafra/OpenROAD/build/bin/openroad',
    '/home/mirafra/final_presentation_VLSI/rtl_agentic_eda_autorepair_rag_neo4j_graphql_v5/.tools/OpenROAD/build/bin/openroad',
    '/home/mirafra/final_presentation_VLSI/rtl_agentic_eda_autorepair_rag_neo4j_graphql_v5/.tools/OpenROAD/bin/openroad',
    str(PROJECT_ROOT / '.tools' / 'OpenROAD' / 'build' / 'bin' / 'openroad'),
)
YOSYS_GENERATE_GRAPH = os.getenv('YOSYS_GENERATE_GRAPH', 'true').lower() == 'true'
LVS_ENABLED = os.getenv('LVS_ENABLED', 'false').lower() == 'true'
