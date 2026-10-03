"""Real Git and synthetic notebook tests for the app-owned cycle boundary."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from test_capture import store, NB, P1, P2
from test_intake import workspace, reader
from rmk.capture import CaptureError
from rmk.runtime import Runtime, SCHEMA, command


@pytest.fixture
def runtime(store,workspace,tmp_path):
    command('git','init',cwd=workspace)
    command('git','config','user.name','Synthetic Test',cwd=workspace)
    command('git','config','user.email','fixture@example.invalid',cwd=workspace)
    command('git','remote','add','origin','https://github.com/skishore1676/pulsar-workspace.git',cwd=workspace)
    command('git','add','.',cwd=workspace);command('git','commit','-m','Fixture base',cwd=workspace)
    scripts=workspace/'scripts';scripts.mkdir()
    check=scripts/'workspace-git.sh';check.write_text('#!/bin/sh\nexit 0\n');check.chmod(0o700)
    command('git','add','.',cwd=workspace);command('git','commit','-m','Synthetic checker',cwd=workspace)
    path=tmp_path/'runtime.json'
    path.write_text(json.dumps({'schema':SCHEMA,'paused':False,'notebook_id':NB,'project':'career',
        'state_dir':str(store.directory.parent.parent),'workspace':str(workspace),'baseline_pages':[P1],
        'max_storage_bytes':10000000,'timezone':'America/Chicago','hour':6,'minute':0}))
    cfg=SimpleNamespace(transport='local',root=store.root)
    return Runtime(path,cfg)


def test_paused_refresh_makes_no_capture_or_publish(runtime,monkeypatch):
    runtime.settings['paused']=True
    monkeypatch.setattr(runtime.store,'capture',lambda:pytest.fail('paused capture'))
    assert runtime.refresh()['status']=='paused'


def test_refresh_uses_selected_baseline_and_reuses_reading_after_publish_failure(runtime,monkeypatch):
    revision=command('git','rev-parse','HEAD',cwd=runtime.workspace)
    monkeypatch.setattr(runtime,'_main',lambda:revision)
    def failed(staged):raise CaptureError('Synthetic offline publication')
    monkeypatch.setattr(runtime,'_publish',failed)
    first=runtime.refresh(reader)
    assert first['status']=='blocked' and first['attention_required']
    assert list(runtime.intake._load()['pages'])==[P1]
    monkeypatch.setattr(runtime,'_publish',lambda staged:{'status':'confirmed_on_main','paths':staged['paths']})
    second=runtime.refresh(lambda *args:pytest.fail('completed reading called twice'))
    assert second['status']=='succeeded' and not second['attention_required']
    assert len(list((runtime.workspace/'projects/career/inbox').glob('remarkable-*.md')))==1


def test_unrelated_dirty_worktree_blocks_before_source_read(runtime,monkeypatch):
    (runtime.workspace/'user.md').write_text('Protected concurrent work')
    monkeypatch.setattr(runtime.store,'capture',lambda:pytest.fail('dirty source capture'))
    result=runtime.refresh(reader)
    assert result['status']=='blocked'
    assert (runtime.workspace/'user.md').read_text()=='Protected concurrent work'


def test_publication_comparison_uses_exact_bytes(runtime):
    path=runtime.workspace/'projects/career/inbox/remarkable-test.md';path.parent.mkdir()
    data=b' leading text\n\n';path.write_bytes(data)
    command('git','add','.',cwd=runtime.workspace);command('git','commit','-m','Exact bytes',cwd=runtime.workspace)
    from rmk.snapshot import digest
    staged={'paths':[{'path':str(path.relative_to(runtime.workspace)),'sha256':digest(data)}]}
    assert runtime._published(staged,'HEAD')
    staged['paths'][0]['sha256']=digest(data.strip()+b'\n')
    assert not runtime._published(staged,'HEAD')


def test_whole_cycle_lock_rejects_overlapping_refresh(runtime):
    with runtime.lock():
        with pytest.raises(CaptureError,match='already owns'):
            runtime.refresh(reader)


def test_status_marks_missing_active_page_as_unavailable(runtime):
    runtime.store.capture()
    (Path(runtime.cfg.root)/NB/f'{P1}.rm').unlink()
    status=runtime.status()
    assert status['source_access']=='unavailable'
    assert status['units'][0]['last']['domain']['attention_required']
    assert status['sync_freshness']=='unknown'


def test_adopt_owned_matching_reading_keeps_original_receipt(runtime,tmp_path):
    runtime.store.capture();runtime.intake.interpret([P1],reader)
    reference=runtime.intake._load()['pages'][P1]
    original=runtime.intake._result(reference['id'])
    private=tmp_path/'handoff.json';private.write_bytes((runtime.store.directory/'interpretations'/f"{reference['id']}.json").read_bytes())
    assert runtime.intake.adopt_reading(private)['interpretation_id']==reference['id']
    original['page_sha256']='f'*64;private.write_text(json.dumps(original))
    with pytest.raises(CaptureError):runtime.intake.adopt_reading(private)


def test_pending_publication_is_reconciled_before_new_capture(runtime,monkeypatch):
    from rmk.capture import _atomic_write
    runtime.store.directory.mkdir(parents=True,exist_ok=True)
    _atomic_write(runtime.publication_path,{'batch':'synthetic','staged':{'paths':[]},'pr':'private'})
    monkeypatch.setattr(runtime.store,'capture',lambda:pytest.fail('capture before prior publication'))
    monkeypatch.setattr(runtime,'_publish',lambda staged:{'status':'awaiting_publication'})
    result=runtime.refresh(reader)
    assert result['status']=='recovering' and not result['attention_required']


def test_already_published_recovery_marks_pending_complete(runtime,monkeypatch):
    from rmk.capture import _atomic_write
    from rmk.snapshot import digest
    staged={'paths':[{'path':'fixture','sha256':'f'*64}]}
    runtime.store.directory.mkdir(parents=True,exist_ok=True)
    _atomic_write(runtime.publication_path,{'batch':digest(json.dumps(staged['paths'],sort_keys=True).encode()),'staged':staged,'pr':'private'})
    monkeypatch.setattr(runtime,'_main',lambda:'a'*40)
    monkeypatch.setattr(runtime,'_published',lambda *args:True)
    monkeypatch.setattr(runtime.intake,'confirm',lambda *args:{'status':'confirmed_on_main'})
    assert runtime._publish(staged)['status']=='confirmed_on_main'
    assert json.loads(runtime.publication_path.read_text())['status']=='confirmed'


def test_resume_without_source_preserves_pause(runtime):
    runtime.settings['paused']=True
    (Path(runtime.cfg.root)/NB/f'{P1}.rm').unlink()
    with pytest.raises(CaptureError,match='Pair/sync'):
        runtime.control('resume-schedule')
    assert runtime.settings['paused']
