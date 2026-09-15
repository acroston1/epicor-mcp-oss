"""Live search against the epiusers.help community forum (Discourse).

Complements the offline help store (``epicor_mcp.index.help_store``) with
fresh forum content.  Anonymous Discourse search ANDs every unquoted term,
so raw user questions ("how do I ... in Epicor Kinetic?") return nothing —
the query is first reduced to a handful of salient terms (or a quoted
error-message phrase) by :meth:`LiveForumSearch.reduce_query`.

Resilience features:

* Token-bucket rate limiting kept well under the forum's anonymous per-IP
  limits (searches: 1/sec burst 2 and 10/min; topic fetches: 30/min).
* A circuit breaker that opens for 120s after 3 consecutive transport
  failures/429s, honoring ``Retry-After`` on 429 for the open duration.
* An in-process TTL+LRU cache (reduced query -> results, 30 min, 256 entries).

``search()`` never raises and never writes to disk — any failure logs a
warning and returns ``[]``.

Usage:
    from epicor_mcp.index.forum_live import LiveForumSearch
    forum = LiveForumSearch()
    results = await forum.search("BAQ subquery criteria not filtering")
"""

from __future__ import annotations

import asyncio
import html
import logging
import re
import time
from collections import OrderedDict
from typing import Any, Callable

import httpx

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------

_SEARCH_BURST = 2.0  # per-second search bucket capacity
_SEARCH_PER_SEC = 1.0  # per-second search bucket refill rate
_SEARCH_PER_MIN = 10.0  # per-minute search bucket capacity
_TOPIC_PER_MIN = 30.0  # per-minute topic-fetch bucket capacity
_BREAKER_THRESHOLD = 3  # consecutive failures before the breaker opens
_BREAKER_OPEN_SECS = 120.0  # default breaker-open duration
_CACHE_TTL_SECS = 1800.0
_CACHE_MAX_ENTRIES = 256
_EXCERPT_MAX_CHARS = 900
_MAX_SOLVED_FETCHES = 2  # solved topics whose accepted answer is fetched
_MAX_QUERY_TERMS = 6
_MAX_PHRASE_WORDS = 8
_FALLBACK_TERMS = 3

_USER_AGENT = (
    "EpicorMCPOpenSource/1.0 (administrator-configured documentation lookup)"
)

# ---------------------------------------------------------------------------
# Query reduction vocabulary
# ---------------------------------------------------------------------------

# Question words, articles, auxiliaries, and filler that add nothing to a
# Discourse AND-search.  "epicor"/"kinetic" are stopwords because every topic
# on epiusers.help is about Epicor.
_STOPWORDS = frozenset(
    """
    a about all am an and any anyone are as at be been being but can cannot
    cant could did do does epicor for from get gets getting got had has have
    having he help her here his how i if in into is it its just kinetic know
    make makes making me my need needs no not of on one or other our out
    please she should so some someone than that the their them then there
    these they this those to tried try trying up us use used using want wants
    was way we were what when where which while who why will with without
    wont would you your
    """.split()
)

# Epicor module/domain words that should survive reduction even when short.
_MODULE_WORDS = frozenset(
    """
    ap aps ar baq baqs bin bo bom boo bpm bpms configurator crm customer
    dashboard dashboards directive dmr dmt eco ecn edi fiscal function
    functions gl invoice invoices job jobs kanban lot mes moq mrp odata order
    orders packslip part parts payroll po pos posting quote quotes receipt
    receipts requisition rest rfq rma serial shipment ssrs sso subquery
    supplier tracker ud udd uom vendor warehouse widget wip
    """.split()
)

# Signature of a pasted Epicor error message / stack trace.
_ERROR_SIG_RE = re.compile(
    r"Exception|Server Side Error|at Ice\.|at Erp\."
)
# A double-quoted span (candidate error-message phrase).
_QUOTED_SPAN_RE = re.compile(r'"([^"]{3,400})"')
# Word tokens; internal . _ - kept so dotted identifiers survive intact.
_WORD_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.\-]*")
# CamelCase (lower followed by upper) inside a token.
_CAMEL_RE = re.compile(r"[a-z][A-Z]")
# Stack-frame boundary used to cut trace noise off an error message.
_STACK_FRAME_SPLIT_RE = re.compile(r"\bat\s+(?:Ice|Erp|System|Epicor)\.")
# "Some.Namespace.FooException: message" or "Server Side Error: message".
_EXC_MESSAGE_RE = re.compile(
    r"(?:\w+(?:\.\w+)*Exception|Server Side Errors?)\s*:?\s*(.+)", re.DOTALL
)

# ---------------------------------------------------------------------------
# HTML -> text
# ---------------------------------------------------------------------------

_SCRIPT_STYLE_RE = re.compile(r"(?is)<(script|style)[^>]*>.*?</\1>")
_TAG_RE = re.compile(r"<[^>]+>")


def _html_to_text(raw: str) -> str:
    """Strip HTML tags from *raw* and collapse all whitespace to single spaces.

    Small regex-based helper — good enough for Discourse ``cooked`` post HTML
    and search ``blurb`` strings; not a general-purpose HTML parser.
    """
    if not raw:
        return ""
    text = _SCRIPT_STYLE_RE.sub(" ", raw)
    text = _TAG_RE.sub(" ", text)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _parse_retry_after(value: str | None) -> float | None:
    """Parse a numeric ``Retry-After`` header value (seconds); None otherwise."""
    if not value:
        return None
    try:
        return max(0.0, float(value.strip()))
    except ValueError:
        return None  # HTTP-date form — fall back to the default duration


# ---------------------------------------------------------------------------
# Token bucket
# ---------------------------------------------------------------------------


class _TokenBucket:
    """Classic token bucket: ``capacity`` tokens refilled at ``rate``/sec."""

    def __init__(
        self,
        capacity: float,
        rate: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._capacity = capacity
        self._rate = rate
        self._clock = clock
        self._tokens = capacity
        self._last = clock()

    def _refill(self) -> None:
        now = self._clock()
        self._tokens = min(self._capacity, self._tokens + (now - self._last) * self._rate)
        self._last = now

    def try_acquire(self, n: float = 1.0) -> bool:
        """Take *n* tokens if available; return whether they were taken."""
        self._refill()
        if self._tokens >= n:
            self._tokens -= n
            return True
        return False

    def refund(self, n: float = 1.0) -> None:
        """Return *n* tokens (used when a paired acquisition fails)."""
        self._tokens = min(self._capacity, self._tokens + n)

    def seconds_until(self, n: float = 1.0) -> float:
        """Seconds until *n* tokens will be available (0.0 if already are)."""
        self._refill()
        if self._tokens >= n:
            return 0.0
        return (n - self._tokens) / self._rate


# ---------------------------------------------------------------------------
# Query reduction (pure functions)
# ---------------------------------------------------------------------------


def _tokenize(text: str) -> list[str]:
    """Extract word tokens, trimming trailing punctuation kept by the regex."""
    tokens = []
    for tok in _WORD_RE.findall(text):
        tok = tok.strip("._-")
        if tok:
            tokens.append(tok)
    return tokens


def _salience(token: str) -> float:
    """Score how distinctive *token* is for a forum search.

    Known Epicor module words, CamelCase/dotted identifiers, and ALLCAPS
    acronyms score highest; capitalized words next; length is a tiebreaker.
    """
    score = min(len(token), 12) / 12.0
    if token.lower() in _MODULE_WORDS:
        score += 5.0
    if _CAMEL_RE.search(token) or "." in token or "_" in token:
        score += 4.0
    elif len(token) >= 2 and token.isupper():
        score += 4.0
    elif token[:1].isupper():
        score += 2.0
    if any(c.isdigit() for c in token) and any(c.isalpha() for c in token):
        score += 1.0
    return score


def _select_salient(tokens: list[str], limit: int) -> list[str]:
    """Pick up to *limit* non-stopword tokens by salience, original order."""
    seen: set[str] = set()
    candidates: list[tuple[int, str, float]] = []
    for i, tok in enumerate(tokens):
        low = tok.lower()
        if low in _STOPWORDS or low in seen:
            continue
        seen.add(low)
        candidates.append((i, tok, _salience(tok)))
    top = sorted(candidates, key=lambda c: -c[2])[:limit]
    top.sort(key=lambda c: c[0])  # restore original order
    return [tok for _, tok, _ in top]


def _extract_error_phrase(query: str) -> str:
    """Extract the most distinctive <=8-word span from a pasted error message."""
    # 1. A long (>=6-word) double-quoted span is the error text verbatim.
    for m in _QUOTED_SPAN_RE.finditer(query):
        words = m.group(1).split()
        if len(words) >= 6:
            return " ".join(words[:_MAX_PHRASE_WORDS])
    # 2. The message after "SomeException:" / "Server Side Error:", with
    #    stack-frame noise cut off.
    m = _EXC_MESSAGE_RE.search(query)
    if m:
        tail = _STACK_FRAME_SPLIT_RE.split(m.group(1))[0]
        tail = tail.splitlines()[0] if tail.splitlines() else ""
        words = tail.replace('"', " ").split()
        if len(words) >= 3:
            return " ".join(words[:_MAX_PHRASE_WORDS])
    # 3. The first line containing the signature (e.g. a bare exception type).
    for line in query.splitlines():
        if _ERROR_SIG_RE.search(line):
            head = _STACK_FRAME_SPLIT_RE.split(line)[0].strip()
            words = (head or line).replace('"', " ").split()
            if words:
                return " ".join(words[:_MAX_PHRASE_WORDS])
    return ""


def _has_error_signature(query: str) -> bool:
    if _ERROR_SIG_RE.search(query):
        return True
    return any(
        len(m.group(1).split()) >= 6 for m in _QUOTED_SPAN_RE.finditer(query)
    )


def _reduce_query(query: str) -> str:
    """Pure implementation behind :meth:`LiveForumSearch.reduce_query`."""
    q = (query or "").strip()
    if not q:
        return ""

    if _has_error_signature(q):
        phrase = _extract_error_phrase(q)
        if phrase:
            phrase_words = {w.lower().strip("._-") for w in phrase.split()}
            context = [
                t.lower()
                for t in _select_salient(_tokenize(q), _MAX_QUERY_TERMS)
                if t.lower() not in phrase_words
            ][:2]
            reduced = f'"{phrase}"'
            if context:
                reduced += " " + " ".join(context)
            return reduced

    tokens = _tokenize(q)
    salient = _select_salient(tokens, _MAX_QUERY_TERMS)
    if salient:
        return " ".join(t.lower() for t in salient)

    # Fallback: never return empty — take the 3 longest words.
    unique = list(dict.fromkeys(t.lower() for t in tokens))
    longest = sorted(unique, key=len, reverse=True)[:_FALLBACK_TERMS]
    return " ".join(longest) if longest else q


def _strongest_terms(reduced: str, limit: int) -> str:
    """The *limit* highest-salience terms of an already-reduced query."""
    tokens = _tokenize(reduced.replace('"', " "))
    seen: set[str] = set()
    scored: list[tuple[int, str, float]] = []
    for i, tok in enumerate(tokens):
        low = tok.lower()
        if low in seen:
            continue
        seen.add(low)
        scored.append((i, tok, _salience(tok)))
    top = sorted(scored, key=lambda c: -c[2])[:limit]
    top.sort(key=lambda c: c[0])
    return " ".join(tok.lower() for _, tok, _ in top)


def _term_count(reduced: str) -> int:
    return len(_tokenize(reduced.replace('"', " ")))


# ---------------------------------------------------------------------------
# LiveForumSearch
# ---------------------------------------------------------------------------


class LiveForumSearch:
    """Anonymous live search against an epiusers.help-style Discourse forum.

    ``search()`` returns ``[]`` on ANY failure (timeout, 429, open circuit
    breaker, rate-limit exhaustion) — it never raises and never writes to
    disk.  Results are dicts with keys ``title``, ``url``, ``created_at``,
    ``solved`` (bool), ``excerpt`` (<=900 chars), ``topic_id`` (int).

    ``last_status`` reflects the most recent ``search()`` call so callers
    can distinguish "no matches" from "forum unavailable": one of ``""``
    (never searched), ``"ok"``, ``"no_results"``, ``"empty_query"``,
    ``"circuit_open"``, ``"rate_limited"``, ``"http_error"``, ``"error"``.
    Best-effort under concurrency — read it right after the call it
    describes.
    """

    def __init__(
        self,
        base_url: str = "https://www.epiusers.help",
        timeout: float = 5.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._clock: Callable[[], float] = time.monotonic
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout, connect=2.0),
            headers={"Accept": "application/json", "User-Agent": _USER_AGENT},
            follow_redirects=True,  # /t/{id}.json 301s to /t/{slug}/{id}.json
        )

        # Rate limiting: searches 1/sec burst 2 AND 10/min; topics 30/min.
        self._search_second = _TokenBucket(_SEARCH_BURST, _SEARCH_PER_SEC, self._clock)
        self._search_minute = _TokenBucket(_SEARCH_PER_MIN, _SEARCH_PER_MIN / 60.0, self._clock)
        self._topic_bucket = _TokenBucket(_TOPIC_PER_MIN, _TOPIC_PER_MIN / 60.0, self._clock)
        self._rate_lock = asyncio.Lock()

        # Circuit breaker state.
        self._consecutive_failures = 0
        self._open_until = 0.0

        # TTL + LRU cache: reduced query -> (expires_at, results).
        self._cache: OrderedDict[str, tuple[float, list[dict[str, Any]]]] = OrderedDict()

        # Outcome of the most recent search() call (see class docstring).
        self.last_status: str = ""

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def aclose(self) -> None:
        """Close the underlying HTTP client."""
        await self._client.aclose()

    # ------------------------------------------------------------------
    # Query reduction
    # ------------------------------------------------------------------

    def reduce_query(self, query: str) -> str:
        """Reduce a natural-language question to a Discourse-friendly query.

        Pure function (no instance state).  Pasted error messages become a
        double-quoted <=8-word phrase plus up to 2 context keywords; anything
        else becomes <=6 lowercase salient terms with stopwords removed.
        Never returns empty for non-empty input (falls back to the 3 longest
        words).
        """
        return _reduce_query(query)

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    async def search(self, query: str, max_topics: int = 5) -> list[dict[str, Any]]:
        """Search the forum; return up to *max_topics* results, [] on failure."""
        try:
            return await self._search_impl(query, max_topics)
        except Exception:
            self.last_status = "error"
            logger.warning("Live forum search failed for query %r", query, exc_info=True)
            return []

    async def _search_impl(self, query: str, max_topics: int) -> list[dict[str, Any]]:
        reduced = self.reduce_query(query)
        if not reduced or max_topics <= 0:
            self.last_status = "empty_query"
            return []

        cached = self._cache_get(reduced)
        if cached is not None:
            self.last_status = "ok" if cached else "no_results"
            return cached[:max_topics]

        if self._breaker_open():
            self.last_status = "circuit_open"
            logger.warning("Forum circuit breaker open; skipping live search")
            return []

        if not await self._acquire_search_slot():
            self.last_status = "rate_limited"
            return []
        payload = await self._get_json(f"{self._base_url}/search.json", params={"q": reduced})
        if payload is None:
            self.last_status = "http_error"
            return []

        topics = [t for t in payload.get("topics") or [] if isinstance(t, dict)]
        posts = [p for p in payload.get("posts") or [] if isinstance(p, dict)]

        # Discourse ANDs terms: an over-specified query returns nothing.
        # Retry once with the 3 strongest terms.
        if not topics and _term_count(reduced) > _FALLBACK_TERMS:
            fallback = _strongest_terms(reduced, _FALLBACK_TERMS)
            if fallback and fallback != reduced and await self._acquire_search_slot():
                payload = await self._get_json(
                    f"{self._base_url}/search.json", params={"q": fallback}
                )
                if payload is not None:
                    topics = [t for t in payload.get("topics") or [] if isinstance(t, dict)]
                    posts = [p for p in payload.get("posts") or [] if isinstance(p, dict)]

        if not topics:
            self._cache_put(reduced, [])
            self.last_status = "no_results"
            return []

        # First (most relevant) blurb per topic from the search results.
        blurbs: dict[int, str] = {}
        for p in posts:
            tid = p.get("topic_id")
            if isinstance(tid, int) and tid not in blurbs:
                blurbs[tid] = p.get("blurb") or ""

        # Solved topics first; API relevance order within each group.
        ranked = sorted(topics, key=lambda t: not bool(t.get("has_accepted_answer")))
        selected = [t for t in ranked if isinstance(t.get("id"), int)][:max_topics]

        results: list[dict[str, Any]] = []
        solved_fetches = 0
        for topic in selected:
            topic_id: int = topic["id"]
            solved = bool(topic.get("has_accepted_answer"))
            excerpt = _html_to_text(blurbs.get(topic_id, ""))
            if solved and solved_fetches < _MAX_SOLVED_FETCHES:
                solved_fetches += 1
                answer = await self._fetch_accepted_answer(topic_id)
                if answer:
                    excerpt = answer
            slug = topic.get("slug") or ""
            url = (
                f"{self._base_url}/t/{slug}/{topic_id}"
                if slug
                else f"{self._base_url}/t/{topic_id}"
            )
            results.append(
                {
                    "title": topic.get("title") or "",
                    "url": url,
                    "created_at": topic.get("created_at") or "",
                    "solved": solved,
                    "excerpt": excerpt[:_EXCERPT_MAX_CHARS],
                    "topic_id": topic_id,
                }
            )

        self._cache_put(reduced, results)
        self.last_status = "ok" if results else "no_results"
        return results

    async def _fetch_accepted_answer(self, topic_id: int) -> str:
        """Fetch /t/{id}.json and return the accepted answer as plain text."""
        if self._breaker_open():
            return ""
        if not self._topic_bucket.try_acquire():
            logger.warning("Forum topic-fetch rate limit reached; using search blurb")
            return ""
        data = await self._get_json(f"{self._base_url}/t/{topic_id}.json")
        if not data:
            return ""

        cooked = ""
        accepted_post_number: int | None = None

        accepted_list = data.get("accepted_answers")
        if isinstance(accepted_list, list) and accepted_list:
            first = accepted_list[0]
            if isinstance(first, dict):
                cooked = first.get("cooked") or first.get("excerpt") or ""
                accepted_post_number = first.get("post_number")

        if not cooked:
            single = data.get("accepted_answer")
            if isinstance(single, dict):
                cooked = single.get("cooked") or single.get("excerpt") or ""
                accepted_post_number = single.get("post_number") or accepted_post_number

        if not cooked:
            posts = (data.get("post_stream") or {}).get("posts") or []
            for post in posts:
                if not isinstance(post, dict):
                    continue
                if post.get("accepted_answer") or (
                    accepted_post_number is not None
                    and post.get("post_number") == accepted_post_number
                ):
                    cooked = post.get("cooked") or ""
                    break

        return _html_to_text(cooked)

    # ------------------------------------------------------------------
    # HTTP + circuit breaker
    # ------------------------------------------------------------------

    async def _get_json(
        self, url: str, params: dict[str, str] | None = None
    ) -> dict[str, Any] | None:
        """GET *url* and return parsed JSON, or None (breaker-aware) on failure."""
        try:
            response = await self._client.get(url, params=params)
        except httpx.HTTPError as exc:
            self._record_failure()
            logger.warning("Forum request failed (%s): %s", url, exc)
            return None

        if response.status_code == 429:
            retry_after = _parse_retry_after(response.headers.get("Retry-After"))
            self._record_failure(retry_after)
            logger.warning(
                "Forum rate-limited (429) on %s (Retry-After=%s)", url, retry_after
            )
            return None
        if response.status_code != 200:
            # Not a transport failure/429 — logged but not a breaker event.
            logger.warning("Forum returned HTTP %d for %s", response.status_code, url)
            return None

        self._record_success()
        try:
            data = response.json()
        except ValueError:
            logger.warning("Forum returned non-JSON body for %s", url)
            return None
        return data if isinstance(data, dict) else None

    def _breaker_open(self) -> bool:
        return self._clock() < self._open_until

    def _record_failure(self, retry_after: float | None = None) -> None:
        """Count a transport failure/429; open the breaker when warranted."""
        self._consecutive_failures += 1
        now = self._clock()
        if retry_after is not None:
            # The server explicitly told us to back off — honor it immediately.
            self._open_until = max(self._open_until, now + retry_after)
        if self._consecutive_failures >= _BREAKER_THRESHOLD:
            duration = max(_BREAKER_OPEN_SECS, retry_after or 0.0)
            self._open_until = max(self._open_until, now + duration)
            logger.warning(
                "Forum circuit breaker open for %.0fs after %d consecutive failures",
                self._open_until - now,
                self._consecutive_failures,
            )

    def _record_success(self) -> None:
        self._consecutive_failures = 0

    # ------------------------------------------------------------------
    # Rate limiting
    # ------------------------------------------------------------------

    async def _acquire_search_slot(self) -> bool:
        """Take one search slot, waiting briefly on the per-second bucket."""
        async with self._rate_lock:
            if not self._search_minute.try_acquire():
                logger.warning(
                    "Forum search per-minute rate limit reached; skipping live search"
                )
                return False
            wait = self._search_second.seconds_until()
            if wait > 0:
                await asyncio.sleep(wait)
            if not self._search_second.try_acquire():
                self._search_minute.refund()
                logger.warning("Forum search per-second rate limit contention; skipping")
                return False
            return True

    # ------------------------------------------------------------------
    # TTL + LRU cache
    # ------------------------------------------------------------------

    def _cache_get(self, key: str) -> list[dict[str, Any]] | None:
        entry = self._cache.get(key)
        if entry is None:
            return None
        expires_at, results = entry
        if self._clock() >= expires_at:
            del self._cache[key]
            return None
        self._cache.move_to_end(key)
        return [dict(r) for r in results]

    def _cache_put(self, key: str, results: list[dict[str, Any]]) -> None:
        self._cache[key] = (self._clock() + _CACHE_TTL_SECS, [dict(r) for r in results])
        self._cache.move_to_end(key)
        while len(self._cache) > _CACHE_MAX_ENTRIES:
            self._cache.popitem(last=False)
