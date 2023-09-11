"""

    Greynir: Natural language processing for Icelandic

    Wrapper functions module

    Copyright (C) 2022 Miðeind ehf.

    This software is licensed under the MIT License:

        Permission is hereby granted, free of charge, to any person
        obtaining a copy of this software and associated documentation
        files (the "Software"), to deal in the Software without restriction,
        including without limitation the rights to use, copy, modify, merge,
        publish, distribute, sublicense, and/or sell copies of the Software,
        and to permit persons to whom the Software is furnished to do so,
        subject to the following conditions:

        The above copyright notice and this permission notice shall be
        included in all copies or substantial portions of the Software.

        THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
        EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
        MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT.
        IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY
        CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT,
        TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE
        SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.


    This module exposes functions to return corrected strings given an input text.
    The following options are defined:

    input:  Defines the input. Can be a string or an iterable of strings, such
            as a file object.
    format: Defines the output format. String.
            text: Output is returned as a corrected version of the input.
            json: Output is returned as a JSON string.
            csv:  Output is returned in a csv format.
            m2:   Output is returned in the M2 format, see https://github.com/nusnlp/m2scorer
                  The output is as follows:
                  S <tokenized system output for sentence 1>
                  A <token start offset> <token end offset>|||<error type>|||<correction1>||<correction2||..||correctionN|||<required>|||<comment>|||<annotator id>
    all_errors: Defines the level of correction. If False, only token-level annotation is carried out.
                If True, sentence-level annotation is carried out.
    annotate_unparsed_sentences: If True, sentences that cannot be parsed are annotated as errors as a whole.
    annotations: If True, can all error annotations are returned at the end of the output. Works with format text.
    generate_suggestion_list: If True, the annotation can in certain cases contain a list of possible corrections, for the user to pick from.
    suppress_suggestions: If True, more farfetched automatically retrieved corrections are rejected and no error is added.
    ignore_wordlist: The value is a set of strings, a whitelist. Each string is a word that should not be marked as an error or corrected.
    one_sent: Defines input as containing only one sentence.
    ignore_rules: A list of error codes that should be ignored in the annotation process.
"""


from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Set, Tuple, Union, cast
from typing_extensions import TypedDict

import argparse
import json
import logging
from dataclasses import dataclass
from functools import partial

from reynir import Terminal
from tokenizer import TOK, detokenize, normalized_text_from_tokens, text_from_tokens
from tokenizer.definitions import AmountTuple, NumberTuple

from reynir_correct.readability import Flesch, RareWords

from .annotation import Annotation
from .checker import CheckResult, GreynirCorrect, load_config
from .classifier import SentenceClassifier
from .errtokenizer import CorrectionPipeline, CorrectToken, Error

log = logging.getLogger(__name__)


class AnnTokenDict(TypedDict, total=False):

    """Type of the token dictionaries returned from check_errors()"""

    # Token kind
    k: int
    # Token text
    x: str
    # Original text of token
    o: str
    # Character offset of token, indexed from the start of the checked text
    i: int


class AnnDict(TypedDict):

    """A single annotation, as returned by the Yfirlestur.is API"""

    start: int
    end: int
    start_char: int
    end_char: int
    code: str
    text: str
    detail: Optional[str]
    suggest: Optional[str]


class AnnResultDict(TypedDict):

    """The annotation result for a sentence"""

    original: str
    corrected: str
    annotations: List[AnnDict]
    tokens: List[AnnTokenDict]


# File types for UTF-8 encoded text files
ReadFile = argparse.FileType("r", encoding="utf-8")
WriteFile = argparse.FileType("w", encoding="utf-8")

# Configure our JSON dump function
json_dumps = partial(json.dumps, ensure_ascii=False, separators=(",", ":"))


def gen(f: Iterator[str]) -> Iterable[str]:
    """Generate the lines of text in the input file"""
    yield from f


def quote(s: str) -> str:
    """Return the string s within double quotes, and with any contained
    backslashes and double quotes escaped with a backslash"""
    if not s:
        return '""'
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def val(t: CorrectToken, quote_word: bool = False) -> Union[None, str, float, Tuple[Any, ...], Sequence[Any]]:
    """Return the value part of the token t"""
    if t.val is None:
        return None
    if t.kind in {TOK.WORD, TOK.PERSON, TOK.ENTITY}:
        # No need to return list of meanings
        return None
    if t.kind in {TOK.PERCENT, TOK.NUMBER, TOK.CURRENCY}:
        return cast(NumberTuple, t.val)[0]
    if t.kind == TOK.AMOUNT:
        num, iso, _, _ = cast(AmountTuple, t.val)
        if quote_word:
            # Format as "1234.56|USD"
            return '"{0}|{1}"'.format(num, iso)
        return num, iso
    if t.kind == TOK.S_BEGIN:
        return None
    if t.kind == TOK.PUNCTUATION:
        punct = t.punctuation
        return quote(punct) if quote_word else punct
    if quote_word and t.kind in {
        TOK.DATE,
        TOK.TIME,
        TOK.DATEABS,
        TOK.DATEREL,
        TOK.TIMESTAMP,
        TOK.TIMESTAMPABS,
        TOK.TIMESTAMPREL,
        TOK.TELNO,
        TOK.NUMWLETTER,
        TOK.MEASUREMENT,
    }:
        # Return a |-delimited list of numbers
        return quote("|".join(str(v) for v in cast(Iterable[Any], t.val)))
    if quote_word and isinstance(t.val, str):
        return quote(t.val)
    return t.val


@dataclass
class CorrectedSentence:
    tokens: List[CorrectToken]
    annotations: Optional[List[Annotation]] = None
    terminals: Optional[List[Terminal]] = None

    def filter_annotations(self, ignore_rules: Set[str]):
        """Remove ignored annotations"""
        if self.annotations is None:
            return
        ann = [a for a in self.annotations if a.code not in ignore_rules]
        self.annotations = ann


@dataclass
class ParseResultStats:
    num_sentences: int
    num_parsed: int
    num_tokens: int
    ambiguity: float
    parse_time: float


@dataclass
class CorrectionResult:
    sentences: List[CorrectedSentence]
    parse_result_stats: Optional[ParseResultStats] = None
    flesch_result: Optional[Tuple[float, str]] = None
    rare_words: Optional[List[Tuple[str, float]]] = None

    def filter_annotations(self, ignore_rules: Set[str]):
        """Remove ignored annotations"""
        for sent in self.sentences:
            sent.filter_annotations(ignore_rules)


class GreynirCorrectAPI:
    """A high level api for correcting Icelandic texts"""

    def __init__(
        self,
        gc: GreynirCorrect,
        sentence_prefilter: Optional[SentenceClassifier] = None,
        do_flesch: bool = False,
        rare_word_analyser: Optional[RareWords] = None,
        do_grammar_check: bool = True,
    ):
        self.gc = gc
        self.do_grammar_check = do_grammar_check
        self.sentence_prefilter = sentence_prefilter
        self.do_flesch = do_flesch
        # If it's defined, it will be used
        self.rare_word_analyser = rare_word_analyser

    @staticmethod
    def from_options(**options) -> "GreynirCorrectAPI":
        """Create a GreynirCorrectAPI from the given options"""
        settings = load_config(options.pop("tov_config", None))
        do_flesch_analysis = bool(options.pop("flesch", False))
        do_rare_word_analysis = bool(options.pop("rare_words", False))
        pipeline = CorrectionPipeline(
            "",
            settings,
            **options,
        )
        gc = GreynirCorrect(
            settings=settings,
            pipeline=pipeline,
            **options,
        )
        rare_word_analyser = RareWords() if do_rare_word_analysis else None
        do_grammar_check = options.get("all_errors", True)
        if options.get("sentence_prefilter", False):
            # Only construct the classifier model if we need it
            from .classifier import SentenceClassifier

            sentence_classifier = SentenceClassifier()
            return GreynirCorrectAPI(
                gc,
                sentence_prefilter=sentence_classifier,
                do_flesch=do_flesch_analysis,
                rare_word_analyser=rare_word_analyser,
                do_grammar_check=do_grammar_check,
            )

        return GreynirCorrectAPI(
            gc,
            sentence_prefilter=None,
            do_flesch=do_flesch_analysis,
            rare_word_analyser=rare_word_analyser,
            do_grammar_check=do_grammar_check,
        )

    def _correct_spelling(
        self, text: Iterable[str], ignore_rules: Optional[Set] = None, suppress_suggestions: bool = False
    ) -> Iterable[CorrectToken]:
        """Correct the token-level errors in the text"""
        # TODO: The pipeline needs a refactoring.
        # We use some hacks here to avoid having to rewrite the pipeline at this point.
        self.gc.pipeline._text_or_gen = text
        self.gc.pipeline._ignore_rules = ignore_rules or set()
        self.gc.pipeline._suppress_suggestions = suppress_suggestions
        return self.gc.pipeline.tokenize()  # type: ignore

    def _sentence_contains_error(self, token_corrected_tokens: Iterable[CorrectToken]) -> bool:
        """Classify a sentence as probably correct or not."""
        if self.sentence_prefilter is None:
            raise ValueError("Sentence classifier not initialized - did you forget to set sentence_prefilter=True?")
        original_sentence = "".join([t.original or t.txt for t in token_corrected_tokens])

        return self.sentence_prefilter.classify(original_sentence)

    def _correct_grammar(self, token_corrected_tokens: Iterable[CorrectToken]) -> CheckResult:
        results = self.gc.parse_all_tokens(token_corrected_tokens)
        return results

    def correct(
        self, text: Iterable[str], ignore_rules: Optional[Set] = None, suppress_suggestions: bool = False
    ) -> CorrectionResult:
        """Correct the input text by first correcting spelling and then grammatical errors."""
        corrected_tokens = self._correct_spelling(
            text, ignore_rules=ignore_rules, suppress_suggestions=suppress_suggestions
        )
        # Convert the tokens to a list, so it can be reused - this must be done at some point anyway
        corrected_tokens = list(corrected_tokens)
        flesch_result = None
        if self.do_flesch:
            flesch_score = Flesch.get_score_from_stream(corrected_tokens)
            flesch_feedback = Flesch.get_feedback(flesch_score)
            flesch_result = (flesch_score, flesch_feedback)
        rare_words = None
        if self.rare_word_analyser is not None:
            # TODO: support setting max_words and low_prob_cutoff on function call
            rare_words = self.rare_word_analyser.get_rare_words_from_stream(
                [tok for tok in corrected_tokens if tok.kind == TOK.WORD], max_words=10, low_prob_cutoff=0.00000005
            )
        if not self.do_grammar_check:
            # Only run the spelling correction
            return CorrectionResult(
                sentences=[CorrectedSentence(corrected_tokens)], flesch_result=flesch_result, rare_words=rare_words
            )
        # Only run the sentence classifier if we should
        if self.sentence_prefilter is not None:
            # Check if the sentence contains an error
            # TODO: We will want to chunk the input into sentences and run the classifier on each sentence
            if not self._sentence_contains_error(corrected_tokens):
                # The sentence is probably correct, so we skip the rest of the processing
                return CorrectionResult(
                    sentences=[CorrectedSentence(corrected_tokens)], flesch_result=flesch_result, rare_words=rare_words
                )
            # The sentence is probably incorrect, so we continue with the full grammar check
        # Run the full grammar check
        check_result = self._correct_grammar(corrected_tokens)
        corrected_sentences = [
            CorrectedSentence(sentence.tokens, sentence.annotations, sentence.terminals)  # type: ignore
            for sentence in check_result["sentences"]
        ]
        result = CorrectionResult(
            sentences=corrected_sentences,
            flesch_result=flesch_result,
            rare_words=rare_words,
        )
        # Filter annotations based on ignore rules
        result.filter_annotations(ignore_rules or set())
        return result


def check_errors(**options: Any) -> str:
    """Return a string in the chosen format and correction level
    using the spelling and grammar checker"""
    all_errors = options.pop("all_errors", True)
    text = options.pop("input", None)
    format = options.pop("format", "json")
    spaced = options.pop("spaced", False)
    normalize = options.pop("normalize", False)
    annotations = options.pop("annotations", False)
    print_all = options.pop("print_all", False)
    ignore_rules = options.pop("ignore_rules", set())
    suppress_suggestions = options.pop("suppress_suggestions", False)
    if text is None:
        raise ValueError("No input text")
    api = GreynirCorrectAPI.from_options(**options)
    if isinstance(text, str):
        text = [text]
    results = api.correct(text, ignore_rules=ignore_rules, suppress_suggestions=suppress_suggestions)
    text_results = ""
    if all_errors:
        text_results = format_grammar(
            results=results, format=format, print_all=print_all, print_annotations=annotations
        )
    else:
        text_results = format_spelling(
            results=results,
            format=format,
            spaced=spaced,
            normalize=normalize,
            print_annotations=annotations,
            print_all=print_all,
        )
    # Add the Flesch score and rare words at the end of the output
    if results.flesch_result is not None:
        flesch_score, flesch_feedback = results.flesch_result
        text_results += f"\nFlesch score: {flesch_score:.2f} ({flesch_feedback})"
    if results.rare_words is not None:
        text_results += "\nRare words:\n"
        for word, prob in results.rare_words:
            text_results += f"\t{word}: {prob:.8f}\n"
    return text_results


def format_spelling(
    results: CorrectionResult,
    format: str = "json",
    spaced: bool = False,
    normalize: bool = False,
    print_annotations: bool = False,
    print_all: bool = False,
) -> str:
    # Initialize sentence accumulator list
    # Function to convert a token list to output text
    if spaced:
        if normalize:
            to_text = normalized_text_from_tokens
        else:
            to_text = text_from_tokens
    else:
        to_text = partial(detokenize, normalize=True)
    unisum: List[str] = []
    allsum: List[str] = []
    annlist: List[str] = []
    for sent in results.sentences:
        sent_tokens = sent.tokens
        if format == "text":
            txt = to_text(sent_tokens)
            if print_annotations:
                for t in sent_tokens:
                    if t.error:
                        annlist.append(str(t.error))
                if annlist and not print_all:
                    txt = txt + "\n" + "\n".join(annlist)
                    annlist = []
            unisum.append(txt)
            continue
        for t in sent_tokens:
            if format == "csv":
                if t.txt:
                    allsum.append(
                        "{0},{1},{2},{3}".format(
                            t.kind,
                            quote(t.txt),
                            val(t, quote_word=True) or '""',
                            quote(str(t.error) if t.error else ""),
                        )
                    )
                elif t.kind == TOK.S_END:
                    # Indicate end of sentence
                    allsum.append('0,"",""')
            elif format == "json":
                # Output the tokens in JSON format, one line per token
                d: Dict[str, Any] = dict(k=TOK.descr[t.kind])
                if t.txt is not None:
                    d["t"] = t.txt
                v = val(t)
                if t.kind not in {TOK.WORD, TOK.PERSON, TOK.ENTITY} and v is not None:
                    d["v"] = v
                if isinstance(t.error, Error):
                    d["e"] = t.error.to_dict()
                allsum.append(json_dumps(d))
        if allsum:
            unisum.extend(allsum)
            allsum = []
    if print_all:
        # We want the annotations at the bottom
        unistr = " ".join(unisum)
        if annlist:
            unistr = unistr + "\n" + "\n".join(annlist)
    else:
        unistr = "\n".join(unisum)
    return unistr


def format_grammar(
    results: CorrectionResult,
    print_annotations: bool = False,
    print_all: bool = False,
    format: str = "json",
) -> str:
    """Do a full spelling and grammar check of the source text"""
    sentence_results: List[Dict[str, Any]] = []

    for sentence in results.sentences:
        annotations = sentence.annotations
        if annotations is None:
            # This should not happen
            raise Exception("Annotations not set in sentence which was supposedly parsed")
        # Sort in ascending order by token start index, and then by end index
        # (more narrow/specific annotations before broader ones)
        annotations.sort(key=lambda ann: (ann.start, ann.end))

        # Generate a sentence with only spelling corrections applied
        partially_corrected_sentence = detokenize(sentence.tokens)

        # Generate a sentence with all corrections applied
        full_correction_toks = sentence.tokens[:]
        for ann in annotations[::-1]:
            if ann.suggest is None:
                # Nothing to correct with, nothing we can do
                continue
            full_correction_toks[ann.start].txt = ann.suggest
            if ann.end > ann.start:
                # Annotation spans many tokens
                # "Okkur börnunum langar í fisk"
                # "Leita að kílómeter af féinu" → leita að kílómetri af fénu → leita að kílómetra af fénu
                # "dást af þeim" → "dást að þeim"
                # Single-token annotations for this span have already been handled
                # Only case is one ann, many toks in toklist
                # Give the first token the correct value
                # Delete the other tokens
                del full_correction_toks[ann.start + 1 : ann.end + 1]
        fully_corrected_sentence = detokenize(full_correction_toks)

        # Make a nice token list
        tokens: List[AnnTokenDict]
        if sentence.terminals is None:
            # Not parsed: use the raw token list
            tokens = [AnnTokenDict(k=d.kind, x=d.txt, o=d.original or d.txt) for d in sentence.tokens]
        else:
            # Successfully parsed: use the text from the terminals (where available)
            # since we have more info there, for instance on em/en dashes.
            # Create a map of token indices to corresponding terminal text
            token_map = {t.index: t.text for t in sentence.terminals}
            tokens = [
                AnnTokenDict(k=d.kind, x=token_map.get(ix, d.txt), o=d.original or d.txt)
                for ix, d in enumerate(sentence.tokens)
            ]

        sentence_results.append(
            {
                "original": "".join(tok.original for tok in sentence.tokens if tok.original is not None),
                "partially_corrected": partially_corrected_sentence,
                "corrected": fully_corrected_sentence,
                "annotations": annotations,
                "tokens": tokens,
            }
        )

    return format_output(sentence_results, format, print_all=print_all, print_annotations=print_annotations)


def format_output(
    sentence_results: List[Dict[str, Any]],
    format_type: str,
    print_annotations: bool = False,
    print_all: bool = False,
) -> str:
    """
    Format grammar analysis results in the given format.

    `sentence_results` is a list of individual sentences and their analysis
    `format_type` is the output format to use, one of 'text', 'json', 'csv', 'm2'
    `extra_text_options` takes extra options for the text format. Ignored for other formats.
    """

    if format_type == "text":
        return format_text(sentence_results, print_all=print_all, print_annotations=print_annotations)
    elif format_type == "json":
        return format_json(sentence_results)
    elif format_type == "csv":
        return format_csv(sentence_results)
    elif format_type == "m2":
        return format_m2(sentence_results)

    raise Exception(f"Tried to format with invalid format: {format_type}")


def format_text(
    sentence_results: List[Dict[str, Any]], print_annotations: bool = False, print_all: bool = False
) -> str:
    output: List[str] = []
    for result in sentence_results:
        txt = result["corrected"]

        if print_annotations and not print_all:
            txt = txt + "\n" + "\n".join([str(ann) for ann in result["annotations"]])
        output.append(txt)

    return "\n".join(output)


def format_json(sentence_results: List[Dict[str, Any]]) -> str:
    formatted_sentences: List[str] = []

    offset = 0
    for result in sentence_results:
        tokens = result["tokens"]

        token_offsets: List[int] = []
        for t in tokens:
            token_offsets.append(offset)
            offset += len(t["o"] or t["x"] or "")
        # Add a past-the-end offset to make later calculations more convenient
        token_offsets.append(offset)

        # Convert the annotations to a standard format before encoding in JSON
        formatted_annotations: List[AnnDict] = [
            AnnDict(
                # Start token index of this annotation
                start=ann.start,
                # End token index (inclusive)
                end=ann.end,
                # Character offset of the start of the annotation in the original text
                start_char=token_offsets[ann.start],
                # Character offset of the end of the annotation in the original text
                # (inclusive, i.e. the offset of the last character)
                end_char=token_offsets[ann.end + 1] - 1,
                code=ann.code,
                text=ann.text,
                detail=ann.detail or "",
                suggest=ann.suggest or "",
            )
            for ann in result["annotations"]
        ]
        ard = AnnResultDict(
            original=result["original"],
            corrected=result["corrected"],
            tokens=tokens,
            annotations=formatted_annotations,
        )

        formatted_sentences.append(json.dumps(ard))

    return "\n".join(formatted_sentences)


def format_csv(sentence_results: List[Dict[str, Any]]) -> str:
    accumul: List[str] = []
    for result in sentence_results:
        for ann in result["annotations"]:
            accumul.append(
                "{},{},{},{},{},{}".format(
                    ann.code,
                    ann.original,
                    ann.suggest,
                    ann.start,
                    ann.end,
                    ann.suggestlist,
                )
            )
    return "\n".join(accumul)


def format_m2(sentence_results: List[Dict[str, Any]]) -> str:
    accumul: List[str] = []
    for result in sentence_results:
        accumul.append("S {0}".format(" ".join([t["x"] for t in result["tokens"]])))
        for ann in result["annotations"]:
            accumul.append(
                "A {0} {1}|||{2}|||{3}|||REQUIRED|||-NONE-|||0".format(ann.start, ann.end, ann.code, ann.suggest)
            )
        accumul.append("")
    return "\n".join(accumul)
