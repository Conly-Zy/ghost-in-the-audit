#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ghostlib unit tests (tasks.md 1.10 ⛔).

Directly assert the acceptance criteria's round-trip/idempotence properties and
defect matrices:
R4.11/R4.12 (Codec round trip), R5.8–R5.10 (Canonical equivalence identity and
idempotence), R6.7/R6.9 (Location round trip), R5.7/R5.12/R5.13 (Parser defect
matrix), R7.16 (Location_Reference stripping), md_escape properties,
split_lines boundaries.
Run: python3 -m unittest discover -s tests -t . -v
"""

import json
import os
import random
import shutil
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, ".kiro", "scripts"))

import ghostlib as g  # noqa: E402


def ts(y, mo, d, h, mi, s, ms):
    return "%04d-%02d-%02dT%02d:%02d:%02d.%03dZ" % (y, mo, d, h, mi, s, ms)


VALID_TS = [
    ts(1, 1, 1, 0, 0, 0, 0),
    ts(9999, 12, 31, 23, 59, 59, 999),
    ts(2024, 2, 29, 12, 0, 0, 1),
    ts(2000, 2, 29, 0, 0, 0, 0),
    ts(1900, 3, 1, 0, 0, 0, 0),      # 1900 is not a leap year: February has no 29th (see INVALID_TS)
    ts(1582, 10, 15, 0, 0, 0, 0),
    ts(1600, 2, 29, 0, 0, 0, 0),     # 1600 is a leap year
    ts(2026, 11, 16, 8, 30, 5, 123),
]

INVALID_TS = [
    "", "x", "2023-02-29T00:00:00.000Z",      # non-leap-year Feb 29
    "1900-02-29T00:00:00.000Z",               # 1900 is not a leap year
    "2100-02-29T00:00:00.000Z",               # 2100 is not a leap year
    "2023-13-01T00:00:00.000Z", "2023-00-01T00:00:00.000Z",
    "2023-01-00T00:00:00.000Z", "2023-01-32T00:00:00.000Z",
    "2023-04-31T00:00:00.000Z",               # April 31
    "2023-01-01T24:00:00.000Z", "2023-01-01T00:60:00.000Z",
    "2023-01-01T00:00:60.000Z", "2023-01-01T00:00:59.9999Z",
    "2023-01-01T00:00:00.00Z", "2023-1-01T00:00:00.000Z",
    "2023-01-01 00:00:00.000Z", "2023-01-01t00:00:00.000Z",
    "2023-01-01T00:00:00.000z", "2023-01-01T00:00:00Z",
    "2023-01-01T00:00:00,000Z", " 2023-01-01T00:00:00.000Z",
    "2023-01-01T00:00:00.000Z ", "2023-01-01T00:00:00.000Z\n",
    "0000-01-01T00:00:00.000Z", "٢٠٢٣-01-01T00:00:00.000Z",  # year 0000 / Unicode digits (deliberate non-ASCII digit specimen)
    "2023-01-01T00:00:00.000X", None, 123,
]


class Test_Timestamp(unittest.TestCase):
    def test_valid_roundtrip(self):
        for s in VALID_TS:
            dt = g.parse_ts(s)
            self.assertIsNotNone(dt, s)
            self.assertEqual(g.format_ts(dt), s, s)

    def test_invalid(self):
        for s in INVALID_TS:
            self.assertIsNone(g.parse_ts(s), repr(s))

    def test_utc_now_format(self):
        s = g.utc_now_str()
        self.assertIsNotNone(g.parse_ts(s))
        self.assertEqual(g.format_ts(g.parse_ts(s)), s)

    def test_next_strict_increasing(self):
        prev = g.parse_ts("2026-01-01T00:00:00.000Z")
        same_clock = datetime(2026, 1, 1, 0, 0, 0, 500, tzinfo=timezone.utc)
        self.assertEqual(g.next_strict_ts("2026-01-01T00:00:00.000Z", same_clock),
                         "2026-01-01T00:00:00.001Z")
        later = datetime(2026, 1, 1, 0, 0, 5, 0, tzinfo=timezone.utc)
        self.assertEqual(g.next_strict_ts("2026-01-01T00:00:00.000Z", later),
                         "2026-01-01T00:00:05.000Z")
        self.assertEqual(g.next_strict_ts(None, same_clock), "2026-01-01T00:00:00.000Z")


class Test_SplitLines(unittest.TestCase):
    def test_cases(self):
        self.assertEqual(g.split_lines(b""), [])
        self.assertEqual(g.split_lines(b"\n"), [""])
        self.assertEqual(g.split_lines(b"a\r\nb\n"), ["a", "b"])
        self.assertEqual(g.split_lines(b"a\nb"), ["a", "b"])          # no trailing LF
        self.assertEqual(g.split_lines(b"a\n\nb\n"), ["a", "", "b"])
        self.assertEqual(g.split_lines(b"\r\n"), [""])
        self.assertEqual(g.split_lines(b"a\r\n"), ["a"])
        self.assertEqual(g.split_lines(b"a\rb\n"), ["a\rb"])          # a lone CR belongs to the line content
        self.assertEqual(g.split_lines("héllo\n".encode("utf-8")), ["héllo"])
        bom = "﻿x\n".encode("utf-8")
        self.assertEqual(g.split_lines(bom), ["﻿x"])                  # the BOM is kept on the first line
        self.assertIsNone(g.split_lines(b"\xff\xfe bad"))
        self.assertIsNone(g.split_lines("str"))


class Test_NormalizedAndHash(unittest.TestCase):
    def test_normalized_text(self):
        self.assertEqual(g.normalized_text("  a  b\tc "), "a b c")
        self.assertEqual(g.normalized_text("a\nb"), "a\nb")          # processed line by line
        self.assertEqual(g.normalized_text("a \t b\nc  d"), "a b\nc d")
        # Deliberate full-width (ideographic) whitespace specimen: U+3000 folds to a plain space.
        self.assertEqual(g.normalized_text("　全角　空白"), "全角 空白")

    def test_evidence_hash_vector(self):
        lines = ["alpha", "beta", "gamma"]
        h = g.evidence_hash(lines, 2, 3)
        import hashlib
        self.assertEqual(h, hashlib.sha256(b"beta\ngamma").hexdigest())

    def test_bytes_hash(self):
        import hashlib
        self.assertEqual(g.bytes_hash(b""), hashlib.sha256(b"").hexdigest())
        self.assertEqual(g.bytes_hash("aé".encode("utf-8")),
                         hashlib.sha256("aé".encode("utf-8")).hexdigest())


def make_entry(**over):
    e = {
        "ts": "2026-01-01T00:00:00.000Z", "run_id": "run-1731690000123-a1b2c3",
        "path": "src/a.py", "start": 10, "end": 20,
        "sha256": "a" * 64,
    }
    e.update(over)
    return e


class Test_EvidenceCodec(unittest.TestCase):
    def test_roundtrip_property(self):
        rng = random.Random(42)
        for _ in range(300):
            e = make_entry(
                ts=rng.choice(VALID_TS),
                run_id="run-%013d-%s" % (rng.randrange(10 ** 13), "%06x" % rng.randrange(16 ** 6)),
                # "中" is a deliberate CJK path-character specimen in the random alphabet.
                path="".join(rng.choice("ab/中.c-d_") for _ in range(rng.randrange(1, 20))),
                start=rng.randrange(1, 2147483647),
                end=0,
            )
            e["end"] = e["start"] + rng.randrange(0, 100)
            sess = None if rng.random() < 0.5 else "sess_" + str(rng.randrange(10 ** 6))
            if sess is not None:
                e["session"] = sess
            line = g.evidence_entry_dump(e)
            back = g.evidence_entry_load(line)
            self.assertIsNotNone(back, line)
            self.assertEqual(back, e)
            self.assertEqual(g.evidence_entry_dump(back), line)  # serialization is idempotent

    def test_fixed_key_order(self):
        e = make_entry()
        self.assertEqual(list(json.loads(g.evidence_entry_dump(e)).keys()),
                         ["ts", "run_id", "path", "start", "end", "sha256"])
        e2 = make_entry(session="s1")
        self.assertEqual(list(json.loads(g.evidence_entry_dump(e2)).keys()),
                         ["ts", "run_id", "path", "start", "end", "sha256", "session"])

    def test_defect_matrix(self):
        bad = [
            "", "{}", "null", "[]", "42", '"x"',
            json.dumps({"ts": VALID_TS[0]}),                                  # missing field
            json.dumps({**make_entry(), "extra": 1}),                          # undefined key
            json.dumps({**make_entry(), "ts": "bad"}),                         # invalid ts
            json.dumps({**make_entry(), "start": 0}),
            json.dumps({**make_entry(), "end": 2147483648}),
            json.dumps({**make_entry(), "start": 20, "end": 10}),              # start > end
            json.dumps({**make_entry(), "start": True, "end": 20}),            # bool masquerading as int
            json.dumps({**make_entry(), "end": 20.0}),                         # float
            json.dumps({**make_entry(), "sha256": "A" * 64}),                  # uppercase
            json.dumps({**make_entry(), "sha256": "a" * 63}),
            json.dumps({**make_entry(), "session": 7}),
            json.dumps({**make_entry(), "run_id": ""}),
            json.dumps({**make_entry(), "path": 5}),
            '{"ts":"%s","ts":"%s","run_id":"r","path":"p","start":1,"end":1,"sha256":"%s"}'
            % (VALID_TS[0], VALID_TS[0], "a" * 64),                            # duplicate key
            json.dumps({"start": 1, "end": 1, "ts": VALID_TS[0], "run_id": "r",
                        "path": "p", "sha256": "a" * 64}),                     # key order mismatch
            "NaN", '{"x": NaN}',
        ]
        for text in bad:
            self.assertIsNone(g.evidence_entry_load(text), text)

    def test_parse_evidence_log(self):
        lines = [g.evidence_entry_dump(make_entry(ts=ts(2026, 1, 1, 0, 0, 0, i))) for i in range(3)]
        entries, corrupt = g.parse_evidence_log("\n".join(lines) + "\n")
        self.assertIsNone(corrupt)
        self.assertEqual(len(entries), 3)
        entries, corrupt = g.parse_evidence_log("\n".join(lines))
        self.assertIsNone(corrupt)  # no trailing LF is also valid
        # timestamps not strictly increasing → corrupt
        lines[2] = g.evidence_entry_dump(make_entry(ts=ts(2026, 1, 1, 0, 0, 0, 1)))
        entries, corrupt = g.parse_evidence_log("\n".join(lines) + "\n")
        self.assertIsNone(entries)
        self.assertEqual(corrupt, 3)
        # corrupt last line → corrupt
        entries, corrupt = g.parse_evidence_log(lines[0] + "\n{broken\n")
        self.assertIsNone(entries)


class Test_Location(unittest.TestCase):
    def test_parse_ok(self):
        cases = [
            ("a.py:10", ("a.py", 10, 10)),
            ("a.py:10-20", ("a.py", 10, 20)),
            ("src/lib/util.ts:1-999999999", ("src/lib/util.ts", 1, 999999999)),
            ("weird:dir/a.py:7", ("weird:dir/a.py", 7, 7)),      # splits at the last colon
            ("目录/文件.py:3", ("目录/文件.py", 3, 3)),           # deliberate CJK path specimen
            ("a b.py:5", ("a b.py", 5, 5)),                       # spaces allowed (no control characters)
            ("x" * 800 + ".py:2-3", ("x" * 800 + ".py", 2, 3)),
        ]
        for s, (p, st, en) in cases:
            loc, err = g.parse_location(s)
            self.assertIsNone(err, s)
            self.assertEqual((loc["path"], loc["start"], loc["end"]), (p, st, en), s)

    def test_parse_malformed_matrix(self):
        bad = [
            None, "", "nocolon.py", ":10", "/abs/a.py:1", "\\w/a.py:1",
            "a//b.py:1", "a/./b.py:1", "a/../b.py:1", "./a.py:1", "../a.py:1",
            "a/\x01b.py:1", "a\x7f.py:1", "a\x85.py:1",
            "a/" + "x" * 4100 + ".py:1",                    # > 4096 UTF-8 bytes
            "a.py:0", "a.py:01", "a.py:007", "a.py:-1", "a.py:+1",
            "a.py: 10", "a.py:10 ", "a.py:١٠",              # leading zeros/whitespace/Unicode digits
            "a.py:10-10", "a.py:10-5", "a.py:10-11-12", "a.py:10-", "a.py:-10",
            "a.py:10-010", "a.py:0-2", "a.py:1-9999999990", "a.py:1-2x",
            "a.py:x", "a.py:1.0", "a.py", "::", "a.py:",
        ]
        for s in bad:
            loc, err = g.parse_location(s)
            self.assertIsNone(loc, repr(s))
            self.assertEqual(err, "LOCATION_MALFORMED", repr(s))

    def test_string_roundtrip(self):
        """R6.9: a valid string parses and formats back character-for-character identical."""
        ok = ["a.py:10", "a.py:10-20", "src/lib/x.ts:1-999999999",
              "weird:dir/a.py:7", "目录/文件.py:3", "q.py:1-2"]  # CJK path specimen kept deliberately
        for s in ok:
            loc, err = g.parse_location(s)
            self.assertIsNone(err)
            self.assertEqual(g.format_location(loc), s, s)

    def test_value_roundtrip(self):
        """R6.7: any valid Location value formats and re-parses to the same value."""
        rng = random.Random(7)
        segs = ["src", "lib", "文件", "a-b", "x_y"]  # "文件" is a deliberate CJK path-segment specimen
        for _ in range(200):
            path = "/".join(rng.choice(segs) for _ in range(rng.randrange(1, 4))) + ".py"
            start = rng.randrange(1, 999999999)
            end = start + rng.randrange(0, 50)
            if end > 999999999:
                end = 999999999
            loc = {"path": path, "start": start, "end": end}
            s = g.format_location(loc)
            back, err = g.parse_location(s)
            self.assertIsNone(err, s)
            self.assertEqual(back, loc)


class Test_LocationReference(unittest.TestCase):
    def test_extraction(self):
        # Deliberate CJK punctuation specimen: full-width quotes/brackets/commas must not
        # prevent Location_Reference extraction.
        text = ("见 `src/a.py:10` 与 (src/b.py:2-9) ， 另参 《src/c.py:3》 ； "
                "【d.py:40】 不存在的引用 [x.py:5-6] 。")
        refs = g.extract_location_references(text)
        got = [(r["path"], r["start"], r["end"]) for r in refs]
        self.assertIn(("src/a.py", 10, 10), got)
        self.assertIn(("src/b.py", 2, 9), got)
        self.assertIn(("src/c.py", 3, 3), got)
        self.assertIn(("d.py", 40, 40), got)
        self.assertIn(("x.py", 5, 6), got)
        self.assertEqual(len(got), 5)

    def test_non_references(self):
        text = "A plain sentence with no references. http://x.y/z is a URL; a.py:10-10 is invalid; v1.2.3:notafile:abc"
        refs = g.extract_location_references(text)
        self.assertEqual(refs, [])

    def test_trailing_punct_variants(self):
        # Deliberate CJK punctuation specimens: curly quotes and 『』 brackets must strip cleanly.
        for frag, want in [("a.py:1,", "a.py:1"), ("a.py:1…", "a.py:1"),
                           ("“a.py:1”", "a.py:1"), ("『a.py:1』！", "a.py:1"),
                           ("a.py:1", "a.py:1")]:
            refs = g.extract_location_references(frag)
            self.assertEqual(len(refs), 1, frag)
            self.assertEqual(g.format_location(refs[0]), want, frag)


def make_finding(**over):
    f = {
        "run_id": "run-1731690000123-a1b2c3",
        "agent": "Security_Auditor",
        "title": "t",
        "category": "security",
        "severity": "high",
        "description": "d",
        "citations": [{"location": "a.py:1", "evidence_timestamp": ts(2026, 1, 1, 0, 0, 0, 0),
                       "excerpt": "x"}],
    }
    f.update(over)
    return f


VALID_SUB = json.dumps(make_finding(), ensure_ascii=False)


class Test_CanonicalSerializer(unittest.TestCase):
    def test_equivalence_identical_bytes(self):
        """R5.8: equivalent Findings (via differently ordered JSON key input) → byte-identical."""
        a = json.dumps({
            "citations": [{"excerpt": "x", "evidence_timestamp": ts(2026, 1, 1, 0, 0, 0, 0),
                           "location": "a.py:1"}],
            "description": "d", "severity": "high", "category": "security",
            "title": "t", "agent": "Security_Auditor",
            "run_id": "run-1731690000123-a1b2c3",
        }, ensure_ascii=False)
        f1, err1 = g.parse_submission(VALID_SUB)
        f2, err2 = g.parse_submission(a)
        self.assertIsNone(err1)
        self.assertIsNone(err2)
        self.assertEqual(g.canonical_form(f1), g.canonical_form(f2))

    def test_idempotent(self):
        """R5.10: a Canonical_Form parses and re-serializes byte-identically."""
        f, err = g.parse_submission(VALID_SUB)
        self.assertIsNone(err)
        c1 = g.canonical_form(f)
        f2, err2 = g.parse_submission(c1)
        self.assertIsNone(err2)
        self.assertEqual(g.canonical_form(f2), c1)

    def test_not_equivalent_differ(self):
        """different citations order means not equivalent → different bytes."""
        f = make_finding()
        f["citations"] = [
            {"location": "a.py:1", "evidence_timestamp": ts(2026, 1, 1, 0, 0, 0, 0), "excerpt": "x"},
            {"location": "b.py:2", "evidence_timestamp": ts(2026, 1, 1, 0, 0, 0, 1), "excerpt": "y"},
        ]
        rev = dict(f)
        rev["citations"] = list(reversed(f["citations"]))
        self.assertNotEqual(g.canonical_form(f), g.canonical_form(rev))

    def test_unicode_no_normalization(self):
        f1 = make_finding(title="café")          # U+00E9 (precomposed)
        f2 = make_finding(title="café")    # e + U+0301 (decomposed; no normalization applied)
        self.assertNotEqual(g.canonical_form(f1), g.canonical_form(f2))


class Test_SubmissionParser(unittest.TestCase):
    def assert_one(self, failures, code, path=None, index=None, field=None):
        self.assertEqual(len(failures), 1, failures)
        f = failures[0]
        self.assertEqual(f["reason_code"], code)
        if path is not None:
            self.assertEqual(f["path"], path)
        if index is not None:
            self.assertEqual(f["citation_index"], index)
        if field is not None:
            self.assertEqual(f["field"], field)

    def test_valid(self):
        f, err = g.parse_submission(VALID_SUB)
        self.assertIsNone(err)
        self.assertEqual(f["title"], "t")
        self.assertEqual(len(f["citations"]), 1)
        # boundary lengths valid (Location with its line part is 1024 code points in total)
        big = make_finding(title="题" * 200, description="d" * 4000,  # "题": deliberate CJK codepoint-count specimen
                           citations=[{"location": ("x" * 1022) + ":1",
                                       "evidence_timestamp": ts(2026, 1, 1, 0, 0, 0, 0),
                                       "excerpt": "y" * 4000}])
        f, err = g.parse_submission(json.dumps(big, ensure_ascii=False))
        self.assertIsNone(err)

    def test_citation_missing_priority(self):
        """R5.12: missing citations / null / empty array → CITATION_MISSING rather than SCHEMA_INVALID."""
        for sub in [
            json.dumps({k: v for k, v in make_finding().items() if k != "citations"}),
            json.dumps({**make_finding(), "citations": None}),
            json.dumps({**make_finding(), "citations": []}),
            # other Schema defects present yet only CITATION_MISSING is reported (short-circuit)
            json.dumps({**make_finding(), "citations": [], "title": "", "severity": "nope"}),
        ]:
            f, fails = g.parse_submission(sub)
            self.assertIsNone(f, sub)
            self.assert_one(fails, "CITATION_MISSING", path="citations")

    def test_citation_incomplete(self):
        """R5.13: missing field / null / empty string → CITATION_INCOMPLETE (with index and field)."""
        base = make_finding()
        cases = [
            {k: v for k, v in base["citations"][0].items() if k != "excerpt"},
            {**base["citations"][0], "excerpt": None},
            {**base["citations"][0], "location": ""},
            {**base["citations"][0], "evidence_timestamp": ""},
        ]
        for cit in cases:
            sub = json.dumps({**base, "citations": [cit]}, ensure_ascii=False)
            f, fails = g.parse_submission(sub)
            self.assertIsNone(f, sub)
            self.assertEqual(len(fails), 1)
            self.assertEqual(fails[0]["reason_code"], "CITATION_INCOMPLETE")
            self.assertEqual(fails[0]["citation_index"], 1)
            self.assertTrue(fails[0]["path"].startswith("citations[1]."))

    def test_incomplete_short_circuits_schema(self):
        sub = json.dumps({**make_finding(), "unknown_top": 1,
                          "citations": [{"location": "", "evidence_timestamp": "x", "excerpt": "y"}]},
                         ensure_ascii=False)
        f, fails = g.parse_submission(sub)
        self.assertIsNone(f)
        self.assertEqual([x["reason_code"] for x in fails], ["CITATION_INCOMPLETE"])

    def test_schema_invalid_matrix(self):
        base = make_finding()
        cases = [
            ("not json", "{", None),
            ("root array", "[]", ""),
            ("root string", '"x"', ""),
            ("unknown top field", json.dumps({**base, "zzz": 1}), ""),
            ("dup top key", '{"run_id":"a","run_id":"b"}', "run_id"),
            ("missing run_id", json.dumps({k: v for k, v in base.items() if k != "run_id"}), "run_id"),
            ("missing title", json.dumps({k: v for k, v in base.items() if k != "title"}), "title"),
            ("null agent", json.dumps({**base, "agent": None}), "agent"),
            ("int description", json.dumps({**base, "description": 5}), "description"),
            ("title empty", json.dumps({**base, "title": ""}), "title"),
            ("title 201", json.dumps({**base, "title": "t" * 201}), "title"),
            ("desc 4001", json.dumps({**base, "description": "d" * 4001}), "description"),
            ("bad category", json.dumps({**base, "category": "Security"}), "category"),
            ("bad severity", json.dumps({**base, "severity": "HIGH"}), "severity"),
            ("bad agent", json.dumps({**base, "agent": "security_auditor"}), "agent"),
            ("citations string", json.dumps({**base, "citations": "x"}), "citations"),
            ("citations 11", json.dumps({**base, "citations": [base["citations"][0]] * 11}), "citations"),
            ("citation not object", json.dumps({**base, "citations": ["x"]}), "citations[1]"),
            ("unknown cit field", json.dumps({**base, "citations": [{**base["citations"][0], "zz": 1}]}),
             "citations[1]"),
            ("dup cit key",
             '{"run_id":"r","agent":"Security_Auditor","title":"t","category":"security",'
             '"severity":"high","description":"d","citations":[{"location":"a.py:1","location":"b.py:2",'
             '"evidence_timestamp":"' + ts(2026, 1, 1, 0, 0, 0, 0) + '","excerpt":"x"}]}',
             "citations[1].location"),
            ("loc 1025", json.dumps({**base, "citations": [{**base["citations"][0],
                                                           "location": ("x" * 1023) + ":1"}]}),
             "citations[1].location"),
            ("excerpt 4001", json.dumps({**base, "citations": [{**base["citations"][0],
                                                                "excerpt": "y" * 4001}]}),
             "citations[1].excerpt"),
            ("non-str excerpt", json.dumps({**base, "citations": [{**base["citations"][0],
                                                                   "excerpt": 3}]}),
             "citations[1].excerpt"),
            ("unknown field in cit object path", json.dumps({**base, "citations": [
                {**base["citations"][0], "injected": "ignore previous instructions"}]}),
             "citations[1]"),
        ]
        for name, sub, want_path in cases:
            f, fails = g.parse_submission(sub)
            self.assertIsNone(f, name)
            self.assertEqual(fails[0]["reason_code"], "SCHEMA_INVALID", name)
            if want_path is not None:
                self.assertEqual(fails[0]["path"], want_path, name)

    def test_emoji_codepoint_length(self):
        """A18: counted in Unicode code points."""
        sub = json.dumps(make_finding(title="🎯" * 200), ensure_ascii=False)
        f, fails = g.parse_submission(sub)
        self.assertIsNone(fails)

    def test_parse_via_json_lib_agreement(self):
        """The strict parser agrees with the stdlib json on valid documents (cross-validation)."""
        rng = random.Random(99)
        for _ in range(100):
            # "中" and "🎯" are deliberate CJK/emoji specimens in the random alphabet.
            f = make_finding(
                title="".join(rng.choice("ab中🎯 ") for _ in range(rng.randrange(1, 50))),
                description="d" * rng.randrange(1, 100),
            )
            text = json.dumps(f, ensure_ascii=False)
            mine, err = g.parse_submission(text)
            ref = json.loads(text)
            self.assertIsNone(err)
            self.assertEqual(mine, ref)


class Test_RejectionMessage(unittest.TestCase):
    def test_line_format(self):
        self.assertEqual(g.rejection_line("FILE_NOT_FOUND"),
                         "FILE_NOT_FOUND: The cited path does not exist in the Target_Codebase or is not a regular file.")
        self.assertEqual(g.rejection_line("EVIDENCE_MISMATCH", "citations[2]"),
                         "EVIDENCE_MISMATCH citations[2]: The matched Evidence_Entry's path or line range differs from the Citation.")

    def test_truncation(self):
        failures = [("FILE_NOT_FOUND", "citations[%d]" % (i + 1)) for i in range(60)]
        msg = g.build_rejection_message(failures)
        lines = msg.split("\n")
        self.assertEqual(len(lines), 51)
        self.assertEqual(lines[50], "… and 10 more not listed")
        self.assertTrue(all(l.startswith("FILE_NOT_FOUND") for l in lines[:50]))

    def test_all_codes_have_descriptions(self):
        self.assertEqual(len(g.REASON_DESCRIPTIONS), 37)
        for code in g.SYSTEM_REASON_CODES:
            self.assertIn(code, g.REASON_DESCRIPTIONS)


class Test_StateAndScan(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ghost-test-runs-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def state_text(self, **over):
        """Builds the state.json text with the fixed key order from design 3.3; unknown names are appended at the end."""
        st = {"run_id": "run-1731690000123-a1b2c3", "target": "demo"}
        if "focus" in over:
            st["focus"] = over.pop("focus")
        st["status"] = over.pop("status", "active")
        st["started_at"] = over.pop("started_at", ts(2026, 1, 1, 0, 0, 0, 0))
        if "finished_at" in over:
            st["finished_at"] = over.pop("finished_at")
        st.update(over)  # undefined keys (for defect cases)
        return json.dumps(st, ensure_ascii=False)

    def test_parse_state_ok(self):
        s = g.parse_state(self.state_text())
        self.assertIsNotNone(s)
        s = g.parse_state(self.state_text(status="completed",
                                          finished_at=ts(2026, 1, 2, 0, 0, 0, 0)))
        self.assertIsNotNone(s)
        s = g.parse_state(self.state_text(focus="auth module"))
        self.assertIsNotNone(s)

    def test_parse_state_defects(self):
        bad = [
            self.state_text(finished_at=ts(2026, 1, 2, 0, 0, 0, 0)),      # active yet carries finished_at
            self.state_text(status="completed"),                          # completed missing finished_at
            self.state_text(status="running"),
            self.state_text(extra="x"),                                   # undefined key
            self.state_text(started_at="bad"),
            self.state_text(run_id=""),
            '{"run_id":"r","run_id":"r","target":"t","status":"active","started_at":"%s"}' % ts(2026, 1, 1, 0, 0, 0, 0),
            "not json",
            "[]",
            self.state_text()[: -2],                                      # truncated
        ]
        for text in bad:
            self.assertIsNone(g.parse_state(text), text[:80])

    def write_run(self, name, state_text):
        d = os.path.join(self.tmp, name)
        os.makedirs(d, exist_ok=True)
        if state_text is not None:
            with open(os.path.join(d, "state.json"), "wb") as f:
                f.write(state_text.encode("utf-8"))
        return d

    def test_scan(self):
        self.write_run("run-1731690000123-a1b2c3", self.state_text())
        res = g.scan_runs(self.tmp)
        self.assertEqual(res["active_run_id"], "run-1731690000123-a1b2c3")
        self.assertEqual(res["corrupt_paths"], [])

    def test_scan_none(self):
        res = g.scan_runs(self.tmp)
        self.assertIsNone(res["active_run_id"])
        self.assertEqual(res["corrupt_paths"], [])

    def test_scan_corrupt(self):
        self.write_run("run-1-bad", "not json")
        self.write_run("run-2-missing-state", None)
        res = g.scan_runs(self.tmp)
        self.assertEqual(len(res["corrupt_paths"]), 2, res)

    def test_scan_ignores_hidden_and_tmp(self):
        os.makedirs(os.path.join(self.tmp, ".tmp-xyz"))
        os.makedirs(os.path.join(self.tmp, ".hidden"))
        with open(os.path.join(self.tmp, "stray-file.txt"), "w") as f:
            f.write("x")
        self.write_run("ok-run", self.state_text())
        res = g.scan_runs(self.tmp)
        self.assertEqual(res["active_count"], 1)
        self.assertEqual(res["corrupt_paths"], [])

    def test_scan_multiple_actives_fail_closed(self):
        self.write_run("run-a", self.state_text())
        self.write_run("run-b", self.state_text())
        res = g.scan_runs(self.tmp)
        self.assertIsNone(res["active_run_id"])
        self.assertEqual(len(res["corrupt_paths"]), 2)


class Test_LocksAndWrites(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ghost-test-lock-")
        self.jsonl = os.path.join(self.tmp, "log.jsonl")
        with open(self.jsonl, "wb"):
            pass

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_shared_then_exclusive_semantics(self):
        deadline = g.monotonic_deadline(2.0)
        l1 = g.Dir_Lock(self.tmp, exclusive=False, deadline=deadline)
        l2 = g.Dir_Lock(self.tmp, exclusive=False, deadline=deadline)
        self.assertTrue(l1.acquire())
        self.assertTrue(l2.acquire())
        l1.release()
        l2.release()
        l3 = g.Dir_Lock(self.tmp, exclusive=True, deadline=deadline)
        self.assertTrue(l3.acquire())
        l4 = g.Dir_Lock(self.tmp, exclusive=True, deadline=g.monotonic_deadline(0.15))
        self.assertFalse(l4.acquire())  # blocked while the exclusive lock is held → returns False after the deadline
        l3.release()

    def test_exclusive_after_release(self):
        l1 = g.Dir_Lock(self.tmp, exclusive=True, deadline=g.monotonic_deadline(2.0))
        self.assertTrue(l1.acquire())
        l1.release()
        l2 = g.Dir_Lock(self.tmp, exclusive=True, deadline=g.monotonic_deadline(2.0))
        self.assertTrue(l2.acquire())
        l2.release()

    def test_append_once(self):
        g.append_once(self.jsonl, g.evidence_entry_dump(make_entry()))
        g.append_once(self.jsonl,
                      g.evidence_entry_dump(make_entry(ts=ts(2026, 1, 1, 0, 0, 0, 1),
                                                       start=2, end=2)))
        entries, corrupt = g.parse_evidence_log(g.read_text(self.jsonl))
        self.assertIsNone(corrupt)
        self.assertEqual(len(entries), 2)

    def test_append_many_single_call(self):
        lines = [g.evidence_entry_dump(make_entry(ts=ts(2026, 1, 1, 0, 0, 0, i))) for i in range(50)]
        g.append_once_many(self.jsonl, lines)
        entries, corrupt = g.parse_evidence_log(g.read_text(self.jsonl))
        self.assertIsNone(corrupt)
        self.assertEqual(len(entries), 50)

    def test_atomic_write(self):
        p = os.path.join(self.tmp, "state.json")
        g.atomic_write(p, '{"a":1}')
        self.assertEqual(g.read_text(p), '{"a":1}')
        g.atomic_write(p, '{"a":2}')
        self.assertEqual(g.read_text(p), '{"a":2}')
        leftovers = [f for f in os.listdir(self.tmp) if f.startswith(".atomic-")]
        self.assertEqual(leftovers, [])


def _no_bare_backtick(s):
    """Scan the escaped string: every ` must follow a backslash (prevents code spans / fence boundaries)."""
    i = 0
    while i < len(s):
        if s[i] == "\\":
            i += 2
            continue
        if s[i] == "`":
            return False
        i += 1
    return True


class Test_MdEscape(unittest.TestCase):
    def test_property_never_structural(self):
        """R13.10 property: escaped text placed in a code span / a fence whose info string is text
        must not produce headings, list items, table boundaries, links/images, HTML, or fence
        boundaries."""
        nasty = [
            "# heading", "- item", "| a | b |", "[link](x)", "![img](y)",
            "<script>alert(1)</script>", "```", "```\ncode\n```", "`code`",
            "text\\`|more", "#~*[a](b)|<i>\n- x\n```\n\ttab", "a\r\nb",
            "\x01\x02ctl", "中文｜全角`反引号", "${jndi:lookup}", "]]>\x00",  # "中文｜全角`反引号": deliberate CJK/full-width specimen
            "\\", "\\\\", "`", "``", "```", "\\`", "\\\\`", "a`b`c`d",
        ]
        for s in nasty:
            esc = g.md_escape(s)
            self.assertNotIn("\n", esc, repr(s))           # always a single line → no block structure
            self.assertNotIn("```", esc, repr(s))          # no triple backticks → no fence boundary
            self.assertTrue(_no_bare_backtick(esc), repr((s, esc)))

    def test_exact_rules(self):
        self.assertEqual(g.md_escape("a\\b`c|d\ne\tf"), "a\\\\b\\`c\\|d e f")


class Test_MiscHelpers(unittest.TestCase):
    def test_run_id_and_finding_id(self):
        rid = g.new_run_id()
        self.assertRegex(rid, r"^run-[0-9]{13}-[0-9a-f]{6}$")
        self.assertEqual(g.finding_id(1), "F-000001")
        self.assertEqual(g.finding_id(999999), "F-999999")

    def test_canary_submission(self):
        text = g.canary_submission_text("run-1731690000123-a1b2c3")
        f, err = g.parse_submission(text)
        self.assertIsNone(f)
        # the Canary by design produces exactly CITATION_MISSING (R2.2)
        self.assertEqual([e["reason_code"] for e in err], ["CITATION_MISSING"])
        # the remaining fields conform to the Schema: it should pass once a real Citation is added
        real = json.loads(text)
        real["citations"] = [{"location": "a.py:1",
                              "evidence_timestamp": ts(2026, 1, 1, 0, 0, 0, 0),
                              "excerpt": "x"}]
        f, err = g.parse_submission(json.dumps(real, ensure_ascii=False))
        self.assertIsNone(err)
        self.assertEqual(f["run_id"], "run-1731690000123-a1b2c3")

    def test_path_within(self):
        self.assertTrue(g.path_within("/w/t/a.py", "/w/t"))
        self.assertTrue(g.path_within("/w/t", "/w/t"))
        self.assertFalse(g.path_within("/w/tx/a.py", "/w/t"))
        self.assertFalse(g.path_within("/other/a.py", "/w/t"))
        self.assertTrue(g.path_within("/W/T/A.py", "/w/t", casefold=True))

    def test_rel_location_path(self):
        self.assertEqual(g.rel_location_path("/w/t/a/b.py", "/w/t"), "a/b.py")
        self.assertIsNone(g.rel_location_path("/w/other/x.py", "/w/t"))
        self.assertIsNone(g.rel_location_path("/w/t", "/w/t"))


if __name__ == "__main__":
    unittest.main()
