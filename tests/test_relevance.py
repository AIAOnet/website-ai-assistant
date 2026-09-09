import json
import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from site_runtime import configuration
from site_runtime.knowledge import KnowledgeStore, content_checksum, page_quality
from site_runtime.grounding import grounded_summary
from site_runtime.tools import AssistantTools
from site_runtime.service import AssistantService, AssistantConfiguration


class RelevanceTests(unittest.TestCase):
    def test_definition_prefers_explanation_over_sales_sentence(self):
        store = self.placeholder_store('Archive_Portal')
        record = dict(store.records[1], content=(
            'Together we decide whether Archive_Portal is right for you.\n\n'
            'Archive_Portal is a workspace for searching organizational documents. '
            'It runs inside the organization. Access requires an approved account.'))
        answer = grounded_summary([record], 'en', query='What is Archive_Portal?')
        self.assertTrue(answer.answer.startswith('Archive_Portal is a workspace'))
        self.assertIn('Access requires', answer.answer)

    def test_policy_passage_and_qualification_stay_together(self):
        record = dict(self.store.records[0], content=(
            'These terms cover accounts, comments and bookings.\n\n'
            'Users protect passwords. Accounts cannot be transferred.\n\n'
            'Comments must be reviewed before publication. '
            'However, staff announcements are exempt. Do not publish personal data.'))
        answer = grounded_summary([record], 'en', query='What are the rules for posting comments?')
        self.assertTrue(answer.answer.startswith('Comments must'))
        self.assertIn('announcements are exempt', answer.answer)
        self.assertIn('Do not publish personal data.', answer.answer)
        self.assertNotIn('passwords', answer.answer)

    def test_correct_passage_selected_from_multi_chunk_document(self):
        record = dict(self.store.records[0], content=(
            'These terms cover comments and accounts. ' + 'Protect your account password. '*65 +
            '\n\nComments must be reviewed before publication. Staff announcements are exempt.'))
        record['checksum'] = content_checksum(record['content'])
        path=self.root/'long.json'; path.write_text(json.dumps([record]),encoding='utf-8')
        result=KnowledgeStore(path).search('What are the rules for comments?', 'en')
        self.assertIn('Comments must be reviewed', result[0]['content'])

    def test_swedish_definition_and_short_answer(self):
        record=dict(self.store.records[-1],content=(
            'Välj Fukt_Koll för nästa projekt.\n\n'
            'Fukt_Koll är en sensor som mäter markfuktighet. Den kräver kalibrering.'))
        answer=grounded_summary([record],'sv',query='Vad är Fukt_Koll?')
        self.assertTrue(answer.answer.startswith('Fukt_Koll är en sensor'))
        self.assertIn('kräver kalibrering',answer.answer)
        record['content']='Open daily.'
        self.assertEqual(grounded_summary([record],'en',query='hours').answer,'Open daily.')

    def placeholder_store(self, name='Garden Sensor', language='en'):
        records = []
        for identifier, content in (
            ('aaa', name + ('\nContent coming soon.' if language == 'en' else '\nInnehåll kommer snart.')),
            ('zzz', name + (' measures soil moisture and reports readings every hour.' if language == 'en'
                           else ' mäter markfuktighet och rapporterar värden varje timme.')),
        ):
            record = dict(self.store.records[0], source_id=identifier, title=name,
                          language=language, content=content,
                          canonical_url='https://different.example/'+identifier,
                          checksum=content_checksum(content))
            records.append(record)
        path = self.root/'placeholder.json'
        path.write_text(json.dumps(records), encoding='utf-8')
        return KnowledgeStore(path)

    def test_substantive_short_page_beats_placeholder_with_ontology_boost(self):
        for name, language in [('Garden Sensor','en'), ('Archive_Portal','en'), ('Fuktsensor','sv')]:
            with self.subTest(name=name):
                store = self.placeholder_store(name, language)
                result = store.search_details(name, language, ontology_source_scores={'aaa':1.0})
                self.assertEqual(result['records'][0]['source_id'], 'zzz')
                self.assertEqual(result['records'][0]['page_quality_factor'], 1.0)
                self.assertTrue(any(r['source_id']=='aaa' for r in result['records']))

    def test_short_facts_categories_and_substantive_announcements_not_penalized(self):
        for content in ['Office hours: Monday to Friday, 09:00–17:00.',
                        'Resources: Installation guide, maintenance guide, warranty policy.',
                        'A museum exhibit is coming soon.',
                        'Content coming soon. ' + 'The device measures soil moisture hourly. '*20]:
            self.assertEqual(page_quality(content, 'information'), 1.0)
        self.assertEqual(page_quality('Content coming soon.', 'Which content coming soon notices exist?'), 1.0)

    def test_semantic_score_cannot_resurrect_thin_placeholder(self):
        store = self.placeholder_store()
        store.settings = store.settings.model_copy(update={'use_semantic':True, 'lexical_weight':.5})
        store.embedding_client.endpoint='https://provider.example/'
        store.embedding_client.key='fixture'; store.embedding_client.model='fixture'
        with patch.object(store.embedding_client, 'embed', side_effect=lambda inputs:[[1.,0.] for _ in inputs]):
            result=store.search_details('Garden Sensor', 'en')
            self.assertTrue(result['semantic_used'])
            self.assertEqual(result['records'][0]['source_id'], 'zzz')

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        config = patch.object(configuration, '_values', {})
        config.start(); self.addCleanup(config.stop)
        records = []
        for identifier, title, content, language in (
            ('aaa', 'Terms of Use | Acme', 'Acme services require accounts. Users must protect passwords. Comments are moderated.', 'en'),
            ('zzz', 'Data analysis | Acme', 'Data analysis supports decisions. Our services include forecasting and visualization. We improve data quality.', 'en'),
            ('bbb', 'Privacy | Acme', 'Acme protects personal data. We retain account data for thirty days.', 'en'),
            ('ccc', 'Villkor | Acme', 'Acme erbjuder tjänster. Konton kräver lösenord.', 'sv'),
            ('ddd', 'Dataanalys | Acme', 'Vi erbjuder dataanalys och visualisering. Våra tjänster hjälper organisationer fatta beslut.', 'sv'),
        ):
            records.append(dict(source_id=identifier, title=title, content=content,
                canonical_url='https://acme.example/'+identifier, category='general',
                language=language, retrieved_at='2026-09-08T00:00:00Z',
                checksum=content_checksum(content), source_status='active'))
        path = self.root/'approved_sources.json'
        path.write_text(json.dumps(records), encoding='utf-8')
        self.store = KnowledgeStore(path)

    def test_natural_question_ranks_service_above_brand_and_policy(self):
        records = self.store.search('What data analysis services does Acme offer?', 'en')
        self.assertEqual(records[0]['source_id'], 'zzz')
        self.assertGreater(records[0]['lexical_score'], .6)

    def test_chat_does_not_exclude_services_with_generic_category(self):
        tools = AssistantTools(self.root)
        service = AssistantService(tools, configuration=AssistantConfiguration('', '', '', 30))
        question = 'What data analysis services does Acme offer?'
        answer = asyncio.run(service.respond_async('relevance', question, 'en'))
        self.assertEqual(answer['sources'][0]['url'], 'https://acme.example/zzz')
        self.assertIn('forecasting', answer['answer'])
        self.assertFalse(answer['appointment_available'])
        self.assertEqual(self.store.search(question, 'en', category='service'), [])

    def test_short_queries_and_real_policy_questions(self):
        for question, expected in [('data analysis', 'zzz'), ('passwords', 'aaa'), ('privacy personal data', 'bbb')]:
            with self.subTest(question=question):
                self.assertEqual(self.store.search(question, 'en')[0]['source_id'], expected)
        self.assertEqual(self.store.search('volcanic basalt', 'en'), [])

    def test_swedish_and_language_filter(self):
        self.assertEqual(self.store.search('Vilka dataanalys tjänster erbjuder Acme?', 'sv')[0]['source_id'], 'ddd')
        self.assertTrue(all(r['language']=='sv' for r in self.store.search('Acme', 'sv')))

    def test_ontology_only_discovery_still_works(self):
        result = self.store.search_details('unlistedalias', 'en', ontology_source_ids=['zzz'])
        self.assertEqual(result['records'][0]['source_id'], 'zzz')

    def test_semantic_and_provider_failure_paths(self):
        self.store.settings = self.store.settings.model_copy(update={'use_semantic':True, 'lexical_weight':.5})
        self.store.embedding_client.endpoint = 'https://provider.example/'
        self.store.embedding_client.key = 'test-only'
        self.store.embedding_client.model = 'fixture'
        with patch.object(
            self.store.embedding_client, 'embed', side_effect=lambda inputs:[[1.,0.] for _ in inputs]):
            result = self.store.search_details('data analysis', 'en')
            self.assertTrue(result['semantic_used'])
            self.assertEqual(result['records'][0]['source_id'], 'zzz')
        with patch.object(
            self.store.embedding_client, 'embed', side_effect=RuntimeError('offline')):
            result = self.store.search_details('data analysis', 'en')
            self.assertEqual(result['fallback_reason'], 'embedding_unavailable')
            self.assertEqual(result['records'][0]['source_id'], 'zzz')

    def test_concise_verbatim_sentences_and_citations(self):
        records = self.store.search('data analysis', 'en')
        records[0]['content'] = ('Unrelated background information. '*40 +
                                'Data analysis supports decisions. Data analysis includes forecasting.')
        answer = grounded_summary(records, 'en', query='data analysis')
        self.assertIn('Data analysis includes forecasting.', answer.answer)
        self.assertLess(len(answer.answer), 900)
        self.assertEqual(answer.sources[0]['url'], records[0]['canonical_url'])
        self.assertEqual(grounded_summary([], 'sv').status, 'UNAVAILABLE')
        self.assertIn('professional decision', grounded_summary(records, 'en', True, 'data analysis').answer)

    def test_lower_ranked_homepage_does_not_displace_service_sentences(self):
        records = self.store.search('data analysis', 'en')
        first = records[0]
        second = dict(first, title='Homepage', canonical_url='https://acme.example/',
                      content='Data analysis data analysis appears in this navigation label.')
        answer = grounded_summary([first, second], 'en', query='data analysis')
        self.assertIn('forecasting', answer.answer)
        self.assertNotIn('navigation label', answer.answer)
        self.assertEqual(len(answer.sources), 1)
