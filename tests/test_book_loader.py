"""
tests/test_book_loader.py

The supporting-book loader (scripts/ingest_book.py). No database and no Cloudflare calls (embedding is replaced):
    venv\\Scripts\\python -m unittest tests.test_book_loader -v
The checks on the real book texts are skipped where the git-ignored corpus/ folder isn't present.
"""

import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import ingest_book as loader                       # noqa: E402
from app.services import embeddings                # noqa: E402


class SavedVectorsAreReusedPassageByPassage(unittest.TestCase):
    """2026-10-18: leaving a few passages out used to re-embed the whole book (Ryle: all 676)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.tmp, "corpus"))
        self.sent = []

        def fake_embed(texts):
            self.sent.extend(texts)
            return [[float(len(t))] for t in texts]
        self.patches = [patch.object(loader, "PROJECT_ROOT", self.tmp),
                        patch.object(embeddings, "embed_passages", fake_embed)]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def test_only_new_text_is_embedded(self):
        first = [("c", "alpha"), ("c", "beta"), ("c", "gamma")]
        self.assertEqual(loader._embeddings("book", first), [[5.0], [4.0], [5.0]])
        self.assertEqual(self.sent, ["alpha", "beta", "gamma"])
        self.sent.clear()
        second = [("c", "alpha"), ("c", "gamma"), ("c", "delta!")]          # beta left out, one new passage
        self.assertEqual(loader._embeddings("book", second), [[5.0], [5.0], [6.0]])
        self.assertEqual(self.sent, ["delta!"], "only the new passage went to Cloudflare")

    def test_a_second_database_costs_nothing(self):
        passages = [("c", "alpha"), ("c", "beta")]
        loader._embeddings("book", passages)
        self.sent.clear()
        loader._embeddings("book", passages)
        self.assertEqual(self.sent, [])

    def test_the_cache_file_is_keyed_by_passage(self):
        loader._embeddings("book", [("c", "alpha")])
        saved = json.load(open(os.path.join(self.tmp, "corpus", "book_embeddings.json"), encoding="utf-8"))
        self.assertEqual(list(saved), ["by_hash"])
        self.assertIn(loader._passage_hash("alpha"), saved["by_hash"])


class TheNewFilters(unittest.TestCase):
    def _reason(self, rules, text):
        return next((why for pattern, why in rules if pattern.search(text)), None)

    def test_end_times_timelines_are_secondary(self):
        self.assertIsNotNone(self._reason([loader._END_TIMES], "the thousand years of Christ's reign"))
        self.assertIsNone(self._reason([loader._END_TIMES], "We wait for his appearing, watching and ready."))

    def test_church_government_and_baptism(self):
        self.assertIsNotNone(self._reason([loader._POLITY], "the Presbyterian and the Independent brethren"))
        self.assertIsNotNone(self._reason([loader._BAPTISM], "the Anabaptists hold"))
        self.assertIsNone(self._reason([loader._POLITY, loader._BAPTISM], "Love one another, as brethren in Christ."))

    def test_the_ryle_rule_spares_his_irenic_line(self):
        irenic = ("The members of this Church do not all worship God in the same way, or use the same form of "
                  "government. Some of them are governed by bishops and some of them by elders.")
        self.assertIsNone(self._reason([loader._RYLE_CHURCH_PARTIES], irenic))
        self.assertIsNotNone(self._reason([loader._RYLE_CHURCH_PARTIES],
                                          "there are two distinct and separate systems of Christianity in England"))


def _have(book):
    return os.path.exists(os.path.join(ROOT, loader.BOOKS[book]["file"]))


class OnTheRealTexts(unittest.TestCase):
    @unittest.skipUnless(_have("ryle"), "corpus/ not present")
    def test_the_ryle_rule_removes_exactly_the_eleven_church_party_paragraphs(self):
        _passages, left_out = loader.build(loader.BOOKS["ryle"])
        party = [(chapter, text) for chapter, why, text in left_out if why.startswith("one church party")]
        self.assertEqual(len(party), 11)
        self.assertEqual({chapter for chapter, _ in party}, {"VISIBLE CHURCHES WARNED"})

    @unittest.skipUnless(_have("henry") and _have("irenicum"), "corpus/ not present")
    def test_every_left_out_chapter_name_matches_a_real_chapter(self):
        for key in ["henry", "irenicum"]:
            titles = {title for title, _ in loader.read_chapters(loader.BOOKS[key])}
            self.assertEqual(loader.BOOKS[key]["skip_chapters"] - titles, set(), key)


if __name__ == "__main__":
    unittest.main()
