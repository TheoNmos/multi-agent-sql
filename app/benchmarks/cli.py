"""CLI for running benchmark evaluations."""

import argparse
import asyncio
import random
import sys
from datetime import datetime
from pathlib import Path

import logfire

from app.benchmarks.datasets import parse_indices, sample_bird_by_difficulty, sample_bird_stratified
from app.benchmarks.runner import print_summary, run_benchmark
from app.config import settings


def main() -> None:
    logfire.configure(token=settings.logfire_token)
    logfire.instrument_pydantic_ai()
    """Main CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Run benchmark evaluation on a subset of questions",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Livraria subset (use gold to validate harness)
  python -m app.benchmarks.cli --dataset livraria --indices 2,4,5 --use-gold-as-pred

  # BIRD subset (EM only, stub predictions)
  python -m app.benchmarks.cli --dataset bird --indices 1-3 --metrics em

  # BIRD subset with EXA (requires DB running)
  python -m app.benchmarks.cli --dataset bird --indices 1,2,3 --metrics em,exa

  # Random subset of 50 questions
  python -m app.benchmarks.cli --dataset bird --subset 50 --metrics em,exa

  # Stratified BIRD sampling (5 simple, 3 moderate, 2 challenging per database)
  python -m app.benchmarks.cli --dataset bird --stratified-bird --metrics em,exa

  # Batch mode: Run by difficulty (3 simple, 2 moderate, 1 challenging per DB, incremental save)
  python -m app.benchmarks.cli --dataset bird --batch-by-difficulty --metrics em,exa
        """,
    )

    parser.add_argument(
        "--dataset",
        type=str,
        choices=["bird", "livraria"],
        required=True,
        help="Dataset to evaluate (bird or livraria)",
    )

    index_group = parser.add_mutually_exclusive_group(required=True)
    index_group.add_argument(
        "--indices",
        type=str,
        help="1-based indices to evaluate (e.g., '1,2,3' or '1-3,5' or '1-3,5,7-9')",
    )
    index_group.add_argument(
        "--subset",
        type=int,
        help="Randomly select N questions from the dataset (e.g., --subset 50)",
    )
    index_group.add_argument(
        "--stratified-bird",
        action="store_true",
        help="Sample BIRD dataset stratified by database and difficulty (5 simple, 3 moderate, 2 challenging per database)",
    )
    index_group.add_argument(
        "--batch-by-difficulty",
        action="store_true",
        help="Run BIRD in batches by difficulty: all simple (3/DB), then all moderate (2/DB), then all challenging (1/DB). Uses incremental save.",
    )

    parser.add_argument(
        "--metrics",
        type=str,
        default="em,exa",
        help="Comma-separated list of metrics to compute: 'em', 'exa', or 'em,exa' (default: em,exa)",
    )

    parser.add_argument(
        "--use-gold-as-pred",
        action="store_true",
        help="Use gold SQL as predicted SQL (useful for testing the harness)",
    )

    parser.add_argument(
        "--db-name",
        type=str,
        default=None,
        help="Override database name (default: bird->passarinho, livraria->livraria)",
    )

    parser.add_argument(
        "--server-dsn",
        type=str,
        default=None,
        help="PostgreSQL server DSN (default: from app.config.db_settings.db_url)",
    )

    parser.add_argument(
        "--timeout-s",
        type=int,
        default=30,
        help="Timeout for SQL execution in seconds (default: 30)",
    )

    parser.add_argument(
        "--out",
        type=str,
        default=None,
        help="Output file path (default: ./benchmark_results/{dataset}-{timestamp}.json or .jsonl for incremental saves)",
    )

    parser.add_argument(
        "--max-concurrent",
        type=int,
        default=10,
        help="Maximum number of concurrent tasks to run (default: 10)",
    )

    args = parser.parse_args()

    # Parse metrics
    metrics = [m.strip().lower() for m in args.metrics.split(",")]
    valid_metrics = {"em", "exa"}
    invalid_metrics = set(metrics) - valid_metrics
    if invalid_metrics:
        print(f"Error: Invalid metrics: {invalid_metrics}. Valid options: {valid_metrics}", file=sys.stderr)
        sys.exit(1)

    # Load dataset to get max index
    if args.dataset == "bird":
        from app.benchmarks.datasets import load_bird

        all_items = load_bird()
    elif args.dataset == "livraria":
        from app.benchmarks.datasets import load_livraria

        all_items = load_livraria()
    else:
        print(f"Error: Unknown dataset: {args.dataset}", file=sys.stderr)
        sys.exit(1)

    max_index = len(all_items)

    # Parse indices or generate random subset
    if args.batch_by_difficulty:
        # Batch mode: run by difficulty with incremental saves
        if args.dataset != "bird":
            print("Error: --batch-by-difficulty can only be used with --dataset bird", file=sys.stderr)
            sys.exit(1)

        # Determine output path (use .json for incremental saves)
        if args.out:
            output_path = args.out
            if not output_path.endswith(".json"):
                output_path = output_path.rsplit(".", 1)[0] + ".json"
        else:
            timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            output_dir = Path("benchmark_results")
            output_dir.mkdir(exist_ok=True)
            output_path = str(output_dir / f"{args.dataset}-batch-{timestamp}.json")

        print(f"Batch mode: Running by difficulty with incremental saves")
        print(f"Output: {output_path}")
        print(f"Metrics: {', '.join(metrics)}")
        print(f"Max concurrent tasks: {args.max_concurrent}")

        all_results = []

        # Batch 1: All simple questions (3 per database)
        print("\n" + "=" * 60)
        print("BATCH 1: Simple questions (3 per database)")
        print("=" * 60)
        try:
            simple_indices = sample_bird_by_difficulty(all_items, "simple", per_db=3)
            print(f"Selected {len(simple_indices)} simple questions")
            simple_results = asyncio.run(
                run_benchmark(
                    dataset=args.dataset,
                    indices=simple_indices,
                    metrics=metrics,
                    server_dsn=args.server_dsn,
                    db_name=args.db_name,
                    timeout_s=args.timeout_s,
                    use_gold_as_pred=args.use_gold_as_pred,
                    output_path=output_path,
                    max_concurrent=args.max_concurrent,
                    run_analyzer=True,
                    incremental_save=True,
                )
            )
            all_results.extend(simple_results)
            print_summary(simple_results, metrics)
            print(f"✓ Batch 1 complete: {len(simple_results)} results saved to {output_path}")
        except Exception as e:
            print(f"Error in batch 1: {e}", file=sys.stderr)
            import traceback

            traceback.print_exc()
            sys.exit(1)

        # Batch 2: All moderate questions (2 per database)
        print("\n" + "=" * 60)
        print("BATCH 2: Moderate questions (2 per database)")
        print("=" * 60)
        try:
            moderate_indices = sample_bird_by_difficulty(all_items, "moderate", per_db=2)
            print(f"Selected {len(moderate_indices)} moderate questions")
            moderate_results = asyncio.run(
                run_benchmark(
                    dataset=args.dataset,
                    indices=moderate_indices,
                    metrics=metrics,
                    server_dsn=args.server_dsn,
                    db_name=args.db_name,
                    timeout_s=args.timeout_s,
                    use_gold_as_pred=args.use_gold_as_pred,
                    output_path=output_path,  # Append to same file
                    max_concurrent=args.max_concurrent,
                    run_analyzer=True,
                    incremental_save=True,
                )
            )
            all_results.extend(moderate_results)
            print_summary(moderate_results, metrics)
            print(f"✓ Batch 2 complete: {len(moderate_results)} results saved to {output_path}")
        except Exception as e:
            print(f"Error in batch 2: {e}", file=sys.stderr)
            import traceback

            traceback.print_exc()
            sys.exit(1)

        # Batch 3: All challenging questions (1 per database)
        print("\n" + "=" * 60)
        print("BATCH 3: Challenging questions (1 per database)")
        print("=" * 60)
        try:
            challenging_indices = sample_bird_by_difficulty(all_items, "challenging", per_db=1)
            print(f"Selected {len(challenging_indices)} challenging questions")
            challenging_results = asyncio.run(
                run_benchmark(
                    dataset=args.dataset,
                    indices=challenging_indices,
                    metrics=metrics,
                    server_dsn=args.server_dsn,
                    db_name=args.db_name,
                    timeout_s=args.timeout_s,
                    use_gold_as_pred=args.use_gold_as_pred,
                    output_path=output_path,  # Append to same file
                    max_concurrent=args.max_concurrent,
                    run_analyzer=True,
                    incremental_save=True,
                )
            )
            all_results.extend(challenging_results)
            print_summary(challenging_results, metrics)
            print(f"✓ Batch 3 complete: {len(challenging_results)} results saved to {output_path}")
        except Exception as e:
            print(f"Error in batch 3: {e}", file=sys.stderr)
            import traceback

            traceback.print_exc()
            sys.exit(1)

        # Final summary
        print("\n" + "=" * 60)
        print("FINAL SUMMARY")
        print("=" * 60)
        print_summary(all_results, metrics)
        print(f"\n✓ All batches complete: {len(all_results)} total results saved to {output_path}")
        return

    elif args.stratified_bird:
        # Stratified sampling for BIRD
        if args.dataset != "bird":
            print("Error: --stratified-bird can only be used with --dataset bird", file=sys.stderr)
            sys.exit(1)
        try:
            indices = sample_bird_stratified(
                all_items,
                simple_per_db=5,
                moderate_per_db=3,
                challenging_per_db=2,
            )
            print(
                f"Stratified sampling: Selected {len(indices)} questions from {len({item['db_id'] for item in all_items})} databases"
            )
            print(f"  Distribution: 5 simple, 3 moderate, 2 challenging per database")
        except ValueError as e:
            print(f"Error in stratified sampling: {e}", file=sys.stderr)
            sys.exit(1)
    elif args.subset is not None:
        # Random subset
        if args.subset < 1:
            print(f"Error: Subset size must be >= 1, got {args.subset}", file=sys.stderr)
            sys.exit(1)
        if args.subset > max_index:
            print(
                f"Warning: Subset size ({args.subset}) exceeds dataset size ({max_index}). Using all {max_index} items.",
                file=sys.stderr,
            )
            indices = list(range(1, max_index + 1))
        else:
            # Randomly select indices
            indices = sorted(random.sample(range(1, max_index + 1), args.subset))
            print(f"Randomly selected {len(indices)} questions from {max_index} total")
    else:
        # Parse explicit indices
        try:
            indices = parse_indices(args.indices, max_index)
        except ValueError as e:
            print(f"Error parsing indices: {e}", file=sys.stderr)
            sys.exit(1)

        if not indices:
            print("Error: No valid indices specified", file=sys.stderr)
            sys.exit(1)

    # Determine output path
    if args.out:
        output_path = args.out
    else:
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        output_dir = Path("benchmark_results")
        output_dir.mkdir(exist_ok=True)
        output_path = str(output_dir / f"{args.dataset}-{timestamp}.json")

    # Run benchmark
    print(f"Running {args.dataset} benchmark on {len(indices)} items (indices: {indices})...")
    print(f"Metrics: {', '.join(metrics)}")
    print(f"Max concurrent tasks: {args.max_concurrent}")
    print(f"Output: {output_path}")

    try:
        results = asyncio.run(
            run_benchmark(
                dataset=args.dataset,
                indices=indices,
                metrics=metrics,
                server_dsn=args.server_dsn,
                db_name=args.db_name,
                timeout_s=args.timeout_s,
                use_gold_as_pred=args.use_gold_as_pred,
                output_path=output_path,
                max_concurrent=args.max_concurrent,
                run_analyzer=True,  # Always run analyzer for each question
            )
        )

        # Print summary
        print_summary(results, metrics)
        print(f"\nResults written to: {output_path}")

    except Exception as e:
        print(f"Error running benchmark: {e}", file=sys.stderr)
        import traceback

        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
