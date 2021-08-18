"""

    Greynir: Natural language processing for Icelandic

    Spelling and grammar checking module

    Copyright (C) 2021 Miðeind ehf.

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


    This module exposes functions to check spelling and grammar for
    text strings.

    It defines subclasses of the classes BIN_Token and Fast_Parser,
    both found in the Greynir package. These classes add error detection
    functionality to their base classes. After parsing a sentence, the
    ErrorFinder and PatternMatcher classes are used to identify grammar
    errors and questionable patterns.

    Error codes generated by this module:
    -------------------------------------

    E001: The sentence could not be parsed
    E002: A nonterminal tagged with 'error' is present in the parse tree
    E003: An impersonal verb occurs with an incorrect subject case
    E004: The sentence is probably not in Icelandic

"""

from typing import Any, cast, Iterable, Iterator, List, Tuple, Dict, Type, Optional
from typing_extensions import TypedDict

from threading import Lock

from reynir import (
    Greynir,
    correct_spaces,
    TOK,
    Tok,
    TokenList,
    Sentence,
    Paragraph,
    ProgressFunc,
    ICELANDIC_RATIO,
)
from reynir.reynir import Job
from reynir.bintokenizer import StringIterable
from reynir.binparser import BIN_Grammar, BIN_Parser, VariantHandler
from reynir.fastparser import (
    Fast_Parser,
    ffi,  # type: ignore
)
from reynir.reducer import Reducer

from .annotation import Annotation
from .errtokenizer import CorrectToken, tokenize as tokenize_and_correct
from .errfinder import ErrorFinder, ErrorDetectionToken
from .pattern import PatternMatcher


# Checking/correction result
class CheckResult(TypedDict):
    paragraphs: List[List["Sentence"]]
    num_sentences: int
    num_parsed: int
    num_tokens: int
    ambiguity: float
    parse_time: float


class ErrorDetectingGrammar(BIN_Grammar):

    """ A subclass of BIN_Grammar that causes conditional sections in the
        Greynir.grammar file, demarcated using
        $if(include_errors)...$endif(include_errors),
        to be included in the grammar as it is read and parsed """

    def __init__(self) -> None:
        super().__init__()
        # Enable the 'include_errors' condition
        self.set_conditions({"include_errors"})


class AnnotatedSentence(Sentence):

    """ A subclass that adds a list of Annotation instances to a Sentence object """

    def __init__(self, job: Job, s: TokenList) -> None:
        super().__init__(job, s)
        self.annotations: List[Annotation] = []


class ErrorDetectingParser(Fast_Parser):

    """ A subclass of Fast_Parser that modifies its behavior to
        include grammar error detection rules in the parsing process """

    _GRAMMAR_BINARY_FILE = Fast_Parser._GRAMMAR_FILE + ".error.bin"

    # Keep a separate grammar class instance and time stamp for
    # ErrorDetectingParser. This Python sleight-of-hand overrides
    # class attributes that are defined in BIN_Parser, see binparser.py.
    _grammar_ts: Optional[float] = None
    _grammar: Optional[BIN_Grammar] = None
    _grammar_class = ErrorDetectingGrammar

    # Also keep separate class instances of the C grammar and its timestamp
    _c_grammar: Any = cast(Any, ffi).NULL
    _c_grammar_ts: Optional[float] = None

    @staticmethod
    def wrap_token(t: Tok, ix: int) -> ErrorDetectionToken:
        """ Create an instance of a wrapped token """
        return ErrorDetectionToken(t, ix)


class GreynirCorrect(Greynir):

    """ Parser augmented with the ability to add spelling and grammar
        annotations to the returned sentences """

    # GreynirCorrect has its own class instances of a parser and a reducer,
    # separate from the Greynir class, as they use different settings and
    # parsing enviroments
    _parser: Optional[ErrorDetectingParser] = None
    _reducer = None
    _lock = Lock()

    def __init__(self) -> None:
        super().__init__()

    def tokenize(self, text: StringIterable) -> Iterator[Tok]:
        """ Use the correcting tokenizer instead of the normal one """
        # The CorrectToken class is a duck-typing implementation of Tok
        return tokenize_and_correct(text)

    @classmethod
    def _dump_token(cls, tok: Tok) -> Tuple[Any, ...]:
        """ Override token dumping function from Greynir,
            providing a JSON-dumpable object """
        assert isinstance(tok, CorrectToken)
        return CorrectToken.dump(tok)

    @classmethod
    def _load_token(cls, *args: Any) -> CorrectToken:
        """ Load token from serialized data """
        largs = len(args)
        if largs == 3:
            # Plain ol' token
            return cast(CorrectToken, super()._load_token(*args))
        # This is a CorrectToken: pass it to that class for handling
        return CorrectToken.load(*args)

    @property
    def parser(self) -> Fast_Parser:
        """ Override the parent class' construction of a parser instance """
        with self._lock:
            if (
                GreynirCorrect._parser is None
                or GreynirCorrect._parser.is_grammar_modified()[0]
            ):
                # Initialize a singleton instance of the parser and the reducer.
                # Both classes are re-entrant and thread safe.
                GreynirCorrect._parser = edp = ErrorDetectingParser()
                GreynirCorrect._reducer = Reducer(edp.grammar)
            return GreynirCorrect._parser

    @property
    def reducer(self) -> Reducer:
        """ Return the reducer instance to be used """
        # Should always retrieve the parser attribute first
        assert GreynirCorrect._reducer is not None
        return GreynirCorrect._reducer

    def annotate(self, sent: Sentence) -> List[Annotation]:
        """ Returns a list of annotations for a sentence object, containing
            spelling and grammar annotations of that sentence """
        ann: List[Annotation] = []
        words_in_bin = 0
        words_not_in_bin = 0
        parsed = sent.deep_tree is not None
        # Create a mapping from token indices to terminal indices.
        # This is necessary because not all tokens are included in
        # the token list that is passed to the parser, and therefore
        # the terminal-token matches can be fewer than the original tokens.
        token_to_terminal: Dict[int, int] = {}
        if parsed:
            token_to_terminal = {
                tnode.index: ix
                for ix, tnode in enumerate(sent.terminal_nodes)
                if tnode.index is not None
            }
        grammar = self.parser.grammar
        # First, add token-level annotations
        for ix, t in enumerate(sent.tokens):
            if t.kind == TOK.WORD:
                if t.val:
                    # The word has at least one meaning
                    words_in_bin += 1
                else:
                    # The word has no recognized meaning
                    words_not_in_bin += 1
            elif t.kind == TOK.PERSON:
                # Person names count as recognized words
                words_in_bin += 1
            elif t.kind == TOK.ENTITY:
                # Entity names do not count as recognized words;
                # we count each enclosed word in the entity name
                words_not_in_bin += t.txt.count(" ") + 1
            # Note: these tokens and indices are the original tokens from
            # the submitted text, including ones that are not understood
            # by the parser, such as quotation marks and exotic punctuation
            if getattr(t, "error_code", None):
                # This is a CorrectToken instance (or a duck typing equivalent)
                assert isinstance(t, CorrectToken)  # Satisfy Mypy
                annotate = True
                if parsed and ix in token_to_terminal:
                    # For the call to suggestion_does_not_match(), we need a
                    # BIN_Token instance, which we can obtain in a bit of a hacky
                    # way by creating it on the fly
                    bin_token = BIN_Parser.wrap_token(t, ix)
                    # Obtain the original BIN_Terminal instance from the grammar
                    terminal_index = token_to_terminal[ix]
                    terminal_node = sent.terminal_nodes[terminal_index]
                    original_terminal = terminal_node.original_terminal
                    assert original_terminal is not None
                    terminal = grammar.terminals[original_terminal]
                    assert isinstance(terminal, VariantHandler)
                    try:
                        if t.suggestion_does_not_match(terminal, bin_token):
                            # If this token is annotated with a spelling suggestion,
                            # do not add it unless it works grammatically
                            annotate = False
                    except AttributeError:
                        # 1,8 milljarður kóna - wrap_tokens seems to fail
                        print("Meaning type doesn't match BinToken: {}, type {}".format(bin_token, type(bin_token.t2[0])))
                if annotate:
                    a = Annotation(
                        start=ix,
                        end=ix + t.error_span - 1,
                        code=t.error_code,
                        text=t.error_description,
                        detail=t.error_detail,
                        original=t.error_original,
                        suggest=t.error_suggest,
                    )
                    ann.append(a)
        # Then, look at the whole sentence
        num_words = words_in_bin + words_not_in_bin
        if num_words > 2 and words_in_bin / num_words < ICELANDIC_RATIO:
            # The sentence contains less than 50% Icelandic
            # words: assume it's in a foreign language and discard the
            # token level annotations
            ann = [
                # E004: The sentence is probably not in Icelandic
                Annotation(
                    start=0,
                    end=len(sent.tokens) - 1,
                    code="E004",
                    text="Málsgreinin er sennilega ekki á íslensku",
                    detail="{0:.0f}% orða í henni finnast ekki í íslenskri orðabók".format(
                        words_not_in_bin / num_words * 100.0
                    ),
                )
            ]
        elif not parsed:
            # If the sentence couldn't be parsed,
            # put an annotation on it as a whole.
            # In this case, we keep the token-level annotations.
            err_index = sent.err_index or 0
            start = max(0, err_index - 1)
            end = min(len(sent.tokens), err_index + 2)
            toktext = correct_spaces(
                " ".join(t.txt for t in sent.tokens[start:end] if t.txt)
            )
            ann.append(
                # E001: Unable to parse sentence
                Annotation(
                    start=0,
                    end=len(sent.tokens) - 1,
                    code="E001",
                    text="Málsgreinin fellur ekki að reglum",
                    detail="Þáttun brást í kring um {0}. tóka ('{1}')".format(
                        err_index + 1, toktext
                    ),
                )
            )
        else:
            # Successfully parsed:
            # Add annotations for error-marked nonterminals from the grammar
            # found in the parse tree
            ErrorFinder(ann, sent).run()
            # Run the pattern matcher on the sentence,
            # annotating questionable patterns
            PatternMatcher(ann, sent).run()
        # Sort the annotations by their start token index,
        # and then by decreasing span length
        ann.sort(key=lambda a: (a.start, -a.end))
        # Eliminate duplicates, i.e. identical annotation
        # codes for identical spans
        i = 1
        while i < len(ann):
            a, prev = ann[i], ann[i-1]
            if a.code == prev.code and a.start == prev.start and a.end == prev.end:
                # Identical annotation: remove it from the list
                del ann[i]
            else:
                # Check the next pair
                i += 1
        return ann

    def create_sentence(self, job: Job, s: TokenList) -> Sentence:
        """ Create a fresh sentence object and annotate it
            before returning it to the client """
        sent = AnnotatedSentence(job, s)
        # Add spelling and grammar annotations to the sentence
        sent.annotations = self.annotate(sent)
        return sent


def check_single(sentence_text: str) -> Optional[Sentence]:
    """ Check and annotate a single sentence, given in plain text """
    # Returns None if no sentence was parsed
    rc = GreynirCorrect()
    return rc.parse_single(sentence_text)


def check(text: str, *, split_paragraphs: bool = False) -> Iterable[Paragraph]:
    """ Return a generator of checked paragraphs of text,
        each being a generator of checked sentences with
        annotations """
    rc = GreynirCorrect()
    # This is an asynchronous (on-demand) parse job
    job = rc.submit(text, parse=True, split_paragraphs=split_paragraphs)
    yield from job.paragraphs()


def check_with_custom_parser(
    text: str,
    *,
    split_paragraphs: bool = False,
    parser_class: Type[GreynirCorrect] = GreynirCorrect,
    progress_func: ProgressFunc = None
) -> CheckResult:
    """ Return a dict containing parsed paragraphs as well as statistics,
        using the given correction/parser class. This is a low-level
        function; normally check_with_stats() should be used. """
    rc = parser_class()
    job = rc.submit(
        text,
        parse=True,
        split_paragraphs=split_paragraphs,
        progress_func=progress_func,
    )
    # Enumerating through the job's paragraphs and sentences causes them
    # to be parsed and their statistics collected
    paragraphs = [[sent for sent in pg] for pg in job.paragraphs()]
    return CheckResult(
        paragraphs=paragraphs,
        num_sentences=job.num_sentences,
        num_parsed=job.num_parsed,
        num_tokens=job.num_tokens,
        ambiguity=job.ambiguity,
        parse_time=job.parse_time,
    )


def check_with_stats(text: str, *, split_paragraphs: bool = False) -> CheckResult:
    """ Return a dict containing parsed paragraphs as well as statistics """
    return check_with_custom_parser(text, split_paragraphs=split_paragraphs)
