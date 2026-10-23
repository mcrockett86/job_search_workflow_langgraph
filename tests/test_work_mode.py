"""Tests for work-mode classification and the work_mode job filter."""

import os
import sqlite3
import tempfile
import unittest

from agents.db import get_conn, upsert_results, upsert_seen, set_preview, list_jobs, counts
from agents.work_mode import (
    HYBRID, IN_PERSON, REMOTE, classify_work_mode)


class ClassifyTests(unittest.TestCase):
    def test_explicit_remote(self):
        self.assertEqual(classify_work_mode("Senior Engineer — 100% Remote"), REMOTE)
        self.assertEqual(classify_work_mode(None, "Work from home, flexible hours"), REMOTE)
        self.assertEqual(classify_work_mode("Engineer", "join our distributed team"), REMOTE)
        self.assertEqual(classify_work_mode("Dev", "WFH friendly"), REMOTE)

    def test_hybrid_beats_remote_wording(self):
        self.assertEqual(
            classify_work_mode("Engineer (Hybrid)", "remote 2 days, in office 3"), HYBRID)
        self.assertEqual(
            classify_work_mode(None, "3 days in the office, remote the rest"), HYBRID)

    def test_in_person(self):
        self.assertEqual(classify_work_mode("Ops Lead — On-site", None), IN_PERSON)
        self.assertEqual(
            classify_work_mode("Engineer", "This role is not remote; must be in the office"),
            IN_PERSON)
        self.assertEqual(classify_work_mode(None, "relocation assistance provided"), IN_PERSON)

    def test_negated_remote_is_in_person(self):
        # mentions "remote" but explicitly rules it out
        self.assertEqual(
            classify_work_mode("Engineer", "not a remote position"), IN_PERSON)

    def test_unknown(self):
        self.assertIsNone(classify_work_mode("Engineer", "great team, competitive salary"))
        self.assertIsNone(classify_work_mode(None, None))
        self.assertIsNone(classify_work_mode(""))

    def test_word_boundaries(self):
        # short tokens must not match inside longer words
        self.assertIsNone(classify_work_mode("Robotics Eng", "operate remotelike vehicles"))
        self.assertIsNone(classify_work_mode("Farmhand", "works at the farmhouse"))


class DbFilterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "jobs.db")
        self.conn = get_conn(self.path)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def _seed(self):
        upsert_results(self.conn, [
            {"job_url": "https://x/1", "job_title": "Eng", "is_match": True,
             "job_description": "100% remote"},
            {"job_url": "https://x/2", "job_title": "Ops", "is_match": False,
             "job_description": "on-site, no remote"},
            {"job_url": "https://x/3", "job_title": "PM", "is_match": True,
             "job_description": "hybrid, 3 days in the office"},
            {"job_url": "https://x/4", "job_title": "QA"},  # no text -> unknown
        ])

    def test_upsert_classifies(self):
        self._seed()
        modes = {r["job_url"]: r["work_mode"] for r in
                 list_jobs(self.conn, status="all")}
        self.assertEqual(modes["https://x/1"], REMOTE)
        self.assertEqual(modes["https://x/2"], IN_PERSON)
        self.assertEqual(modes["https://x/3"], HYBRID)
        self.assertIsNone(modes["https://x/4"])

    def test_filter_by_mode(self):
        self._seed()
        urls = lambda mode: [r["job_url"] for r in
                             list_jobs(self.conn, status="all", work_mode=mode)]
        self.assertEqual(urls("remote"), ["https://x/1"])
        self.assertEqual(urls("hybrid"), ["https://x/3"])
        self.assertEqual(urls("in_person"), ["https://x/2"])
        self.assertEqual(urls("unknown"), ["https://x/4"])
        self.assertEqual(len(urls("all")), 4)

    def test_preview_backfills_mode(self):
        upsert_seen(self.conn, ["https://x/9"])
        self.assertIsNone(list_jobs(self.conn, status="seen")[0]["work_mode"])
        set_preview(self.conn, "https://x/9", "Fully remote role with async team", None)
        self.assertEqual(list_jobs(self.conn, status="seen")[0]["work_mode"], REMOTE)

    def test_counts_include_modes(self):
        self._seed()
        c = counts(self.conn)
        self.assertEqual(c["mode_remote"], 1)
        self.assertEqual(c["mode_hybrid"], 1)
        self.assertEqual(c["mode_in_person"], 1)
        self.assertNotIn("mode_unknown", c)  # unknown = NULL, not counted

    def test_migration_adds_column(self):
        """A DB created before work_mode existed gains it on next open."""
        path = os.path.join(self.tmp.name, "legacy.db")
        legacy = sqlite3.connect(path)
        legacy.executescript(
            """CREATE TABLE jobs (
                 job_url TEXT PRIMARY KEY, first_seen TEXT, last_seen TEXT,
                 status TEXT, is_match INTEGER, company_name TEXT, job_title TEXT,
                 job_type TEXT, salary_min REAL, salary_max REAL, summary TEXT,
                 preview TEXT, preview_at TEXT, run_count INTEGER NOT NULL DEFAULT 1);"""
        )
        legacy.execute(
            "INSERT INTO jobs (job_url, first_seen, last_seen, status, run_count) "
            "VALUES ('https://x/7','t','t','seen',1)")
        legacy.execute("UPDATE jobs SET summary='work from anywhere' "
                       "WHERE job_url='https://x/7'")
        legacy.commit()
        legacy.close()

        conn = get_conn(path)  # runs _migrate + backfill
        try:
            rows = list_jobs(conn, status="all")
            self.assertEqual(rows[0]["work_mode"], REMOTE)  # backfilled on open
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
