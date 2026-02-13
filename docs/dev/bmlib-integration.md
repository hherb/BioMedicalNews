# bmlib Integration

bmnews delegates shared infrastructure to [bmlib](https://github.com/hherb/bmlib), a companion library providing LLM abstraction, database utilities, template rendering, quality assessment, and more.

This guide covers which bmlib modules bmnews uses, how they're integrated, and how to extend both projects together.

## Dependency relationship

bmlib is installed as a Git dependency:

```toml
# pyproject.toml
dependencies = [
    "bmlib @ git+https://github.com/hherb/bmlib.git",
]
```

Optional dependency groups pull in bmlib extras:

```toml
[project.optional-dependencies]
anthropic = ["bmlib[anthropic]"]
ollama = ["bmlib[ollama]"]
postgresql = ["bmlib[postgresql]"]
transparency = ["bmlib[transparency]"]
```

## bmlib modules used by bmnews

### `bmlib.llm` — LLM provider abstraction

**Used in:** `pipeline.py` (client creation), `scoring/relevance_agent.py` (via BaseAgent)

`LLMClient` provides a unified interface to multiple LLM providers:

```python
from bmlib.llm import LLMClient

# Created in pipeline.build_llm_client()
llm = LLMClient(
    default_provider="ollama",
    ollama_host="http://localhost:11434",
)
```

**Key concepts:**
- Model strings use `"provider:model_name"` format (e.g., `"ollama:llama3.1"`, `"anthropic:claude-sonnet-4-5-20250929"`)
- The client routes requests to the correct provider
- Tracks token usage and costs
- Supports JSON mode for structured responses

bmnews doesn't call `LLMClient` directly for scoring — it goes through `BaseAgent`. The client is constructed in `build_llm_client()` and passed to the scoring layer.

### `bmlib.db` — Database abstraction

**Used in:** `db/schema.py`, `db/operations.py`, `pipeline.py`, `cli.py`

Pure functions over DB-API connections:

```python
from bmlib.db import (
    connect_sqlite,       # Used in open_db() for SQLite backend
    connect_postgresql,   # Used in open_db() for PostgreSQL backend
    execute,              # Used in all write operations
    fetch_one,            # Used in get_paper_by_doi()
    fetch_all,            # Used in queries returning multiple rows
    fetch_scalar,         # Used in paper_exists()
    transaction,          # Used as context manager for atomic writes
    create_tables,        # Used in init_db()
    table_exists,         # Available for schema checks
)
```

**Pattern:** Every operation in `db/operations.py` takes a `conn` parameter. bmlib handles the actual SQL execution, cursor management, and transaction boundaries.

```python
# Example from operations.py
def paper_exists(conn, doi):
    ph = _placeholder(conn)
    val = fetch_scalar(conn, f"SELECT 1 FROM papers WHERE doi = {ph}", (doi,))
    return val is not None
```

### `bmlib.templates` — Jinja2 template engine

**Used in:** `pipeline.py` (engine creation), `scoring/relevance_agent.py` (via BaseAgent), `digest/renderer.py`

```python
from bmlib.templates import TemplateEngine

# Created in pipeline.build_template_engine()
engine = TemplateEngine(
    user_dir=Path("~/.bmnews/templates"),   # User overrides (optional)
    default_dir=Path("templates/"),          # Built-in defaults
)
```

**Resolution order:** user directory first, then default directory. This lets users override any template without modifying the package.

The engine is used directly in `render_digest()` and indirectly through `BaseAgent.render_template()` in the scoring agent.

### `bmlib.agents` — Base agent class

**Used in:** `scoring/relevance_agent.py`

`BaseAgent` provides the scaffolding for LLM-powered agents:

```python
from bmlib.agents.base import BaseAgent

class RelevanceAgent(BaseAgent):
    def score(self, title, abstract, interests, categories):
        prompt = self.render_template("relevance_scoring.txt", ...)
        system = self.render_template("relevance_system.txt")
        response = self.chat(
            [self.system_msg(system), self.user_msg(prompt)],
            json_mode=True,
        )
        result = self.parse_json(response.content)
        return result
```

**What BaseAgent provides:**
- `render_template(name, **kwargs)` — renders a Jinja2 template via the engine
- `system_msg(content)` / `user_msg(content)` / `assistant_msg(content)` — creates `LLMMessage` objects
- `chat(messages, json_mode=False)` — sends messages to the LLM and returns an `LLMResponse`
- `parse_json(text)` — extracts JSON from LLM output, handling markdown code blocks

**Constructor:** `BaseAgent(llm, model, template_engine)` — receives all dependencies from the outside, nothing is hardcoded.

### `bmlib.quality` — Quality assessment pipeline

**Used in:** `scoring/scorer.py`

bmnews currently uses Tier 1 (metadata-based) quality assessment:

```python
from bmlib.quality.metadata_filter import classify_from_metadata
from bmlib.quality.data_models import QualityAssessment, QualityTier, StudyDesign

# Extract publication types from paper metadata
pub_types = _extract_pub_types(paper)

# Classify study design from metadata
assessment = classify_from_metadata(pub_types)
# Returns QualityAssessment with:
#   .study_design (StudyDesign enum)
#   .quality_tier (QualityTier enum)
#   .quality_score (float, 0-10)
```

**Quality data models:**
- `StudyDesign` — enum with 14 types: `RCT`, `COHORT_PROSPECTIVE`, `CASE_REPORT`, `META_ANALYSIS`, `SYSTEMATIC_REVIEW`, etc.
- `QualityTier` — 5 tiers from `TIER_1_ANECDOTAL` to `TIER_5_SYNTHESIS`
- `QualityAssessment` — dataclass with design, tier, score, bias risk, strengths, limitations

**Available but not yet integrated:**
- Tier 2: `bmlib.quality.study_classifier.StudyClassifier` — LLM-based classification
- Tier 3: `bmlib.quality.quality_agent.QualityAgent` — deep LLM assessment with bias analysis

### `bmlib.transparency` — Transparency analysis (optional)

**Available but optional in bmnews.** When enabled in config, queries multiple APIs for publication integrity data:

- **CrossRef** — publication metadata, DOI resolution
- **EuropePMC** — citation counts, open access status
- **OpenAlex** — author affiliations, funding sources
- **ClinicalTrials.gov** — trial registration, results availability

Requires `pip install -e ".[transparency]"`.

## Extending bmnews with bmlib

### Adding a new agent

To create a new agent (e.g., for deeper paper analysis):

1. Create a new module in `bmnews/scoring/`:

```python
from bmlib.agents.base import BaseAgent

class AnalysisAgent(BaseAgent):
    def analyze(self, title, abstract):
        prompt = self.render_template("analysis_prompt.txt", title=title, abstract=abstract)
        system = self.render_template("analysis_system.txt")
        response = self.chat([self.system_msg(system), self.user_msg(prompt)])
        return self.parse_json(response.content)
```

2. Add templates in `templates/`
3. Wire it into `scorer.py` or `pipeline.py`

### Adding Tier 2/3 quality assessment

The infrastructure exists in bmlib. To integrate:

```python
from bmlib.quality.study_classifier import StudyClassifier
from bmlib.quality.quality_agent import QualityAgent

# Tier 2: Fast LLM classification
classifier = StudyClassifier(llm=llm, model="ollama:small-model")
assessment = classifier.classify(title=title, abstract=abstract)

# Tier 3: Deep assessment
agent = QualityAgent(llm=llm, model="anthropic:claude-sonnet-4-5-20250929")
assessment = agent.assess(title=title, abstract=abstract, study_design=design)
```

### Adding a new database utility

If you need a new database operation:

1. Add the function to `bmnews/db/operations.py` following the existing pattern
2. Use `bmlib.db` functions (`execute`, `fetch_all`, etc.) for execution
3. Use `_placeholder(conn)` for backend-aware SQL

### Developing bmlib alongside bmnews

For local development of both projects:

```bash
# Clone both
git clone https://github.com/hherb/bmlib.git
git clone https://github.com/hherb/BioMedicalNews.git

# Install bmlib in editable mode
cd bmlib && pip install -e ".[dev]"

# Install bmnews (it will use the local bmlib)
cd ../BioMedicalNews && pip install -e ".[dev]"
```

Changes to bmlib are immediately reflected in bmnews without reinstalling.
