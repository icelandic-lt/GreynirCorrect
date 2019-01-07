"""

    Reynir: Natural language processing for Icelandic

    Spelling and grammar checking module

    Copyright(C) 2018 Miðeind ehf.

        This program is free software: you can redistribute it and/or modify
        it under the terms of the GNU General Public License as published by
        the Free Software Foundation, either version 3 of the License, or
        (at your option) any later version.

        This program is distributed in the hope that it will be useful,
        but WITHOUT ANY WARRANTY; without even the implied warranty of
        MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
        GNU General Public License for more details.

    You should have received a copy of the GNU General Public License
    along with this program.  If not, see <https://www.gnu.org/licenses/>.


    This module exposes functions to check spelling and grammar for
    text strings.

"""

from threading import Lock

from reynir import Reynir, correct_spaces
from reynir.binparser import BIN_Token
from reynir.fastparser import Fast_Parser, ParseForestNavigator
from reynir.reducer import Reducer
from reynir.settings import VerbSubjects

from .errtokenizer import tokenize as tokenize_and_correct


class Annotation:

    """ An annotation of a span of a token list for a sentence """

    def __init__(self, start, end, text, code):
        assert isinstance(start, int)
        self._start = start
        assert isinstance(end, int)
        self._end = end
        self._text = text
        self._code = code

    @property
    def start(self):
        """ The index of the first token to which the annotation applies """
        return self._start

    @property
    def end(self):
        """ The index of the last token to which the annotation applies """
        return self._end

    @property
    def text(self):
        """ A description of the annotation """
        return self._text

    @property
    def code(self):
        """ A code for the annotation type, usually an error or warning code """
        return self._code


class ErrorFinder(ParseForestNavigator):

    """ Utility class to find nonterminals in parse trees that are
        tagged as errors in the grammar """

    def __init__(self, ann, toklist):
        super().__init__(visit_all=True)
        # Annotation list
        self._ann = ann
        self._toklist = toklist

    def _visit_token(self, level, node):
        """ Entering a terminal/token match node """
        if (
            node.terminal.category == "so"
            and node.terminal.is_subj
            and node.terminal.has_variant("op")
        ):
            # Check whether the associated verb is allowed
            # with a subject in this case
            pass
        return None

    def _visit_nonterminal(self, level, node):
        """ Entering a nonterminal node """
        if node.is_interior or node.nonterminal.is_optional:
            pass
        elif node.nonterminal.has_tag("error"):
            # This node has a nonterminal that is tagged with $tag(error)
            # in the grammar file (Reynir.grammar)
            txt = correct_spaces(
                " ".join(t.txt for t in self._toklist[node.start:node.end] if t.txt)
            )
            self._ann.append(
                # E002: Probable grammatical error
                # !!! TODO: add further info and guidance to the text field
                Annotation(
                    start=node.start,
                    end=node.end-1,
                    text="'{0}' er líklega málfræðilega rangt (regla '{1}')"
                        .format(txt, node.nonterminal.name),
                    code="E002"
                )
            )
        return None


class ErrorDetectionToken(BIN_Token):

    """ A subclass of BIN_Token that adds error detection behavior
        to the base class """

    _VERB_ERROR_SUBJECTS = VerbSubjects.VERBS_ERRORS

    @staticmethod
    def verb_is_impersonal(verb):
        """ Return True if the given verb is strictly impersonal,
            i.e. never appears with a nominative subject """
        # Here, we return False because we want to catch errors
        # where impersonal verbs are used with a nominative subject
        return False

    def verb_subject_matches(self, verb, subj):
        """ Returns True if the given subject type/case is allowed for this verb
            or if it is an erroneous subject which we can flag """
        return (
            subj in self._VERB_SUBJECTS.get(verb, set())
            or subj in self._VERB_ERROR_SUBJECTS.get(verb, set())
        )


class ErrorDetectingParser(Fast_Parser):

    """ A subclass of Fast_Parser that modifies its behavior to
        include grammar error detection rules in the parsing process """

    @staticmethod
    def _create_wrapped_token(t, ix):
        """ Create an instance of a wrapped token """
        return ErrorDetectionToken(t, ix)


class ReynirCorrect(Reynir):

    """ Parser augmented with the ability to add spelling and grammar
        annotations to the returned sentences """

    # ReynirCorrect has its own class instances of a parser and a reducer,
    # separate from the Reynir class, as they use different settings and
    # parsing enviroments
    _parser = None
    _reducer = None
    _lock = Lock()

    def __init__(self):
        super().__init__()

    def tokenize(self, text):
        """ Use the correcting tokenizer instead of the normal one """
        return tokenize_and_correct(text)

    @property
    def parser(self):
        """ Override the parent class' construction of a parser instance """
        with self._lock:
            if ReynirCorrect._parser is None:
                # Initialize a singleton instance of the parser and the reducer.
                # Both classes are re-entrant and thread safe.
                ReynirCorrect._parser = edp = ErrorDetectingParser()
                ReynirCorrect._reducer = Reducer(edp.grammar)
            return ReynirCorrect._parser

    @property
    def reducer(self):
        """ Return the reducer instance to be used """
        # Should always retrieve the parser attribute first
        assert ReynirCorrect._reducer is not None
        return ReynirCorrect._reducer

    @staticmethod
    def annotate(sent):
        """ Returns a list of annotations for a sentence object, containing
            spelling and grammar annotations of that sentence """
        ann = []
        # First, add token-level annotations
        for ix, t in enumerate(sent.tokens):
            if t.error_code:
                ann.append(
                    Annotation(
                        start=ix,
                        end=ix + t.error_span - 1,
                        text=t.error_description,
                        code=t.error_code
                    )
                )
        # Then: if the sentence couldn't be parsed,
        # put an annotation on it as a whole
        if sent.deep_tree is None:
            ann.append(
                # E001: Unable to parse sentence
                Annotation(
                    start=0,
                    end=len(sent.tokens)-1,
                    text="Ekki tókst að þátta setninguna",
                    code="E001"
                )
            )
        else:
            # Successfully parsed:
            # Add error rules from the grammar
            ErrorFinder(ann, sent.tokens).go(sent.deep_tree)
        # Sort the annotations by their start token index,
        # and then by decreasing span length
        ann.sort(key=lambda a: (a.start, -a.end))
        return ann

    def create_sentence(self, job, s):
        """ Create a fresh sentence object and annotate it
            before returning it to the client """
        sent = super().create_sentence(job, s)
        # Add spelling and grammar annotations to the sentence
        sent.annotations = self.annotate(sent)
        return sent


def check_single(sentence):
    """ Check and annotate a single sentence, given in plain text """
    rc = ReynirCorrect()
    return rc.parse_single(sentence)


def check(text):
    """ Return a generator of checked paragraphs of text,
        each being a generator of checked sentences with
        annotations """
    rc = ReynirCorrect()
    job = rc.submit(text, parse=True)
    yield from job.paragraphs()
