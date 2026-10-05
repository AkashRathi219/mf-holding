"""Production ``channel_runner`` binding for the agent's escalation seam (SPEC §11.3).

``src.agents.agent.Agent`` accepts an injectable ``channel_runner`` callable -
``channel_runner(channel_name, ticket, *, failed_strategy=None) -> ChannelResult``
- but ships none: without a production binding the §11.3 escalation queue's OPEN
tickets are never worked.  :func:`build_dispatcher` supplies that binding.  It
returns a runner with the exact signature ``Agent._work_escalations`` calls and
routes each dispatch to the REAL channel modules (``src.agents.channels`` for
``amc_recheck`` / ``web_search``, ``src.agents.source_amfi`` /
``src.agents.source_advisorkhoj`` for the source agents), forwarding the
injected I/O callables unchanged - the dispatcher is pure orchestration over
dependencies, exactly like the agent and the channels themselves.

Every fetch channel in :data:`CHANNEL_IMPLS` is serviceable (``amc_recheck``,
``web_search``, ``amfi``, ``advisorkhoj``).  ``manual`` is NOT a fetch - it is
the §11.4 human step - so a dispatch to it is reported as a FAILED
``ChannelResult`` with reason ``manual_channel_is_a_human_step`` and a valid
taxonomy code, while a genuinely unknown name keeps
``channel_not_implemented:<name>``.  The runner NEVER raises for either, so the
queue's own attempt accounting (``EscalationQueue.record_attempt``) keeps
moving and the §11.3 ladder proceeds.

Fraction-scale remediation (AC-7): AMFI / Advisorkhoj payloads often carry
weights as FRACTIONS (``0.0457`` meaning 4.57%).  For the ``amfi`` and
``advisorkhoj`` routes the dispatcher wraps the forwarded ``parse`` (defaulting
to the channel's own parser) so every parsed record whose holdings sum into
``[0.90, 1.10]`` is normalised through
:func:`src.agents.remediation.normalize_scale` BEFORE the channel's tier
judgement - a fraction-scale full portfolio reaches T0 instead of failing as an
incomplete sum.  ``amc_recheck`` / ``web_search`` parses are forwarded
untouched.

Rules honoured here (the shared channel contract, ``src.agents.channels``):

* All I/O arrives via the injected callables - the dispatcher performs NO
  network calls and imports no httpx/playwright.
* Politeness (AC-6): with a ``limiter`` supplied, ``limiter.is_blocked(host)``
  is consulted BEFORE any channel invocation, keyed by the ticket's host
  (``ticket.amc`` - the same host key the agent framework uses for
  ``state.is_blocked`` and the ladder's limiter check).  A blocked host gets a
  blocked ``ChannelResult`` (``ERR_WAF_CLOUDFLARE_1015``) and zero channel
  calls - never hammer a blocked host.
* Every failure carries exactly one of the 15 fixed taxonomy codes: a raised
  channel call is mapped through ``taxonomy.classify_exception`` (an
  unclassified exception falls back to ``ERR_SCHEME_MISSING_IN_DB``, the
  channel modules' own convention); codes are never invented and
  ``failure_code`` is never ``None`` on a failed result.
* ``failed_strategy`` is forwarded untouched: the forced-alternate rule (AC-18)
  stays the channel's own rotation - the dispatcher never picks a parse
  strategy itself.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping

from src.agents import source_advisorkhoj, source_amfi, taxonomy
from src.agents.channels import ChannelResult
from src.agents.channels import amc_recheck, web_search
from src.agents.escalation import CHANNEL_MANUAL, CHANNELS, Ticket
from src.agents.remediation import normalize_scale

DispatcherFn = Callable[..., ChannelResult]

WAF_CODE = "ERR_WAF_CLOUDFLARE_1015"
CODE_INCOMPLETE_SUM = "ERR_HOLDINGS_INCOMPLETE_SUM"
CODE_MISSING = "ERR_SCHEME_MISSING_IN_DB"
REASON_NOT_IMPLEMENTED = "channel_not_implemented"
REASON_MANUAL_HUMAN_STEP = "manual_channel_is_a_human_step"

for _code in (WAF_CODE, CODE_INCOMPLETE_SUM, CODE_MISSING):
    if not taxonomy.is_valid_code(_code):
        raise RuntimeError(f"dispatch references unknown taxonomy code {_code!r}")

# §11.3 FETCH channels the dispatcher services, mapped to their module paths;
# ``manual`` is deliberately absent - it is the §11.4 human step, never a
# fetch, and is reported as manual_channel_is_a_human_step.
CHANNEL_IMPLS: dict[str, str] = {
    amc_recheck.CHANNEL: amc_recheck.__name__,
    web_search.CHANNEL: web_search.__name__,
    source_amfi.CHANNEL: source_amfi.__name__,
    source_advisorkhoj.CHANNEL: source_advisorkhoj.__name__,
}

if not set(CHANNEL_IMPLS) <= set(CHANNELS):
    raise RuntimeError(
        f"dispatch implements channels outside the §11.3 order: {sorted(CHANNEL_IMPLS)}"
    )

DEFAULT_CHANNELS_IMPLEMENTED: frozenset[str] = frozenset(CHANNEL_IMPLS)


def _ticket_host(ticket: Ticket) -> str:
    """The ticket's host key (``ticket.amc``) - the agent framework's breaker key."""
    return str(getattr(ticket, "amc", "") or "")


def _blocked_result(name: str, host: str) -> ChannelResult:
    """FAILED result for a host inside its circuit-breaker window (AC-6)."""
    return ChannelResult(
        channel=name,
        success=False,
        reason=(
            f"host {host!r} is inside its circuit-breaker window; "
            f"zero requests dispatched (AC-6)"
        ),
        failure_code=WAF_CODE,
    )


def _not_implemented_result(name: str) -> ChannelResult:
    """FAILED result for a §11.3 channel the dispatcher cannot service yet."""
    return ChannelResult(
        channel=name,
        success=False,
        reason=f"{REASON_NOT_IMPLEMENTED}:{name}",
        failure_code=CODE_INCOMPLETE_SUM,
    )


def _manual_result(name: str) -> ChannelResult:
    """FAILED result for the ``manual`` channel - a human step, never a fetch.

    The §11.4 manual channel is owned by a human (``manual_register``); the
    dispatcher can never service it, so the report says exactly that instead
    of pretending the channel is merely unimplemented.  Still a FAILED result
    with a valid, non-``None`` taxonomy code (AC-5).
    """
    return ChannelResult(
        channel=name,
        success=False,
        reason=REASON_MANUAL_HUMAN_STEP,
        failure_code=CODE_INCOMPLETE_SUM,
    )


def _rescaled_rows(rows: list) -> list:
    """Fraction-scale holdings (Σ in [0.90, 1.10]) rescaled to percent.

    :func:`src.agents.remediation.normalize_scale` does both the band judgement
    and the rescale; an out-of-band total (or an unparseable row) leaves the
    rows untouched - that is an incomplete-holdings problem, not a scale
    problem.  ``pct_nav`` is re-mirrored from the scaled ``percent_nav`` so a
    written record stays consistent for the webapp loader (which reads
    ``pct_nav`` while the tier layer reads ``percent_nav``).
    """
    fixed, info = normalize_scale(rows)
    if fixed is None or not info.get("scaled"):
        return rows
    return [
        {**row, "pct_nav": row["percent_nav"]}
        if "pct_nav" in row and "percent_nav" in row
        else row
        for row in fixed
    ]


def _normalised_scale(record: object) -> object:
    """Rescale every fraction-scale ``holdings`` bucket inside a parsed record.

    Walks the record's nested shape - the AMFI ``schemes`` mapping and the
    Advisorkhoj ``files``/``sheets``/``plans`` tree alike - and rescales each
    ``holdings`` list whose weights sum into the fraction band.  A record with
    no fraction-scale bucket is returned untouched (same object), so
    percent-scale payloads can never be double-scaled.
    """
    if isinstance(record, Mapping):
        out: dict = {}
        changed = False
        for key, value in record.items():
            new_value = _normalised_scale(value)
            changed = changed or new_value is not value
            out[key] = new_value
        holdings = record.get("holdings")
        if isinstance(holdings, list) and holdings:
            fixed = _rescaled_rows(holdings)
            if fixed is not holdings:
                out["holdings"] = fixed
                changed = True
        return out if changed else record
    if isinstance(record, list):
        out = []
        changed = False
        for item in record:
            new_item = _normalised_scale(item)
            changed = changed or new_item is not item
            out.append(new_item)
        return out if changed else record
    return record


def _scale_fixing_parse(module: object, parse: object) -> object:
    """A parse callable that normalises fraction-scale holdings before tiering.

    Wraps the injected ``parse`` (the source channel's own ``parse_candidate``
    when none is injected) with the source channels' parse contract
    ``(candidate, *, source_file)`` so the channel's tier judgement sees
    percent-scale weights.
    """
    base = parse if parse is not None else module.parse_candidate

    def scaled_parse(candidate: object, *, source_file: str = "") -> object:
        return _normalised_scale(base(candidate, source_file=source_file))

    return scaled_parse


def _mapped_failure(name: str, exc: BaseException) -> ChannelResult:
    """FAILED result for a channel that raised; the taxonomy code is never invented."""
    code = taxonomy.classify_exception(exc) or CODE_MISSING
    return ChannelResult(
        channel=name,
        success=False,
        reason=f"channel {name!r} raised {type(exc).__name__}: {exc}",
        failure_code=code,
    )


def build_dispatcher(
    *,
    limiter: object = None,
    downloader: object = None,
    parse: object = None,
    provider: object = None,
    fetcher: object = None,
    client: object = None,
    out_dir: object = None,
    is_blocked: object = None,
    now: object = None,
) -> DispatcherFn:
    """Bind the agent's ``channel_runner`` seam to the real §11.3 channels.

    Returns ``runner(channel_name, ticket, *, failed_strategy=None) ->
    ChannelResult`` - the exact call ``Agent._work_escalations`` makes
    (``self.channel_runner(channel, ticket, failed_strategy=None)``).  The
    injected dependencies are forwarded per channel, untouched:

    * ``amc_recheck`` -> ``amc_recheck.run(ticket, downloader=..., parse=...,
      failed_strategy=..., now=...)``.
    * ``web_search`` -> ``web_search.run(ticket, provider=..., fetcher=...,
      parse=..., is_blocked=..., failed_strategy=..., now=...)``; the
      ``is_blocked`` host guard is the caller-supplied callable when given,
      else a limiter-backed guard that is always ``False`` without a limiter.
    * ``amfi`` -> ``source_amfi.run(ticket, client=..., out_dir=..., parse=...,
      now=...)`` and ``advisorkhoj`` -> ``source_advisorkhoj.run(...)`` with
      the same injected dependencies.  ``client`` / ``out_dir`` (both optional,
      default ``None``) are forwarded, ``out_dir`` falling back to the
      channel's own parsed-directory default when not supplied.  The forwarded
      ``parse`` is wrapped so fraction-scale holdings (Σ in [0.90, 1.10]) are
      normalised through ``src.agents.remediation.normalize_scale`` BEFORE the
      channel's tier judgement (AC-7); the ``amc_recheck`` / ``web_search``
      parses are forwarded untouched.
    * ``manual`` is NOT a fetch: it is reported as a FAILED ``ChannelResult``
      with reason ``manual_channel_is_a_human_step`` and a valid taxonomy code
      (the §11.4 human step owns it).  Any genuinely unknown name keeps a
      ``channel_not_implemented:<name>`` failure; the runner NEVER raises for
      either.

    With a ``limiter`` supplied, ``limiter.is_blocked(ticket.amc)`` is
    consulted before any FETCH channel invocation; a blocked host returns a
    blocked ``ChannelResult`` without a single channel call (AC-6 - never
    hammer a blocked host; the manual channel makes no requests and is decided
    before the guard).  A channel that raises is caught and mapped through
    ``taxonomy.classify_exception`` (an unclassified exception falls back to
    ``ERR_SCHEME_MISSING_IN_DB``) - a failed result never carries
    ``failure_code=None``.  The dispatcher itself performs no network I/O:
    every byte on the wire belongs to the injected callables.
    """

    def _host_blocked(host: str) -> bool:
        return limiter is not None and bool(limiter.is_blocked(host))

    def runner(
        channel_name: str,
        ticket: Ticket,
        *,
        failed_strategy: str | None = None,
    ) -> ChannelResult:
        name = str(channel_name or "")
        if name == CHANNEL_MANUAL:
            return _manual_result(name)
        host = _ticket_host(ticket)
        if _host_blocked(host):
            return _blocked_result(name, host)
        if name not in CHANNEL_IMPLS:
            return _not_implemented_result(name)
        try:
            if name == amc_recheck.CHANNEL:
                return amc_recheck.run(
                    ticket,
                    downloader=downloader,
                    parse=parse,
                    failed_strategy=failed_strategy,
                    now=now,
                )
            if name == web_search.CHANNEL:
                return web_search.run(
                    ticket,
                    provider=provider,
                    fetcher=fetcher,
                    parse=parse,
                    is_blocked=is_blocked or _host_blocked,
                    failed_strategy=failed_strategy,
                    now=now,
                )
            if name == source_amfi.CHANNEL:
                return source_amfi.run(
                    ticket,
                    client=client,
                    out_dir=(
                        out_dir if out_dir is not None else source_amfi.AMFI_PARSED_DIR
                    ),
                    parse=_scale_fixing_parse(source_amfi, parse),
                    now=now,
                )
            if name == source_advisorkhoj.CHANNEL:
                return source_advisorkhoj.run(
                    ticket,
                    client=client,
                    out_dir=(
                        out_dir
                        if out_dir is not None
                        else source_advisorkhoj.AK_PARSED_DIR
                    ),
                    parse=_scale_fixing_parse(source_advisorkhoj, parse),
                    now=now,
                )
        except Exception as exc:
            return _mapped_failure(name, exc)
        # A CHANNEL_IMPLS entry without a branch above degrades to
        # not-implemented - never to a raise or a ``failure_code=None``.
        return _not_implemented_result(name)

    return runner


__all__ = [
    "CHANNEL_IMPLS",
    "CODE_INCOMPLETE_SUM",
    "CODE_MISSING",
    "DEFAULT_CHANNELS_IMPLEMENTED",
    "DispatcherFn",
    "REASON_MANUAL_HUMAN_STEP",
    "REASON_NOT_IMPLEMENTED",
    "WAF_CODE",
    "build_dispatcher",
]
