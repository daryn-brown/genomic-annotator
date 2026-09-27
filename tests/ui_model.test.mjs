import test from "node:test";
import assert from "node:assert/strict";
import {
  arcPath, chromosomeSegments, pageLabel, polar, positionAngle, shortStatus, sourceLabel,
} from "../genomic_annotator/static/model.mjs";

test("all chromosomes get equal non-overlapping sectors, not invented reference lengths", () => {
  const chromosomes = Array.from({ length: 25 }, (_, index) => ({
    name: String(index + 1), count: index + 1, max_position: (index + 1) * 1000,
  }));
  const segments = chromosomeSegments(chromosomes);
  assert.equal(segments.length, 25);
  assert(segments[0].start > 0);
  assert(segments.at(-1).end < 360);
  for (let index = 1; index < segments.length; index++) {
    assert(segments[index].start > segments[index - 1].end);
    assert(Math.abs(
      (segments[index].end - segments[index].start) -
      (segments[0].end - segments[0].start)
    ) < 1e-9);
  }
  assert.deepEqual(chromosomeSegments([]), []);
});

test("a one-chromosome map is a valid almost-full ring", () => {
  const [segment] = chromosomeSegments([{ name: "MT", max_position: 1 }]);
  const path = arcPath(340, 277, 216, 230, segment.start, segment.end);
  assert(!path.includes("NaN"));
  assert.match(path, /A 230 230 0 1 1/);
  assert.equal(positionAngle(segment, 1), segment.start);
});

test("the selected variant maps to its actual normalized input coordinate", () => {
  const segment = { start: 10, end: 30, max_position: 101 };
  assert.equal(positionAngle(segment, 1), 10);
  assert.equal(positionAngle(segment, 51), 20);
  assert.equal(positionAngle(segment, 101), 30);
  assert.equal(positionAngle(segment, -5), 10);
  assert.equal(positionAngle(segment, 102), 30);
});

test("polar coordinates put zero degrees at the top", () => {
  const [x, y] = polar(10, 10, 5, 0);
  assert(Math.abs(x - 10) < 1e-9);
  assert.equal(y, 5);
  assert.deepEqual(polar(10, 10, 5, 90), [15, 10]);
});

test("paging is row-based, with explicit empty and last-page labels", () => {
  assert.equal(pageLabel(0, 30, 700), "1 - 30 of 700");
  assert.equal(pageLabel(690, 10, 700), "691 - 700 of 700");
  assert.equal(pageLabel(0, 0, 0), "0 matching variants");
});

test("missing and partial annotations are never portrayed as benign", () => {
  assert.equal(shortStatus({ annotation_status: "offline_cache_miss" }), "No cached annotation");
  assert.equal(shortStatus({ annotation_status: "fetch_failed" }), "Lookup incomplete");
  assert.equal(shortStatus({ annotation_status: "unsupported_id" }), "Local-only identifier");
  assert.equal(shortStatus({ annotation_status: "annotated", clinical_significance: null }), "Annotation available");
  assert.equal(sourceLabel("synthetic"), "Synthetic demo");
  assert.equal(sourceLabel("none"), "Not available");
});
