# Copyright (c) 2025-2026 CardinalHQ, Inc. All rights reserved.
"""Background range runs: a detached worker drives the windows, Claude listens.

execute_behavior_range creates ranges/<id>/ and starts this module as a
detached process, then returns at once. The worker runs the windows one at a
time through the existing Behavior.start/poll paths (range_run.py has the
window planning and edge rules) and writes everything to local files:

  ranges/<id>/state.json     the run's durable state (resumable)
  ranges/<id>/events.jsonl   one JSON event per line, appended as it happens:
                             the stream the agent follows (Claude Code:
                             `cardinal-checks range watch <id>` under its
                             Monitor; other runtimes: the get_behavior_range
                             MCP tool, polled)
  ranges/<id>/summary.json   the bounded result Claude reads first
  ranges/<id>/result.json    every raw per-window verdict plus the canonical
                             column, read on demand
  ranges/<id>/worker.log     the worker's stderr

The worker stops at three points and says so in a terminal event:
needs_extents (span extents for edge traces are needed from the query tools;
continue_behavior_range supplies them and restarts it), completed and failed.
A worker that dies is restarted by continue_behavior_range from state.json.
A slow results read never blocks Claude: the worker retries with back-off and,
after MAX_POLL_FAILURES, splits the window (or reports it unreadable).
"""
from __future__ import annotations

import argparse
from collections import Counter
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import range_run
from compilation.files import private_json
from compilation.hosted import APIRejected

MAX_POLL_FAILURES = 3
POLL_BACKOFF_S = 10
LIST_LIMIT = 100
TERMINAL_EVENTS = ('needs_extents', 'completed', 'failed')
EXPERIMENTAL = 'Judge-based check: experimental. Judge verdicts can vary between runs on identical evidence.'


def range_dir(output: Path, range_id: str) -> Path:
    if not isinstance(range_id, str) or len(range_id) != 32 or any(c not in '0123456789abcdef' for c in range_id):
        raise ValueError('invalid range reference')
    return Path(output) / 'ranges' / range_id


class RangeRunner:
    def __init__(self, behavior, range_id):
        self.behavior = behavior
        self.dir = range_dir(behavior.output, range_id)
        self.range_id = range_id

    # --- files ---------------------------------------------------------------

    @property
    def state_file(self):
        return self.dir / 'state.json'

    def read_state(self):
        state = json.loads(self.state_file.read_text())
        if state.get('range_id') != self.range_id:
            raise ValueError('range receipt identity mismatch')
        return state

    def save(self, state):
        private_json(self.state_file, state)

    def emit(self, event, **fields):
        line = json.dumps({'event': event, 'at': time.time(), 'range_id': self.range_id, **fields},
                          ensure_ascii=False, separators=(',', ':'))
        self.dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(self.dir / 'events.jsonl', os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(fd, (line + '\n').encode('utf-8'))
        finally:
            os.close(fd)

    def last_event(self):
        try:
            lines = (self.dir / 'events.jsonl').read_text().splitlines()
        except OSError:
            return None
        for line in reversed(lines):
            try:
                return json.loads(line)
            except ValueError:
                continue
        return None

    # --- creation and control ------------------------------------------------

    @classmethod
    def create(cls, behavior, accepted_behavior, population, start, end, judge_based, max_windows):
        windows = range_run.split_windows(range_run.parse_time(start), range_run.parse_time(end))
        if len(windows) > max_windows:
            raise ValueError(f'range covers {len(windows)} one-hour windows; at most {max_windows} are allowed')
        runner = cls(behavior, os.urandom(16).hex())
        state = {'range_id': runner.range_id, 'accepted_behavior': accepted_behavior, 'population': population,
                 'start': range_run.format_time(windows[0][0]), 'end': range_run.format_time(windows[-1][1]),
                 'judge_based': judge_based, 'phase': 'windows', 'error': None, 'halvings': 0,
                 'pending': [{'window': w} for w in windows], 'current': None, 'windows': [], 'abandoned': [],
                 'whole_pending': [], 'whole': [], 'extents': {}, 'created_at': time.time()}
        runner.dir.mkdir(parents=True, exist_ok=False, mode=0o700)
        runner.save(state)
        runner.emit('started', population=population, start=state['start'], end=state['end'],
                    windows=len(windows), judge_based=judge_based)
        return runner

    def supply_extents(self, trace_extents):
        state = self.read_state()
        if state['phase'] not in ('windows', 'edges'):
            raise ValueError('trace_extents are accepted until the edge phase ends')
        state['extents'].update(range_run.parse_extents(trace_extents))
        if state['phase'] == 'edges':
            self.plan_whole(state)
            self.emit('extents_received', traces=len(state['extents']),
                      whole_trace_windows=len(state['whole_pending']))
        self.save(state)

    def worker_running(self):
        """True while a worker holds the range lock."""
        try:
            with (self.dir / 'lock').open('a') as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(lock, fcntl.LOCK_UN)
                return False
        except BlockingIOError:
            return True
        except OSError:
            return False

    def spawn(self, config_path=None):
        """Start the detached worker unless one runs already. False if running."""
        if self.worker_running():
            return False
        command = [sys.executable, str(Path(__file__).resolve()), '--range', self.range_id,
                   '--output', str(self.behavior.output)]
        if config_path:
            command += ['--config', str(config_path)]
        runtime = self.behavior.config.get('runtime')
        if runtime:
            # The worker resolves the same runtime's connection (never a key on argv).
            command += ['--runtime', str(runtime)]
        log = open(self.dir / 'worker.log', 'ab')
        try:
            subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=log,
                             close_fds=True, start_new_session=True, cwd=str(self.dir))
        finally:
            log.close()
        return True

    # --- the worker loop -----------------------------------------------------

    def run(self, sleep=time.sleep):
        """Drive the run until a terminal event. Holds the range lock."""
        with (self.dir / 'lock').open('a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return
            try:
                state = self.read_state()
                while self.step(state, sleep):
                    pass
            except Exception as exc:  # noqa: BLE001 - reported, then re-raised to the log
                self.emit('stalled', error=f'{type(exc).__name__}: {str(exc)[:300]}',
                          next_action='continue_behavior_range restarts the worker from its saved state.')
                raise
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def step(self, state, sleep=time.sleep):
        """One transition. False once the worker should stop."""
        fmt = range_run.format_time
        if state['phase'] == 'edges':
            self.emit('needs_extents', **self.edges_view(state))
            return False
        if state['phase'] in ('completed', 'failed'):
            return False
        current = state.get('current')
        if current:
            try:
                response = self.behavior.poll(current['execution_id'], wait_seconds=20)
            except RuntimeError as exc:
                current['poll_failures'] = current.get('poll_failures', 0) + 1
                current['last_error'] = str(exc)[:300]
                self.emit('read_retry', window=[fmt(t) for t in current['window']],
                          execution_id=current['execution_id'], failures=current['poll_failures'],
                          final=current['poll_failures'] >= MAX_POLL_FAILURES, error=current['last_error'])
                if current['poll_failures'] >= MAX_POLL_FAILURES:
                    self.abandon(state, current)
                else:
                    sleep(POLL_BACKOFF_S * current['poll_failures'])
                self.save(state)
                return True
            current.pop('poll_failures', None)
            current.pop('last_error', None)
            if response.get('receipt') or response['execution_status'] == 'FAILED':
                done = {**current, 'status': 'COMPLETED' if response.get('receipt') else 'FAILED'}
                kind = 'window' if state['phase'] == 'windows' else 'whole'
                state['windows' if kind == 'window' else 'whole'].append(done)
                state['current'] = None
                self.save(state)
                self.emit(f'{kind}_done', window=[fmt(t) for t in done['window']], execution_id=done['execution_id'],
                          status=done['status'], verdicts=response.get('verdicts'),
                          progress=self.progress(state))
            else:
                self.save(state)
            return True
        queue = state['pending'] if state['phase'] == 'windows' else state['whole_pending']
        if queue:
            item = queue.pop(0)
            try:
                submitted = self.behavior.start(state['accepted_behavior'], state['population'],
                                                fmt(item['window'][0]), fmt(item['window'][1]))
            except APIRejected as exc:
                window = [fmt(t) for t in item['window']]
                if state['phase'] == 'whole':
                    # A whole-trace window cannot be narrowed: its traces stay UNKNOWN.
                    state['whole'].append({**item, 'status': 'REJECTED', 'error': str(exc)[:500]})
                    self.emit('whole_rejected', window=window, error=str(exc)[:300])
                elif range_run.is_size_rejection(exc):
                    halves = range_run.halve(item['window'])
                    if halves:
                        queue[0:0] = [{'window': h} for h in halves]
                        state['halvings'] += 1
                        self.emit('window_split', window=window, reason='selection too large')
                    else:
                        state['windows'].append({**item, 'status': 'REJECTED', 'error': str(exc)[:500]})
                        self.emit('window_rejected', window=window, error=str(exc)[:300])
                else:
                    # Not a size problem (e.g. the version is bound to another
                    # service): every window would fail the same way.
                    queue.insert(0, item)
                    state['phase'], state['error'] = 'failed', str(exc)[:1000]
                    self.save(state)
                    self.emit('failed', error=state['error'],
                              next_action='Report the error; fix the population or version and start a new range.')
                    return False
                self.save(state)
                return True
            state['current'] = {**item, 'execution_id': submitted['execution_id']}
            self.save(state)
            self.emit('window_submitted' if state['phase'] == 'windows' else 'whole_submitted',
                      window=[fmt(t) for t in item['window']], execution_id=submitted['execution_id'])
            return True
        if state['phase'] == 'windows':
            state['phase'] = 'edges'
            self.save(state)
            return True
        self.finalize(state)
        return False

    def abandon(self, state, current):
        """Give up reading an execution's results. An ordinary window is halved
        and rerun (smaller executions read faster); one at the minimum size, or a
        whole-trace window, stays an UNREADABLE hole. Its partial receipts still
        count toward judge usage, flagged incomplete."""
        state['current'] = None
        entry = {**current, 'status': 'UNREADABLE',
                 'error': 'results could not be read: ' + current.get('last_error', '')}
        halves = range_run.halve(current['window']) if state['phase'] == 'windows' else None
        window = [range_run.format_time(t) for t in current['window']]
        if halves:
            state['abandoned'].append(entry)
            state['pending'][0:0] = [{'window': h} for h in halves]
            state['halvings'] += 1
            self.emit('window_split', window=window, reason='results could not be read; rerunning as two halves')
        else:
            state['windows' if state['phase'] == 'windows' else 'whole'].append(entry)
            self.emit('window_unreadable', window=window, execution_id=entry['execution_id'])

    # --- results -------------------------------------------------------------

    def window_rows(self, entries):
        """Compact rows (plus execution_id) and raw results for completed executions."""
        from server import compact
        rows, raw = [], []
        for entry in entries:
            if entry.get('status') != 'COMPLETED':
                rows.append([])
                continue
            results = self.behavior.read(entry['execution_id'])['results']
            raw.extend(results)
            rows.append([{**compact(r), 'execution_id': entry['execution_id']} for r in results])
        return rows, raw

    def observations(self, state):
        done = [w for w in state['windows'] if w.get('status') == 'COMPLETED']
        rows, raw = self.window_rows(done)
        return [w['window'] for w in done], rows, raw

    def plan_whole(self, state):
        """Aligned whole-trace windows for the edge traces that have extents."""
        windows, rows, _ = self.observations(state)
        observed = {}
        for index, window_rows in enumerate(rows):
            for row in window_rows:
                observed.setdefault(row['trace_id'], []).append(index)
        edges = range_run.detect_edges(windows, observed, state['extents'])
        extents = {t: state['extents'][t] for t in edges if t in state['extents']}
        groups, _ = range_run.align_whole(extents)
        state['whole_pending'] = [{'window': g['window'], 'trace_ids': g['trace_ids']} for g in groups]
        state['phase'] = 'whole'

    def edges_view(self, state):
        windows, rows, _ = self.observations(state)
        seen = Counter(row['trace_id'] for window_rows in rows for row in window_rows)
        multi = sorted(t for t, n in seen.items() if n > 1)
        return {'boundaries': range_run.boundaries(windows), 'multi_window_traces': multi[:LIST_LIMIT],
                'multi_window_trace_count': len(multi), 'evaluated_traces': len(seen),
                'next_action': ('Measure, with the spans query tools, the span extent (earliest start, latest end, '
                                'every service) of each multi_window_trace and of each trace of this population with '
                                'spans on both sides of a boundary (look a few minutes either side). Then call '
                                'continue_behavior_range with trace_extents {trace_id: {start, end}}, or {} when none '
                                'cross. Edge traces without an extent become UNKNOWN (window-edge).')}

    def finalize(self, state):
        windows, rows, raw = self.observations(state)
        whole_rows, whole_raw = self.window_rows(state['whole'])
        whole_runs = [{**entry, 'results': whole_rows[i]} for i, entry in enumerate(state['whole'])]
        result = range_run.canonicalize(windows, rows, state['extents'], whole_runs)
        unreadable = state['abandoned'] + [w for w in state['windows'] + state['whole']
                                           if w.get('status') == 'UNREADABLE']
        partial_raw = []
        for entry in unreadable:
            try:
                partial_raw.extend(self.behavior.read(entry['execution_id'])['results'])
            except (OSError, ValueError, KeyError):
                pass
        usage = range_run.judge_usage(raw + whole_raw + partial_raw)
        usage['whole_trace_reevaluation'] = range_run.judge_usage(whole_raw)
        if unreadable:
            usage['usage_complete'] = False
            usage['unreadable_executions'] = [e['execution_id'] for e in unreadable]
            usage['note'] = ('Some executions\' results could not be read in full; their judge usage is '
                             'counted only as far as it was read.')
        fmt = range_run.format_time
        holes = [{'window': [fmt(t) for t in w['window']], 'status': w['status'],
                  'execution_id': w.get('execution_id'), 'error': w.get('error')}
                 for w in state['windows'] if w.get('status') != 'COMPLETED']
        executions = self.executions(state)
        header = {'range_id': state['range_id'], 'accepted_behavior': state['accepted_behavior'],
                  'population': state['population'], 'start': state['start'], 'end': state['end'],
                  'judge_based': state['judge_based']}
        private_json(self.dir / 'result.json', {**header, 'coverage_holes': holes, 'judge_usage': usage,
                                                 'executions': executions, **result})
        traces = result['traces']
        flagged = [{'trace_id': t['trace_id'], 'reason': t['canonical']['reason'],
                    'witness_refs': t['canonical']['witness_refs'],
                    'execution_id': t['canonical']['execution_id'], 'source': t['canonical']['source']}
                   for t in traces if t['canonical']['verdict'] == 'MATCH']
        summary = {**header, 'counts': result['counts'], 'population_size': result['population'],
                   'raw_window_counts': result['raw_window_counts'],
                   'unknown_reasons': range_run.unknown_reasons(traces),
                   'flagged': flagged[:LIST_LIMIT], 'flagged_count': len(flagged),
                   'edge_traces': result['edge_traces'][:LIST_LIMIT], 'edge_trace_count': len(result['edge_traces']),
                   'conflicts': result['conflicts'][:LIST_LIMIT], 'conflict_count': len(result['conflicts']),
                   'judge_usage': usage, 'coverage_holes': holes, 'executions': executions,
                   'result_file': str(self.dir / 'result.json'),
                   'note': ('Every per-window raw verdict is kept in result_file; canonical verdicts only add a '
                            'column. render_storyboard works per completed execution.')}
        if state['judge_based'] or usage['judge_calls']:
            summary['experimental'] = EXPERIMENTAL
        private_json(self.dir / 'summary.json', summary)
        state['phase'] = 'completed'
        self.save(state)
        self.emit('completed', counts=result['counts'], population=result['population'],
                  flagged=len(flagged), edge_traces=len(result['edge_traces']),
                  conflicts=len(result['conflicts']), coverage_holes=len(holes),
                  judge_calls=usage['judge_calls'], experimental=bool(summary.get('experimental')),
                  summary_file=str(self.dir / 'summary.json'))

    @staticmethod
    def executions(state):
        fmt = range_run.format_time
        out = [{'kind': 'window', 'window': [fmt(t) for t in w['window']], 'execution_id': w.get('execution_id'),
                'status': w['status'], **({'error': w['error']} if w.get('error') else {})} for w in state['windows']]
        out += [{'kind': 'abandoned-window', 'window': [fmt(t) for t in w['window']], 'execution_id': w.get('execution_id'),
                 'status': w['status'], 'error': w.get('error')} for w in state['abandoned']]
        out += [{'kind': 'whole-trace', 'window': [fmt(t) for t in w['window']], 'execution_id': w.get('execution_id'),
                 'trace_ids': w['trace_ids'], 'status': w['status'], **({'error': w['error']} if w.get('error') else {})}
                for w in state['whole']]
        return out

    @staticmethod
    def progress(state):
        return {'windows_done': len(state['windows']),
                'windows_left': len(state['pending']) + (1 if state.get('current') and state['phase'] == 'windows' else 0),
                'halvings': state['halvings'], 'phase': state['phase']}

    def wait_hint(self):
        """How the agent follows this run: Claude Code listens with Monitor on a
        command; every other runtime polls the get_behavior_range MCP tool."""
        if (self.behavior.config.get('runtime') or 'claude') == 'claude':
            return {'watch_command': f'cardinal-checks range watch {self.range_id}'}
        return {'poll': {'tool': 'get_behavior_range',
                         'arguments': {'range_id': self.range_id, 'after_event': 0, 'wait_seconds': 20}}}

    def snapshot(self):
        """What a caller needs right now, read from local files only."""
        state = self.read_state()
        out = {'range_id': self.range_id, 'phase': state['phase'], 'population': state['population'],
               'start': state['start'], 'end': state['end'], 'judge_based': state['judge_based'],
               'progress': self.progress(state), 'worker_running': self.worker_running(),
               'events_file': str(self.dir / 'events.jsonl'), 'state_file': str(self.state_file)}
        out.update(self.wait_hint())
        if state.get('error'):
            out['error'] = state['error']
        if state['phase'] == 'completed':
            out['summary_file'] = str(self.dir / 'summary.json')
            out['result_file'] = str(self.dir / 'result.json')
        last = self.last_event()
        if last:
            out['last_event'] = last.get('event')
        return out


def main(argv=None):
    parser = argparse.ArgumentParser(description='Cardinal behavior range worker (started by execute_behavior_range).')
    parser.add_argument('--range', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--config')
    parser.add_argument('--runtime')
    args = parser.parse_args(argv)
    import server
    config = (json.loads(Path(args.config).expanduser().read_text()) if args.config
              else server.default_config(runtime=args.runtime))
    if args.runtime:
        config.setdefault('runtime', args.runtime)
    config['output_dir'] = args.output
    RangeRunner(server.Behavior(config), args.range).run()


if __name__ == '__main__':
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    main()
