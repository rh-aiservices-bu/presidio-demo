"""Presidio, served over HTTP. Nothing more.

This module deliberately contains no masking logic of its own. It makes Presidio calls
and returns what Presidio produced:

    analyze()            -> candidate spans, with confidence scores
    anonymize(keep)      -> the spans that survived Presidio's conflict resolution,
                            as offsets into the submitted text
    anonymize(replace)   -> the masked text, byte-identical to what NeMo's rail emits

Numbering (`<PERSON_1>`), handle formats, surrogate values and the handle -> value
mapping are NOT here. They are application concerns and live in presidio-ui. That
keeps this service a faithful mirror of the NeMo masking service, plus the one thing
NeMo computes and then discards: the spans.

WHY `keep` DECIDES WHICH SPANS SURVIVE

`analyze()` returns overlapping candidates -- a PERSON inside a longer LOCATION, an
EMAIL_ADDRESS inside a URL. Choosing between them is real logic, and Presidio already
has it: anonymizing with the `keep` operator returns the text unchanged along with the
matches it kept. So /analyze reports Presidio's decision rather than making its own,
and the caller does not reimplement overlap handling either.

WHAT PRESIDIO DOES NOT GIVE US

`anonymize()` returns `EngineResult.items`, whose own docstring reads "the indices of
their replacements in the anonymized text": each item says where a replacement landed
and what it is, never what it replaced. There is no original -> replacement mapping in
Presidio, and its docs state it keeps no state. A mapping only falls out of `keep`
because an identity operator leaves the offsets pointing at the original text.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from presidio_analyzer import AnalyzerEngine, PatternRecognizer
from presidio_analyzer.nlp_engine import NlpEngineProvider
from presidio_anonymizer import AnonymizerEngine
from presidio_anonymizer.entities import OperatorConfig

log = logging.getLogger(__name__)

# Presidio pins no weights of its own; this must be installed in the image.
SPACY_MODEL = "en_core_web_lg"


@dataclass(frozen=True)
class Span:
    """One detected entity, as an offset into the text that was submitted."""

    start: int
    end: int
    entity_type: str
    text: str

    def as_dict(self) -> dict:
        return {
            "start": self.start,
            "end": self.end,
            "entity_type": self.entity_type,
            "text": self.text,
        }


class Engine:
    """Analyzer + anonymizer, built once. Both are thread safe for read use."""

    def __init__(self, spacy_model: str = SPACY_MODEL) -> None:
        provider = NlpEngineProvider(
            nlp_configuration={
                "nlp_engine_name": "spacy",
                "models": [{"lang_code": "en", "model_name": spacy_model}],
            }
        )
        self._analyzer = AnalyzerEngine(nlp_engine=provider.create_engine())
        self._anonymizer = AnonymizerEngine()

    def _analyze(
        self, text: str, entities: list[str], recognizers: list[dict], score_threshold: float
    ) -> list:
        return self._analyzer.analyze(
            text=text,
            language="en",
            entities=entities,
            # NeMo pins this at 0.4 and ignores its own configured value; honouring it
            # is the one intentional behavioural difference from the rail.
            score_threshold=score_threshold,
            ad_hoc_recognizers=[PatternRecognizer.from_dict(r) for r in recognizers],
        )

    def spans(
        self,
        text: str,
        entities: list[str],
        recognizers: list[dict] | None = None,
        score_threshold: float = 0.4,
    ) -> list[Span]:
        """Presidio's surviving matches, in text order."""
        if not text or not entities:
            return []
        results = self._analyze(text, entities, recognizers or [], score_threshold)
        if not results:
            return []
        kept = self._anonymizer.anonymize(
            text=text,
            analyzer_results=results,
            operators={entity: OperatorConfig("keep") for entity in set(entities)},
        )
        return [
            Span(start=i.start, end=i.end, entity_type=i.entity_type, text=text[i.start : i.end])
            for i in sorted(kept.items, key=lambda item: item.start)
        ]

    def mask(
        self,
        text: str,
        entities: list[str],
        recognizers: list[dict] | None = None,
        score_threshold: float = 0.4,
    ) -> str:
        """The masked text NeMo's rail produces: `<ENTITY_TYPE>` for every match.

        `OperatorConfig("replace")` with no parameters is exactly what
        `mask_sensitive_data` builds, so this output matches the rail's.
        """
        if not text or not entities:
            return text
        results = self._analyze(text, entities, recognizers or [], score_threshold)
        if not results:
            return text
        return self._anonymizer.anonymize(
            text=text,
            analyzer_results=results,
            operators={entity: OperatorConfig("replace") for entity in set(entities)},
        ).text
