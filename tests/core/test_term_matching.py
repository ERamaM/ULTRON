from ultron.core.rules import term_matching
from ultron.core.rules.term_matching import contains_term, extract_matching_evidence, find_terms, normalize_text


def test_normalization_and_word_boundaries():
    assert normalize_text("ESPAÑA") == "espana"
    assert contains_term("Sr. Engineer in España", "sr.")
    assert contains_term("Remote EU", "remote eu")
    assert not contains_term("facet storage Leuven", "cet")
    assert not contains_term("facet storage Leuven", "rag")
    assert not contains_term("facet storage Leuven", "eu")
    assert not contains_term("text", " ")
    assert find_terms("España, On-Site, remote EU", ["espana", "on-site", "espana", "remote eu", ""]) == [
        "espana",
        "on-site",
        "remote eu",
    ]
    assert find_terms("", ["remote"]) == []


def test_evidence_uses_first_matching_sentence_and_limits_length():
    assert extract_matching_evidence("No match. On-site in España.", ["onsite", "espana"]) == "On-site in España."
    assert extract_matching_evidence("Storage in Leuven.", ["rag", "eu"]) == ""
    assert len(extract_matching_evidence("Remote " + "x" * 400, ["remote"])) == 280


def test_long_sentence_window_contains_the_actual_match():
    for term, source in (("remote", "Remote"), ("espana", "España"), ("cafe", "cafe\u0301")):
        sentence = "x" * 310 + " " + source + " " + "z" * 310
        evidence = extract_matching_evidence(sentence, [term])
        assert len(evidence) <= 280
        assert contains_term(evidence, term)
        assert evidence in sentence
    oversized = "x" * 281
    sentence = oversized + " remote"
    assert contains_term(extract_matching_evidence(sentence, [oversized, "remote"]), "remote")
    sentence = "e" + "\u0301" * 281 + " e"
    assert extract_matching_evidence(sentence, ["e"]).endswith(" e")


def test_find_terms_normalizes_the_input_only_once(monkeypatch):
    text = "Unrelated accented España text " * 1000
    normalize = term_matching.normalize_text
    calls = []

    def record(value):
        calls.append(value)
        return normalize(value)

    monkeypatch.setattr(term_matching, "normalize_text", record)
    assert find_terms(text, ["espana", "missing", "espana"]) == ["espana"]
    assert calls.count(text) == 1
