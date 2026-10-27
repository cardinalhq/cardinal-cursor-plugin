# Copyright (c) 2025-2026 CardinalHQ, Inc. All rights reserved.
"""Checks preview: the teaching-label ledger, accepted checks, and range-run reads.

One implementation behind two front ends:
  - the cardinal-behavior MCP tools (server.py), which the checks skill uses on
    every runtime: the stdio server runs outside the agent's sandbox, so it can
    write the ledger under the user's home;
  - the `cardinal-checks` CLI (cardinal_core.checks_cli), kept for Claude
    Code's Monitor (`cardinal-checks range watch`) and for people.

Standard library only (this file ships in lib/behavior next to server.py and is
loaded by the CLI from there).

State:
  ~/.<runtime>/cardinal/checks/drafts/<draft>.json   the teaching examples of one
      check being built (per runtime: removed with that runtime's identity caches
      on disconnect)
  ~/.cardinal/behavior-executions/                    the behavior MCP's output
      (programs/, compilation/, ranges/<id>/): shared by every runtime

The guided flow (CORE.md steps 6-8) separates two things a user might
conflate: whether a trace's evidence is sufficient under the contract
(mechanical, decided before anything is shown) and how the contract applies to
a trace with sufficient evidence (the user's call). A label cannot create
evidence that is not in the trace, so an insufficient example is UNKNOWN by
construction and no correction can relabel it. Labels are fixed before the
first test_behavior call: the regression ledger refuses a changed expected
verdict for a tested trace (compilation/regression.py reserve), so a later
correction needs a revised contract, i.e. a new draft.
"""
from __future__ import annotations

from collections import Counter
import json
import os
from pathlib import Path
import re
import time

RUNTIMES = ('claude', 'codex', 'cursor', 'gemini', 'copilot')
VERDICTS = ('MATCH', 'NON_MATCH', 'UNKNOWN', 'NOT_APPLICABLE')
DRAFT_RE = re.compile(r'^[A-Za-z0-9_-]{1,64}$')
TRACE_RE = re.compile(r'^[a-f0-9]{32}$')
MAX_EXAMPLES = 8


def home_dir(home=None) -> Path:
    if home is not None:
        return Path(home)
    return Path(os.environ.get('HOME') or str(Path.home()))


def state_dir(runtime: str = 'claude', home=None) -> Path:
    """~/.<runtime>/cardinal/checks (AgentPaths(runtime).runtime_dir / "checks")."""
    if runtime not in RUNTIMES:
        raise ValueError('runtime must be one of ' + ', '.join(RUNTIMES))
    return home_dir(home) / f'.{runtime}' / 'cardinal' / 'checks'


# --- teaching-example ledger ----------------------------------------------

class LabelRefused(ValueError):
    """A label or correction the evidence or the draft's state does not allow."""


def draft_path(draft: str, state: Path) -> Path:
    if not isinstance(draft, str) or not DRAFT_RE.match(draft):
        raise ValueError('draft must match [A-Za-z0-9_-]{1,64}')
    return Path(state) / 'drafts' / f'{draft}.json'


def read_draft(draft: str, state: Path) -> dict:
    path = draft_path(draft, state)
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        data = None
    if not isinstance(data, dict):
        return {'draft': draft, 'examples': {}, 'locked': False}
    data.setdefault('examples', {})
    data.setdefault('locked', False)
    return data


def private_write(path: Path, text: str, private_dirs=()) -> None:
    """Write text atomically as a 0600 file (compilation.files.private_json's
    discipline, standard library only: the CLI loads this file on its own).
    `private_dirs` (the file's own directories, e.g. the checks state dir and
    drafts/) are created or tightened to 0700."""
    import tempfile
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    for directory in private_dirs:
        Path(directory).mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(directory, 0o700)
    fd, temporary = tempfile.mkstemp(prefix='.' + path.name + '-', dir=str(path.parent))
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def write_draft(data: dict, state: Path) -> None:
    path = draft_path(data['draft'], state)
    private_write(path, json.dumps(data, indent=2, sort_keys=True) + '\n', (Path(state), path.parent))


def _check_trace(trace) -> None:
    if not isinstance(trace, str) or not TRACE_RE.match(trace):
        raise ValueError('trace must be a 32-character lowercase hex trace id')


def _check_verdict(verdict) -> None:
    if verdict not in VERDICTS:
        raise ValueError('verdict must be one of ' + ', '.join(VERDICTS))


def _check_unlocked(data: dict) -> None:
    if data.get('locked'):
        raise LabelRefused(
            'these labels were already used in a test; a changed label for a tested trace is refused '
            'by the regression suite. Revise the contract (a new draft) instead.')


def set_example(draft: str, trace: str, sufficient: bool, proposed: str, reason: str,
                missing=None, *, state: Path) -> dict:
    """Record a candidate with its sufficiency result and the agent's proposed label.

    An insufficient candidate must be proposed as UNKNOWN and name what is
    missing; it is approved as UNKNOWN at once (there is nothing for the user
    to decide)."""
    _check_trace(trace)
    _check_verdict(proposed)
    data = read_draft(draft, state)
    _check_unlocked(data)
    if len(data['examples']) >= MAX_EXAMPLES and trace not in data['examples']:
        raise LabelRefused('a teaching batch holds at most 8 traces')
    if not sufficient:
        if proposed != 'UNKNOWN':
            raise LabelRefused('evidence is insufficient, so this example is UNKNOWN by construction')
        if not (missing or '').strip():
            raise ValueError('name the missing evidence for an insufficient example')
    if not (reason or '').strip():
        raise ValueError('give the reason for the proposed classification')
    example = {'trace_id': trace, 'sufficient': bool(sufficient), 'missing': (missing or '').strip() or None,
               'proposed': proposed, 'reason': reason.strip(), 'label': None, 'approved': False,
               'corrected': False}
    if not sufficient:
        example.update(label='UNKNOWN', approved=True)
    data['examples'][trace] = example
    write_draft(data, state)
    return example


APPROVED_VIA = ('mcp', 'cli')
MAX_USER_REPLY = 2000


def approve(draft: str, trace: str, label=None, *, state: Path, approved_via: str = 'cli',
            user_reply=None) -> dict:
    """Approve the proposed label, or record the user's correction.

    Records provenance with the label: approved_via (`mcp`: the
    approve_check_example tool; `cli`: cardinal-checks example approve),
    approved_at (epoch seconds) and, when given, user_reply (the user's own
    words, quoted; at most MAX_USER_REPLY characters).

    Refused: a correction away from UNKNOWN on an insufficient example (the
    label cannot create evidence), and any change once the labels were used in
    a test."""
    if approved_via not in APPROVED_VIA:
        raise ValueError('approved_via must be one of ' + ', '.join(APPROVED_VIA))
    if user_reply is not None and not isinstance(user_reply, str):
        raise ValueError('user_reply must be a string')
    _check_trace(trace)
    data = read_draft(draft, state)
    _check_unlocked(data)
    example = data['examples'].get(trace)
    if example is None:
        raise ValueError(f'no candidate {trace} in draft {draft}; record it first')
    final = label or example['proposed']
    _check_verdict(final)
    if not example['sufficient'] and final != 'UNKNOWN':
        raise LabelRefused(
            f"refused: {final} needs evidence this trace does not have ({example['missing']}); "
            'it stays UNKNOWN, or swap in another candidate')
    example.update(label=final, approved=True, corrected=final != example['proposed'],
                   approved_via=approved_via, approved_at=time.time())
    reply = (user_reply or '').strip()[:MAX_USER_REPLY]
    if reply:
        example['user_reply'] = reply
    else:
        example.pop('user_reply', None)
    write_draft(data, state)
    return example


def remove_example(draft: str, trace: str, *, state: Path) -> bool:
    """Drop a candidate (e.g. a mistyped trace id) before the labels are locked."""
    _check_trace(trace)
    data = read_draft(draft, state)
    _check_unlocked(data)
    removed = data['examples'].pop(trace, None) is not None
    if removed:
        write_draft(data, state)
    return removed


def expected_verdicts(draft: str, *, state: Path, lock: bool = True) -> dict:
    """{trace_id: label} for test_behavior's expected_verdicts.

    Every example must be approved. Locks the draft: from now on its labels
    are the regression suite's gold."""
    data = read_draft(draft, state)
    examples = data['examples']
    if not examples:
        raise LabelRefused('no examples recorded for this draft')
    pending = sorted(t for t, e in examples.items() if not e.get('approved'))
    if pending:
        raise LabelRefused('approve every example before the first test; pending: ' + ', '.join(pending))
    if lock and not data.get('locked'):
        data['locked'] = True
        data['locked_at'] = time.time()
        write_draft(data, state)
    return {t: e['label'] for t, e in sorted(examples.items())}


# --- accepted checks (reuse) ------------------------------------------------

def behavior_output_dir(environ=None, home=None) -> Path:
    env = os.environ if environ is None else environ
    return Path(env.get('CARDINAL_BEHAVIOR_OUTPUT_DIR')
                or str(home_dir(home) / '.cardinal' / 'behavior-executions')).expanduser()


def accepted_checks(output=None, service=None) -> list:
    """Checks accepted through the behavior MCP, newest first, read from its
    retained programs: description (the contract), service, the accepted
    incumbent version and when it was accepted. Read-only."""
    output = Path(output) if output is not None else behavior_output_dir()
    found: dict = {}
    try:
        programs = list((output / 'programs').glob('*.json'))
    except OSError:
        programs = []
    for path in programs:
        try:
            program = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if not isinstance(program, dict):
            continue
        context = program.get('compilation_context') or {}
        if not program.get('acceptance_id') or not context:
            continue
        scope = program.get('compilation_scope')
        incumbent = None
        if scope:
            try:
                incumbent = json.loads((output / 'compilation' / scope / 'regression.json').read_text()).get('incumbent')
            except (OSError, ValueError, AttributeError):
                incumbent = None
        version = program.get('diagnostic_version')
        if incumbent and version != incumbent:
            continue
        try:
            mtime = path.stat().st_mtime
        except OSError:
            mtime = 0
        key = scope or version
        if key in found and found[key]['accepted_at'] >= mtime:
            continue
        found[key] = {'description': context.get('description'), 'service_name': context.get('service_name'),
                      'diagnostic_version': version, 'acceptance_id': program.get('acceptance_id'),
                      'compilation_scope': scope, 'accepted_at': mtime}
    checks = sorted(found.values(), key=lambda c: c['accepted_at'], reverse=True)
    if service:
        checks = [c for c in checks if c['service_name'] == service]
    return checks


# --- background range runs (written by range_runner.py) ---------------------

RANGE_TERMINAL = ('needs_extents', 'completed', 'failed', 'stalled')
RANGE_ID_RE = re.compile(r'^[a-f0-9]{32}$')
# A worker that holds no lock and wrote nothing for this long has stopped.
RANGE_IDLE_S = 30.0
MAX_WAIT_S = 30
# get_behavior_range returns at most this many events per call: a first poll
# (after_event 0) of a long segment returns its LAST ones, a later one the
# next ones from after_event, so one MCP result stays small.
RANGE_POLL_MAX_EVENTS = 50


def range_dir(range_id: str, output=None) -> Path:
    if not isinstance(range_id, str) or not RANGE_ID_RE.match(range_id):
        raise ValueError('range id must be 32 lowercase hex characters')
    return (Path(output) if output is not None else behavior_output_dir()) / 'ranges' / range_id


def range_worker_running(directory: Path) -> bool:
    import fcntl
    try:
        with (Path(directory) / 'lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(lock, fcntl.LOCK_UN)
            return False
    except BlockingIOError:
        return True
    except OSError:
        return False


def _counts(counts) -> str:
    if not isinstance(counts, dict):
        return 'counts not reported'
    order = ('MATCH', 'NON_MATCH', 'UNKNOWN', 'NOT_APPLICABLE', 'ERROR')
    return ' · '.join(f'{k} {counts.get(k, 0)}' for k in order)


def _window(event) -> str:
    w = event.get('window') or ['?', '?']
    return f'{w[0]}–{w[1]}'


def format_range_event(event: dict, events_file) -> str | None:
    """One notification line per event worth acting on; None for the rest."""
    kind = event.get('event')
    if kind == 'started':
        return (f"started: {event.get('population')} {event.get('start')}–{event.get('end')}, "
                f"{event.get('windows')} window(s){' (judge-based: experimental)' if event.get('judge_based') else ''}")
    if kind in ('window_done', 'whole_done'):
        progress = event.get('progress') or {}
        where = 'whole-trace window' if kind == 'whole_done' else 'window'
        tail = (f" ({progress.get('windows_done')} done, {progress.get('windows_left')} left)"
                if kind == 'window_done' else '')
        return f"{where} {_window(event)} {event.get('status', '').lower()}: {_counts(event.get('verdicts'))}{tail}"
    if kind == 'window_split':
        return f"window {_window(event)} split in two: {event.get('reason')}"
    if kind in ('window_rejected', 'whole_rejected', 'window_unreadable'):
        return f"{kind.replace('_', ' ')} {_window(event)}: {event.get('error') or 'results could not be read'}"
    if kind == 'read_retry':
        then = 'giving up on this execution' if event.get('final') else 'retrying'
        return f"results read for {_window(event)} failed ({event.get('failures')}x): {event.get('error')}; {then}"
    if kind == 'needs_extents':
        return (f"needs_extents: {event.get('multi_window_trace_count', 0)} trace(s) seen in several windows, "
                f"{len(event.get('boundaries') or [])} boundaries. Read the last line of {events_file}, measure "
                'span extents with the query tools, then call continue_behavior_range.')
    if kind == 'resumed':
        return 'resumed: the range worker restarted from its saved state'
    if kind == 'extents_received':
        return (f"extents received for {event.get('traces')} trace(s); "
                f"{event.get('whole_trace_windows')} whole-trace window(s) to run")
    if kind == 'completed':
        return (f"completed: {event.get('population')} traces · {_counts(event.get('counts'))} · flagged "
                f"{event.get('flagged')} · edge traces {event.get('edge_traces')} · conflicts {event.get('conflicts')}"
                f" · coverage holes {event.get('coverage_holes')} · judge calls {event.get('judge_calls')}"
                f"{' · EXPERIMENTAL' if event.get('experimental') else ''}. Read {event.get('summary_file')}")
    if kind == 'failed':
        return f"failed: {event.get('error')}"
    if kind == 'stalled':
        return f"stalled: {event.get('error')}. Call continue_behavior_range to restart the worker."
    return None


def read_events(events_file: Path) -> list:
    """Every complete line of events.jsonl, parsed (None for a line that does
    not parse, so indexes stay stable)."""
    try:
        data = Path(events_file).read_bytes()
    except OSError:
        return []
    complete = data[: data.rfind(b'\n') + 1] if b'\n' in data else b''
    events = []
    for raw in complete.decode('utf-8', 'replace').splitlines():
        try:
            event = json.loads(raw)
        except ValueError:
            event = None
        events.append(event if isinstance(event, dict) else None)
    return events


def current_segment(events: list) -> int:
    """Index of the first event of the current segment: the events after the
    last terminal event that later events superseded (continue_behavior_range
    writes `resumed` or `extents_received` before it returns), so a reader
    that starts after a continue never stops at an old needs_extents."""
    superseded = [i for i, e in enumerate(events[:-1]) if e and e.get('event') in RANGE_TERMINAL]
    return superseded[-1] + 1 if superseded else 0


def watch_range(range_id: str, out, poll_s: float = 1.0, idle_s: float = RANGE_IDLE_S, output=None,
                sleep=None) -> int:
    """Stream a range run's events as lines until a terminal event.

    Starts at the current segment (current_segment), then follows the file.
    Exits 0 on a terminal event that is the latest event; prints a stalled
    line and exits 0 when the worker is gone with nothing new for idle_s.
    Exits 2 for an unknown range."""
    sleep = sleep or time.sleep
    directory = range_dir(range_id, output)
    events_file = directory / 'events.jsonl'
    if not directory.is_dir():
        print(f'cardinal-checks: no range run {range_id} under {directory.parent}', file=out)
        return 2
    offset, idle, first = 0, 0.0, True
    while True:
        try:
            with events_file.open('rb') as handle:
                handle.seek(offset)
                chunk = handle.read()
        except OSError:
            chunk = b''
        complete = chunk[: chunk.rfind(b'\n') + 1] if b'\n' in chunk else b''
        offset += len(complete)
        events = []
        for raw in complete.decode('utf-8', 'replace').splitlines():
            try:
                events.append(json.loads(raw))
            except ValueError:
                continue
        if first:
            first = False
            events = events[current_segment(events):]
        for index, event in enumerate(events):
            line = format_range_event(event, events_file)
            if line:
                print(line, file=out, flush=True)
            if event.get('event') in RANGE_TERMINAL and index == len(events) - 1:
                return 0
        if complete:
            idle = 0.0
        else:
            idle += poll_s
            if idle >= idle_s and not range_worker_running(directory):
                print(f'stalled: the range worker is not running and wrote nothing for {int(idle)}s. '
                      'Call continue_behavior_range to restart it.', file=out, flush=True)
                return 0
        sleep(poll_s)


def range_status(range_id: str, output=None) -> dict:
    directory = range_dir(range_id, output)
    state = json.loads((directory / 'state.json').read_text())
    out = {'range_id': range_id, 'phase': state.get('phase'), 'population': state.get('population'),
           'start': state.get('start'), 'end': state.get('end'),
           'windows_done': len(state.get('windows', [])), 'windows_left': len(state.get('pending', [])),
           'worker_running': range_worker_running(directory), 'events_file': str(directory / 'events.jsonl')}
    if state.get('error'):
        out['error'] = state['error']
    if (directory / 'summary.json').is_file():
        out['summary_file'] = str(directory / 'summary.json')
        out['result_file'] = str(directory / 'result.json')
    return out


NEXT_ACTIONS = {
    'needs_extents': ('Measure, with the spans query tools, the span extent (earliest start, latest end, every '
                      'service) of each trace in needs_extents.multi_window_traces and of each trace of this '
                      'population with spans on both sides of a boundary. Then call continue_behavior_range '
                      'with trace_extents {trace_id: {start, end}}, or {} when none cross, and poll again '
                      'from next_event.'),
    'stalled': 'Call continue_behavior_range to restart the worker from its saved state, then poll again from next_event.',
    'failed': 'Report the error verbatim and stop.',
    'completed': 'Read summary (result_file holds every raw per-window verdict plus the canonical column).',
}


def _idle_for(directory: Path, events: list, now: float) -> float:
    latest = next((e for e in reversed(events) if e), None)
    at = latest.get('at') if latest else None
    if not isinstance(at, (int, float)):
        try:
            at = (directory / 'events.jsonl').stat().st_mtime
        except OSError:
            try:
                at = (directory / 'state.json').stat().st_mtime
            except OSError:
                return 0.0
    return max(0.0, now - at)


def range_poll(range_id: str, after_event: int = 0, wait_seconds: float = 0, output=None,
               idle_s: float = RANGE_IDLE_S, sleep=None, clock=None,
               max_events: int = RANGE_POLL_MAX_EVENTS) -> dict:
    """The polling twin of watch_range, for the get_behavior_range MCP tool.

    Returns the events after `after_event` (an index from an earlier call's
    next_event; 0 = from the current segment) as one line each, waiting up to
    wait_seconds (at most MAX_WAIT_S) for at least one new event. `done` is
    true while the latest event is needs_extents, completed, failed or
    stalled (a worker with no lock that wrote nothing for idle_s): the caller
    acts on it (next_action) instead of polling again. At most max_events
    events per call: the latest ones of a long segment on a first poll
    (omitted_events, segment_counts and a note on paging), else the next
    ones from after_event (more_events; next_event pages on). Read-only."""
    sleep = sleep or time.sleep
    clock = clock or time.time
    if type(after_event) is not int or after_event < 0:
        raise ValueError('after_event must be a nonnegative integer')
    try:
        wait = max(0.0, min(float(MAX_WAIT_S), float(wait_seconds or 0)))
    except (TypeError, ValueError):
        raise ValueError('wait_seconds must be a number') from None
    directory = range_dir(range_id, output)
    if not directory.is_dir():
        raise ValueError(f'no range run {range_id}')
    events_file = directory / 'events.jsonl'
    deadline = clock() + wait
    while True:
        events = read_events(events_file)
        start = current_segment(events) if after_event == 0 else min(after_event, len(events))
        latest = next((e for e in reversed(events) if e), None)
        latest_kind = latest.get('event') if latest else None
        done = latest_kind in RANGE_TERMINAL
        stalled = False
        if not done and not range_worker_running(directory) and _idle_for(directory, events, clock()) >= idle_s:
            done = stalled = True
        if start < len(events) or done or clock() >= deadline:
            break
        sleep(min(1.0, max(0.0, deadline - clock())))
    end = len(events)
    page = None
    if end - start > max_events:
        if after_event == 0:
            # The first look at a long segment: its latest events, with counts.
            page = {'omitted_events': end - max_events - start,
                    'segment_counts': dict(Counter(e.get('event') for e in events[start:] if e))}
            page['note'] = (f"Showing the last {max_events} of {end - start} events in this segment; the "
                            f"{page['omitted_events']} earlier ones are counted by kind in segment_counts. To read "
                            f"them, call get_behavior_range with after_event {max(start, 1)} (after_event 0 always "
                            f"returns the latest {max_events}) and follow next_event, {max_events} per call; "
                            f"{events_file} holds every event, one JSON object per line.")
            start = end - max_events
        else:
            end = start + max_events
            page = {'more_events': len(events) - end,
                    'note': f'{len(events) - end} more events after these: call again with after_event {end}.'}
    lines = [line for line in (format_range_event(e, events_file) for e in events[start:end] if e) if line]
    if stalled and end == len(events):
        lines.append('stalled: the range worker is not running and wrote nothing for '
                     f'{int(idle_s)}s. Call continue_behavior_range to restart it.')
    if stalled:
        latest_kind = 'stalled'
    out = {'range_id': range_id, 'events': lines, 'next_event': end, 'latest_event': latest_kind,
           'done': done, 'status': range_status(range_id, output)}
    if page:
        out.update(page)
    if done:
        out['next_action'] = NEXT_ACTIONS.get(latest_kind, NEXT_ACTIONS['stalled'])
    else:
        out['next_action'] = (f'The run continues. Call get_behavior_range again with after_event {end} '
                              'and wait_seconds 20; keep the user posted in a line or two as windows finish.')
    if latest_kind == 'needs_extents' and latest:
        out['needs_extents'] = {k: latest.get(k) for k in ('multi_window_traces', 'multi_window_trace_count',
                                                            'boundaries', 'evaluated_traces', 'next_action')}
    if latest_kind == 'completed':
        try:
            out['summary'] = json.loads((directory / 'summary.json').read_text())
        except (OSError, ValueError):
            pass
    return out
