import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from site_runtime import configuration
from site_runtime.knowledge import content_checksum
from site_runtime.models import ModelResponse
from site_runtime.service import AssistantConfiguration, AssistantService
from site_runtime.tools import AssistantTools


class CitationDisplayTests(unittest.TestCase):
    def run_answer(self, draft):
        with tempfile.TemporaryDirectory() as directory, patch.object(configuration, '_values', {}):
            root = Path(directory)
            content = 'The sensor measures water. The sensor stores hourly readings.'
            record = dict(source_id='sensor_1', title='Sensor', content=content,
                canonical_url='https://fixture.example/sensor', category='information', language='en',
                source_status='active', retrieved_at='2026-09-09T00:00:00Z', checksum=content_checksum(content))
            (root/'approved_sources.json').write_text(json.dumps([record]), encoding='utf-8')
            service = AssistantService(AssistantTools(root), configuration=AssistantConfiguration('','','',30))
            with patch.object(service, 'generate_grounded_answer', new=AsyncMock(return_value=ModelResponse(draft,'fixture'))):
                return asyncio.run(service.checked_grounded_answer('What does the sensor measure?', 'en'))

    def test_validated_markers_removed_links_retained(self):
        answer=self.run_answer('The sensor measures water. [sensor_1] The sensor stores hourly readings.[sensor_1]')
        self.assertFalse(answer.used_fallback)
        self.assertEqual(answer.result.answer,'The sensor measures water. The sensor stores hourly readings.')
        self.assertEqual(answer.result.sources[0]['url'],'https://fixture.example/sensor')
        self.assertEqual(len(answer.result.sources),1)

    def test_unknown_marker_still_rejected(self):
        answer=self.run_answer('The sensor measures water. [unapproved]')
        self.assertTrue(answer.used_fallback)
        self.assertIn('unknown_citation',answer.reasons)

    def test_uncited_answer_still_rejected(self):
        answer=self.run_answer('The sensor measures water.')
        self.assertTrue(answer.used_fallback)
        self.assertIn('missing_citation',answer.reasons)
