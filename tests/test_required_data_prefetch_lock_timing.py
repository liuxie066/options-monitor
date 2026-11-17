from contextlib import contextmanager
from pathlib import Path

import pytest

from src.application.multi_tick import required_data_prefetch as prefetch
from src.application import multi_tick_audit as audit


@pytest.mark.parametrize('failure', [None, 'acquire', 'work', 'release'])
@pytest.mark.parametrize('error_type', [OSError, KeyboardInterrupt])
@pytest.mark.parametrize('logging', ['ok', 'broken', 'disabled'])
def test_lock_timing_preserves_boundaries_result_and_exceptions(
    monkeypatch, tmp_path, failure, error_type, logging,
):
    clock = [0.0]
    events = []
    operations = []
    error = error_type('private detail must not enter timing data')
    result = {'sentinel': object()}
    monkeypatch.setattr(audit, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(prefetch, 'check_fetch_control', lambda *_: None)
    monkeypatch.setattr(prefetch, 'require_market_data_provider', lambda *_: None)

    class Log:
        def safe_event(self, step, status, **kw):
            if logging == 'broken':
                raise RuntimeError('logger unavailable')
            phase = kw['data']['phase']
            if phase == 'required_data_lock_wait' and status == 'start':
                assert operations == []
            elif phase == 'required_data_lock_wait':
                assert operations == (['acquire'] if failure == 'acquire' else ['acquire', 'held'])
            else:
                assert operations == ['acquire', 'held']
            events.append((status, phase, kw.get('duration_ms')))
            assert 'private detail' not in str(kw)

    @contextmanager
    def lock(path):
        assert path == tmp_path / 'output_shared/state/required_data_prefetch.lock'
        operations.append('acquire')
        clock[0] += 2
        if failure == 'acquire':
            raise error
        operations.append('held')
        try:
            yield
        finally:
            operations.append('release')
            clock[0] += 3
            if failure == 'release':
                raise error

    def work(**kwargs):
        assert operations[-1] == 'held'
        assert 'runlog' not in kwargs
        clock[0] += 5
        if failure == 'work':
            raise error
        return result

    monkeypatch.setattr(prefetch, 'exclusive_private_file_lock', lock)
    monkeypatch.setattr(prefetch, '_prefetch_required_data_unlocked', work)
    kwargs = dict(vpy=Path('python'), base=tmp_path, cfg={}, shared_required=tmp_path)
    if logging != 'disabled':
        kwargs['runlog'] = Log()
    if failure:
        with pytest.raises(error_type) as caught:
            prefetch.prefetch_required_data(**kwargs)
        assert caught.value is error
    else:
        assert prefetch.prefetch_required_data(**kwargs) is result
    assert operations == (['acquire'] if failure == 'acquire' else ['acquire', 'held', 'release'])
    if logging != 'ok':
        assert events == []
        return
    assert events[:2] == [
        ('start', 'required_data_lock_wait', None),
        ('error' if failure == 'acquire' else 'ok', 'required_data_lock_wait', 2000),
    ]
    if failure == 'acquire':
        assert len(events) == 2
    else:
        assert events[2:] == [
            ('start', 'required_data_lock_held', None),
            ('error' if failure == 'work' else 'ok', 'required_data_lock_held', 5000),
        ]
