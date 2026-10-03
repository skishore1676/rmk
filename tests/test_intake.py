"""Synthetic connected capture -> Broker contract -> bounded project intake."""
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from test_capture import store, NB, P1, P2, P3, set_order
from rmk import intake as module
from rmk.capture import CaptureError
from rmk.intake import Intake, FIELDS, validate_reading, broker_reader


def reading():
    result = {key: [] for key in FIELDS}
    result.update(faithful_reading="Private full reading, should not reach Git",
                  extract="Monthly direction: make progress on the selected project.",
                  expressed_intent=["Practice the selected example"],
                  reported_progress=["Suman reports the earlier practice done"],
                  agent_suggestions=["Consider one feasible next practice"],
                  uncertain_spans=["One date is unclear"])
    return result


def reader(identity, prompt, images, working_dir):
    assert "untrusted source material" in prompt
    assert images and all(Path(image).is_file() for image in images)
    return {"status": "succeeded", "output_text": json.dumps(reading()),
            "receipt": {"receipt_id": f"synthetic:{identity}", "status": "succeeded"}}


@pytest.fixture
def intake(store):
    store.capture()
    return Intake(store)


@pytest.fixture
def workspace(tmp_path):
    root = tmp_path / "workspace";root.mkdir()
    (root / "WORKSPACE.md").write_text("# Synthetic workspace")
    for project in ('day-planning-attention-system', 'career'):
        path = root / 'projects' / project;path.mkdir(parents=True)
        (path / 'PROJECT.md').write_text('# Test project')
        (path / 'CURRENT.md').write_text('# Current test state')
    return root


def test_baseline_must_select_pages_and_replay_does_not_enroll_history(intake):
    with pytest.raises(CaptureError, match="baseline"):
        intake.plan([])
    assert len(intake.plan([P1])) == 1
    intake.interpret([P1], reader)
    assert intake.plan([]) == []
    assert intake.plan([P1]) == []
    assert len(intake.plan([P2])) == 1  # Can deliberately select more history.


def test_incremental_edit_old_page_new_page_and_unchanged(intake):
    intake.interpret([P1], reader)
    root = Path(intake.store.root)
    (root / NB / f'{P1}.rm').write_bytes(b'edited older page')
    (root / NB / f'{P3}.rm').write_bytes(b'new page')
    set_order(root,[P1,P2,P3]);intake.store.capture()
    jobs=intake.plan([])
    assert [j['identity_basis']['page_id'] for j in jobs] == [P1,P3]
    assert 'Private full reading' in jobs[0]['prompt']
    intake.interpret([],reader)
    assert intake.plan([]) == []


def test_rename_reorder_only_has_no_model_job(intake):
    intake.interpret([P1,P2],reader)
    root=Path(intake.store.root)
    set_order(root,[P2,P1])
    meta=root/f'{NB}.metadata';d=json.loads(meta.read_text());d['visibleName']='renamed';meta.write_text(json.dumps(d))
    intake.store.capture()
    assert intake.plan([])==[]


def test_partial_model_failure_keeps_success_and_retries_only_pending(intake):
    calls=[]
    def partial(*args):
        calls.append(args[0])
        if len(calls)==2:
            return {'status':'failed','output_text':'', 'receipt':{'status':'failed','receipt_id':'synthetic:failed'}}
        return reader(*args)
    with pytest.raises(CaptureError,match='Broker'):
        intake.interpret([P1,P2],partial)
    state=json.loads(intake.path.read_text())
    assert list(state['pages'])==[P1]
    remaining=intake.plan([P1,P2]);assert len(remaining)==1 and remaining[0]['identity_basis']['page_id']==P2
    intake.interpret([P1,P2],reader)
    assert intake.plan([])==[]
    assert len(list((intake.store.directory/'interpretations').glob('attempt-*'))) == 3


def test_invalid_model_output_keeps_capture_and_interpretation_separate(intake):
    checkpoint=(intake.store.directory/'state.json').read_bytes()
    def invalid(*args):
        return {'status':'succeeded','output_text':'not JSON', 'receipt':{'status':'succeeded','receipt_id':'synthetic:bad'}}
    with pytest.raises(CaptureError,match='JSON'):
        intake.interpret([P1],invalid)
    assert not intake.path.exists()
    assert (intake.store.directory/'state.json').read_bytes()==checkpoint
    assert list((intake.store.directory/'interpretations').glob('attempt-*'))


def test_interpretation_checkpoint_failure_reuses_completed_model_result(intake,monkeypatch):
    original=module._atomic_write
    monkeypatch.setattr(module,'_atomic_write',lambda *a: (_ for _ in ()).throw(OSError('interrupted checkpoint')))
    with pytest.raises(OSError):
        intake.interpret([P1],reader)
    monkeypatch.setattr(module,'_atomic_write',original)
    intake.interpret([P1],lambda *a:pytest.fail('must reuse completed model result'))
    assert intake.plan([P1])==[]


def test_prompt_reprocessing_supersedes_prior_interpretation(intake):
    first=intake.interpret([P1],reader)['interpreted'][0]
    second=intake.interpret([P1],reader,prompt_version='faithful-page-v2')['interpreted'][0]
    assert first!=second
    assert intake._result(second)['identity_basis']['previous_id']==first


def test_stage_bounded_idempotent_and_not_delivered(intake,workspace):
    intake.interpret([P1],reader)
    first=intake.stage(workspace,'day-planning-attention-system')
    assert first['status']=='staged_only'
    path=workspace/first['paths'][0]['path'];before=path.read_bytes()
    assert b'Private full reading' not in before
    assert b'Reported progress (not verified completion)' in before
    assert intake.stage(workspace,'day-planning-attention-system')==first
    assert path.read_bytes()==before and intake._load()['deliveries']=={}
    with pytest.raises(CaptureError,match='canonical home'):
        intake.stage(workspace,'career')


def test_conflict_preserves_user_edits_and_never_advances_delivery(intake,workspace):
    intake.interpret([P1],reader)
    staged=intake.stage(workspace,'career');path=workspace/staged['paths'][0]['path'];path.write_text('Human edit\n')
    with pytest.raises(CaptureError,match='conflicts'):
        intake.stage(workspace,'career')
    assert path.read_text()=='Human edit\n' and intake._load()['deliveries']=={}


def test_stale_or_removed_page_not_staged_as_current(intake,workspace):
    intake.interpret([P1,P2],reader)
    root=Path(intake.store.root);set_order(root,[P1]);(root/NB/f'{P1}.rm').write_bytes(b'new edit')
    intake.store.capture()
    assert intake.stage(workspace,'career')['paths']==[]


@pytest.mark.parametrize('project',['../escape','unknown','career/../career'])
def test_arbitrary_or_missing_project_rejected(intake,workspace,project):
    with pytest.raises(CaptureError):
        intake.stage(workspace,project)


def test_corrupt_interpretation_fails_closed(intake):
    identity=intake.interpret([P1],reader)['interpreted'][0]
    path=intake.store.directory/'interpretations'/f'{identity}.json';path.write_text('{}')
    with pytest.raises(CaptureError):
        intake.plan([])


def test_reading_schema_limits_and_no_invented_destinations():
    for value in ({**reading(),'path':'../../anything'}, {**reading(),'extract':'x'*1201},
                  {**reading(),'reported_progress':['x'*501]}):
        with pytest.raises(CaptureError):
            validate_reading(value)


def test_confirm_needs_remote_main_exact_content_and_no_false_delivery(intake,workspace,monkeypatch):
    intake.interpret([P1],reader);staged=intake.stage(workspace,'career')
    content=(workspace/staged['paths'][0]['path']).read_bytes()
    def git(command,**kwargs):
        args=command[3:]
        if args[:2]==['remote','get-url']:
            return SimpleNamespace(stdout=b'https://github.com/skishore1676/pulsar-workspace.git\n')
        if args[0]=='show':return SimpleNamespace(stdout=content)
        return SimpleNamespace(stdout=b'')
    monkeypatch.setattr(module.subprocess,'run',git)
    assert intake.confirm(workspace,'career','a'*40)['status']=='confirmed_on_main'
    assert list(intake._load()['deliveries'].values())[0]['workspace_revision']=='a'*40


def test_publication_failure_does_not_mark_delivered(intake,workspace,monkeypatch):
    intake.interpret([P1],reader)
    def failed(*a,**k):raise subprocess.CalledProcessError(1,a[0])
    monkeypatch.setattr(module.subprocess,'run',failed)
    with pytest.raises(subprocess.CalledProcessError):intake.confirm(workspace,'career','a'*40)
    assert intake._load()['deliveries']=={}


def test_real_broker_adapter_preserves_configured_policy_and_receipt(monkeypatch,tmp_path):
    import agent_broker
    import agent_broker.policy
    seen={}
    class Broker:
        def __init__(self,policy):seen['policy']=policy
        def run(self,spec,task):
            seen['spec']=spec;seen['task']=task
            return SimpleNamespace(status='succeeded',output_text=json.dumps(reading()),
                receipt=SimpleNamespace(to_dict=lambda:{'receipt_id':'synthetic:adapter','status':'succeeded'}))
    monkeypatch.setattr(agent_broker,'AgentBroker',Broker)
    monkeypatch.setattr(agent_broker.policy,'load_policy',lambda p:('existing-policy',p))
    cfg=SimpleNamespace(broker=SimpleNamespace(policy_path='approved.yaml',lane='rmk',actor='reader',role='note_reader',timeout=600))
    response=broker_reader(cfg,'id','prompt',['image.png'],str(tmp_path))
    assert seen['policy']==('existing-policy','approved.yaml')
    assert seen['task'].context.images==('image.png',) and seen['task'].task_id=='id'
    assert response['receipt']['receipt_id']=='synthetic:adapter'


def test_broker_provider_failure_falls_through_and_intake_preserves_receipt(intake, monkeypatch, tmp_path):
    import agent_broker.runner as runner
    from agent_broker.contracts import ProviderResult
    monkeypatch.setenv('AGENT_BROKER_HOME', str(tmp_path/'hub'))
    monkeypatch.setenv('AGENT_BROKER_LEDGER_DISABLE', '1')
    # Use the application's canonical medium policy, with isolated hub/ledger.
    path=tmp_path/'remarkable.yaml'
    path.write_text('schema: agent_broker.policy.v1\ndefault: {brain: balanced}\n')
    calls=[]
    class Provider:
        supports_images=True
        def __init__(self, provider): self.provider=provider
        def run(self, spec, task):
            calls.append(self.provider)
            assert task.context.images and spec.lane_id=='rmk'
            if self.provider=='codex':
                return ProviderResult(status='failed', output_text='', failure_summary='synthetic primary unavailable', provider_id=self.provider)
            return ProviderResult(status='succeeded', output_text=json.dumps(reading()), provider_id=self.provider)
    monkeypatch.setattr(runner, 'build_provider', lambda name, options: Provider(name))
    cfg=SimpleNamespace(broker=SimpleNamespace(policy_path=str(path),lane='rmk',actor='reader',role='note_reader',timeout=600))
    intake.interpret([P1],lambda *args: broker_reader(cfg,*args))
    result=intake._result(intake._load()['pages'][P1]['id'])
    assert calls==['codex','claude']
    receipt=result['receipt']
    assert receipt['provider_id']=='claude' and receipt['provider_layer']==1
    assert receipt['provider_chain']==['codex','claude','openrouter','openrouter']
    assert receipt['degraded'] is True and 'synthetic primary unavailable' in receipt['failover_failures'][0]


def test_status_distinguishes_interpreted_from_unconfirmed_delivery(intake):
    assert intake.store.status()['interpretation']['status']=='not_started'
    intake.interpret([P1],reader)
    status=intake.store.status()
    assert status['interpretation']['current_pages']==1
    assert status['delivery']['status']=='not_confirmed'


def test_selected_delivery_keeps_other_historical_readings_private(intake,workspace):
    intake.interpret([P1,P2],reader)
    staged=intake.stage(workspace,'career',[P2])
    assert len(staged['paths'])==1
    result=intake._result(staged['paths'][0]['interpretation_id'])
    assert result['page_id']==P2
    assert len(list((workspace/'projects/career/inbox').glob('remarkable-*.md')))==1
