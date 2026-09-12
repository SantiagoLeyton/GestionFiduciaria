import difflib
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Iterable


MATCH_EXACT = "exact"
MATCH_ALIAS = "alias"
MATCH_SIMILAR = "similar"
MATCH_NOT_FOUND = "not_found"
MATCH_AMBIGUOUS = "ambiguous"

SIMILARITY_THRESHOLD = 0.92
SIMILARITY_MARGIN = 0.04


@dataclass(frozen=True)
class HeaderCandidate:
    header: str
    normalized_header: str
    column_index: int
    column_letter: str
    score: float = 1.0


@dataclass(frozen=True)
class HeaderResolution:
    expected_key: str
    expected_header: str
    normalized_expected: str
    actual_header: str = ""
    normalized_actual: str = ""
    match_type: str = MATCH_NOT_FOUND
    sheet_name: str = ""
    column_index: int | None = None
    column_letter: str = ""
    candidates: tuple[HeaderCandidate, ...] = field(default_factory=tuple)

    @property
    def found(self) -> bool:
        return self.match_type in {MATCH_EXACT, MATCH_ALIAS, MATCH_SIMILAR}

    @property
    def ambiguous(self) -> bool:
        return self.match_type == MATCH_AMBIGUOUS


def normalize_header(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.casefold()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


class HeaderResolver:
    def __init__(
        self,
        *,
        expected_headers: dict[str, str],
        aliases: dict[str, Iterable[str]] | None = None,
        threshold: float = SIMILARITY_THRESHOLD,
        ambiguity_margin: float = SIMILARITY_MARGIN,
    ):
        self.expected_headers = expected_headers
        self.aliases = {key: {normalize_header(alias) for alias in values} for key, values in (aliases or {}).items()}
        self.threshold = threshold
        self.ambiguity_margin = ambiguity_margin

    def resolve(self, expected_key: str, headers: Iterable[HeaderCandidate], *, sheet_name: str = "") -> HeaderResolution:
        matches = self.resolve_all(expected_key, headers, sheet_name=sheet_name)
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            return self._ambiguous(expected_key, sheet_name, matches)
        return self._not_found(expected_key, sheet_name)

    def resolve_all(self, expected_key: str, headers: Iterable[HeaderCandidate], *, sheet_name: str = "") -> list[HeaderResolution]:
        headers = list(headers)
        expected_header = self.expected_headers[expected_key]
        normalized_expected = normalize_header(expected_header)

        exact = [candidate for candidate in headers if candidate.normalized_header == normalized_expected]
        if exact:
            return [self._resolved(expected_key, expected_header, normalized_expected, candidate, MATCH_EXACT, sheet_name) for candidate in exact]

        alias_values = self.aliases.get(expected_key, set())
        alias = [candidate for candidate in headers if candidate.normalized_header in alias_values]
        if alias:
            return [self._resolved(expected_key, expected_header, normalized_expected, candidate, MATCH_ALIAS, sheet_name) for candidate in alias]

        similar = self._similar_candidates(normalized_expected, headers)
        if not similar:
            return []
        if len(similar) > 1 and similar[0].score - similar[1].score <= self.ambiguity_margin:
            return [self._ambiguous(expected_key, sheet_name, similar)]
        return [self._resolved(expected_key, expected_header, normalized_expected, similar[0], MATCH_SIMILAR, sheet_name)]

    def resolve_repeated(self, expected_key: str, headers: Iterable[HeaderCandidate], *, sheet_name: str = "") -> list[HeaderResolution]:
        headers = list(headers)
        expected_header = self.expected_headers[expected_key]
        normalized_expected = normalize_header(expected_header)
        alias_values = self.aliases.get(expected_key, set())

        exact_or_alias = [
            self._resolved(
                expected_key,
                expected_header,
                normalized_expected,
                candidate,
                MATCH_EXACT if candidate.normalized_header == normalized_expected else MATCH_ALIAS,
                sheet_name,
            )
            for candidate in headers
            if candidate.normalized_header == normalized_expected or candidate.normalized_header in alias_values
        ]
        resolved_indexes = {resolution.column_index for resolution in exact_or_alias}
        similar = [
            self._resolved(expected_key, expected_header, normalized_expected, candidate, MATCH_SIMILAR, sheet_name)
            for candidate in self._similar_candidates(normalized_expected, headers)
            if candidate.column_index not in resolved_indexes
        ]
        if len(similar) > 1 and similar[0].candidates[0].score - similar[1].candidates[0].score <= self.ambiguity_margin:
            return [self._ambiguous(expected_key, sheet_name, [item.candidates[0] for item in similar])]
        if exact_or_alias:
            return sorted(exact_or_alias + similar, key=lambda item: item.column_index or 0)
        return self.resolve_all(expected_key, headers, sheet_name=sheet_name)

    def _similar_candidates(self, normalized_expected: str, headers: list[HeaderCandidate]) -> list[HeaderCandidate]:
        candidates = []
        for candidate in headers:
            if _is_dangerous_subset(normalized_expected, candidate.normalized_header):
                continue
            score = max(
                difflib.SequenceMatcher(None, normalized_expected, candidate.normalized_header).ratio(),
                _token_order_independent_score(normalized_expected, candidate.normalized_header),
            )
            if score >= self.threshold:
                candidates.append(
                    HeaderCandidate(
                        header=candidate.header,
                        normalized_header=candidate.normalized_header,
                        column_index=candidate.column_index,
                        column_letter=candidate.column_letter,
                        score=score,
                    )
                )
        return sorted(candidates, key=lambda item: item.score, reverse=True)

    def _resolved(self, expected_key, expected_header, normalized_expected, candidate, match_type, sheet_name):
        return HeaderResolution(
            expected_key=expected_key,
            expected_header=expected_header,
            normalized_expected=normalized_expected,
            actual_header=candidate.header,
            normalized_actual=candidate.normalized_header,
            match_type=match_type,
            sheet_name=sheet_name,
            column_index=candidate.column_index,
            column_letter=candidate.column_letter,
            candidates=(candidate,),
        )

    def _ambiguous(self, expected_key, sheet_name, candidates):
        expected_header = self.expected_headers[expected_key]
        return HeaderResolution(
            expected_key=expected_key,
            expected_header=expected_header,
            normalized_expected=normalize_header(expected_header),
            match_type=MATCH_AMBIGUOUS,
            sheet_name=sheet_name,
            candidates=tuple(candidates),
        )

    def _not_found(self, expected_key, sheet_name):
        expected_header = self.expected_headers[expected_key]
        return HeaderResolution(
            expected_key=expected_key,
            expected_header=expected_header,
            normalized_expected=normalize_header(expected_header),
            match_type=MATCH_NOT_FOUND,
            sheet_name=sheet_name,
        )


def header_candidates_from_sheet(sheet, row_number: int) -> list[HeaderCandidate]:
    candidates = []
    for column in range(1, sheet.used_columns + 1):
        cell = sheet.cell(row_number, column)
        if not cell:
            continue
        header = _clean_header(cell.value)
        normalized = normalize_header(header)
        if not normalized:
            continue
        candidates.append(
            HeaderCandidate(
                header=header,
                normalized_header=normalized,
                column_index=column,
                column_letter=cell.letter,
            )
        )
    return candidates


def _clean_header(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    text = re.sub(r"[\t\r\n]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _is_dangerous_subset(expected: str, actual: str) -> bool:
    expected_tokens = set(expected.split())
    actual_tokens = set(actual.split())
    if not expected_tokens or not actual_tokens or expected_tokens == actual_tokens:
        return False
    return expected_tokens.issubset(actual_tokens) or actual_tokens.issubset(expected_tokens)


def _token_order_independent_score(expected: str, actual: str) -> float:
    expected_tokens = expected.split()
    actual_tokens = actual.split()
    if not expected_tokens or not actual_tokens or set(expected_tokens) != set(actual_tokens):
        return 0.0
    if len(expected_tokens) != len(actual_tokens):
        return 0.0
    return 0.99
