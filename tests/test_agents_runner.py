"""[T11] Fleet runner (SPEC §8.1, PLAN T11, AC-1/AC-11): the CLI entry point that
invokes the per-AMC agent fleet. ``run_all`` selects AMCs from the registry
(all entries, or a case-insensitive substring filter), builds ONE shared
limiter / escalation queue / episode journal / register path / dispatcher,
runs one agent per AMC under a bounded worker pool (``max_agents``), and
never lets a single-AMC failure escape: a discovery crash is recorded in
``errors`` while the other AMCs still complete, a dry run never touches the
downloader but still writes episodes, and with no ``discover`` injected the
default no-op keeps the whole loop zero-network. ``run_all`` forwards
``parse`` into the dispatcher and, only under the opt-in ``production``
flag, wires ``src.agents.production.production_kwargs`` (real adapters /
downloader / parser) - without the flag ``production_kwargs`` is never
called and no network is attempted. ``load_registry`` degrades to ``[]`` on
a missing or corrupt file, and ``main`` parses ``--all`` / ``--dry-run`` /
``--production`` into a completed, exit-0 run. All collaborators are fakes;
tmp_path only, zero network."""

from __future__ import annotations

import json
import socket
import threading
import time

from src.agents import production as production_module
from src.agents import runner as runner_module
from src.agents.channels import ChannelResult
from src.agents.episodes import OUTCOME_SUCCESS
from src.agents.runner import RunSummary, load_registry, main, run_all
from src.agents.taxonomy import is_valid_code

NOW = "2026-10-05T06:00:00+00:00"


def _registry(tmp_path, names):
    entries = [
        {
            "mf_id": str(index),
            "mf_name": name,
            "amc_monthly_portfolio_disclosure": "https://example.test/disclosures",
        }
        for index, name in enumerate(names)
    ]
    path = tmp_path / "amc_registry.json"
    path.write_text(json.dumps(entries), encoding="utf-8")
    return path


def _sandbox(tmp_path):
    return dict(
        episode_root=tmp_path / "episodes",
        state_root=tmp_path / "state",
        queue_path=tmp_path / "escalation_queue.jsonl",
        manual_csv_path=tmp_path / "manual_intervention.csv",
        now=NOW,
    )


def test_run_all_three_amcs_returns_total_three(tmp_path):
    registry_path = _registry(tmp_path, ["Alpha AMC", "Beta AMC", "Gamma AMC"])

    summary = run_all(registry_path=registry_path, **_sandbox(tmp_path))

    assert isinstance(summary, RunSummary)
    assert summary.total == 3
    assert len(summary.results) == 3
    assert summary.errors == []
    assert summary.succeeded + summary.failed == 3
    assert [result.amc_name for result in summary.results] == [
        "Alpha AMC",
        "Beta AMC",
        "Gamma AMC",
    ]
    assert all(result.outcome == "FAILED" for result in summary.results)
    assert all(is_valid_code(result.failure_code) for result in summary.results)


def test_raising_discover_captured_in_errors_and_other_amcs_complete(tmp_path):
    registry_path = _registry(tmp_path, ["Good One", "Bad AMC", "Good Two"])

    def discover(strategy, amc_name):
        if amc_name == "Bad AMC":
            raise RuntimeError("discovery exploded")
        return []

    summary = run_all(registry_path=registry_path, discover=discover, **_sandbox(tmp_path))

    assert summary.total == 3
    assert [error["amc"] for error in summary.errors] == ["Bad AMC"]
    assert "discovery exploded" in summary.errors[0]["error"]
    completed = {result.amc_name for result in summary.results}
    assert {"Good One", "Good Two"} <= completed
    assert all(result.outcome in ("SUCCESS", "PARTIAL", "FAILED") for result in summary.results)


def test_dry_run_never_downloads_but_still_writes_episodes(tmp_path):
    registry_path = _registry(tmp_path, ["Dry Run AMC"])
    downloads = []

    def download(links):
        downloads.append(list(links))
        return len(links)

    def discover(strategy, amc_name):
        return ["https://example.test/monthly.pdf"]

    episode_root = tmp_path / "episodes"
    summary = run_all(
        registry_path=registry_path,
        discover=discover,
        download=download,
        dry_run=True,
        episode_root=episode_root,
        state_root=tmp_path / "state",
        queue_path=tmp_path / "escalation_queue.jsonl",
        manual_csv_path=tmp_path / "manual_intervention.csv",
        now=NOW,
    )

    assert downloads == []
    assert summary.dry_run is True
    result = summary.results[0]
    assert result.outcome == OUTCOME_SUCCESS
    assert result.downloaded_count == 0
    files = sorted(episode_root.rglob("*.jsonl"))
    assert len(files) == 1
    lines = [line for line in files[0].read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["amc_name"] == "Dry Run AMC"
    assert record["outcome"] == "SUCCESS"
    assert record["downloaded_count"] == 0
    assert record["tools_used"] == ["discover"]


def test_max_agents_bounds_observed_concurrency(tmp_path):
    registry_path = _registry(tmp_path, [f"AMC {index:02d}" for index in range(6)])
    lock = threading.Lock()
    probe = {"in_flight": 0, "max_in_flight": 0}

    def discover(strategy, amc_name):
        with lock:
            probe["in_flight"] += 1
            probe["max_in_flight"] = max(probe["max_in_flight"], probe["in_flight"])
        time.sleep(0.1)
        with lock:
            probe["in_flight"] -= 1
        return ["https://example.test/monthly.pdf"]

    summary = run_all(
        registry_path=registry_path,
        discover=discover,
        max_agents=2,
        **_sandbox(tmp_path),
    )

    assert summary.total == 6
    assert len(summary.results) == 6
    assert probe["max_in_flight"] <= 2
    assert probe["max_in_flight"] >= 2


def test_load_registry_missing_or_corrupt_returns_empty_without_raising(tmp_path):
    assert load_registry(tmp_path / "missing.json") == []
    corrupt = tmp_path / "corrupt.json"
    corrupt.write_text("{not json", encoding="utf-8")
    assert load_registry(corrupt) == []
    wrong_shape = tmp_path / "wrong_shape.json"
    wrong_shape.write_text('{"mf_name": "not a list"}', encoding="utf-8")
    assert load_registry(wrong_shape) == []
    mixed = tmp_path / "mixed.json"
    mixed.write_text(
        json.dumps([{"mf_id": "1", "mf_name": "Named AMC"}, {"mf_id": "2"}]),
        encoding="utf-8",
    )
    assert load_registry(mixed) == [{"mf_id": "1", "mf_name": "Named AMC"}]


def test_amc_filter_selects_case_insensitive_substring_subset(tmp_path):
    registry_path = _registry(
        tmp_path,
        ["Alpha Mutual Fund", "beta mutual fund", "Gamma AMC", "Alphaland Capital"],
    )

    summary = run_all(amc_names=["ALPHA"], registry_path=registry_path, **_sandbox(tmp_path))

    assert summary.total == 2
    assert {result.amc_name for result in summary.results} == {
        "Alpha Mutual Fund",
        "Alphaland Capital",
    }

    summary_union = run_all(
        amc_names=["alpha", "gamma"], registry_path=registry_path, **_sandbox(tmp_path)
    )

    assert summary_union.total == 3
    assert {result.amc_name for result in summary_union.results} == {
        "Alpha Mutual Fund",
        "Alphaland Capital",
        "Gamma AMC",
    }


def test_run_all_without_discover_completes_safely_with_zero_network(tmp_path):
    registry_path = _registry(tmp_path, ["Safe AMC"])

    summary = run_all(registry_path=registry_path, **_sandbox(tmp_path))

    assert summary.total == 1
    assert summary.errors == []
    result = summary.results[0]
    assert result.discovered_count == 0
    assert result.downloaded_count == 0
    assert result.outcome == "FAILED"
    assert is_valid_code(result.failure_code)


def test_main_parses_all_and_dry_run_into_completed_summary(tmp_path, monkeypatch, capsys):
    registry_path = _registry(tmp_path, ["CLI AMC"])
    monkeypatch.setattr(runner_module, "DEFAULT_REGISTRY_PATH", registry_path)
    episode_root = tmp_path / "episodes"
    shared = [
        "--register",
        str(tmp_path / "reg.json"),
        "--queue",
        str(tmp_path / "q.jsonl"),
        "--state",
        str(tmp_path / "state"),
        "--manual",
        str(tmp_path / "m.csv"),
    ]

    exit_code = main(["--all", "--dry-run", "--episodes", str(episode_root), *shared])

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "total=1" in output
    assert "dry-run" in output
    files = list(episode_root.rglob("*.jsonl"))
    assert len(files) == 1
    lines = [line for line in files[0].read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(lines) == 1

    filtered_root = tmp_path / "episodes_filtered"
    exit_code_filtered = main(
        ["--amc", "cli", "--dry-run", "--episodes", str(filtered_root), *shared]
    )

    assert exit_code_filtered == 0
    assert "total=1" in capsys.readouterr().out
    assert main([]) == 2


# ---------------------------------------------------------------------------
# parse forwarding into the dispatcher
# ---------------------------------------------------------------------------


def test_run_all_forwards_parse_into_the_dispatcher(tmp_path, monkeypatch):
    registry_path = _registry(tmp_path, ["Parse AMC"])
    seen: dict = {}

    def fake_build_dispatcher(**kwargs):
        seen.clear()
        seen.update(kwargs)
        return lambda channel, ticket, failed_strategy=None: ChannelResult(
            channel=str(channel),
            success=False,
            reason="fake dispatcher",
            failure_code="ERR_SCHEME_MISSING_IN_DB",
        )

    monkeypatch.setattr(runner_module, "build_dispatcher", fake_build_dispatcher)
    parse = object()

    run_all(registry_path=registry_path, parse=parse, **_sandbox(tmp_path))

    assert seen["parse"] is parse

    run_all(registry_path=registry_path, **_sandbox(tmp_path))

    assert seen["parse"] is None


# ---------------------------------------------------------------------------
# --production: opt-in real bindings, unchanged rehearsal without it
# ---------------------------------------------------------------------------


def _fake_production_bundle(monkeypatch, calls, discover_calls, download_calls):
    def fake_discover(strategy, amc_name):
        discover_calls.append((strategy, amc_name))
        return []

    def fake_download(links):
        download_calls.append(list(links))
        return len(links)

    def fake_parse(path, parse_strategy=None, session=None):
        return None

    def fake_production_kwargs(**kwargs):
        calls.append(kwargs)
        dry_run = bool(kwargs.get("dry_run"))
        return {
            "discover": fake_discover,
            "download": None if dry_run else fake_download,
            "parse": fake_parse,
            "dispatcher": lambda channel, ticket, failed_strategy=None: ChannelResult(
                channel=str(channel),
                success=False,
                reason="fake dispatcher",
                failure_code="ERR_SCHEME_MISSING_IN_DB",
            ),
            "dry_run": dry_run,
        }

    monkeypatch.setattr(production_module, "production_kwargs", fake_production_kwargs)


def test_production_flag_wires_production_kwargs_and_dry_run_stays_meaningful(
    tmp_path, monkeypatch, capsys
):
    registry_path = _registry(tmp_path, ["Prod AMC"])
    monkeypatch.setattr(runner_module, "DEFAULT_REGISTRY_PATH", registry_path)
    calls: list[dict] = []
    discover_calls: list[tuple[str, str]] = []
    download_calls: list[list] = []
    _fake_production_bundle(monkeypatch, calls, discover_calls, download_calls)
    shared = [
        "--register",
        str(tmp_path / "reg.json"),
        "--queue",
        str(tmp_path / "q.jsonl"),
        "--state",
        str(tmp_path / "state"),
        "--manual",
        str(tmp_path / "m.csv"),
    ]

    exit_code = main(
        ["--all", "--production", "--episodes", str(tmp_path / "episodes_prod"), *shared]
    )

    assert exit_code == 0
    assert len(calls) == 1
    assert calls[0]["dry_run"] is False
    assert discover_calls, "the bundle's discover was never wired into the agents"
    assert download_calls == []

    episode_root_dry = tmp_path / "episodes_dry"
    exit_code_dry = main(
        ["--all", "--production", "--dry-run", "--episodes", str(episode_root_dry), *shared]
    )

    assert exit_code_dry == 0
    assert len(calls) == 2
    assert calls[1]["dry_run"] is True
    assert download_calls == []
    files = list(episode_root_dry.rglob("*.jsonl"))
    assert len(files) == 1
    lines = [line for line in files[0].read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(lines) == 1

    exit_code_plain = main(
        ["--all", "--episodes", str(tmp_path / "episodes_plain"), *shared]
    )

    assert exit_code_plain == 0
    assert len(calls) == 2


def test_without_production_the_run_stays_a_zero_network_rehearsal(tmp_path, monkeypatch):
    registry_path = _registry(tmp_path, ["Rehearsal AMC"])
    calls: list[dict] = []

    def fake_production_kwargs(**kwargs):
        calls.append(kwargs)
        raise AssertionError("production_kwargs must not be used without --production")

    monkeypatch.setattr(production_module, "production_kwargs", fake_production_kwargs)

    def _no_network(*args, **kwargs):
        raise AssertionError("network attempted without --production")

    monkeypatch.setattr(socket, "create_connection", _no_network)

    summary = run_all(registry_path=registry_path, **_sandbox(tmp_path))

    assert calls == []
    assert summary.total == 1
    assert summary.errors == []
    result = summary.results[0]
    assert result.discovered_count == 0
    assert result.downloaded_count == 0
    assert result.outcome == "FAILED"
    assert is_valid_code(result.failure_code)
