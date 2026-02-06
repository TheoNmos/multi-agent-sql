"""Dataset loaders for BIRD and Livraria benchmarks."""

import json
import random
from collections import defaultdict
from pathlib import Path
from typing import Any

# Dataset paths relative to project root
PROJECT_ROOT = Path(__file__).parent.parent.parent
BIRD_PATH = PROJECT_ROOT / "datasets" / "bird" / "benchmark.json"
LIVRARIA_PATH = PROJECT_ROOT / "datasets" / "livraria" / "benchmark.json"


def load_livraria() -> list[dict[str, Any]]:
    """Load Livraria benchmark dataset.

    Returns:
        List of dicts with keys: 'pergunta', 'sql', 'question_id'
    """
    with open(LIVRARIA_PATH, encoding="utf-8") as f:
        return json.load(f)


def load_bird() -> list[dict[str, Any]]:
    """Load BIRD benchmark dataset.

    Returns:
        List of dicts with keys: 'question', 'SQL', 'question_id', 'db_id', etc.
    """
    with open(BIRD_PATH, encoding="utf-8") as f:
        return json.load(f)


def parse_indices(indices_str: str, max_index: int) -> list[int]:
    """
    Parse indices string into a list of 1-based indices.

    Supports:
    - Comma-separated: "1,2,3"
    - Ranges: "1-3" (inclusive)
    - Mixed: "1-3,5,7-9"

    Args:
        indices_str: String like "1,2,3" or "1-3,5"
        max_index: Maximum valid index (1-based)

    Returns:
        Sorted list of unique 1-based indices

    Raises:
        ValueError: If any index is out of bounds or invalid
    """
    indices: set[int] = set()

    for part in indices_str.split(","):
        part = part.strip()
        if not part:
            continue

        if "-" in part:
            # Range like "1-3"
            try:
                start, end = part.split("-", 1)
                start_idx = int(start.strip())
                end_idx = int(end.strip())
                if start_idx < 1 or end_idx < 1:
                    raise ValueError(f"Indices must be >= 1, got {part}")
                if start_idx > end_idx:
                    raise ValueError(f"Invalid range: {part} (start > end)")
                indices.update(range(start_idx, end_idx + 1))
            except ValueError as e:
                if "invalid literal" in str(e):
                    raise ValueError(f"Invalid range format: {part}") from e
                raise
        else:
            # Single index
            try:
                idx = int(part)
                if idx < 1:
                    raise ValueError(f"Indices must be >= 1, got {idx}")
                indices.add(idx)
            except ValueError as e:
                if "invalid literal" in str(e):
                    raise ValueError(f"Invalid index: {part}") from e
                raise

    # Validate bounds
    invalid = [idx for idx in indices if idx > max_index]
    if invalid:
        raise ValueError(f"Indices out of bounds: {invalid}. Maximum valid index is {max_index}")

    return sorted(indices)


def select_items(dataset: list[dict[str, Any]], indices: list[int]) -> list[dict[str, Any]]:
    """
    Select items from dataset by 1-based indices.

    Args:
        dataset: Full dataset list
        indices: 1-based indices to select

    Returns:
        Selected items in order of indices
    """
    return [dataset[idx - 1] for idx in indices]


def sample_bird_stratified(
    dataset: list[dict[str, Any]],
    simple_per_db: int = 5,
    moderate_per_db: int = 3,
    challenging_per_db: int = 2,
) -> list[int]:
    """
    Sample BIRD dataset stratified by database and difficulty.

    For each database in BIRD, samples:
    - simple_per_db simple questions
    - moderate_per_db moderate questions
    - challenging_per_db challenging questions

    Args:
        dataset: Full BIRD dataset list
        simple_per_db: Number of simple questions to sample per database
        moderate_per_db: Number of moderate questions to sample per database
        challenging_per_db: Number of challenging questions to sample per database

    Returns:
        Sorted list of 1-based indices of selected items

    Raises:
        ValueError: If a database doesn't have enough questions of a given difficulty
    """
    # Group questions by database and difficulty
    by_db_and_diff: dict[str, dict[str, list[int]]] = defaultdict(lambda: defaultdict(list))

    for idx, item in enumerate(dataset, start=1):
        db_id = item.get("db_id")
        difficulty = item.get("difficulty")
        if db_id and difficulty:
            by_db_and_diff[db_id][difficulty].append(idx)

    selected_indices: list[int] = []

    # Sample from each database
    for db_id in sorted(by_db_and_diff.keys()):
        db_questions = by_db_and_diff[db_id]

        # Sample simple questions (sample what's available if less than requested)
        simple_available = db_questions.get("simple", [])
        simple_to_sample = min(simple_per_db, len(simple_available))
        if simple_to_sample > 0:
            selected_indices.extend(random.sample(simple_available, simple_to_sample))

        # Sample moderate questions (sample what's available if less than requested)
        moderate_available = db_questions.get("moderate", [])
        moderate_to_sample = min(moderate_per_db, len(moderate_available))
        if moderate_to_sample > 0:
            selected_indices.extend(random.sample(moderate_available, moderate_to_sample))

        # Sample challenging questions (sample what's available if less than requested)
        challenging_available = db_questions.get("challenging", [])
        challenging_to_sample = min(challenging_per_db, len(challenging_available))
        if challenging_to_sample > 0:
            selected_indices.extend(random.sample(challenging_available, challenging_to_sample))

    return sorted(selected_indices)


def sample_bird_by_difficulty(
    dataset: list[dict[str, Any]],
    difficulty: str,
    per_db: int,
) -> list[int]:
    """
    Sample BIRD dataset by difficulty level across all databases.

    Samples per_db questions of the specified difficulty from each database.

    Args:
        dataset: Full BIRD dataset list
        difficulty: Difficulty level ('simple', 'moderate', or 'challenging')
        per_db: Number of questions to sample per database

    Returns:
        Sorted list of 1-based indices of selected items
    """
    if difficulty not in ("simple", "moderate", "challenging"):
        raise ValueError(f"Invalid difficulty: {difficulty}. Must be 'simple', 'moderate', or 'challenging'")

    # Group questions by database and difficulty
    by_db_and_diff: dict[str, dict[str, list[int]]] = defaultdict(lambda: defaultdict(list))

    for idx, item in enumerate(dataset, start=1):
        db_id = item.get("db_id")
        item_difficulty = item.get("difficulty")
        if db_id and item_difficulty:
            by_db_and_diff[db_id][item_difficulty].append(idx)

    selected_indices: list[int] = []

    # Sample from each database
    for db_id in sorted(by_db_and_diff.keys()):
        db_questions = by_db_and_diff[db_id]
        available = db_questions.get(difficulty, [])
        to_sample = min(per_db, len(available))
        if to_sample > 0:
            selected_indices.extend(random.sample(available, to_sample))

    return sorted(selected_indices)
