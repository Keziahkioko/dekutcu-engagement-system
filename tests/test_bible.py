"""
tests/test_bible.py

The Bible in the RAG companion (Keziah, 2026-10-12): finding references in a question (English, abbreviations,
Swahili book names, no false alarms on everyday words), looking passages up with context, keeping the Bible's
search separate from DeKUTCU's own materials, and labelling Scripture in the sources. No database:
    venv\\Scripts\\python -m unittest tests.test_bible -v
"""

import unittest
from unittest.mock import patch

from app.services import rag_companion
from app.services.bible_refs import find_references, citation


class FindingReferences(unittest.TestCase):
    def test_references_members_write(self):
        cases = {
            "Explain Romans 12:2": [("Romans", 12, 2, 2)],
            "what about Rom 12:2-5?": [("Romans", 12, 2, 5)],
            "I love 1 Cor 13": [("1 Corinthians", 13, None, None)],
            "First John 4:8": [("1 John", 4, 8, 8)],
            "II Timothy 3:16": [("2 Timothy", 3, 16, 16)],
            "John 3 verse 16": [("John", 3, 16, 16)],
            "explain psalm 23": [("Psalm", 23, None, None)],
            "psalms 119:105": [("Psalm", 119, 105, 105)],
            "Warumi 12:2 inamaanisha nini": [("Romans", 12, 2, 2)],
            "1 Yohana 1:9": [("1 John", 1, 9, 9)],
            "Zaburi 23": [("Psalm", 23, None, None)],
            "Isa 53:5": [("Isaiah", 53, 5, 5)],
            "Song of Songs 2:4": [("Song of Solomon", 2, 4, 4)],
            "Romans 8:28-30, and 1 Pet 5:7": [("Romans", 8, 28, 30), ("1 Peter", 5, 7, 7)],
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(find_references(text), expected)

    def test_everyday_words_are_not_references(self):
        for text in ["I am 20 years old", "this is 3 days late", "my job 2 years ago", "mark 4 of 10",
                     "I got a 2:1 in my exam", "the numbers 3 and 4", "song 3 was nice", "3 Corinthians 1:1",
                     "Can I vote at the AGM?"]:
            with self.subTest(text=text):
                self.assertEqual(find_references(text), [])

    def test_citations(self):
        self.assertEqual(citation("Romans", 12, 1, 4), "Romans 12:1-4 (BSB)")
        self.assertEqual(citation("Romans", 12, 2, 2), "Romans 12:2 (BSB)")
        self.assertEqual(citation("Psalm", 23, None, None), "Psalm 23 (BSB)")


def _verses(book, chapter, first=None, last=None):
    if (book, chapter) == ("Romans", 12):
        rows = [{"verse": v, "text": f"text {v}"} for v in range(1, 22)]
    elif (book, chapter) == ("Psalm", 119):
        rows = [{"verse": v, "text": f"text {v}"} for v in range(1, 177)]
    else:
        rows = []
    return [r for r in rows if first is None or first <= r["verse"] <= last]


class LookingUpPassages(unittest.TestCase):
    def setUp(self):
        self.patch = patch("app.models.bible.get_verses", _verses)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()

    def test_a_named_verse_comes_with_context(self):
        [chunk] = rag_companion._lookup_chunks("Explain Romans 12:2")
        self.assertEqual(chunk["citation"], "Romans 12:1-4 (BSB)", "two verses either side, never before verse 1")
        self.assertEqual(chunk["tier"], "bible")
        self.assertTrue(chunk["content"].startswith("Romans 12:1-4. 1 text 1 2 text 2"))

    def test_a_long_chapter_is_capped(self):
        [chunk] = rag_companion._lookup_chunks("What is Psalm 119 about?")
        self.assertEqual(chunk["citation"], "Psalm 119:1-40 (BSB)")

    def test_no_such_verse_looks_nothing_up(self):
        self.assertEqual(rag_companion._lookup_chunks("Explain Romans 99:99"), [])

    def test_bible_and_own_materials_are_searched_separately(self):
        own = [{"citation": "DeKUTCU Constitution, Art. 10: Objectives", "tier": "constitution", "content": "x", "similarity": 0.6}]
        found = [{"citation": "Romans 12:1-5 (BSB)", "tier": "bible", "content": "y", "similarity": 0.7},
                 {"citation": "Romans 8:26-30 (BSB)", "tier": "bible", "content": "z", "similarity": 0.6}]
        calls = []

        def nearest(embedding, limit, tiers=None):
            calls.append((limit, tiers))
            return found if tiers == ("bible",) else own
        with patch.object(rag_companion, "embed_query", lambda q: [0.0]), \
             patch.object(rag_companion, "nearest_chunks", nearest):
            chunks = rag_companion._retrieve("Explain Romans 12:2")
        self.assertIn((rag_companion.TOP_K, ("constitution", "supporting", "dekutcu")), calls)
        self.assertIn((rag_companion.TOP_K_BIBLE, ("bible",)), calls)
        self.assertEqual([c["citation"] for c in chunks],
                         ["Romans 12:1-4 (BSB)", "DeKUTCU Constitution, Art. 10: Objectives", "Romans 12:1-5 (BSB)", "Romans 8:26-30 (BSB)"],
                         "the named passage first, then DeKUTCU's own, then the Bible's closest")


class Sources(unittest.TestCase):
    def test_scripture_is_labelled_as_scripture(self):
        line = rag_companion._source_line([
            {"citation": "DeKUTCU Constitution, Art. 11(F): Doctrinal Basis", "tier": "constitution"},
            {"citation": "John 3:14-18 (BSB)", "tier": "bible"}])
        self.assertEqual(line, "Source: DeKUTCU Constitution, Art. 11(F): Doctrinal Basis\nScripture: John 3:14-18 (BSB)")

    def test_passages_tell_the_model_what_they_are(self):
        text = rag_companion._format_passages([{"citation": "Romans 12:1-4 (BSB)", "tier": "bible", "content": "c"}])
        self.assertIn("(Bible (BSB)) Romans 12:1-4 (BSB)", text)


if __name__ == "__main__":
    unittest.main()
