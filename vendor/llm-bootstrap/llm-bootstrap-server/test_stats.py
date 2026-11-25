"""The statistics store's rows: per model and per measure, kept whole.

    python test_stats.py

No model, no GPU, no network, no subprocess. Every row has a folder of its
own, and reads what the store WROTE - the file's bytes, or the summary read
back from them - never a field it merely set. Where a row needs the swap to
fail, it makes the store's own swap fail (`ModelStats.replace`).
"""
from __future__ import annotations

import json
import os
import random
import sys
import tempfile
import threading
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from runner import stats as ST                                  # noqa: E402

NAN, INF = float("nan"), float("inf")


class Case(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.dir = Path(self._tmp.name) / "runner"
        self.path = self.dir / ST.FILE
        self.stats = ST.ModelStats(self.path)

    def tearDown(self):
        self._tmp.cleanup()

    def kept(self) -> dict:
        """The summary as the file holds it - its bytes, read back."""
        return json.loads(self.path.read_bytes().decode("utf-8"))

    def entry(self, model="story-12b", measure="load_s") -> dict:
        return self.kept()["models"][model][measure]

    def note_all(self, values, model="story-12b", measure="load_s"):
        for i, v in enumerate(values):
            self.stats.note(model, measure, v, at=1790000000.0 + i)

    def beside(self) -> list:
        """What the store's folder holds."""
        return sorted(p.name for p in self.dir.iterdir())


class FigureRows(Case):
    """Over all time: count, mean, least, most, last. Of the last 200: the
    samples, for the median and the 90th percentile."""

    def test_each_figure_over_all_time(self):
        self.note_all([4.0, 2.0, 6.0, 5.0])
        self.assertEqual(self.entry(), {"count": 4, "mean": 4.25, "least": 2.0, "most": 6.0,
                                        "last": 5.0, "last_at": 1790000003.0,
                                        "samples": [4.0, 2.0, 6.0, 5.0]})

    def test_a_note_answers_the_entry_as_kept(self):
        got = self.stats.note("story-12b", "load_s", 3.5, at=1790000000.0)
        self.assertEqual(got, self.entry())
        self.assertEqual((got["count"], got["last"]), (1, 3.5))

    def test_the_window_is_the_last_200_and_the_rest_is_over_all_time(self):
        self.note_all([float(v) for v in range(1, 251)])
        e = self.entry()
        self.assertEqual(e["samples"], [float(v) for v in range(51, 251)],
                         "the last 200, oldest first")
        self.assertEqual((e["count"], e["least"], e["most"], e["mean"], e["last"]),
                         (250, 1.0, 250.0, 125.5, 250.0),
                         "a sample that has left the window still counts over all time")

    def test_the_median_and_the_90th_percentile_are_of_the_samples_kept(self):
        self.note_all([float(v) for v in range(1, 251)])
        d = ST.describe(self.entry())
        self.assertEqual((d["median"], d["p90"], d["kept"], d["count"], d["least"]),
                         (150.5, 230.0, 200, 250, 1.0))

    def test_the_90th_percentile_is_a_sample_that_happened_the_nearest_rank(self):
        for values, p90 in (([float(v) for v in range(1, 11)], 9.0),     # never 9.1
                            ([4.0, 2.0, 6.0, 5.0], 6.0),
                            ([3.0, 1.0, 2.0], 3.0),
                            ([7.5], 7.5)):
            with self.subTest(values=values):
                e = {"count": len(values), "mean": 0.0, "least": min(values),
                     "most": max(values), "last": values[-1], "last_at": 1.0,
                     "samples": values}
                self.assertEqual(ST.describe(e)["p90"], p90)

    def test_the_median_of_an_even_count_is_the_middle_two_averaged(self):
        for values, median in (([4.0, 2.0, 6.0, 5.0], 4.5), ([3.0, 1.0, 2.0], 2.0),
                               ([7.5], 7.5)):
            with self.subTest(values=values):
                e = {"count": len(values), "mean": 0.0, "least": min(values),
                     "most": max(values), "last": values[-1], "last_at": 1.0,
                     "samples": values}
                self.assertEqual(ST.describe(e)["median"], median)

    def test_each_model_and_each_measure_is_its_own(self):
        self.stats.note("story-12b", "load_s", 1.0, at=10.0)
        self.stats.note("story-12b", "answer_s", 2.0, at=11.0)
        self.stats.note("draft-7b", "load_s", 3.0, at=12.0)
        self.assertEqual({m: {k: (e["count"], e["last"]) for k, e in ms.items()}
                          for m, ms in self.kept()["models"].items()},
                         {"story-12b": {"load_s": (1, 1.0), "answer_s": (1, 2.0)},
                          "draft-7b": {"load_s": (1, 3.0)}})

    def test_nothing_kept_reads_as_an_empty_summary_and_a_read_writes_nothing(self):
        self.assertEqual(self.stats.read(), {"version": 1, "models": {}})
        self.assertFalse(self.dir.exists(), "a read made the folder")


class RebuildRows(Case):
    """The records the summary is summed from stay, and it can be summed
    again from them."""

    def test_the_summary_rebuilt_from_its_records_equals_the_one_kept(self):
        rng = random.Random(20260925)
        records = [{"model": rng.choice(["story-12b", "draft-7b"]),
                    "measure": rng.choice(["load_s", "first_token_s"]),
                    "value": round(rng.uniform(0.05, 90.0), rng.choice([1, 3, 6])),
                    "at": 1790000000.0 + i * 0.37} for i in range(1000)]
        for r in records:
            self.stats.note(r["model"], r["measure"], r["value"], at=r["at"])
        kept = self.stats.read()
        self.assertTrue(any(e["count"] > ST.SAMPLES_KEPT for ms in kept["models"].values()
                            for e in ms.values()), "some measure's window was trimmed")
        self.assertEqual(kept, ST.rebuild(records))
        self.assertEqual(kept, self.kept(), "read() says what the file holds")

    def test_a_record_the_store_would_refuse_is_refused_by_the_rebuild_too(self):
        with self.assertRaises(ValueError):
            ST.rebuild([{"model": "story-12b", "measure": "load_s", "value": NAN, "at": 1.0}])


class WriteRows(Case):
    """Written whole or not at all: a lock, a temporary name of its own, the
    swap, and bytes with LF."""

    def test_the_file_is_utf8_bytes_with_lf_and_says_what_read_says(self):
        self.stats.note("story-12b", "load_s", 4.0, at=1.0)
        raw = self.path.read_bytes()
        self.assertNotIn(b"\r", raw)
        self.assertTrue(raw.endswith(b"}\n"), raw[-20:])
        self.assertEqual(json.loads(raw.decode("utf-8")), self.stats.read())

    def test_every_write_goes_through_a_temporary_name_of_its_own(self):
        used = []

        def swap(src, dst):
            used.append(Path(src))
            os.replace(src, dst)
        self.stats.replace = swap
        other = ST.ModelStats(self.path)
        other.replace = swap
        for i in range(10):
            self.stats.note("story-12b", "load_s", float(i), at=float(i))
            other.note("draft-7b", "load_s", float(i), at=float(i))
        self.assertEqual(len(used), 20)
        self.assertEqual(len(set(used)), 20, "two writes shared a temporary name")
        self.assertTrue(all(u.parent == self.dir and u != self.path for u in used),
                        "the swap is a rename beside the file")
        self.assertEqual(self.beside(), [ST.FILE], "nothing is left beside it")

    def test_a_swap_that_fails_leaves_the_kept_file_whole_and_nothing_beside_it(self):
        self.stats.note("story-12b", "load_s", 4.0, at=1.0)
        before = self.path.read_bytes()
        tries = []

        def refuse(src, dst):
            tries.append(src)
            raise PermissionError(13, "the file is held by another process")
        self.stats.replace = refuse
        with self.assertRaises(PermissionError):
            self.stats.note("story-12b", "load_s", 9.0, at=2.0)
        self.assertEqual(self.path.read_bytes(), before, "never half written")
        self.assertEqual(self.beside(), [ST.FILE], "the temporary file was taken away")
        self.assertEqual(len(tries), ST.SWAP_TRIES, "tried, and bounded")

    def test_a_swap_refused_for_a_moment_is_tried_again(self):
        self.stats.note("story-12b", "load_s", 4.0, at=1.0)
        refused = []

        def once_held(src, dst):
            if len(refused) < 2:
                refused.append(src)
                raise PermissionError(13, "the file is held by a reader")
            os.replace(src, dst)
        self.stats.replace = once_held
        self.stats.note("story-12b", "load_s", 9.0, at=2.0)
        self.assertEqual((len(refused), self.entry()["count"], self.entry()["last"]),
                         (2, 2, 9.0))

    def test_writers_at_once_lose_nothing(self):
        per, threads = 30, 8
        noted, errors = [], []
        lock = threading.Lock()

        def writer(k):
            try:
                for i in range(per):
                    measure = ("load_s", "answer_s")[i % 2]
                    value, at = float(k * 1000 + i), 1790000000.0 + k * 1000 + i
                    got = self.stats.note("story-12b", measure, value, at=at)
                    with lock:
                        noted.append((measure, got["count"],
                                      {"model": "story-12b", "measure": measure,
                                       "value": value, "at": at}))
            except Exception as e:                          # noqa: BLE001
                with lock:
                    errors.append(f"{type(e).__name__}: {e}")
        ts = [threading.Thread(target=writer, args=(k,), daemon=True) for k in range(threads)]
        for t in ts:
            t.start()
        for t in ts:
            t.join(60)
        self.assertFalse(any(t.is_alive() for t in ts), "a writer never finished")
        self.assertEqual(errors, [])
        half = per * threads // 2
        for measure in ("load_s", "answer_s"):
            with self.subTest(measure=measure):
                self.assertEqual(sorted(c for m, c, _ in noted if m == measure),
                                 list(range(1, half + 1)), "every note counted once")
        in_order = [r for _, _, r in sorted(noted, key=lambda n: (n[0], n[1]))]
        self.assertEqual(self.stats.read(), ST.rebuild(in_order))


class ManyRows(Case):
    """S11b: what one thing measured - a load's steps, an answer's figures -
    is noted in ONE write (`note_many`), folded in the order it was given:
    never half of an answer's samples kept, and never two writes for one."""

    MEASURES = {"first_token_s": 0.4, "answer_s": 2.5, "tokens": 120.0, "tokens_per_s": 57.0}

    def test_several_measures_fold_in_order_as_one_note_each_would(self):
        self.stats.note_many("story-12b", {"answer_s": 1.0}, at=1.0)
        got = self.stats.note_many("story-12b", self.MEASURES, at=2.0)
        one = ST.ModelStats(self.dir / "one-at-a-time.json")
        one.note("story-12b", "answer_s", 1.0, at=1.0)
        for measure, value in self.MEASURES.items():
            one.note("story-12b", measure, value, at=2.0)
        self.assertEqual(self.kept(), one.read(), "folded as note folds each, in order")
        self.assertEqual(list(got), list(self.MEASURES), "each measure, in the order given")
        self.assertEqual(got["answer_s"], self.kept()["models"]["story-12b"]["answer_s"],
                         "each answered as kept")
        self.assertEqual(self.kept()["models"]["story-12b"]["answer_s"]["count"], 2)

    def test_it_is_one_write(self):
        swaps = []

        def swap(src, dst):
            swaps.append(src)
            os.replace(src, dst)
        self.stats.replace = swap
        self.stats.note_many("story-12b", self.MEASURES, at=1.0)
        self.assertEqual(len(swaps), 1, "one write for everything one thing measured")

    def test_one_refused_value_refuses_them_all_before_the_file_is_touched(self):
        self.stats.note("story-12b", "load_s", 4.0, at=1.0)
        before = self.path.read_bytes()
        for bad in ({"answer_s": 1.0, "tokens": NAN}, {"answer_s": 1.0, "": 2.0},
                    {"answer_s": 1.0, "tokens": True}):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    self.stats.note_many("story-12b", bad, at=2.0)
                self.assertEqual(self.path.read_bytes(), before, "nothing of it kept")
        with self.assertRaises(ValueError):
            self.stats.note_many("story-12b", {"answer_s": 1.0}, at=INF)
        self.assertEqual(self.path.read_bytes(), before)

    def test_an_unreadable_file_is_refused_and_left_as_it_was(self):
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path.write_bytes(b"[1, 2]")
        with self.assertRaises(ST.StatsUnreadable):
            self.stats.note_many("story-12b", self.MEASURES, at=1.0)
        self.assertEqual(self.path.read_bytes(), b"[1, 2]")


class RefusalRows(Case):
    """What cannot be a sample is refused before anything is written, and a
    file that cannot be read is never written over."""

    def test_a_sample_that_is_not_a_finite_number_is_refused(self):
        for v in (NAN, INF, -INF, True, "3.5", None):
            with self.subTest(value=v), self.assertRaises(ValueError):
                self.stats.note("story-12b", "load_s", v, at=1.0)
        self.assertFalse(self.path.exists(), "nothing was written")
        self.stats.note("story-12b", "load_s", 4.0, at=1.0)
        before = self.path.read_bytes()
        with self.assertRaises(ValueError):
            self.stats.note("story-12b", "load_s", NAN, at=2.0)
        self.assertEqual(self.path.read_bytes(), before)

    def test_a_time_that_is_not_a_finite_number_is_refused(self):
        with self.assertRaises(ValueError):
            self.stats.note("story-12b", "load_s", 4.0, at=NAN)
        self.assertFalse(self.path.exists())

    def test_a_model_or_a_measure_that_is_not_a_name_is_refused(self):
        for model, measure in (("", "load_s"), (None, "load_s"), ("story-12b", ""),
                               ("story-12b", 7)):
            with self.subTest(model=model, measure=measure), self.assertRaises(ValueError):
                self.stats.note(model, measure, 4.0, at=1.0)
        self.assertFalse(self.path.exists())

    def test_a_file_that_is_not_a_summary_is_never_written_over(self):
        entry = {"count": 1, "mean": 1.0, "least": 1.0, "most": 1.0, "last": 1.0,
                 "last_at": 1.0, "samples": [1.0]}
        for name, raw in (
                ("not JSON", b"{\"version\": 1, \"models\": {"),
                ("JSON, not a summary", b"[1, 2]"),
                ("another version", json.dumps({"version": 2, "models": {}}).encode()),
                ("a count that is not a number", json.dumps(
                    {"version": 1, "models": {"m": {"load_s": {**entry, "count": "3"}}}}).encode()),
                ("a count with no samples", json.dumps(
                    {"version": 1, "models": {"m": {"load_s": {**entry, "samples": []}}}}).encode()),
                ("a sample that is not a number", json.dumps(
                    {"version": 1, "models": {"m": {"load_s": {**entry, "samples": ["x"]}}}}).encode())):
            with self.subTest(name):
                self.dir.mkdir(parents=True, exist_ok=True)
                self.path.write_bytes(raw)
                with self.assertRaises(ST.StatsUnreadable):
                    self.stats.note("m", "load_s", 4.0, at=2.0)
                with self.assertRaises(ST.StatsUnreadable):
                    self.stats.read()
                self.assertEqual(self.path.read_bytes(), raw, "left as it was")


class BesideRows(Case):
    """S17: answers to different models run at once, so a sample can say how
    many other answers ran beside it (`beside`, a count) - kept beside each
    sample, in step with the samples, so a lone answer is told from a shared
    one. A measure never told it keeps no such list; one told it once keeps
    null for the samples before."""

    def test_each_sample_keeps_how_many_answers_ran_beside_it(self):
        self.stats.note_many("story-12b", {"answer_s": 2.0, "tokens": 40}, at=1.0, beside=0)
        self.stats.note_many("story-12b", {"answer_s": 3.0, "tokens": 40}, at=2.0, beside=1)
        for measure in ("answer_s", "tokens"):
            self.assertEqual(self.entry(measure=measure)["beside"], [0, 1])
        self.assertEqual(self.entry(measure="answer_s")["samples"], [2.0, 3.0])

    def test_a_measure_never_told_keeps_none_and_one_told_later_keeps_null_before(self):
        self.stats.note("story-12b", "load_s", 4.0, at=1.0)
        self.assertNotIn("beside", self.entry())
        self.stats.note("story-12b", "load_s", 5.0, at=2.0, beside=2)
        self.stats.note("story-12b", "load_s", 6.0, at=3.0)
        self.assertEqual(self.entry()["beside"], [None, 2, None])

    def test_it_leaves_the_window_in_step_with_its_sample(self):
        for i in range(ST.SAMPLES_KEPT + 3):
            self.stats.note("story-12b", "answer_s", float(i), at=float(i + 1), beside=i % 2)
        e = self.entry(measure="answer_s")
        self.assertEqual(len(e["beside"]), ST.SAMPLES_KEPT)
        self.assertEqual(e["beside"], [int(x) % 2 for x in e["samples"]])

    def test_the_rebuild_keeps_it_too(self):
        records = [{"model": "story-12b", "measure": "answer_s", "value": 2.0, "at": 1.0,
                    "beside": 1},
                   {"model": "story-12b", "measure": "answer_s", "value": 3.0, "at": 2.0}]
        for r in records:
            self.stats.note(r["model"], r["measure"], r["value"], at=r["at"],
                            beside=r.get("beside"))
        self.assertEqual(self.kept(), ST.rebuild(records))
        self.assertEqual(ST.rebuild(records)["models"]["story-12b"]["answer_s"]["beside"],
                         [1, None])

    def test_a_count_that_is_not_one_is_refused_before_the_file_is_touched(self):
        self.stats.note("story-12b", "answer_s", 1.0, at=1.0)
        raw = self.path.read_bytes()
        for bad in (-1, 1.5, True, "1", NAN):
            with self.subTest(beside=bad):
                with self.assertRaises(ValueError):
                    self.stats.note("story-12b", "answer_s", 2.0, at=2.0, beside=bad)
                with self.assertRaises(ValueError):
                    self.stats.note_many("story-12b", {"answer_s": 2.0}, at=2.0, beside=bad)
                self.assertEqual(self.path.read_bytes(), raw)

    def test_a_summary_whose_list_is_out_of_step_is_not_a_summary(self):
        for beside in ([1, 2], ["one"], [-1], "1"):
            with self.subTest(beside=beside):
                doc = {"version": ST.VERSION, "models": {"story-12b": {"answer_s": {
                    "count": 1, "mean": 1.0, "least": 1.0, "most": 1.0, "last": 1.0,
                    "last_at": 1.0, "samples": [1.0], "beside": beside}}}}
                self.dir.mkdir(parents=True, exist_ok=True)
                self.path.write_bytes(json.dumps(doc).encode("utf-8"))
                with self.assertRaises(ST.StatsUnreadable):
                    self.stats.read()
        doc["models"]["story-12b"]["answer_s"]["beside"] = [None]
        self.path.write_bytes(json.dumps(doc).encode("utf-8"))
        self.assertEqual(self.stats.read(), doc)


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    from _suites import report                             # the tally, counted in rows
    report(unittest.main(exit=False, verbosity=1))
