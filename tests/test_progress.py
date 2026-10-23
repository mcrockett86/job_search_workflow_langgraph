"""Progress-callback plumbing tests (no network, no LLM)."""

import asyncio
import os
import sys
import unittest
from unittest import mock

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agents import job_finder  # noqa: E402


class _FakeGraph:
    def __init__(self, results):
        self._results = results

    async def ainvoke(self, state, config, stream_mode=None):
        return self._results[state["job_url"]]


class RunGraphStageTests(unittest.TestCase):
    def test_stage_callback_counts_each_job(self):
        df = pd.DataFrame({"job_url": [f"http://x/{i}" for i in range(3)]})
        results = {
            url: {"is_match": True, "job_url": url, "job_description": ""}
            for url in df["job_url"]
        }
        seen = []

        with mock.patch("agents.graph.create_job_graph",
                        return_value=_FakeGraph(results)):
            out = asyncio.run(
                job_finder.run_graph(df, stage=lambda d, t, l: seen.append((d, t, l))))

        self.assertEqual([s[0] for s in seen], [1, 2, 3])   # done increments
        self.assertTrue(all(s[1] == 3 for s in seen))        # total = row count
        self.assertTrue(all(s[2] == "screening jobs" for s in seen))
        self.assertTrue(bool(out["is_match"].all()))

    def test_stage_callback_survives_row_errors(self):
        """A failing job must still advance the counter (no stuck bar)."""
        df = pd.DataFrame({"job_url": ["http://x/1", "http://x/2"]})

        class _Boom:
            async def ainvoke(self, state, config, stream_mode=None):
                if state["job_url"] == "http://x/1":
                    raise RuntimeError("boom")
                return {"is_match": False, "job_url": state["job_url"]}

        seen = []
        with mock.patch("agents.graph.create_job_graph", return_value=_Boom()):
            asyncio.run(job_finder.run_graph(df,
                            stage=lambda d, t, l: seen.append(d)))
        self.assertEqual(seen, [1, 2])

    def test_no_stage_callback_is_fine(self):
        df = pd.DataFrame({"job_url": ["http://x/1"]})
        with mock.patch("agents.graph.create_job_graph",
                        return_value=_FakeGraph({"http://x/1": {"is_match": False}})):
            asyncio.run(job_finder.run_graph(df))  # must not raise


class ScrapeProgressMathTests(unittest.TestCase):
    def test_progress_signature(self):
        """_scrape_urls must call progress(done, total, label) — the shape
        main_pipeline's _scrape_progress expects."""
        import inspect

        from agents import linkedin_scraper

        sig = inspect.signature(linkedin_scraper._scrape_urls)
        self.assertIn("progress", sig.parameters)
        src = inspect.getsource(linkedin_scraper._scrape_urls)
        self.assertIn("progress(url_idx * max_pages + page_number - 1, total,", src)


if __name__ == "__main__":
    unittest.main()
