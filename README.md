# Datasets / Benchmarks

## Bird
Github: https://github.com/bird-bench/mini_dev
Drive: https://drive.google.com/file/d/13VLWIwpw5E3d5DUkMvzw7hvHE67a4XkG/view

Baixe e coloque em /bird


## Spider
Spider

Github: https://github.com/taoyds/spider
Drive: https://drive.google.com/file/d/1403EGqzIDoHMdQF4c9Bkyl7dZLZ5Wt6J/view

## Benchmark Runner

The benchmark runner evaluates text-to-SQL systems on subsets of the BIRD and Livraria datasets.

### Usage

```bash
python -m app.benchmarks.cli --dataset <bird|livraria> --indices <indices>
```

### Required Arguments

- `--dataset`: Dataset to evaluate (`bird` or `livraria`)
- `--indices`: 1-based indices to evaluate. Supports:
  - Comma-separated: `1,2,3`
  - Ranges: `1-3` (inclusive)
  - Mixed: `1-3,5,7-9`

### Optional Arguments

- `--metrics`: Comma-separated metrics to compute (`em`, `exa`, or `em,exa`). Default: `em,exa`
- `--use-gold-as-pred`: Use gold SQL as predicted SQL (useful for testing the harness)
- `--db-name`: Override database name (default: `bird` → `passarinho`, `livraria` → `livraria`)
- `--server-dsn`: PostgreSQL server DSN (default: from `app.config.db_settings.db_url`)
- `--timeout-s`: Timeout for SQL execution in seconds (default: 30)
- `--out`: Output JSON file path (default: `./benchmark_results/{dataset}-{timestamp}.json`)

### Examples

```bash
# Livraria subset (use gold to validate harness)
python -m app.benchmarks.cli --dataset livraria --indices 2,4,5 --use-gold-as-pred

# BIRD subset (EM only, stub predictions)
python -m app.benchmarks.cli --dataset bird --indices 1-3 --metrics em

# BIRD subset with EXA (requires DB running)
python -m app.benchmarks.cli --dataset bird --indices 1,2,3 --metrics em,exa
```

### Metrics

- **Exact Match (EM)**: String equality comparison between gold and predicted SQL
- **Execution Accuracy (EXA)**: Executes both queries and compares result sets (order-insensitive, value-normalized)

### Automatic Failure Analysis

When a benchmark case fails (both `exact_match` and `execution_match` are `False`), the system automatically runs an LLM-powered analyzer to diagnose the failure:

- **Failure Step Identification**: Identifies which agent/step introduced the error (interpreter, retriever, generator, or validator)
- **Failure Reason**: Detailed explanation of why the failure occurred at the identified step
- **Query Issues**: Specific issues in the generated SQL compared to the gold SQL (missing clauses, incorrect aggregations, wrong references, etc.)
- **Root Cause Analysis**: Fundamental reason for the incorrect query
- **Improvement Suggestions**: Actionable recommendations for preventing similar failures

The analyzer examines the full execution trace, including:
- Question interpretation and clarification
- Schema retrieval and table/column selection
- SQL generation reasoning
- Validation feedback (if any)
- Execution results comparison

Analysis results are automatically included in the output JSON file under the `analyzer_output` field.

### Output

Results are written to a JSON file containing an array of result objects. Each result includes:
- Dataset and index information
- Question and SQL (gold and predicted)
- Metric results (EM, EXA)
- Execution results (gold and predicted query outputs)
- Errors (if any)
- Agent execution trace (full pipeline trace with step-by-step outputs)
- **Failure analysis** (`analyzer_output`): Automatic LLM analysis for failed cases (when both EM and EXA are False)
  - Failure step identification
  - Failure reason
  - Query issues
  - Root cause analysis
  - Improvement suggestions
- Metadata (latency, tokens, model name, etc.)

A summary is printed to stdout with counts and rates for each metric.

### Plugging in Your System

To use your own text-to-SQL system, replace the stub in `app/benchmarks/run_question.py`:

```python
def run_question(question: str, db_name: str) -> RunQuestionResult:
    # Your implementation here
    predicted_sql = your_system.generate_sql(question, db_name)
    return RunQuestionResult(
        predicted_sql=predicted_sql,
        latency_ms=latency,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        model_name="your-model",
        error=None,
    )
```
