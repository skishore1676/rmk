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



def test_changed_host_page_uses_verified_prior_only_for_comparison(runtime,tmp_path):
    source=Path(runtime.cfg.root)/NB/f'{P1}.rm'
    prior_strokes=tmp_path/'prior.rm';prior_strokes.write_bytes(source.read_bytes())
    runtime.store.capture();runtime.intake.interpret([P1],reader)
    ref=runtime.intake._load()['pages'][P1]
    private=tmp_path/'reading.json';private.write_bytes((runtime.store.directory/'interpretations'/f"{ref['id']}.json").read_bytes())
    source.write_bytes(source.read_bytes()+b' changed')
    runtime.store.capture()
    with pytest.raises(CaptureError):runtime.intake.adopt_reading(private)
    assert runtime.intake.adopt_reading(private,prior_strokes=prior_strokes)['status']=='adopted_prior_comparison'
    jobs=runtime.intake.plan([])
    assert len(jobs)==1 and jobs[0]['identity_basis']['previous_id']==ref['id']
    assert runtime.intake.summary(runtime.store._load()[1])['interpretation']['current_pages']==0


@pytest.mark.parametrize('failure', ['bootstrap', 'not_loaded', 'after_bootstrap', 'checkpoint'])
def test_resume_failure_preserves_pause_and_has_owner_reason(runtime,monkeypatch,failure):
    from rmk.capture import _atomic_write
    runtime.settings['paused']=True
    _atomic_write(runtime.path,runtime.settings)
    original=runtime.path.read_bytes()
    import rmk.runtime as module
    monkeypatch.setattr(module.sys,'platform','darwin')
    loaded={'value':False}
    monkeypatch.setattr(runtime,'_service_loaded',lambda:loaded['value'])
    rollback=[]
    def pause():rollback.append('own service only');loaded['value']=False
    monkeypatch.setattr(runtime,'_pause_service',pause)
    actual_write=module._atomic_write
    def interrupted_checkpoint(path,value):
        actual_write(path,value)
        if path==runtime.path and not value['paused']:raise OSError('Synthetic checkpoint fsync failure')
    if failure=='checkpoint':monkeypatch.setattr(module,'_atomic_write',interrupted_checkpoint)
    def install(load=False):
        assert load and json.loads(runtime.path.read_text())['paused']
        with pytest.raises(CaptureError,match='already owns'):
            runtime.refresh(reader)
        if failure=='bootstrap':raise CaptureError('launchd bootstrap failed; inspect owner service')
        loaded['value']=failure in ('after_bootstrap','checkpoint')
        if failure=='after_bootstrap':raise CaptureError('launchd acknowledgement lost; inspect owner service')
        return {'status':'loaded'}  # A return string does not prove loaded state.
    monkeypatch.setattr(runtime,'install',install)
    with pytest.raises(CaptureError):runtime.control('resume-schedule')
    assert runtime.path.read_bytes()==original and runtime.settings['paused']
    assert rollback==['own service only']
    status=runtime.status()['units'][0]
    assert not status['declared_enabled'] and not status['effective_enabled']
    assert status['last']['domain']['attention_required']
    assert status['last']['domain']['status']=='activation_failed'
    assert 'launchd' in status['human_action']


def test_resume_commits_enabled_only_after_loaded_verification(runtime,monkeypatch):
    from rmk.capture import _atomic_write
    runtime.settings['paused']=True;_atomic_write(runtime.path,runtime.settings)
    loaded={'value':False}
    monkeypatch.setattr(runtime,'_service_loaded',lambda:loaded['value'])
    def install(load=False):
        assert load and json.loads(runtime.path.read_text())['paused']
        loaded['value']=True
    monkeypatch.setattr(runtime,'install',install)
    assert runtime.control('resume-schedule')['status']=='resumed'
    assert not json.loads(runtime.path.read_text())['paused']
    unit=runtime.status()['units'][0]
    assert unit['effective_enabled'] and not unit['last']['domain']['attention_required']


def test_enabled_service_disappearance_requires_owner_attention(runtime,monkeypatch):
    monkeypatch.setattr(runtime,'_service_loaded',lambda:False)
    unit=runtime.status()['units'][0]
    assert unit['declared_enabled'] and not unit['effective_enabled']
    assert unit['lifecycle']=='stuck' and unit['last']['domain']['attention_required']
    assert unit['last']['domain']['status']=='service_missing'
    assert 'resume-schedule' in unit['human_action']


def test_transient_publication_succeeds_without_duplicate_read(runtime,monkeypatch):
    import rmk.runtime as module
    monkeypatch.setattr(runtime,'_main',lambda:command('git','rev-parse','HEAD',cwd=runtime.workspace))
    reads=[];publications=[];recovery=[]
    def reading(*args):reads.append(args[0]);return reader(*args)
    def publish(staged):
        publications.append(staged)
        if len(publications)==1:raise module.RetryableError('Synthetic connection reset')
        return {'status':'confirmed_on_main'}
    def wait(delay):
        x=json.loads(runtime.receipt_path.read_text());recovery.append(x)
        assert x['status']=='recovering' and not x['attention_required']
    monkeypatch.setattr(runtime,'_publish',publish);monkeypatch.setattr(module.time,'sleep',wait)
    x=runtime.refresh(reading)
    assert x['status']=='succeeded' and x['cycle_attempts']==2
    assert len(reads)==1 and publications[0]['paths']==publications[1]['paths']
    assert len(recovery)==1 and len(list((runtime.workspace/'projects/career/inbox').glob('remarkable-*.md')))==1


def publication_intent(runtime):
    from rmk.capture import _atomic_write
    from rmk.snapshot import digest
    base=command('git','rev-parse','HEAD',cwd=runtime.workspace)
    runtime.store.capture();runtime.intake.interpret([P1],reader)
    staged=runtime.intake.stage(runtime.workspace,'career')
    command('git','add','.',cwd=runtime.workspace);command('git','commit','-m','Owned exact batch',cwd=runtime.workspace)
    commit=command('git','rev-parse','HEAD',cwd=runtime.workspace)
    batch=digest(json.dumps(staged['paths'],sort_keys=True).encode())
    pending={'batch':batch,'staged':staged,'commit':commit,
             'branch':f'codex/project-update-career-rmk-{batch[:16]}','pr':'synthetic-existing-pr'}
    _atomic_write(runtime.publication_path,pending)
    return base,commit,pending


def test_publication_replays_recorded_commit_even_after_checkout_head_changed(runtime,monkeypatch):
    import rmk.runtime as module
    base,commit,pending=publication_intent(runtime)
    command('git','checkout','--detach',base,cwd=runtime.workspace)
    assert command('git','rev-parse','HEAD',cwd=runtime.workspace)!=commit
    revisions=iter([base,commit]);monkeypatch.setattr(runtime,'_main',lambda:next(revisions))
    pushed=[]
    def isolated_command(*args,**kwargs):
        assert args[:2]==('git','push')
        pushed.append(args[3]);return ''
    monkeypatch.setattr(module,'command',isolated_command)
    monkeypatch.setattr(runtime.intake,'confirm',lambda *args:{'status':'confirmed_on_main'})
    assert runtime._publish(pending['staged'])['status']=='confirmed_on_main'
    assert pushed==[f"{commit}:refs/heads/{pending['branch']}"]


def test_exhausted_transport_recovery_retains_exact_intent_and_can_catch_up(runtime,monkeypatch):
    import rmk.runtime as module
    base,commit,pending=publication_intent(runtime)
    original=runtime.publication_path.read_bytes()
    monkeypatch.setattr(runtime,'_main',lambda:base)
    monkeypatch.setattr(runtime,'_service_loaded',lambda:True)
    actual_command=module.command;pushes=[];waiting=[]
    def interrupted(*args,**kwargs):
        if args[:2]==('git','push'):
            pushes.append(args[3]);raise module.RetryableError('Synthetic connection reset')
        return actual_command(*args,**kwargs)
    def wait(delay):
        unit=runtime.status()['units'][0]
        waiting.append(unit['last']['domain']['attention_required'])
        assert unit['lifecycle']=='recovering'
    monkeypatch.setattr(module,'command',interrupted);monkeypatch.setattr(module.time,'sleep',wait)
    result=runtime.refresh(lambda *args:pytest.fail('completed reading repeated'))
    assert result['status']=='blocked' and result['attention_required'] and result['cycle_attempts']==3
    assert waiting==[False,False] and pushes==[f"{commit}:refs/heads/{pending['branch']}"]*3
    assert runtime.publication_path.read_bytes()==original
    assert runtime._published(pending['staged'],commit)
    assert 'restore transport' in runtime.status()['units'][0]['human_action']
    # A later existing-cadence retry reconciles remote success without a push or read.
    monkeypatch.setattr(module,'command',actual_command);monkeypatch.setattr(runtime,'_main',lambda:commit)
    monkeypatch.setattr(runtime.intake,'confirm',lambda *args:{'status':'confirmed_on_main'})
    assert runtime.refresh(lambda *args:pytest.fail('catch-up made a model call'))['status']=='succeeded'


@pytest.mark.parametrize('args,stderr,timed_out,expected',[
    (('git','fetch','origin','main'),'Could not resolve host',False,True),
    (('git','-C','fixture','fetch','origin','main'),'The requested URL returned error: 503',False,True),
    (('gh','pr','create'),'HTTP 502: Bad Gateway',False,True),
    (('gh','pr','list'),'lookup api.github.com: no such host',False,True),
    (('git','push','origin','recorded:branch'),'',True,True),
    (('gh','pr','list'),'HTTP 401: Requires authentication PRIVATE_BODY',False,False),
    (('git','push','origin','recorded:branch'),'non-fast-forward',False,False),
])
def test_only_known_safe_command_failures_are_retried(args,stderr,timed_out,expected):
    from rmk.runtime import command_failure,RetryableError
    failure=command_failure(args,stderr,timed_out=timed_out)
    assert isinstance(failure,RetryableError)==expected
    assert 'PRIVATE_BODY' not in str(failure)


def test_pending_commit_mismatch_stops_before_push(runtime,monkeypatch):
    import rmk.runtime as module
    base,commit,pending=publication_intent(runtime)
    from rmk.capture import _atomic_write
    pending['commit']=base;_atomic_write(runtime.publication_path,pending)
    monkeypatch.setattr(runtime,'_main',lambda:base)
    monkeypatch.setattr(module,'command',lambda *args,**kwargs:pytest.fail('invalid intent pushed'))
    with pytest.raises(CaptureError,match='exact commit'):
        runtime._publish(pending['staged'])



def test_uncertain_pr_creation_reconciles_existing_pr_without_duplicate_effect(runtime,monkeypatch):
    import subprocess
    import rmk.runtime as module
    from rmk.capture import _atomic_write
    base,commit,pending=publication_intent(runtime)
    pending['pr']=None;_atomic_write(runtime.publication_path,pending)
    main_reads=[];lists=[];creates=[];pushes=[]
    def main():
        main_reads.append(True);return base if len(main_reads)<=2 else commit
    monkeypatch.setattr(runtime,'_main',main)
    actual_run=subprocess.run
    def network(args,**kwargs):
        if tuple(args[:2])==('git','push'):
            pushes.append(args[3]);return subprocess.CompletedProcess(args,0,'','')
        if tuple(args[:3])==('gh','pr','list'):
            lists.append(True)
            return subprocess.CompletedProcess(args,0,'[]' if len(lists)==1 else '[{"url":"synthetic-existing-pr"}]','')
        if tuple(args[:3])==('gh','pr','create'):
            creates.append(True)
            # Simulates a remote PR created before the client lost its response.
            raise subprocess.TimeoutExpired(args,60)
        return actual_run(args,**kwargs)
    monkeypatch.setattr(module.subprocess,'run',network)
    monkeypatch.setattr(module.time,'sleep',lambda delay:None)
    monkeypatch.setattr(runtime.intake,'confirm',lambda *args:{'status':'confirmed_on_main'})
    result=runtime.refresh(lambda *args:pytest.fail('publication retry reread source'))
    assert result['status']=='succeeded' and result['cycle_attempts']==2
    assert len(lists)==2 and len(creates)==1
    assert pushes==[f"{commit}:refs/heads/{pending['branch']}"]*2
    current=json.loads(runtime.publication_path.read_text())
    assert current['status']=='confirmed' and current['pr']=='synthetic-existing-pr'
    assert current['commit']==commit and current['staged']==pending['staged']
