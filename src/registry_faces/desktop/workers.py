"""QThread workers for long-running operations.

Three workers (build, ingest, sync-photos). Each captures stdout/stderr into
a Qt signal stream so the UI's log panel updates live without polling.

Workers MUST NOT touch widgets directly — they emit signals and the UI's
main thread handles updates in slots.
"""

from __future__ import annotations

import sys
import traceback
from pathlib import Path

from PySide6.QtCore import QThread, Signal


class _SignalingStream:
    """File-like object that emits a Qt signal on each completed line."""

    def __init__(self, signal: Signal) -> None:
        self._signal = signal
        self._buffer = ""

    def write(self, s: str) -> int:
        if not isinstance(s, str):
            s = str(s)
        self._buffer += s
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            self._signal.emit(line)
        return len(s)

    def flush(self) -> None:
        if self._buffer:
            self._signal.emit(self._buffer)
            self._buffer = ""


class _StdoutCaptureWorker(QThread):
    """Shared base — provides stdout/stderr capture into a log signal."""

    log_line = Signal(str)
    finished_with_report = Signal(bool, str)  # (success, message)
    progress = Signal(int, int)  # (done, total). total == 0 means indeterminate.

    def _do_work(self) -> str:
        raise NotImplementedError

    def run(self) -> None:  # type: ignore[override]
        stream = _SignalingStream(self.log_line)
        old_out, old_err = sys.stdout, sys.stderr
        sys.stdout = stream  # type: ignore[assignment]
        sys.stderr = stream  # type: ignore[assignment]
        try:
            report = self._do_work()
            stream.flush()
            self.finished_with_report.emit(True, report)
        except Exception as e:
            stream.flush()
            tb = traceback.format_exc()
            self.finished_with_report.emit(False, f"{type(e).__name__}: {e}\n\n{tb}")
        finally:
            sys.stdout, sys.stderr = old_out, old_err


# ---------------------------------------------------------------------------
# Build (agent)


class BuildWorker(_StdoutCaptureWorker):
    def __init__(
        self,
        url: str,
        name: str,
        jurisdiction: str,
        provider: str | None,
        model: str | None,
        mode: str,
    ) -> None:
        super().__init__()
        self.url = url
        self.name = name
        self.jurisdiction = jurisdiction
        self.provider = provider
        self.model = model
        self.mode = mode

    def _do_work(self) -> str:
        from ..agent.builder import build_adapter_from_url, resolve_mode

        resolved = resolve_mode(self.mode, self.name)  # type: ignore[arg-type]
        print(
            f"Adapter {self.name!r} from {self.url} "
            f"(provider={self.provider or 'default'}, mode={self.mode} -> {resolved})"
        )
        return build_adapter_from_url(
            self.url,
            self.name,
            self.jurisdiction,
            provider=self.provider,
            model=self.model,
            mode=self.mode,  # type: ignore[arg-type]
        )


# ---------------------------------------------------------------------------
# Ingest


class IngestWorker(_StdoutCaptureWorker):
    def __init__(self, name: str, registry_root: Path) -> None:
        super().__init__()
        self.name = name
        self.registry_root = registry_root

    def _do_work(self) -> str:
        from ..cli import _load_adapter
        from ..store import FileStore

        adapter = _load_adapter(self.name)
        count = 0
        with FileStore(self.registry_root) as store:
            for record, photo_refs in adapter.run():
                store.upsert(record, photos=photo_refs)
                count += 1
                if count % 25 == 0:
                    # total=0 = indeterminate (we don't know how many records the source has)
                    self.progress.emit(count, 0)
                    print(f"  ingested {count} ...")
            self.progress.emit(count, count)
        return f"Done. Ingested {count} records."


# ---------------------------------------------------------------------------
# Sync photos


class SyncPhotosWorker(_StdoutCaptureWorker):
    def __init__(
        self,
        jurisdiction: str | None,
        registry_root: Path,
        refresh: bool = False,
    ) -> None:
        super().__init__()
        self.jurisdiction = jurisdiction
        self.registry_root = registry_root
        self.refresh = refresh

    def _do_work(self) -> str:
        from ..photos import sync_photos

        records_root = self.registry_root / "records"
        if not records_root.exists():
            return "No records yet. Run Ingest first."
        summary = sync_photos(
            records_root,
            jurisdiction=self.jurisdiction,
            refresh=self.refresh,
            progress_callback=self.progress.emit,
        )
        lines = [
            f"Downloaded: {summary['downloaded']}",
            f"Skipped: {summary['skipped']}",
            f"Failed: {len(summary['failed'])}",
        ]
        for url, err in summary["failed"][:20]:
            lines.append(f"  FAIL {url}: {err}")
        if len(summary["failed"]) > 20:
            lines.append(f"  ... and {len(summary['failed']) - 20} more failures")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Verify


class VerifyWorker(_StdoutCaptureWorker):
    def __init__(self, registry_root: Path) -> None:
        super().__init__()
        self.registry_root = registry_root

    def _do_work(self) -> str:
        from ..photos import iter_person_dirs, verify_person_photos

        records_root = self.registry_root / "records"
        if not records_root.exists():
            return "No records yet."
        issues: list[str] = []
        for person_dir in iter_person_dirs(records_root):
            for issue in verify_person_photos(person_dir):
                issues.append(issue)
                print(issue)
        if not issues:
            return "All photo manifests are consistent."
        return f"{len(issues)} issue(s) found."
