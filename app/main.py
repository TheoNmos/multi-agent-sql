import asyncio

import logfire

# from app.benchmark import run_bird_benchmark
from app.config import settings
from app.db.connection import close_connection_pool
from app.agents.workflow import run_new_pipeline


async def main():
    _ = logfire.configure(token=settings.logfire_token)
    logfire.instrument_pydantic_ai()
    try:
        await run_interactive_mode()
        # if option == "benchmark":
        #     await run_benchmark_mode()
        # else:
        #     await run_interactive_mode()
    finally:
        # Clean up connection pool
        await close_connection_pool()


async def run_interactive_mode():
    """Run in interactive mode with sample query"""
    # Sample run
    result = await run_new_pipeline("How much did customer 6 consume in total between August and November 2013?")
    print(result)


# async def run_benchmark_mode():
#     """Run benchmark mode"""
#     # Default path to BIRD mini dev dataset
#     bird_path = Path(__file__).parent.parent / "bird" / "minidev" / "MINIDEV" / "mini_dev_postgresql.json"

#     # Parse command line arguments
#     difficulties = None
#     limit = None

#     if len(sys.argv) > 2:
#         if sys.argv[2] in ["simple", "moderate", "challenging"]:
#             difficulties = [sys.argv[2]]
#         elif sys.argv[2].isdigit():
#             limit = int(sys.argv[2])

#     if len(sys.argv) > 3 and sys.argv[3].isdigit():
#         limit = int(sys.argv[3])

#     print(f"Running BIRD benchmark from: {bird_path}")
#     print(f"Difficulties: {difficulties or 'all'}")
#     print(f"Limit: {limit or 'all'}")
#     print("-" * 50)

#     try:
#         benchmark_result = await run_bird_benchmark(
#             data_path=bird_path,
#             difficulties=difficulties,
#             limit=limit,
#             max_concurrent=3,  # Conservative concurrency to avoid overwhelming the DB
#         )

#         # Print results
#         evaluation = benchmark_result["evaluation"]
#         stats = benchmark_result["stats"]

#         print("\n" + "=" * 50)
#         print("BENCHMARK RESULTS")
#         print("=" * 50)
#         print(f"Overall Accuracy: {evaluation['overall_accuracy']:.2f}")
#         print(f"Total Questions: {evaluation['total_questions']}")
#         print(f"Correct Predictions: {evaluation['correct_predictions']}")

#         print("\nBy Difficulty:")
#         for diff, metrics in evaluation["by_difficulty"].items():
#             print(f"  {diff}: {metrics['accuracy']:.2f} ({metrics['correct']}/{metrics['total']})")

#         print("\nDataset Stats:")
#         print(f"Total Questions in Dataset: {stats['total_questions']}")
#         print(f"Unique Databases: {stats['unique_databases']}")
#         print("\nDifficulties Distribution:")
#         for diff, count in stats["difficulties"].items():
#             print(f"  {diff}: {count}")

#     except FileNotFoundError:
#         print(f"Error: BIRD dataset not found at {bird_path}")
#         print("Make sure the BIRD dataset is properly set up.")
#     except Exception as e:
#         print(f"Error running benchmark: {e}")


if __name__ == "__main__":
    asyncio.run(main())
