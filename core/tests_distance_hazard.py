from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from core.models import AstronautProfile
from core.services import (
    COMM_DELAY_NOTE,
    DISTANCE_GUIDANCE,
    NASA_DISTANCE_HAZARD_URL,
    calculate_risk_vectors,
)


def _distance(evals):
    return next(v for v in calculate_risk_vectors(evals) if v['id'] == 'distance_from_earth')


class DistanceHazardTileTests(SimpleTestCase):
    def test_five_hazards_with_distance_last_and_wide(self):
        vectors = calculate_risk_vectors({})
        self.assertEqual(len(vectors), 5)
        self.assertEqual(vectors[-1]['id'], 'distance_from_earth')
        self.assertTrue(vectors[-1]['wide'])

    def test_no_data_when_no_indicator_has_data(self):
        tile = _distance({'sleep': {'status': 'no_data'}})
        self.assertEqual(tile['status'], 'nodata')
        self.assertIn(DISTANCE_GUIDANCE['nodata'], tile['indicators'])

    def test_any_red_indicator_makes_it_critical(self):
        tile = _distance({'cabin_co2': {'status': 'critical'}, 'sleep': {'status': 'nominal'}})
        self.assertEqual(tile['status'], 'critical')
        self.assertIn('flight surgeon', tile['indicators'])

    def test_amber_without_red_is_caution(self):
        tile = _distance({'mood': {'status': 'caution'}, 'hrv': {'status': 'nominal'}})
        self.assertEqual(tile['status'], 'caution')
        self.assertTrue(tile['is_caution'])

    def test_all_nominal_is_nominal_and_always_shows_comm_delay(self):
        tile = _distance({'resting_hr': {'status': 'nominal'}})
        self.assertEqual(tile['status'], 'nominal')
        self.assertIn(COMM_DELAY_NOTE, tile['indicators'])

    def test_experimental_tiles_do_not_drive_it(self):
        tile = _distance({'hydration': {'status': 'critical'}, 'mood_stress': {'status': 'critical'}})
        self.assertEqual(tile['status'], 'nodata')


class DistanceHazardPagesTests(TestCase):
    def test_sources_page_cites_nasa_distance_hazard(self):
        html = self.client.get(reverse('core:sources')).content.decode()
        self.assertIn('NASA HRP hazards', html)
        self.assertIn(NASA_DISTANCE_HAZARD_URL, html)

    def test_dashboard_renders_distance_tile(self):
        AstronautProfile.objects.create(name='Amara Reyes', email='amara.reyes@mission.example', is_active_commander=True)
        user = get_user_model().objects.create_user('amara.reyes', email='amara.reyes@mission.example', password='pw-for-tests-only')
        self.client.force_login(user)
        resp = self.client.get(reverse('core:dashboard'))
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()
        self.assertIn('Distance from Earth', html)
        self.assertIn('5 HRP Hazards', html)
