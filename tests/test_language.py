import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from site_runtime import configuration
from site_runtime.grounding import grounded_summary
from site_runtime.language import language_mismatch
from site_runtime.knowledge import content_checksum
from site_runtime.models import ModelResponse
from site_runtime.service import AssistantService, AssistantConfiguration
from site_runtime.tools import AssistantTools


class LanguageTests(unittest.TestCase):
    def record(self, text, identifier='source'):
        return dict(source_id=identifier, title='Sensor', canonical_url='https://fixture.example/'+identifier,
                    content=text, category='information', language='en', source_status='active',
                    retrieved_at='2026-09-09T00:00:00Z', checksum=content_checksum(text))

    def test_opposite_languages_and_mixed_sentences(self):
        self.assertTrue(language_mismatch('Teknisk kunskap och användningsfall för sensorn.', 'en'))
        self.assertTrue(language_mismatch('The sensor is available for your organization.', 'sv'))
        self.assertTrue(language_mismatch('The sensor is available. Den är inte tillgänglig för privatpersoner.', 'en'))
        self.assertFalse(language_mismatch('The sensor is available for your organization.', 'en'))
        self.assertFalse(language_mismatch('Sensorn är tillgänglig för vår organisation.', 'sv'))

    def test_names_identifiers_and_short_facts_are_retained(self):
        for text in ['Stockholm', 'Fukt_Koll X-200', '09:00–17:00', 'Open daily.', '[web_och_for_123] Sensor']:
            for language in ['en', 'sv']:
                self.assertFalse(language_mismatch(text, language))

    def test_mixed_passage_is_not_partially_quoted(self):
        mixed=self.record('The sensor is available. Den är inte tillgänglig för privatpersoner.')
        answer=grounded_summary([mixed],'en',query='sensor')
        self.assertEqual(answer.status,'UNAVAILABLE')
        self.assertEqual(answer.sources,[])
        self.assertNotIn('sensor is available',answer.answer)

    def test_compatible_alternative_keeps_its_own_citation(self):
        records=[self.record('Teknisk kunskap och användningsfall för sensorn.'),
                 self.record('The sensor measures water levels.', 'english')]
        answer=grounded_summary(records,'en',query='sensor')
        self.assertEqual(answer.answer,'The sensor measures water levels.')
        self.assertEqual(answer.sources[0]['url'],'https://fixture.example/english')
        self.assertEqual(len(answer.sources),1)

    def test_generated_wrong_language_falls_back_before_grounding(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(configuration,'_values',{}):
            root=Path(directory)
            (root/'approved_sources.json').write_text(json.dumps([self.record('The sensor measures water levels.')]),encoding='utf-8')
            service=AssistantService(AssistantTools(root),configuration=AssistantConfiguration('','','',30))
            with patch.object(service,'generate_grounded_answer',new=AsyncMock(return_value=ModelResponse(
                    'Sensorn är tillgänglig för vår organisation.','fixture'))):
                answer=asyncio.run(service.checked_grounded_answer('What is the sensor?','en'))
            self.assertTrue(answer.used_fallback)
            self.assertIn('language_mismatch',answer.reasons)
            self.assertIn('water levels',answer.result.answer)
