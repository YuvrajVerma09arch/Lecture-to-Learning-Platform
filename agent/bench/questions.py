"""The benchmark's question set.

The questions cover the cases the brief calls out, not just a set of easy
ones:

* questions the lecture covers, in the language the lecture is in;
* questions the lecture does **not** cover, which the tutor must decline;
* pause-heavy Hinglish questions, where the student hesitates before a
  technical term;
* a question in a different script from the lecture, since the corpus is
  multilingual and the student may speak any language.

Each question records its language and what is expected of the tutor, so
the harness can tell a *correct abstention* from a *failure*.
"""

from __future__ import annotations

from dataclasses import dataclass

#: The lecture the benchmark runs against: a Telugu lecture on the Bhagavad
#: Gita. The tutor's student-facing language is deliberately varied, because
#: a student studying a Telugu lecture may well ask in Hinglish.
DEFAULT_LECTURE_TITLE = "Bhagavad Gita Chapter 5 with Shankara Bhashya"

#: The languages a question can be asked in.
LANGUAGES = ("te-IN", "hi-IN", "gu-IN", "en-IN", "hi-en")


@dataclass
class Question:
    """One benchmark question."""

    text: str
    language: str
    #: Whether the lecture is expected to cover this.
    expected_covered: bool
    #: A gap of silence to splice into the middle of the question, in
    #: seconds. This is the "pauses before a technical term" case.
    pause_seconds: float = 0.0
    #: Where in the question the pause goes, as a fraction of its length.
    pause_at: float = 0.0
    #: What the question is testing, for the report.
    note: str = ""
    #: The voice to synthesise it with, when it differs from the
    #: language's default.
    voice: str | None = None

    @property
    def has_pause(self) -> bool:
        return self.pause_seconds > 0


#: Questions the lecture covers. The Telugu ones are the natural case; the
#: English and Hinglish ones test that the retrieval works across scripts.
COVERED: list[Question] = [
    Question(
        "ప్రవృత్తి ధర్మం మరియు నివృత్తి ధర్మం మధ్య తేడా ఏమిటి?",
        "te-IN",
        True,
        note="the lecture's opening question, in the lecture's own language",
    ),
    Question(
        "సంఖ్య యోగం దేనికి సమ్మతి అని భావిస్తారు?",
        "te-IN",
        True,
        note="Sankhya Yoga and Viveka",
    ),
    Question(
        "కర్మ యోగం యొక్క ప్రధాన లక్ష్యం ఏమిటి?",
        "te-IN",
        True,
        note="the goal of Karma Yoga",
    ),
    Question(
        "అజ్ఞానం వల్ల జన్మ మృత్యు బంధం ఎలా కలుగుతుంది?",
        "te-IN",
        True,
        note="Ajnana and the cycle of birth and death",
    ),
    Question(
        "ప్రేయ శ్రేయుల మధ్య భేదం ఏమిటి?",
        "te-IN",
        True,
        note="Preya and Shreya, the final section",
    ),
    Question(
        "మోక్షం అంటే ఏమిటి?",
        "te-IN",
        True,
        note="the definition of Moksha",
    ),
    Question(
        "What is Moksha?",
        "en-IN",
        True,
        note="a short English question about the lecture's central term",
    ),
    Question(
        "మోక్షం ఎలా పొందుతారు?",
        "te-IN",
        True,
        note="how Moksha is attained, in the lecture's language",
    ),
    Question(
        "What is the difference between Pravritti Dharma and Nivritti Dharma?",
        "en-IN",
        True,
        note="the same question in English, about a Telugu lecture",
    ),
    Question(
        "jnana yoga aur karma yoga me kya difference hai?",
        "hi-en",
        True,
        note="the same question in Hinglish",
    ),
    Question(
        "Pravritti Dharma aur Nivritti Dharma mein antar kya hai?",
        "hi-IN",
        True,
        note="the same question in Hindi",
    ),
    Question(
        "Pravritti Dharma ane Nivritti Dharma ma farak shu che?",
        "gu-IN",
        True,
        note="the same question in Gujarati",
    ),
]

#: Questions the lecture does not cover. The tutor must decline, and saying
#: so is a *pass*, not a failure.
NOT_COVERED: list[Question] = [
    Question(
        "Who won the FIFA World Cup in 2018, and how many goals did he score?",
        "en-IN",
        False,
        note="unrelated to the lecture",
    ),
    Question(
        "How do I bake a chocolate cake?",
        "en-IN",
        False,
        note="unrelated, and an everyday question",
    ),
    Question(
        "బెంగళూరు బ్రెడ్ ఎలా తయారు చేస్తారు?",
        "te-IN",
        False,
        note="unrelated, in the lecture's language",
    ),
    Question(
        "Python me list aur tuple me kya farak hai?",
        "hi-en",
        False,
        note="unrelated, in Hinglish",
    ),
    Question(
        "derivatives kaise nikalte hain?",
        "hi-en",
        False,
        note="unrelated technical topic",
    ),
]

#: Pause-heavy questions: the student hesitates before a technical term.
#: The pause is spliced into the synthesised audio.
PAUSE_HEAVY: list[Question] = [
    Question(
        "ప్రవృత్తి ధర్మం... మరియు... నివృత్తి ధర్మం... వేరు ఏమిటి?",
        "te-IN",
        True,
        pause_seconds=0.9,
        pause_at=0.35,
        note="a pause before the second technical term",
    ),
    Question(
        "jnana yoga... matlab... viveka... kya hota hai?",
        "hi-en",
        True,
        pause_seconds=0.9,
        pause_at=0.4,
        note="a pause before the term the student is unsure of",
    ),
    Question(
        "karmayoga... yaani... kartavya karm... kya hota hai?",
        "hi-en",
        True,
        pause_seconds=1.2,
        pause_at=0.3,
        note="a longer pause, in the middle of the question",
    ),
    Question(
        "వివేక... అంటే... ఏమిటి?",
        "te-IN",
        True,
        pause_seconds=1.0,
        pause_at=0.25,
        note="a pause immediately after the technical term",
    ),
    Question(
        "prem... yaani... preya shreya... mein kya antar hai?",
        "hi-en",
        True,
        pause_seconds=0.8,
        pause_at=0.5,
        note="a pause before the term",
    ),
]

#: Questions with awkward content: a very long term, and heavy background
#: phrasing that a real student might use.
AWKWARD: list[Question] = [
    Question(
        "ఓ జన సంఖ్య సూత్రం పరిమాణం అని పేర్కొన్న గణిత సూత్రంలోని అంశం ఏమిటి?",
        "te-IN",
        True,
        note="a long, multi-word technical term",
    ),
    Question(
        "bhai ek baat batao ye sankhya yoga aur jnana yoga dono ek hi cheez hai kya ya alag alag hai, thoda sa detail mein samjha do please?",
        "hi-en",
        True,
        note="heavy background phrasing, a long run-on question",
    ),
    Question(
        "मुझे यह समझ नहीं आया कि कर्म योग करने से क्या होता है, क्या यह सच में मुक्ति देता है या नहीं?",
        "hi-IN",
        True,
        note="a long Hindi question with a hedge",
    ),
]


def all_questions() -> list[Question]:
    """Every question, in the order the harness runs them."""
    return [*COVERED, *NOT_COVERED, *PAUSE_HEAVY, *AWKWARD]


def summary() -> dict[str, int]:
    """How many of each kind there are."""
    questions = all_questions()
    return {
        "total": len(questions),
        "covered": sum(1 for q in questions if q.expected_covered),
        "not_covered": sum(1 for q in questions if not q.expected_covered),
        "pause_heavy": sum(1 for q in questions if q.has_pause),
        "languages": len({q.language for q in questions}),
    }
