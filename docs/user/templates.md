# Customizing Templates

bmnews uses Jinja2 templates for two purposes:

1. **LLM prompts** — the instructions sent to the LLM when scoring papers
2. **Digest rendering** — the HTML and plain-text output for email and terminal display

You can override any template by placing a file with the same name in your custom template directory.

## Setting up custom templates

1. Create a templates directory:
   ```bash
   mkdir -p ~/.bmnews/templates
   ```

2. Tell bmnews to use it in your config:
   ```toml
   [general]
   template_dir = "~/.bmnews/templates"
   ```

3. Copy any template you want to modify:
   ```bash
   cp templates/digest_email.html ~/.bmnews/templates/
   ```

4. Edit your copy. bmnews will use your version instead of the built-in default.

Templates you don't copy will continue using the built-in defaults. You only need to override the ones you want to change.

## Available templates

### `relevance_system.txt` — LLM system prompt

The system message sent to the LLM before every scoring request. Defines the LLM's role and the expected JSON response format.

**Default content:**

```
You are a biomedical research assistant that evaluates scientific publications
for relevance to a researcher's interests. You provide structured assessments
in JSON format.

You must respond with valid JSON only, no additional text. The JSON must contain:
- "relevance_score": a float between 0.0 and 1.0
- "summary": a concise 2-3 sentence summary
- "relevance_rationale": a brief explanation of relevance
- "key_findings": a list of 1-3 key findings

Scoring guidelines:
- 0.0-0.2: Not relevant
- 0.2-0.4: Tangentially related
- 0.4-0.6: Moderately relevant
- 0.6-0.8: Highly relevant
- 0.8-1.0: Extremely relevant, core focus area

Be accurate and honest. Do not inflate scores.
```

**When to customize:** If you want different scoring criteria, a different JSON schema, or domain-specific instructions (e.g., "prioritize papers with clinical trial data").

**Warning:** If you change the JSON response fields, you'll also need to update `relevance_scoring.txt` and potentially the scoring code.

### `relevance_scoring.txt` — LLM user prompt

The per-paper prompt sent to the LLM. Uses Jinja2 variables filled from each paper's data.

**Available variables:**

| Variable | Type | Description |
|----------|------|-------------|
| `title` | string | Paper title |
| `abstract` | string | Paper abstract |
| `interests` | list[string] | Your `research_interests` from config |
| `categories` | string | Paper categories/subjects |

**Default content:**

```jinja
Evaluate the following publication for relevance to the researcher's
interests and provide a concise summary.

## Researcher's Interests
{% for interest in interests %}- {{ interest }}
{% endfor %}

## Publication
**Title:** {{ title }}
{% if categories %}**Categories:** {{ categories }}
{% endif %}
**Abstract:**
{{ abstract }}

Respond with a JSON object containing: relevance_score, summary,
relevance_rationale, and key_findings.
```

**When to customize:** To add additional instructions, change the format, or include extra context in the prompt.

### `digest_email.html` — HTML email digest

The HTML template for email digests and file output.

**Available variables:**

| Variable | Type | Description |
|----------|------|-------------|
| `papers` | list[dict] | Scored papers, each with fields below |
| `paper_count` | int | Number of papers |
| `subject_prefix` | string | Email subject prefix from config |
| `generated_at` | string | Timestamp string (YYYY-MM-DD HH:MM) |

**Paper dict fields:**

| Field | Type | Description |
|-------|------|-------------|
| `title` | string | Paper title |
| `url` | string | DOI link or direct URL |
| `authors` | string | Author list |
| `published_date` | string | Publication date |
| `source` | string | Source server name |
| `summary` | string | LLM-generated summary |
| `relevance_score` | float | 0.0–1.0 relevance score |
| `quality_tier` | string | Quality tier name |
| `study_design` | string | Study design classification |
| `combined_score` | float | Weighted combined score |

**When to customize:** To change the visual styling, add your institution's branding, rearrange information, or add/remove fields.

### `digest_text.txt` — Plain-text digest

The plain-text template used for terminal output and as the email fallback for clients that don't support HTML.

Same variables as the HTML template. Default output is a numbered list with title, URL, metadata, and summary.

**When to customize:** To change the terminal output format or plain-text email appearance.

## Jinja2 basics

If you're not familiar with Jinja2, here are the essentials:

### Variables

```jinja
{{ variable_name }}
{{ paper.title }}
```

### Loops

```jinja
{% for paper in papers %}
{{ loop.index }}. {{ paper.title }}
{% endfor %}
```

### Conditionals

```jinja
{% if paper.summary %}
Summary: {{ paper.summary }}
{% endif %}
```

### Filters

```jinja
{{ paper.relevance_score * 100 }}%
{{ "%.0f"|format(paper.relevance_score * 100) }}%
{{ paper.title[:80] }}
```

### Whitespace control

Use `-` to strip whitespace around tags:

```jinja
{%- if paper.authors %} by {{ paper.authors }}{%- endif %}
```

For full Jinja2 documentation, see https://jinja.palletsprojects.com/.

## Example: Minimal digest template

A stripped-down text digest showing only title, score, and summary:

```jinja
Top {{ paper_count }} papers — {{ generated_at }}

{% for paper in papers %}
[{{ "%.0f"|format(paper.relevance_score * 100) }}%] {{ paper.title }}
{{ paper.summary }}

{% endfor %}
```

## Example: Adding a field to the HTML template

To show the abstract in the HTML digest, add this inside the `.paper` div:

```html
{% if paper.abstract %}
<div class="abstract" style="font-size: 0.9em; color: #555; margin-top: 8px;">
    {{ paper.abstract[:300] }}{% if paper.abstract|length > 300 %}...{% endif %}
</div>
{% endif %}
```
