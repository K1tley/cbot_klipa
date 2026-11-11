"""Readable daily overview of what ClipBot chose and why.

Each run writes:
  * tables to the GitHub run's Summary page (Actions -> open the run -> Summary)
  * the same tables as a Google Sheet on Drive: ClipBot/Reports/<date>/<name>
"""
from __future__ import annotations

import csv
import io
import logging
import os
import tempfile
from pathlib import Path

log = logging.getLogger(__name__)


class Report:
    def __init__(self, title: str) -> None:
        self.title = title
        self.blocks: list[tuple[str, object]] = []  # ("text", str) | ("table", (heading, headers, rows))

    def text(self, line: str) -> None:
        self.blocks.append(("text", line))

    def table(self, heading: str, headers: list[str], rows: list[list]) -> None:
        self.blocks.append(("table", (heading, headers, rows)))

    # ---------------------------------------------------------------- output formats
    def markdown(self) -> str:
        out = [f"## {self.title}", ""]
        for kind, val in self.blocks:
            if kind == "text":
                out += [str(val), ""]
                continue
            heading, headers, rows = val
            out += [f"### {heading}", ""]
            if not rows:
                out += ["_nothing_", ""]
                continue
            cell = lambda v: str(v).replace("|", "\\|").replace("\n", " ")  # noqa: E731
            out.append("| " + " | ".join(headers) + " |")
            out.append("|" + "---|" * len(headers))
            out += ["| " + " | ".join(cell(v) for v in r) + " |" for r in rows]
            out.append("")
        return "\n".join(out)

    def csv_text(self) -> str:
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow([self.title])
        for kind, val in self.blocks:
            w.writerow([])
            if kind == "text":
                w.writerow([val])
                continue
            heading, headers, rows = val
            w.writerow([heading])
            w.writerow(headers)
            w.writerows(rows)
        return buf.getvalue()

    # ---------------------------------------------------------------- publish
    def publish(self, drive=None, day_label: str | None = None, name: str | None = None) -> None:
        summary = os.environ.get("GITHUB_STEP_SUMMARY")
        if summary:
            try:
                with open(summary, "a", encoding="utf-8") as fh:
                    fh.write(self.markdown() + "\n")
            except OSError as e:
                log.warning("Could not write the GitHub summary: %s", e)
        else:
            log.info("\n%s", self.markdown())
        if drive is not None and day_label and name:
            try:
                with tempfile.TemporaryDirectory() as tmp:
                    path = Path(tmp) / f"{name}.csv"
                    path.write_text(self.csv_text(), encoding="utf-8")
                    folder = drive.folder_path("Reports", day_label)
                    drive.upload_sheet(path, name, folder)
                log.info("Report saved to Drive: Reports/%s/%s", day_label, name)
            except Exception as e:  # noqa: BLE001 - a report must never break the run
                log.warning("Could not save the report to Drive: %s", e)
