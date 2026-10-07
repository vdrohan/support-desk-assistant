import json

from fakes import word_count
from support_desk_assistant.config import ChunkSettings, Settings
from support_desk_assistant.ingest.chunk import (
    Section,
    chunk_corpus,
    chunk_page,
    drop_repeated_blocks,
    merge_small_sections,
    pack_blocks,
    parse_page,
    split_oversized_block,
)

URL = "https://www.canada.ca/en/services/benefits/ei/apply.html"
CFG = ChunkSettings(target_tokens=60, max_tokens=100, min_tokens=15)


def words(n: int, word: str = "word") -> str:
    return " ".join([word] * n)


PAGE = f"""Source: {URL}
Date modified: 2026-08-31

# Apply for EI

{words(20, "intro")}

## Eligibility

{words(30, "elig")}

### Hours worked

{words(25, "hours")}

#### Detail heading kept in body

{words(10, "detail")}

## Tiny

just a few words

## How to apply

{words(40, "applya")}

{words(40, "applyb")}

{words(40, "applyc")}
"""


def chunks(markdown: str = PAGE, url: str = URL, cfg: ChunkSettings = CFG):
    return chunk_page(
        markdown, url=url, program="ei", date_modified="2026-08-31", cfg=cfg, count=word_count
    )


# --- parsing ---------------------------------------------------------------------


def test_parse_page_sections_and_header_stripped():
    title, sections = parse_page(PAGE)
    assert title == "Apply for EI"
    assert [s.path for s in sections] == [
        ("Apply for EI",),
        ("Apply for EI", "Eligibility"),
        ("Apply for EI", "Eligibility", "Hours worked"),
        ("Apply for EI", "Tiny"),
        ("Apply for EI", "How to apply"),
    ]
    flat = "\n".join(b for s in sections for b in s.blocks)
    assert "Source:" not in flat and "Date modified:" not in flat
    assert "#### Detail heading kept in body" in sections[2].blocks


def test_empty_heading_sections_dropped():
    _, sections = parse_page("# T\n\n## Empty\n\n### Child\n\ntext here")
    assert [s.path for s in sections] == [("T", "Empty", "Child")]


# --- merging ---------------------------------------------------------------------


def test_tiny_section_merged_into_neighbour_with_heading_kept():
    _, sections = parse_page(PAGE)
    merged = merge_small_sections(sections, word_count, CFG.min_tokens)
    paths = [s.path for s in merged]
    assert ("Apply for EI", "Tiny") not in paths
    how = next(s for s in merged if s.path == ("Apply for EI", "How to apply"))
    # No next section under the same H2 -> falls back to a neighbour; heading travels along
    holder = next(s for s in merged if any("## Tiny" in b for b in s.blocks))
    assert "just a few words" in "\n".join(holder.blocks)
    assert holder is not None and how is not None


def test_merge_prefers_next_section_under_same_h2():
    sections = [
        Section(("T", "A"), ["two words"]),
        Section(("T", "A", "Sub"), [words(30)]),
        Section(("T", "B"), [words(30)]),
    ]
    merged = merge_small_sections(sections, word_count, 15)
    assert [s.path for s in merged] == [("T", "A", "Sub"), ("T", "B")]
    assert merged[0].blocks[0] == "two words"  # same H2: no extra heading needed


def test_single_small_section_kept():
    merged = merge_small_sections([Section(("T",), ["tiny"])], word_count, 15)
    assert merged == [Section(("T",), ["tiny"])]


# --- packing ---------------------------------------------------------------------


def test_pack_respects_target_and_block_boundaries():
    blocks = [words(40, "a"), words(40, "b"), words(40, "c")]
    out = pack_blocks(blocks, word_count, target=60, max_tokens=100, min_tokens=15)
    assert out == blocks  # 40+40 > 60, so one block per chunk; never split mid-block


def test_pack_grows_past_target_while_under_min():
    out = pack_blocks([words(10, "a"), words(55, "b")], word_count, 60, 100, 15)
    assert out == [f"{words(10, 'a')}\n\n{words(55, 'b')}"]


def test_small_tail_folded_into_previous():
    out = pack_blocks([words(50, "a"), words(5, "b")], word_count, 50, 100, 15)
    assert len(out) == 1


def test_list_kept_whole():
    lst = "\n".join(f"- item {i} " + words(3) for i in range(10))  # 50 words, no blank lines
    out = pack_blocks([words(30), lst], word_count, 60, 100, 15)
    assert lst in out


def test_oversized_table_split_with_header_repeated():
    header = "| Benefit | Amount |\n| --- | --- |"
    rows = "\n".join(f"| row {i} {words(8)} | $1 |" for i in range(30))
    pieces = split_oversized_block(f"{header}\n{rows}", word_count, 100)
    assert len(pieces) > 1
    assert all(p.startswith(header) for p in pieces)
    assert all(word_count(p) <= 100 for p in pieces)


def test_oversized_paragraph_split_by_words():
    pieces = split_oversized_block(words(250), word_count, 100)
    assert [word_count(p) for p in pieces] == [100, 100, 50]


# --- chunks ----------------------------------------------------------------------


def test_prefix_and_payload_fields():
    first, *_ = chunks()
    assert first.embed_text.startswith("Apply for EI\n\n")
    hours = next(c for c in chunks() if c.heading_path == ["Eligibility", "Hours worked"])
    assert hours.embed_text.startswith("Apply for EI > Eligibility > Hours worked\n\n")
    assert hours.text == hours.embed_text.split("\n\n", 1)[1]
    assert hours.title == "Apply for EI"
    assert hours.tokens == word_count(hours.embed_text)


def test_all_chunks_within_max_and_indexed_in_order():
    big = PAGE + "\n\n## Huge\n\n" + "\n\n".join(words(70, f"p{i}") for i in range(6))
    out = chunks(big)
    assert all(c.tokens <= CFG.max_tokens for c in out)
    assert [c.chunk_index for c in out] == list(range(len(out)))


def test_ids_deterministic_and_unique():
    a, b = chunks(), chunks()
    assert [c.id for c in a] == [c.id for c in b]
    assert len({c.id for c in a}) == len(a)
    other = chunks(url=URL.replace("apply", "eligibility"))
    assert not {c.id for c in a} & {c.id for c in other}


def test_edit_in_one_section_keeps_other_ids():
    before = {tuple(c.heading_path): c for c in chunks()}
    after = {tuple(c.heading_path): c for c in chunks(PAGE.replace("elig elig", "elig CHANGED"))}
    changed = ("Eligibility",)
    assert before[changed].id == after[changed].id  # same position -> same ID (upsert)
    assert before[changed].content_hash != after[changed].content_hash
    for path, chunk in before.items():
        if path != changed:
            assert after[path].content_hash == chunk.content_hash


def test_chunk_corpus_reads_manifest_and_skips_missing(tmp_path, caplog):
    settings = Settings(_env_file=None, data_dir=tmp_path, chunk=CFG)
    (tmp_path / "processed" / "ei").mkdir(parents=True)
    (tmp_path / "processed" / "ei" / "services__benefits__ei__apply.md").write_text(PAGE)
    rows = [
        {"url": URL, "program": "ei", "date_modified": "2026-08-31"},
        {"url": URL.replace("apply", "gone"), "program": "ei", "date_modified": None},
        {"url": "https://www.canada.ca/en/x/sin.html", "program": "sin", "date_modified": None},
    ]
    settings.manifest_path.write_text("".join(json.dumps(r) + "\n" for r in rows))

    out = chunk_corpus(settings, word_count, programs=["ei"])
    assert out and all(c.program == "ei" and c.url == URL for c in out)
    assert "missing processed file" in caplog.text


# --- within-page dedup -----------------------------------------------------------

STEPS = words(20, "step")  # a shared block, as in multi-scenario instruction pages
WIZARD = f"""# Child passport

## Scenario A

{STEPS}

{words(20, "only-a")}

## Scenario B

{STEPS}

{words(20, "only-b")}

## Scenario C

{STEPS}

short repeat

short repeat
"""


def test_repeated_blocks_dropped_keeping_first_occurrence():
    _, sections = parse_page(WIZARD)
    deduped, dropped = drop_repeated_blocks(sections, word_count, 8)
    assert dropped == 2
    a, b, c = deduped
    assert a.blocks == [STEPS, words(20, "only-a")]
    assert b.blocks == [words(20, "only-b")]
    assert c.blocks == ["short repeat", "short repeat"]  # under 8 tokens: kept


def test_section_emptied_by_dedup_is_removed():
    _, sections = parse_page(f"# T\n\n## A\n\n{STEPS}\n\n## B\n\n{STEPS}")
    deduped, dropped = drop_repeated_blocks(sections, word_count, 8)
    assert dropped == 1 and [s.path for s in deduped] == [("T", "A")]


def test_chunk_page_reports_dropped_and_has_no_duplicate_text():
    dropped: dict[str, int] = {}
    out = chunk_page(
        WIZARD, url=URL, program="passport", date_modified=None,
        cfg=CFG.model_copy(update={"min_tokens": 5}), count=word_count, dropped=dropped,
    )  # fmt: skip
    assert dropped == {URL: 2}
    assert sum(c.text.count(STEPS) for c in out) == 1


def test_dedup_is_per_page_not_corpus_wide():
    first = chunks(f"# T\n\n## A\n\n{STEPS}")
    second = chunks(f"# T\n\n## A\n\n{STEPS}", url=URL.replace("apply", "other"))
    assert STEPS in first[0].text and STEPS in second[0].text
