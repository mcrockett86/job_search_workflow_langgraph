"""Entry point for running the job-search pipeline."""

import asyncio

from agents.job_finder import main_pipeline


async def run() -> None:
    """Run the full job search pipeline."""
    await main_pipeline()


if __name__ == "__main__":
    asyncio.run(run())