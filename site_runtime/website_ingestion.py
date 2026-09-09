"""Homepage ingestion adapter for the copied application's existing data contracts."""
import json
import time
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from website_assistant.build_jobs import DiscoveryJobs, StartDiscovery, JobConflict
from website_assistant.ontology_builder import validate_ontology
from .knowledge import KnowledgeStore, validate_record
from .ontology_admin import inspect_ontology, OntologyAdmin
from .source_admin import SourceAdmin
from .rag_settings import atomic_json
from .bootstrap import selected_directory
from .configuration import setting


def convert_ontology(graph):
    counts=Counter(e['label'] for e in graph['entities'])
    labels={e['entity_id']:e['label'] if counts[e['label']]==1 else e['label']+' ['+e['entity_id'][-8:]+']' for e in graph['entities']}
    proof={e['evidence_id']:e for e in graph['evidence']}
    rows=[];seen=set()
    for edge in graph['relationships']:
        row={'subject':labels[edge['subject_id']], 'predicate':edge['predicate'],
             'object':labels.get(edge['object_id'],edge['object_id']), 'source_id':proof[edge['evidence_ids'][0]]['source_id']}
        key=tuple(row.values())
        if key not in seen:rows.append(row);seen.add(key)
    aliases=[];alias_counts=Counter((a['language'],a['label'].casefold()) for a in graph['aliases'])
    canonical={label.casefold() for label in labels.values()}
    for alias in graph['aliases']:
        if alias_counts[alias['language'],alias['label'].casefold()]!=1 or alias['label'].casefold() in canonical:continue
        aliases.append({'entity':labels[alias['entity_id']], 'alias':alias['label'],'language':alias['language'],
                        'source_id':proof[alias['evidence_ids'][0]]['source_id']})
    return {'version':'website-'+uuid4().hex,'relationships':rows,'aliases':aliases}


class Confirm(BaseModel):
    confirmed: bool = False


class WebsiteIngestion:
    def __init__(self, app, data):
        self.app,self.data=app,Path(data).resolve()
        self.jobs=DiscoveryJobs(self.data/'ingestion')
        self.jobs.on_complete=self.completed

    def _candidate(self, directory):
        store=KnowledgeStore(directory/'approved_sources.json',use_index=False)
        graph=json.loads((directory/'ontology.json').read_text(encoding='utf-8'))
        contacts=json.loads((directory/'contacts.json').read_text(encoding='utf-8'))
        if not store.records:raise ValueError('No usable source content was found')
        report=inspect_ontology(SimpleNamespace(knowledge=store,ontology=graph,contacts=contacts))
        if report['issues'] or any(r['issues'] for r in report['relationships']+report['aliases']):
            raise ValueError('Generated ontology is incompatible with the source registry')
        return store,graph,contacts

    def _select(self, directory):
        store,graph,contacts=self._candidate(directory)
        app=self.app
        previous=app.state.rag.tools.knowledge
        store.settings=previous.settings
        store.embedding_client=previous.embedding_client
        atomic_json(self.data/'active_site.json',{'directory':directory.relative_to(self.data).as_posix(),'updated':time.time()})
        tools=app.state.rag.tools
        tools.root,tools.knowledge,tools.ontology,tools.contacts=directory,store,graph,contacts
        if hasattr(app.state, 'calendar'):
            from .appointments import DemoCalendarProvider
            appointments = app.state.calendar.appointments
            appointments.contacts = {c['contact_id']: c for c in contacts if c.get('active')}
            appointments.calendar = DemoCalendarProvider(set(appointments.contacts))
        app.state.sources=SourceAdmin(app.state.rag)
        app.state.ontology=OntologyAdmin(app.state.rag)
        # Do not carry a prior site's conversation hints into a replacement site.
        from .memory import ConversationStore
        app.state.service.memory=ConversationStore()
        return {'state':'active','version':directory.name,'source_count':len(store.records)}

    def publish(self, job_id, *, automatic=False):
        job=self.jobs.get(job_id)
        graph=self.jobs.ontology(job_id)
        origin=self.jobs.data/'builds'/job_id
        records=json.loads((origin/'sources.json').read_text(encoding='utf-8'))['records']
        records=[validate_record(r) for r in records]
        validate_ontology(graph,records)
        if not records:raise ValueError('No usable source content was found; current knowledge was kept')
        with self.app.state.rag.lock:
            current=self.app.state.rag.tools
            if automatic and current.knowledge.source_path.stat().st_mtime > job['created']:
                raise ValueError('Knowledge changed during discovery; review this build before publishing')
            old_ids={r['source_id'] for r in current.knowledge.records}
            new_ids={r['source_id'] for r in records}
            # A new explicitly supplied website replaces the old dataset. Same-site refreshes detect missing pages.
            from urllib.parse import urlsplit
            same_site=bool(current.knowledge.records) and urlsplit(current.knowledge.records[0]['canonical_url']).netloc==urlsplit(records[0]['canonical_url']).netloc
            threshold=float(setting('WEBSITE_ASSISTANT_MAX_SOURCE_DROP_FRACTION','0.30'))
            if automatic and same_site and len(old_ids-new_ids)/len(old_ids)>threshold:
                raise ValueError('Source coverage dropped; review this build and publish it explicitly')
            directory=self.data/'site_builds'/uuid4().hex
            directory.mkdir(parents=True)
            atomic_json(directory/'approved_sources.json',{'snapshot':job_id,'records':records})
            atomic_json(directory/'ontology.json',convert_ontology(graph))
            # Crawled contact labels never grant appointment or routing permissions.
            atomic_json(directory/'contacts.json',[])
            atomic_json(directory/'rag_settings.json',current.knowledge.settings.model_dump())
            store,_,_=self._candidate(directory)
            atomic_json(directory/'knowledge'/'index.json',{'generated_at':str(time.time()),'records':store.records,'chunks':store.chunks})
            atomic_json(directory/'build.json',{'job_id':job_id,'homepage':job['homepage'],'created':time.time(),'source_count':len(records)})
            return self._select(directory)

    def completed(self, job_id):
        if setting('WEBSITE_ASSISTANT_AUTO_ACTIVATE','true').lower()!='true':
            return {'state':'staged','reason':'Automatic publication is disabled'}
        try:return self.publish(job_id,automatic=True)
        except (ValueError,OSError,KeyError,TypeError):
            return {'state':'review_required','reason':'Build requires review or could not be published. Current knowledge was kept.'}

    def status(self):
        current=selected_directory(self.data)
        versions=[]
        for path in sorted((self.data/'site_builds').glob('*/build.json'),key=lambda p:p.stat().st_mtime,reverse=True)[:20]:
            info=json.loads(path.read_text(encoding='utf-8'));versions.append({**info,'version':path.parent.name,'active':path.parent==current})
        return {'jobs':self.jobs.list(),'versions':versions,'source_count':len(self.app.state.rag.tools.knowledge.records)}

    def restore(self, version):
        if len(version)!=32 or any(c not in '0123456789abcdef' for c in version):raise ValueError('Invalid version')
        with self.app.state.rag.lock:return self._select(self.data/'site_builds'/version)


def install(app,data):
    ingestion=WebsiteIngestion(app,data);app.state.ingestion=ingestion
    router=APIRouter(prefix='/api/admin/ingestion')
    @router.get('')
    def status():return ingestion.status()
    @router.post('')
    def start(body:StartDiscovery):
        try:return ingestion.jobs.start(body)
        except JobConflict as error:raise HTTPException(409,str(error)) from error
    @router.get('/{job_id}')
    def job(job_id:str):
        try:return ingestion.jobs.get(job_id)
        except KeyError:raise HTTPException(404,'Build not found')
    @router.post('/{job_id}/cancel')
    def cancel(job_id:str):
        try:return ingestion.jobs.cancel(job_id)
        except KeyError:raise HTTPException(404,'Build not found')
    @router.post('/{job_id}/publish')
    def publish(job_id:str,body:Confirm):
        if not body.confirmed:raise HTTPException(422,'Confirm publication')
        try:return ingestion.publish(job_id)
        except (ValueError,KeyError,OSError,TypeError):raise HTTPException(409,'Build is not ready or cannot be published')
    @router.post('/versions/{version}/restore')
    def restore(version:str,body:Confirm):
        if not body.confirmed:raise HTTPException(422,'Confirm restoration')
        try:return ingestion.restore(version)
        except (ValueError,KeyError,OSError,TypeError):raise HTTPException(409,'Version cannot be restored')
    app.include_router(router)
