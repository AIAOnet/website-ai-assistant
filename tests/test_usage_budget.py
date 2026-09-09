import asyncio
import json
import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from site_runtime import configuration
from site_runtime.knowledge import EmbeddingClient, content_checksum
from site_runtime.models import ModelResponse
from site_runtime.service import AssistantConfiguration, AssistantService
from site_runtime.provider_probe import ProviderProbe
from site_runtime.embedding_settings import EmbeddingProviderProbe
from site_runtime.tools import AssistantTools
from site_runtime.usage_budget import UsageBudget, UsageBudgetChange, UsageBudgetExceeded


class UsageBudgetTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'usage.db';self.now=[datetime(2026,9,9,12,tzinfo=timezone.utc)]
        self.budget=UsageBudget(self.path,clock=lambda:self.now[0])

    def save(self,gd=2,gm=4,ed=3,em=6):
        return self.budget.save(UsageBudgetChange(generation_daily=gd,generation_monthly=gm,
            embedding_daily=ed,embedding_monthly=em,confirmed=True))

    def test_daily_monthly_limits_and_restart(self):
        self.save();self.budget.reserve('generation');self.budget.reserve('generation')
        with self.assertRaises(UsageBudgetExceeded):self.budget.reserve('generation')
        restarted=UsageBudget(self.path,clock=lambda:self.now[0])
        self.assertEqual(restarted.status()['generation']['daily_used'],2)
        self.now[0]=datetime(2026,9,10,1,tzinfo=timezone.utc)
        restarted.reserve('generation');restarted.reserve('generation')
        self.now[0]=datetime(2026,9,11,1,tzinfo=timezone.utc)
        with self.assertRaises(UsageBudgetExceeded):restarted.reserve('generation')

    def test_concurrent_reservations_do_not_exceed_limit(self):
        self.save(gd=5,gm=5);barrier=threading.Barrier(15);results=[];lock=threading.Lock()
        def reserve():
            item=UsageBudget(self.path,clock=lambda:self.now[0]);barrier.wait()
            try:item.reserve('generation');allowed=True
            except UsageBudgetExceeded:allowed=False
            with lock:results.append(allowed)
        threads=[threading.Thread(target=reserve) for _ in range(15)]
        [thread.start() for thread in threads];[thread.join(10) for thread in threads]
        self.assertEqual(sum(results),5);self.assertEqual(len(results),15)

    def test_embedding_reserves_each_input_before_network(self):
        self.save(ed=2,em=2);client=EmbeddingClient('https://provider.example','key','model',1);client.usage_budget=self.budget
        with patch('urllib.request.urlopen') as request:
            with self.assertRaises(UsageBudgetExceeded):client.embed(['one','two','three'])
            request.assert_not_called()

    def test_generation_exhaustion_uses_source_fallback_without_provider(self):
        self.save(gd=0,gm=0)
        root=Path(self.temp.name)/'knowledge';root.mkdir()
        content='The sensor measures water.'
        record={'source_id':'sensor_1','title':'Sensor','content':content,'canonical_url':'https://fixture.example/sensor','category':'information','language':'en','source_status':'active','retrieved_at':'2026-09-09T00:00:00Z','checksum':content_checksum(content)}
        (root/'approved_sources.json').write_text(json.dumps([record]),encoding='utf-8')
        with patch.object(configuration,'_values',{}):
            provider=AsyncMock();service=AssistantService(AssistantTools(root),provider=provider,
                configuration=AssistantConfiguration('https://fixture','key','model',1),usage_budget=self.budget)
            answer=asyncio.run(service.checked_grounded_answer('What does the sensor measure?','en'))
        self.assertTrue(answer.used_fallback);provider.generate.assert_not_awaited()
        self.assertIn('water',answer.result.answer)

    def test_invalid_limits_and_measurement_status(self):
        with self.assertRaises(ValueError):self.save(gd=5,gm=4)
        status=self.budget.status();self.assertEqual(status['measurement']['embedding'],'input_texts')

    def test_provider_probes_do_not_call_network_when_exhausted(self):
        self.save(gd=0,gm=0,ed=0,em=0)
        provider=AsyncMock();service=SimpleNamespace(provider=provider,usage_budget=self.budget)
        async def generate(messages):self.budget.reserve('generation')
        service.generate_grounded_answer=generate
        result=asyncio.run(ProviderProbe(service).run())
        self.assertEqual(result['outcome'],'usage_budget_exhausted');provider.generate.assert_not_awaited()
        client=EmbeddingClient('https://provider.example','key','model',1);client.usage_budget=self.budget
        embedding=EmbeddingProviderProbe(SimpleNamespace(client=client)).run()
        self.assertEqual(embedding['outcome'],'usage_budget_exhausted')
