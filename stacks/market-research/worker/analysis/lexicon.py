"""Pain / willingness-to-pay lexicon for Tier 0 (Analysis-Architecture.md section 1).

The doc's seeds ("i wish there was", "is there a tool that", "manually", "spreadsheet hell",
"we pay $X for", "switched away from", "workaround", "cancelled because", "i would pay for",
"takes me hours" ...) expanded to a HIGH-RECALL lexicon covering pain SHAPES, not just literal
"I wish". Six buckets, one per shape:

  desire        — explicit demand / question form for a tool or solution ("is there a tool that")
  frustration   — pain/anger/emotion with a current process or product ("drives me crazy")
  manual_toil   — effort: doing by hand what should be automated ("takes me hours")
  switching     — switching / comparison: leaving or replacing a product ("switched away from")
  wtp           — cost / willingness-to-pay: money spent or offered ("i would pay for")
  lack_negation — lack / negation: the gap that no product fills ("there's no easy way to")

These do double duty:
  1. as websearch tsquery phrases (the SET-BASED Postgres FTS gate), and
  2. as a Python matcher for `lexicon_hits[]` provenance on each surviving row.

Recall is widened further by a STRUCTURAL keep-path (is_question_shape + long-detailed length) that
ORs with the lexicon in Tier 0, so question/demand posts and long detailed posts survive even with no
literal lexicon phrase. Engagement is NEVER a keep-gate (top-upvoted threads are mostly jokes/drama);
it is only a ranking signal downstream.

ICP_KEYWORDS maps coarse ICP guesses (agency | ecom | saas_operator) from cheap keyword presence —
a placeholder until Tier 3 does it properly. Keep this module dependency-free so the tests and the
tier0 connector can both import it without pulling in psycopg2 / numpy.
"""
import re

# ---------------------------------------------------------------------------
# The lexicon. Phrases are lowercase; multi-word phrases are matched as a unit.
# Grouped by bucket so callers can weight buckets later if they want.
# ---------------------------------------------------------------------------
LEXICON = {
    "desire": [
        "i wish there was",
        "i wish there were",
        "is there a tool that",
        "is there a tool for",
        "is there an app that",
        "is there a way to",
        "does anyone know a tool",
        "looking for a tool",
        "looking for a solution",
        "looking for an alternative",
        "need a tool that",
        "need a better way",
        "there should be a",
        "why is there no",
        "why isn't there a",
        "someone should build",
        "wish i could just",
        "if only there was",
        "what do you use for",
        "how do you all handle",
        "any recommendations for",
        "best tool for",
    ],
    "frustration": [
        "i hate that",
        "i hate when",
        "so frustrating",
        "drives me crazy",
        "driving me crazy",
        "such a pain",
        "huge pain",
        "biggest pain",
        "pain point",
        "the worst part",
        "nightmare",
        "spreadsheet hell",
        "excel hell",
        "doesn't work",
        "stopped working",
        "keeps breaking",
        "fed up with",
        "sick of",
        "tired of",
        "clunky",
        "no good solution",
        "there's no easy way",
    ],
    "manual_toil": [
        "manually",
        "by hand",
        "copy paste",
        "copy and paste",
        "copy-paste",
        "data entry",
        "takes me hours",
        "takes hours",
        "takes forever",
        "hours every week",
        "every single time",
        "over and over",
        "tedious",
        "time consuming",
        "time-consuming",
        "have to do it manually",
        "doing it manually",
        "manual process",
        "manual work",
    ],
    "switching": [
        "switched away from",
        "switched from",
        "switching from",
        "moving away from",
        "migrated away from",
        "cancelled because",
        "canceled because",
        "cancelled my subscription",
        "canceled my subscription",
        "stopped using",
        "gave up on",
        "looking to replace",
        "alternative to",
        "instead of paying",
        "ditched",
        "the last straw",
    ],
    "wtp": [
        "i would pay for",
        "i'd pay for",
        "would happily pay",
        "shut up and take my money",
        "take my money",
        "we pay for",
        "we pay $",
        "i pay $",
        "paying $",
        "costs us $",
        "per month for",
        "a month for",
        "wasting money on",
        "not worth the money",
        "too expensive",
        "way too expensive",
        "worth paying for",
        "happy to pay",
    ],
    "lack_negation": [
        "there's no easy way to",
        "there is no easy way",
        "no tool that",
        "nothing that does",
        "can't find a tool",
        "can't find an app",
        "couldn't find a tool",
        "doesn't exist",
        "no way to automate",
        "wish it could",
        "would be nice if",
        "it would be great if",
        "the problem is that",
        "hard to find",
    ],
}

# Flat, de-duplicated list of all phrases, longest-first so the matcher prefers
# the most specific phrase when several overlap.
ALL_PHRASES = sorted(
    {p for bucket in LEXICON.values() for p in bucket},
    key=lambda s: (-len(s), s),
)

# Reverse index phrase -> bucket (first bucket wins; phrases are unique across buckets here).
_PHRASE_BUCKET = {}
for _bucket, _phrases in LEXICON.items():
    for _p in _phrases:
        _PHRASE_BUCKET.setdefault(_p, _bucket)


# ---------------------------------------------------------------------------
# Coarse ICP guess (placeholder; refined in Tier 3). Keyword presence only.
# ---------------------------------------------------------------------------
ICP_KEYWORDS = {
    "agency": [
        "client", "clients", "agency", "freelance", "freelancer", "retainer",
        "deliverable", "billable", "white label", "white-label",
    ],
    "ecom": [
        "shopify", "ecommerce", "e-commerce", "dropship", "dropshipping", "store",
        "merchant", "sku", "inventory", "fulfillment", "amazon seller", "etsy",
        "woocommerce", "d2c", "abandoned cart", "shipping",
    ],
    "saas_operator": [
        "saas", "churn", "mrr", "arr", "onboarding", "crm", "subscription",
        "api", "integration", "webhook", "dashboard", "self-serve", "self serve",
    ],
}


def _normalize(text):
    """Lowercase + collapse whitespace; keep $ (WTP signal). Cheap, allocation-light."""
    return re.sub(r"\s+", " ", (text or "").lower())


def lexicon_hits(text):
    """Return the sorted list of lexicon phrases present in `text` (substring match on a
    whitespace-normalized copy). This is the provenance written to pain_signals.lexicon_hits[].
    """
    norm = _normalize(text)
    if not norm:
        return []
    hits = [p for p in ALL_PHRASES if p in norm]
    return sorted(set(hits))


def has_pain(text):
    """True if ANY lexicon phrase is present — the boolean Tier-0 survival predicate."""
    norm = _normalize(text)
    if not norm:
        return False
    return any(p in norm for p in ALL_PHRASES)


# Question / demand starters — the SHAPE of someone asking for a solution. Used by the structural
# keep-path so a help-seeking post survives even with zero literal lexicon phrase. Matched at the
# start of the text or right after a sentence boundary (so "how do I ..." counts, but an incidental
# "what" mid-sentence does not).
QUESTION_STARTERS = [
    "how do i", "how do you", "how can i", "how does anyone", "how would i",
    "what do you use", "what's the best", "what is the best", "what tool",
    "is there a", "is there any", "are there any", "does anyone",
    "any recommendations", "any suggestions", "anyone know", "anyone else",
    "looking for", "need help", "help with", "recommend a", "recommend any",
    "which tool", "which app", "best way to", "advice on", "suggestions for",
]
_QUESTION_RE = re.compile(
    r"(?:^|[.!?\n]\s*)(?:" + "|".join(re.escape(s) for s in QUESTION_STARTERS) + r")\b"
)


def is_question_shape(text):
    """True if the text reads like a help-seeking question/demand — a structural keep signal that
    ORs with the lexicon in Tier 0. Looks for a question-starter at a sentence boundary, or a body
    that contains a question mark AND a question-word. Independent of engagement."""
    norm = _normalize(text)
    if not norm:
        return False
    if _QUESTION_RE.search(norm):
        return True
    if "?" in norm and any(w in norm for w in (
            "how ", "what ", "which ", "where ", "anyone", "is there", "are there")):
        return True
    return False


def guess_icp(text):
    """Coarse ICP guess from keyword presence; returns the bucket with the most hits, else None."""
    norm = _normalize(text)
    if not norm:
        return None
    best, best_n = None, 0
    for icp, kws in ICP_KEYWORDS.items():
        n = sum(1 for kw in kws if kw in norm)
        if n > best_n:
            best, best_n = icp, n
    return best


def tsquery_string():
    """Build a single Postgres websearch tsquery OR-string from the lexicon.

    websearch_to_tsquery treats a quoted multi-word phrase as an adjacency (<->) match and ORs
    the space-separated terms. We join every phrase with OR so a row matches if it contains ANY
    lexicon phrase. Phrases are double-quoted so multi-word phrases match as adjacency, not as
    independent ANDed lexemes. Punctuation ($, ') is stripped — tsvector ignores it anyway and
    the precise $-amount / contraction matching is recovered by the Python lexicon_hits() pass.
    """
    parts = []
    for phrase in ALL_PHRASES:
        cleaned = re.sub(r"[^a-z0-9 ]", " ", phrase).strip()
        cleaned = re.sub(r"\s+", " ", cleaned)
        if not cleaned:
            continue
        parts.append('"%s"' % cleaned)
    # de-dup after cleaning (e.g. "copy paste" / "copy-paste" collapse)
    seen, uniq = set(), []
    for p in parts:
        if p not in seen:
            seen.add(p)
            uniq.append(p)
    return " OR ".join(uniq)
