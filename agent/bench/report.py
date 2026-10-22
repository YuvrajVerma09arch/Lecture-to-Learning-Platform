"""Rendering the benchmark results.

The report separates what was measured from what was not. A number with
no conditions attached is not a result, so every figure is printed with
the model, the region, and the sample count that produced it.
"""

from __future__ import annotations

from typing import Any


def _fmt(value: Any, suffix: str = " s") -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float):
        return f"{value:.3f}{suffix}"
    return f"{value}{suffix}"


def _stats_row(name: str, stats: dict[str, Any]) -> str:
    return (
        f"| {name} | {stats.get('n', 0)} | {_fmt(stats.get('p50'))} | "
        f"{_fmt(stats.get('p95'))} | {_fmt(stats.get('mean'))} | "
        f"{_fmt(stats.get('min'))} | {_fmt(stats.get('max'))} |"
    )


def render(result: Any) -> str:
    """Render a benchmark result as Markdown."""
    conditions = result.conditions
    overall = result.overall
    failures = result.failures
    abstention = result.abstention_accuracy()

    lines: list[str] = []
    lines.append("# Voice benchmark")
    lines.append("")
    lines.append(
        "Latency is measured from the end of the student's speech to the "
        "first frame of the agent's answer arriving at the client. Both "
        "readings are on a monotonic clock, and the two sides do not share "
        "one, so the server's own markers travel through the room's data "
        "channel rather than being compared as timestamps."
    )
    lines.append("")

    lines.append("## Conditions")
    lines.append("")
    lines.append("| Condition | Value |")
    lines.append("|---|---|")
    for key, value in conditions.to_dict().items():
        lines.append(f"| {key.replace('_', ' ')} | {value} |")
    lines.append("")

    lines.append("## MEASURED")
    lines.append("")
    lines.append("| Case | n | p50 | p95 | mean | min | max |")
    lines.append("|---|---|---|---|---|---|---|")
    lines.append(_stats_row("All turns", overall))
    for name, stats in result.by_case.items():
        lines.append(_stats_row(name.replace("_", " "), stats))
    for language, stats in result.by_language.items():
        lines.append(_stats_row(f"language {language}", stats))
    lines.append(_stats_row("Answer length", result.answer_durations))
    lines.append("")

    lines.append("### Turn outcomes")
    lines.append("")
    lines.append(f"- Turns run: **{len(result.records)}**")
    lines.append(f"- Turns that failed: **{len(failures)}**")
    lines.append(
        f"- Questions the lecture does not cover: **{abstention['expected_to_decline']}**, "
        f"declined correctly **{abstention['declined_correctly']}**, "
        f"answered anyway **{abstention['answered_incorrectly']}**"
    )
    lines.append("")
    lines.append(
        "A decline is a pass. A confident answer to a question the lecture "
        "never covered is the fabrication the brief forbids, so it is "
        "counted as a failure even though the turn itself succeeded."
    )
    lines.append("")

    if failures:
        lines.append("### Failures")
        lines.append("")
        for record in failures:
            lines.append(
                f"- `{record.language}` {record.question[:70]!r}: {record.error}"
            )
        lines.append("")

    lines.append("## Method")
    lines.append("")
    lines.append(
        "- **p50** is the median, so one slow turn does not move it."
    )
    lines.append(
        "- **p95** uses the nearest-rank method: the sample at index "
        "`ceil(0.95 * n) - 1` of the sorted values. It always returns a "
        "value that was actually observed, which matters at this sample "
        "size."
    )
    lines.append(
        "- Failed turns are excluded from the latency figures and reported "
        "separately."
    )
    lines.append(
        f"- {conditions.warmup_turns_excluded} warm-up turn was run and "
        "excluded: the first call pays for a cold connection and a cold "
        "model."
    )
    lines.append(
        "- The turns are scripted: the question is synthesised with Gnani "
        "Timbre and published as an audio track, and the agent's answer "
        "is captured from the room. It is a real conversation through the "
        "real pipeline, but it is not a human speaking."
    )
    lines.append("")

    lines.append("## ESTIMATED")
    lines.append("")
    lines.append(
        "- The deployment comparison is an estimate for configuration B "
        "and configuration C. Only configuration A was run. No latency "
        "figure is quoted for B or C, because none was measured."
    )
    lines.append("")

    lines.append("## MOCKED")
    lines.append("")
    lines.append(
        "- The statistics, the record schema and the report renderer are "
        "covered by tests with the provider replaced. The question set's "
        "composition is checked the same way."
    )
    lines.append("")

    lines.append("## UNVERIFIED")
    lines.append("")
    lines.append(
        "- The credit figure for a Gnani transcription is an estimate: "
        "Gnani publishes no rate card, so the figure uses a reseller's "
        "and the authoritative number is the usage panel."
    )
    lines.append(
        "- The regions of the three AI providers were not recorded from "
        "the project."
    )
    lines.append(
        "- How a real student phrases a question, and how often they "
        "hesitate, is not measured here. The scripted questions are a "
        "proxy."
    )
    lines.append("")

    return "\n".join(lines)
