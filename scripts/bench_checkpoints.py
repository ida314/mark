"""What a checkpoint costs, per boundary type, on this machine.

Pass 4a's exit criterion asks for checkpoint write overhead to be measured rather than
reasoned about, and passes 2a, 2b and 2c each closed with "the fsync cost is unmeasured".
This is the harness that produces the number, so the figure in
`docs/records/pass-04-outcome.md` can be re-checked after a change instead of being a
historical claim.

What it measures, per trigger: wall-clock latency of `Checkpointer.write` - which is one
buffer flush, one synchronous `checkpoint_written` append and one row insert, three commits
against a `synchronous=FULL` WAL database - and the serialized size of the record. It runs
against a throwaway journal in a temp directory, not against `~/.local/share/agent`, and it
writes nothing anywhere else.

The run shape matters to the number: `open_workers` and the effects cursor are derived by
querying the run, so a long run costs more to snapshot than a short one. `--events` sets how
much history each snapshot is taken over.

    .venv/bin/python scripts/bench_checkpoints.py [--events 200] [--repeat 30]
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, "src")

from agentd.journal.checkpoints import TRIGGERS, Checkpointer  # noqa: E402
from agentd.journal.ledger import EffectLedger  # noqa: E402
from agentd.journal.runtime import RunJournal  # noqa: E402
from agentd.journal.store import JournalStore  # noqa: E402
from agentd.journal.writer import JournalWriter  # noqa: E402


def _seed(rj: RunJournal, events: int) -> None:
    rj.emit(
        "agent_started",
        {
            "session_id": "bench", "turn_id": "bench-turn", "role": "main", "actor": "main",
            "origin": "interactive", "channel": "cli", "autonomy": "act", "model": "bench",
            "max_steps": 8, "input_chars": 4, "input_preview": "bench",
            "parent_turn_id": None,
        },
    )
    for i in range(events):
        rj.emit(
            "message_appended",
            {
                "role": "assistant", "actor": "main", "chars": 40,
                "preview": f"a line of assistant output, number {i}", "trust": "trusted",
            },
            step_id=f"s{i}",
        )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--events", type=int, default=200, help="journal events to snapshot over")
    ap.add_argument("--repeat", type=int, default=30, help="checkpoints per trigger")
    ap.add_argument("--effects", type=int, default=5, help="open ledger rows in the run")
    # How much of a checkpoint is the fsync rather than the derivation: run it once at FULL
    # (what ships) and once at OFF, and the difference is what durability costs here.
    ap.add_argument("--synchronous", default="FULL", choices=["FULL", "NORMAL", "OFF"])
    args = ap.parse_args()

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "journal.db"
        writer = JournalWriter(JournalStore(path, synchronous=args.synchronous))
        try:
            rj = RunJournal(writer, "bench-run")
            _seed(rj, args.events)
            ledger = EffectLedger(writer)
            for i in range(args.effects):
                ledger.intend(
                    run_id="bench-run", step_id=f"s{i}", tool="sends_mail",
                    effect_class="unsafe_write", args={"to": "someone@example.com", "i": i},
                ).dispatched()
            checkpointer = Checkpointer(writer)

            print(f"{args.events} events, {args.effects} open effects, "
                  f"{args.repeat} checkpoints per trigger, "
                  f"synchronous={args.synchronous}, WAL")
            print(f"{'trigger':<16}{'median ms':>11}{'p90 ms':>9}{'max ms':>9}{'bytes':>8}")
            for trigger in TRIGGERS:
                latencies: list[float] = []
                size = 0
                for _ in range(args.repeat):
                    started = time.perf_counter()
                    cp = checkpointer.write("bench-run", trigger=trigger)
                    latencies.append((time.perf_counter() - started) * 1000)
                    size = len(
                        json.dumps(cp.as_dict(), separators=(",", ":")).encode()
                    )
                    # Each checkpoint adds its own announcement to the run, so the next one
                    # covers one more event. That is the real shape of a run, not an
                    # artefact: boundaries accumulate.
                latencies.sort()
                p90 = latencies[int(len(latencies) * 0.9) - 1]
                print(
                    f"{trigger:<16}{statistics.median(latencies):>11.2f}{p90:>9.2f}"
                    f"{max(latencies):>9.2f}{size:>8}"
                )
            on_disk = path.stat().st_size
            rows = writer.store.query("SELECT COUNT(*) AS n FROM checkpoint")[0]["n"]
            print(f"\n{rows} checkpoint rows; journal.db is {on_disk / 1024:.0f} KiB")
        finally:
            writer.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
