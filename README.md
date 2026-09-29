# RTL Agentic EDA — Local Agentic RTL-to-EDA Automation

## Run the dashboard safely

Start Uvicorn without a reloader while running EDA jobs. The pipeline writes
testbenches, synthesis files and reports under `runs/`; a reloader can restart
the server mid-job if it watches generated workspaces.

```bash
source .venv/bin/activate
uvicorn backend.main:app --host 127.0.0.1 --port 8000
```

For development reloads, use `uvicorn backend.main:app --reload --reload-dir backend --port 8000` so `runs/` is not watched.

The message `Vector RAG unavailable; using SQLite FTS5 fallback` is expected
when Qdrant is not installed or running. It does not block EDA execution.

## Precise project summary

**RTL Agentic EDA is a local, FastAPI-based agentic hardware-design system that converts a natural-language RTL request into an executable EDA workflow, generates SystemVerilog and Cocotb artifacts with local LLMs, runs simulation/synthesis/STA/physical-design stages, diagnoses tool failures, proposes a minimal artifact-level repair, validates and re-runs the failed stage, automatically rolls back unsafe repairs, and stores every failure and repair outcome as searchable engineering experience.**

The project combines:

- **FastAPI + WebSocket** for the local GUI/API and live pipeline events.
- **Ollama** for local LLM planning, code generation, debugging and review.
- **SQLite** as the system of record for jobs, stages, artifacts, events and repair history.
- **Hybrid RAG** using **SQLite FTS5 + Qdrant vector search + embeddings + metadata-aware reranking**.
- **Neo4j + GraphQL** as an optional knowledge graph for relationships among jobs, failures, repairs, artifacts and retrieved evidence.
- **Yosys, Cocotb/Icarus or Verilator, OpenSTA and OpenROAD/OpenLane** as the EDA integration boundaries.
- **Bounded self-healing**: the agent never receives unrestricted shell access; it edits one selected workspace artifact through a unified diff, validates the patch, runs the affected stage and rolls back when the candidate is unsafe or unsuccessful.

The design goal is not merely to generate RTL. It is to create a **closed learning loop**:

```text
Natural-language hardware request
        ↓
LLM planning
        ↓
RTL + testbench generation
        ↓
Simulation → Synthesis → STA → Physical Design → Verification
        ↓
        failure?
        ↓
Failure log + diagnosis
        ↓
Hybrid RAG retrieves prior EDA experience
        ↓
Minimal patch generation
        ↓
Patch validation
        ↓
Re-run failed stage + quality gates
        ↓
   ┌────┴────┐
   ↓         ↓
 accepted   failed
   ↓         ↓
 store      rollback
   └────┬────┘
        ↓
Future jobs can retrieve the experience
```

---

# 1. What the project does

A user can enter a request such as:

```text
Create a 4 bit adder and run the complete EDA flow.
```

The system creates a job and executes a planned workflow:

1. **Planning** — convert the request into a structured EDA plan.
2. **RTL generation** — generate synthesizable SystemVerilog.
3. **Testbench generation** — generate a Cocotb testbench.
4. **Simulation** — execute the generated verification flow.
5. **Synthesis** — synthesize the RTL with Yosys.
6. **Static timing analysis** — run OpenSTA when configured.
7. **Physical design** — invoke the configured OpenROAD/OpenLane boundary.
8. **Physical verification** — invoke configured DRC/LVS verification hooks.
9. **Reporting** — collect verified status, outputs, metrics and artifacts.

Every stage records status, attempts, output, errors and metrics in SQLite.

If an executable stage fails, the system enters the repair loop rather than blindly continuing.

---

# 2. High-level architecture

```text
                           Browser
                              │
                              ▼
                         FastAPI GUI
                              │
                 ┌────────────┴────────────┐
                 │                         │
             REST API                 WebSocket
                 │                         │
                 └────────────┬────────────┘
                              ▼
                       Workflow Engine
                              │
             ┌────────────────┼─────────────────┐
             │                │                 │
             ▼                ▼                 ▼
          Planner           Coder           Debugger/Reviewer
             │                │                 │
             └────────────────┼─────────────────┘
                              │
                           Ollama
                              │
                       Local LLM models
                              │
                              ▼
                 ┌─────────────────────────┐
                 │       EDA adapters      │
                 │                         │
                 │ Cocotb / simulator      │
                 │ Yosys                   │
                 │ OpenSTA                 │
                 │ OpenROAD/OpenLane       │
                 │ Physical verification   │
                 └────────────┬────────────┘
                              │
                       failure / result
                              │
                              ▼
                    Agentic repair loop
                              │
              ┌───────────────┴────────────────┐
              │                                │
              ▼                                ▼
        Hybrid RAG                         Neo4j graph
     SQLite FTS5 + Qdrant               optional knowledge graph
              │
              ▼
       repair experience
```

---

# 3. Project components

## FastAPI application

`backend/main.py`

Provides:

- local HTML/CSS/JavaScript GUI;
- REST API;
- WebSocket live events;
- GraphQL endpoint;
- health endpoint.

There is **no Node.js/npm frontend server**. FastAPI serves the frontend directly.

Main endpoints:

```text
GET  /
GET  /health

POST /api/design
GET  /api/designs
GET  /api/design/{job_id}
GET  /api/design/{job_id}/artifact

GET  /api/rag/health
POST /api/rag/reindex

GET  /api/graph/health
GET  /api/graph/{job_id}

WS   /ws/{job_id}
POST /graphql
```

Swagger/OpenAPI is available at:

```text
http://127.0.0.1:8000/docs
```

---

# 4. Workflow engine

`backend/workflow/engine.py`

## Sky130 synthesis and timing handoff

The synthesis adapter runs Yosys from the configured OpenLane container image
(`KLAYOUT_CONTAINER_RUNTIME` and `KLAYOUT_CONTAINER_IMAGE`) to keep Yosys and
ABC compatible. It runs `synth -noabc`, maps sequential cells with
`dfflibmap` when needed, then maps combinational logic with `abc -liberty` using
`OPENSTA_LIBERTY_PATH`. The resulting `synthesized.v` is the input to both
OpenSTA and OpenROAD. The netlist preview is rendered from that output directly
and rasterized to `synthesized.png` with `rsvg-convert` or ImageMagick, so
synthesis does not launch a second Yosys process just to draw a graph.

The focused real-tool regression is `pytest -q tests/test_sky130_flow.py`. It
checks Sky130 mapping, timing analysis, and OpenROAD routing/layout when the
configured tools and PDK files are available.

## OpenROAD GDSII stream-out

The physical-design adapter runs floorplanning, placement, optional CTS,
global routing, detailed routing with OpenROAD, and real DEF-to-GDSII stream-out
with KLayout. It requires real PDK files; it never renames a DEF or converts an
image into GDS. Configure these paths in `.env` before running a physical-design
job:

`KLAYOUT_STREAM_OUT_SCRIPT` and the KLayout container settings must point to a
KLayout stream-out implementation. OpenROAD remains responsible for the routed
DEF; KLayout merges that DEF with the standard-cell GDS library using the PDK
layer map.

```text
OPENROAD_PATH=/absolute/path/to/openroad
OPENROAD_TECH_LEF_PATH=/absolute/path/to/technology.lef
OPENROAD_CELL_LEF_PATH=/absolute/path/to/standard-cell.lef
OPENSTA_LIBERTY_PATH=/absolute/path/to/standard-cell.lib
OPENROAD_CELL_GDS_PATH=/absolute/path/to/standard-cell.gds
OPENROAD_GDS_MAP_PATH=/absolute/path/to/gds-layer.map
OPENROAD_SDC_PATH=/absolute/path/to/constraints.sdc
OPENROAD_CLOCK_BUFFER=sky130_fd_sc_hd__clkbuf_1
```

Run the application from the project directory:

```bash
source .venv/bin/activate
uvicorn backend.main:app --reload --reload-dir backend --port 8000
```

After a design job completes, the genuine stream-out is at
`runs/<job_id>/final.gds`; the routed database is also written as
`runs/<job_id>/final.def`.

Physical verification is reported separately. OpenROAD now requires
`design_is_routed` before accepting the DEF, and Sky130 Magic runs DRC against
`final.gds`, producing `drc.log`. LVS is not inferred from the image or DEF:
configure real transistor-level extracted and reference SPICE netlists plus
the Sky130 Netgen setup before LVS can pass:

```text
LVS_REFERENCE_SPICE=/absolute/path/to/reference.spice
LVS_EXTRACTED_SPICE=/absolute/path/to/extracted.spice
NETGEN_SETUP_PATH=/absolute/path/to/sky130A/libs.tech/netgen/sky130A_setup.tcl
```

The workflow engine is the central orchestrator.

It:

- creates the workspace;
- invokes the planner;
- invokes RTL/testbench generation;
- runs EDA stages;
- records results;
- diagnoses failures;
- retrieves historical evidence;
- applies bounded repairs;
- validates candidate changes;
- re-runs the failed stage;
- applies stage-specific quality gates;
- rolls back failed repairs;
- stores repair outcomes;
- mirrors relevant information into Neo4j when available.

The engine deliberately keeps tool execution separate from LLM reasoning.

The LLM proposes changes; the workflow engine decides whether the change is valid and executes the EDA tool.

---

# 5. Local LLM layer

The project uses Ollama through:

`backend/agent/llm.py`

Default model configuration:

```text
Planner  : llama3.1:latest
Coder    : qwen2.5-coder:7b
Reviewer : llama3.1:latest
```

The model roles are intentionally separated:

### Planner

Converts the natural-language request into a structured flow.

### Coder

Generates:

- SystemVerilog;
- Cocotb;
- repair patches.

### Debugger

Analyzes:

- stage;
- error;
- stdout/stderr;
- historical RAG evidence.

It returns structured diagnosis information such as:

```json
{
  "diagnosis": "...",
  "target": "sta",
  "artifact": "sta.tcl",
  "action": "modify",
  "confidence": 0.86,
  "patch_instructions": "..."
}
```

### Reviewer

Produces a final summary using verified pipeline facts.

---

# 6. EDA execution layer

EDA adapters are under:

```text
backend/eda/
```

Current integration boundaries include:

```text
backend/eda/simulator.py
backend/eda/yosys.py
backend/eda/opensta.py
backend/eda/openlane.py
backend/eda/shell.py
```

The project supports a safe mock mode so the complete agentic workflow can be tested without a full PDK/toolchain.

## Mock mode

```text
MOCK_EDA=true
```

This is the recommended first run.

## Real EDA mode

```text
MOCK_EDA=false
```

Real physical design and timing analysis are technology-specific. OpenSTA requires appropriate Liberty/SDC information, and OpenROAD/OpenLane requires a compatible PDK/configuration.

Mock timing values must never be interpreted as real silicon timing.

---

# 7. Agentic failure repair

The project uses bounded self-healing.

When a stage fails:

```text
EDA command
   ↓
non-zero exit / failed result
   ↓
capture output + metrics
   ↓
store failure in RAG
   ↓
retrieve similar historical experience
   ↓
LLM debugger diagnoses failure
   ↓
select ONE repairable artifact
   ↓
LLM produces unified diff
   ↓
validate patch text
   ↓
apply patch
   ↓
candidate pre-flight validation
   ↓
re-run failed stage
   ↓
quality gate
```

If the candidate fails:

```text
candidate failed
     ↓
restore backup
     ↓
record rolled_back
```

If the candidate succeeds:

```text
candidate passed
     ↓
record accepted
     ↓
store repair + metrics
     ↓
link retrieved evidence
```

The system also stores:

- `patch_rejected`;
- `validation_failed`;
- `rolled_back`;
- `accepted`.

This is important because **failed repair attempts are useful negative experience** for future retrieval.

The repair system does not give the LLM unrestricted shell access.

---

# 8. Hybrid RAG architecture

The RAG implementation is:

```text
backend/rag.py
```

The system uses **two retrieval paths**.

```text
                         Current failure
                              │
                 ┌────────────┴────────────┐
                 │                         │
                 ▼                         ▼
             SQLite FTS5              Qdrant
             lexical search        semantic search
                 │                         │
                 └────────────┬────────────┘
                              ▼
                    candidate merge
                              │
                              ▼
                         reranking
                              │
                              ▼
                         top results
```

## SQLite FTS5

Good for exact EDA terminology:

```text
OpenSTA
link_design
synthesized.v
syntax error
clock
Yosys
read_verilog
```

It is especially useful for identifiers, commands, filenames and exact tool errors.

## Qdrant

Provides semantic retrieval.

For example, historical experience containing:

```text
STA cannot resolve a clock signal.
```

can be retrieved for a new failure such as:

```text
OpenSTA reports an unresolved timing clock.
```

even when the wording is different.

## Embedding model

Default:

```text
BAAI/bge-small-en-v1.5
```

The model converts the RAG experience into vectors.

The vector itself is stored in Qdrant; SQLite remains the authoritative document store.

---

# 9. Why SQLite and Qdrant are both used

The project intentionally does **not** replace SQLite.

SQLite is the system of record:

```text
SQLite
 ├── jobs
 ├── steps
 ├── artifacts
 ├── repair_history
 ├── events
 └── rag_documents
```

Qdrant is the semantic retrieval index:

```text
Qdrant
 └── eda_experience
       ├── vector
       └── metadata
```

This separation gives the project:

- durable application state;
- easy local development;
- exact lexical search;
- semantic search;
- metadata filtering;
- simple backup of authoritative RAG records;
- ability to rebuild vectors from SQLite.

Qdrant can therefore be recreated without losing the original RAG documents.

---

# 10. RAG document model

Failures are stored with information such as:

```text
job_id
kind = failure
stage
tool
artifact
failure_type
title
diagnosis
log
outcome
created_at
```

Repairs additionally store:

```text
attempt
patch
rationale
runner output
metrics
outcome
```

Example repair experience:

```text
Stage: sta
Tool: opensta
Failure type: clock_missing
Artifact: sta.tcl

Diagnosis:
OpenSTA could not resolve the clock definition.

Repair rationale:
Added the missing clock constraint using the existing clock port.

Outcome:
accepted

Patch:
...

Metrics:
...
```

---

# 11. Hybrid ranking

Retrieval combines:

```text
45% semantic similarity
25% lexical retrieval
10% token overlap
 7% stage match
 5% artifact match
 4% tool match
 4% outcome signal
```

These are starting weights, not claims of optimality. They should be tuned against an evaluation dataset as the RAG corpus grows.

The ranking deliberately gives strong importance to EDA metadata because a semantically similar failure from a different stage/tool can be misleading.

For example:

```text
stage = sta
tool  = opensta
artifact = sta.tcl
```

is more useful than a generic "syntax error" from an unrelated synthesis script.

---

# 12. RAG indexing lifecycle

Whenever a failure occurs:

```text
failure
  ↓
SQLite rag_documents
  ↓
FTS5 trigger
  ↓
Qdrant embedding
```

Whenever a repair attempt finishes:

```text
repair outcome
  ↓
SQLite rag_documents
  ↓
FTS5 trigger
  ↓
Qdrant embedding
```

Existing SQLite records can be backfilled into Qdrant using:

```text
POST /api/rag/reindex
```

or directly:

```python
from backend.rag import EdaRAG

rag = EdaRAG()
rag.reindex_vectors()
```

Qdrant points use the SQLite document ID, so reindexing is idempotent.

---

# 13. RAG fallback behavior

Vector RAG is enabled by default in the updated project.

If Qdrant or the embedding dependency is unavailable, the project automatically falls back to SQLite FTS5.

Therefore:

```text
Qdrant available
      ↓
Hybrid RAG

Qdrant unavailable
      ↓
FTS5 RAG
```

This prevents an unavailable vector service from stopping an EDA job.

For production experiments, Qdrant should be enabled and monitored rather than relying permanently on the fallback.

---

# 14. RAG configuration

`.env.example` contains:

```text
RAG_VECTOR_ENABLED=true

QDRANT_URL=http://127.0.0.1:6333
QDRANT_API_KEY=
QDRANT_COLLECTION=eda_experience

RAG_EMBEDDING_MODEL=BAAI/bge-small-en-v1.5
```

Change these values if Qdrant is hosted elsewhere.

---

# 15. Run Qdrant locally

The repository includes:

```text
docker-compose.qdrant.yml
```

Start Qdrant:

```bash
docker compose -f docker-compose.qdrant.yml up -d
```

Check the container:

```bash
docker ps
```

The default Qdrant API is:

```text
http://127.0.0.1:6333
```

Stop it:

```bash
docker compose -f docker-compose.qdrant.yml down
```

The named Docker volume keeps Qdrant's vector data between container restarts.

---

# 16. First-time setup

## Requirements

Recommended:

```text
Python 3.11+
Ollama
Docker (for Qdrant)
```

For real EDA mode, also install the required EDA tools and compatible technology files.

## Create environment

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
```

## Configure environment

```bash
cp .env.example .env
```

Start Qdrant:

```bash
docker compose -f docker-compose.qdrant.yml up -d
```

Check Ollama:

```bash
ollama --version
ollama list
```

Start Ollama if required:

```bash
ollama serve
```

Pull the configured models if they are not already installed:

```bash
ollama pull llama3.1:latest
ollama pull qwen2.5-coder:7b
```

The planner model can be changed in `.env` to match a model installed locally.

---

# 17. Start the application

From the project root:

```bash
source .venv/bin/activate
uvicorn backend.main:app --reload --host 127.0.0.1 --port 8000
```

Open:

```text
GUI:
http://127.0.0.1:8000

API docs:
http://127.0.0.1:8000/docs

GraphQL:
http://127.0.0.1:8000/graphql
```

Check:

```bash
curl http://127.0.0.1:8000/health
curl http://127.0.0.1:8000/api/rag/health
```

The RAG health endpoint reports:

- SQLite document count;
- Qdrant collection;
- Qdrant vector count;
- embedding model;
- active backend (`hybrid` or `fts5`).

---

# 18. First end-to-end test

Keep:

```text
MOCK_EDA=true
```

In the GUI enter:

```text
Create a 4 bit adder and run the complete EDA flow.
```

Click:

```text
Run Pipeline
```

The dashboard should show the stages progressing through the configured workflow.

Generated artifacts are placed under:

```text
runs/<job_id>/
```

Typical files:

```text
plan.json
rtl/design.sv
testbench/test_design.py
synthesis.ys
sta.tcl
reports/report.json
reports/report.html
```

---

# 19. Real EDA mode

After the mock workflow works:

```text
MOCK_EDA=false
```

## Simulation

Verify the simulator:

```bash
iverilog -V
```

and Cocotb:

```bash
python -c "import cocotb; print(cocotb.__version__)"
```

The starter simulator adapter invokes:

```text
pytest -q -s
```

inside the generated testbench directory.

For a production setup, configure the preferred Cocotb runner/Makefile.

## Yosys

```bash
yosys -V
```

The generated Yosys script reads the SystemVerilog and produces synthesis outputs.

## OpenSTA

```bash
sta -version
```

A real timing flow requires compatible:

- Liberty files;
- SDC/clock constraints;
- synthesized netlist;
- technology information.

## OpenROAD/OpenLane

Physical design requires a compatible installation and PDK.

The adapter is an integration boundary, not a universal PDK configuration.

---

# 20. SQLite persistence

The main database is:

```text
eda_pipeline.db
```

It stores:

```text
jobs
steps
artifacts
repair_history
events
rag_documents
```

SQLite uses WAL mode to improve concurrent read/write behavior.

RAG-specific tables include:

```text
rag_documents
rag_documents_fts
```

The FTS5 table is an external-content index over `rag_documents`.

Insert, update and delete triggers keep FTS5 synchronized.

---

# 21. Neo4j knowledge graph

Neo4j is optional and best-effort.

Configure:

```text
NEO4J_URI=neo4j://localhost:7687
NEO4J_USERNAME=neo4j
NEO4J_PASSWORD=your-password
NEO4J_DATABASE=neo4j
```

The graph captures relationships such as:

```text
(Job)-[:HAS_FAILURE]->(Failure)-[:LED_TO]->(Repair)
(Job)-[:HAS_REPAIR]->(Repair)-[:MODIFIES]->(Artifact)
(Repair)-[:USED_EVIDENCE]->(Evidence)
```

The graph is useful for questions involving relationships rather than simple text retrieval.

For example:

```text
Which repairs were attempted for failures in this job?
Which evidence contributed to an accepted repair?
Which artifacts were repeatedly modified?
```

Neo4j is not required for the core EDA pipeline.

---

# 22. GraphQL

GraphQL is available at:

```text
/graphql
```

Example:

```graphql
query {
  graphHealth

  jobGraph(jobId: "YOUR_JOB_ID") {
    job {
      labels
      properties
    }
    related {
      relationship
      node {
        labels
        properties
      }
    }
  }

  failures(jobId: "YOUR_JOB_ID") {
    id
    stage
    failureType
    artifact
    diagnosis
  }

  repairs(jobId: "YOUR_JOB_ID") {
    id
    stage
    attempt
    artifact
    outcome
    rationale
  }
}
```

There is also a `similarFailures` GraphQL query backed by the graph layer.

---

# 23. Repository structure

```text
.
├── backend/
│   ├── agent/
│   │   ├── coder.py
│   │   ├── llm.py
│   │   ├── planner.py
│   │   ├── prompts.py
│   │   └── reviewer.py
│   │
│   ├── api/
│   │   ├── routes.py
│   │   └── websocket.py
│   │
│   ├── eda/
│   │   ├── base.py
│   │   ├── shell.py
│   │   ├── simulator.py
│   │   ├── yosys.py
│   │   ├── opensta.py
│   │   └── openlane.py
│   │
│   ├── workflow/
│   │   ├── engine.py
│   │   ├── events.py
│   │   └── models.py
│   │
│   ├── reports/
│   │   └── generator.py
│   │
│   ├── rag.py
│   ├── graph.py
│   ├── graphql_schema.py
│   ├── db.py
│   ├── config.py
│   ├── main.py
│   └── web/
│       ├── index.html
│       ├── app.js
│       └── style.css
│
├── runs/
├── eda_pipeline.db
├── requirements.txt
├── .env.example
├── docker-compose.qdrant.yml
└── README.md
```

---

# 24. Important design principles

## 1. LLMs propose; tools verify

An LLM-generated repair is never considered correct merely because the model produced it.

The EDA toolchain must verify it.

```text
LLM proposal
    ↓
patch validation
    ↓
tool execution
    ↓
quality gate
    ↓
accept / rollback
```

## 2. Smallest possible repair

The repair agent is instructed to modify exactly one artifact and produce a unified diff.

This reduces accidental changes.

## 3. No unrestricted shell access

The LLM does not receive arbitrary shell execution capability.

The workflow engine controls tool execution.

## 4. Historical evidence is not ground truth

RAG provides prior experience.

The current artifact and current EDA execution determine whether the proposed repair is valid.

## 5. Negative experience is retained

A rolled-back repair is still valuable because it tells the agent what previously did not work.

## 6. Vector storage is rebuildable

SQLite stores the authoritative RAG document.

Qdrant stores the derived embedding index.

Therefore Qdrant can be rebuilt with:

```text
POST /api/rag/reindex
```

---

# 25. Troubleshooting

## `npm: command not found`

Ignore it.

This project does not require Node.js/npm.

## Ollama connection refused

```bash
ollama list
curl http://localhost:11434/api/tags
```

Start Ollama if necessary.

## Model not found

Check:

```bash
ollama list
```

Then make `.env` match the installed model names.

## Qdrant connection refused

Check:

```bash
docker ps
```

Start it:

```bash
docker compose -f docker-compose.qdrant.yml up -d
```

Then:

```bash
curl http://127.0.0.1:6333
```

If Qdrant remains unavailable, the RAG falls back to SQLite FTS5.

## Embedding model download takes time

The first vector indexing/search operation may download:

```text
BAAI/bge-small-en-v1.5
```

After it is cached locally, subsequent runs are faster.

## RAG says `fts5`

Check:

```bash
curl http://127.0.0.1:8000/api/rag/health
```

Look at:

```text
backend
qdrant_error
qdrant_documents
```

## Rebuild vectors

```bash
curl -X POST http://127.0.0.1:8000/api/rag/reindex
```

## FastAPI import errors

Run from the project root:

```bash
source .venv/bin/activate
pip install -r requirements.txt
uvicorn backend.main:app --reload --port 8000
```

## Real EDA command not found

```bash
which yosys
which sta
which iverilog
```

Make sure the tools are on `PATH`.

---

# 26. Recommended development progression

```text
1. Python environment
        ↓
2. Ollama + local models
        ↓
3. MOCK_EDA=true
        ↓
4. FastAPI GUI + WebSocket
        ↓
5. SQLite persistence
        ↓
6. Agentic failure diagnosis
        ↓
7. SQLite FTS5 RAG
        ↓
8. Qdrant + embeddings
        ↓
9. Hybrid retrieval + reranking
        ↓
10. Repair validation + rollback
        ↓
11. Real Cocotb
        ↓
12. Real Yosys
        ↓
13. Real OpenSTA + Liberty/SDC
        ↓
14. OpenROAD/OpenLane
        ↓
15. Neo4j + GraphQL
        ↓
16. RAG evaluation/regression suite
```

---

# 27. Future improvements

The current hybrid RAG is intentionally lightweight. The next improvements should focus on measurable quality rather than adding infrastructure for its own sake.

Recommended next steps:

### RAG evaluation

Build a fixed dataset of:

```text
failure → relevant historical repairs
```

Measure:

```text
Recall@K
Precision@K
MRR
```

### Repair evaluation

Track:

```text
repair success rate
rollback rate
average repair attempts
time to successful repair
```

### Better metadata

Add:

```text
tool_version
yosys_version
opensta_version
pdk
language
design_type
failure_fingerprint
```

This allows retrieval to prefer experience from compatible environments.

### Failure fingerprints

Normalize tool errors into reusable signatures such as:

```text
sta|opensta|link_error|synthesized.v
```

This supports exact historical lookup before semantic search.

### Reranking model

The current reranker is deterministic and lightweight. A cross-encoder or specialized reranker can later be evaluated against the project's retrieval dataset.

### Better experience records

Separate:

```text
failure
diagnosis
repair_attempt
validation
final_outcome
```

rather than treating the complete experience only as a single text block.

---

# 28. Final project summary

**RTL Agentic EDA is a local, end-to-end agentic EDA platform that combines LLM reasoning with deterministic EDA execution. A natural-language hardware request is converted into RTL, verification and downstream EDA artifacts; each stage is executed and persisted; failures are diagnosed using local LLMs and historical EDA experience; the system generates a minimal artifact-level patch, validates it, re-runs the failed stage and automatically rolls it back when it does not pass. Every failure and repair outcome becomes persistent engineering memory through a hybrid SQLite FTS5 + Qdrant RAG system, while Neo4j provides an optional relationship graph and GraphQL interface. The architecture is deliberately bounded: LLMs propose actions, but the EDA tools and validation gates decide whether those actions are actually successful.**

The core idea can be summarized as:

```text
                 REQUEST
                    │
                    ▼
               PLAN WITH LLM
                    │
                    ▼
             GENERATE ARTIFACTS
                    │
                    ▼
              EXECUTE EDA FLOW
                    │
             ┌──────┴──────┐
             │             │
           PASS          FAIL
             │             │
             │             ▼
             │        DIAGNOSE
             │             │
             │             ▼
             │        HYBRID RAG
             │             │
             │             ▼
             │        GENERATE PATCH
             │             │
             │             ▼
             │        VALIDATE PATCH
             │             │
             │             ▼
             │        RE-RUN STAGE
             │          ┌──┴──┐
             │          │     │
             │        PASS   FAIL
             │          │     │
             │          │   ROLLBACK
             │          │     │
             └──────────┴─────┘
                       │
                       ▼
                STORE EXPERIENCE
                       │
                       ▼
                 FUTURE REPAIRS
```

The project therefore evolves from a simple **"prompt → RTL"** application into a **persistent, self-improving, tool-verified EDA automation system**.
