"""Tests for the public /sources/ provenance page and its catalog helper."""

from datetime import datetime, timezone as dt_timezone

from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from core import donki, rules, sources
from core.constants import METRIC_CHOICES
from core.models import SpaceWeatherSnapshot
from core.services import EXPERIMENTAL_TAG


class RuleBandTextTests(TestCase):
    def test_every_official_indicator_has_amber_and_red_from_its_docstring(self):
        for key in rules.INDICATORS:
            bands = sources.rule_band_text(key)
            doc = rules.RULES[key].__doc__
            self.assertTrue(bands["amber"], msg=key)
            self.assertTrue(bands["red"], msg=key)
            # The assembled text is a subsequence of the docstring words, so a
            # later edit of the rule docstring shows up on the sources page.
            self.assertIn(bands["amber"].split()[0], doc, msg=key)
            self.assertIn(bands["red"].split()[0], doc, msg=key)
            for phrase in bands["amber"].replace("(", " ").replace(")", " ").split(","):
                snippet = phrase.strip()
                if len(snippet) >= 8:
                    self.assertIn(snippet[:8], " ".join(doc.split()), msg=f"{key} amber")

    def test_known_bands_match_rules_docstrings_verbatim(self):
        # Anchors copied from core/rules.py function docstrings.
        expected = {
            "resting_hr": (
                "10-20% above for 3 days.",
                "more than 20% above for 3 days, or above 100 bpm at rest (absolute).",
            ),
            "hrv": (
                "15-30% below for 3 days.",
                "more than 30% below for 5 days, or amber plus a red resting HR.",
            ),
            "exercise": (
                "70-90% of plan for 2 weeks, or pain 4-6 for 3 days.",
                "under 70% of plan for 2 weeks, or pain 7+ for 2 days.",
            ),
            "body_temp": (
                "+0.5 to +1.0 C for 2 days, or a new symptom (on its own).",
                "+1.0 C or more, or amber for 2 more days (4 in a row) with symptoms.",
            ),
            "sleep": (
                "more than 1 h below for 3 nights.",
                "more than 2 h below for 4 nights, or under 5 h on 2 nights in a row "
                "(the 5 h rule is absolute and needs no baseline).",
            ),
            "mood": (
                "drop of 1 point or more for 3 days (Science Lead table, verbatim).",
                "drop of 2 points or more for 3 days, or drop of 1 point or more for 7 days.",
            ),
            "cabin_co2": (
                "3,500 ppm or more for 24 h.",
                "above 5,000 ppm for 6 h in a row, or above 7,000 at any time.",
            ),
        }
        self.assertEqual(set(expected), set(rules.INDICATORS))
        for key, (amber, red) in expected.items():
            bands = sources.rule_band_text(key)
            self.assertEqual(bands["amber"], amber, msg=key)
            self.assertEqual(bands["red"], red, msg=key)
            self.assertIn(amber, " ".join((rules.RULES[key].__doc__ or "").split()), msg=key)


class SourcesPageTests(TestCase):
    def test_sources_page_is_public_and_returns_200(self):
        resp = self.client.get(reverse("core:sources"))
        self.assertEqual(resp.status_code, 200)
        # Must not bounce to login (LoginRequiredMiddleware).
        self.assertIsNone(resp.get("Location"))
        self.assertNotIn("/login/", resp.content.decode("utf-8")[:200])

    def test_page_lists_seven_indicators_donki_disclaimer_and_experimental_labels(self):
        resp = self.client.get(reverse("core:sources"))
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode("utf-8")
        names = dict(METRIC_CHOICES)
        for key in rules.INDICATORS:
            self.assertIn(names[key], html, msg=key)
        self.assertIn("DONKI", html)
        self.assertIn("not a medical device", html.lower())
        self.assertIn(EXPERIMENTAL_TAG, html)
        self.assertIn("Physio Reserve", html)
        self.assertIn("Cumulative Stress", html)
        self.assertIn("Bio-Index", html)
        self.assertIn("Hydration", html)
        self.assertIn("simulated", html.lower())
        self.assertIn("OSDR", html)
        self.assertIn("150", html)
        self.assertIn(str(rules.BASELINE_DAYS), html)
        self.assertIn("COMPANION_LLM_ENABLED=True", html)

    def test_rule_texts_on_the_page_match_the_rules_source(self):
        resp = self.client.get(reverse("core:sources"))
        html = resp.content.decode("utf-8")
        for key in rules.INDICATORS:
            bands = sources.rule_band_text(key)
            self.assertIn(bands["amber"], html, msg=f"{key} amber")
            self.assertIn(bands["red"], html, msg=f"{key} red")

    def test_sample_snapshot_source_and_fetched_at_are_shown(self):
        fetched = timezone.make_aware(datetime(2026, 11, 15, 12, 30, 0))
        SpaceWeatherSnapshot.objects.create(
            source=donki.SOURCE_SAMPLE,
            payload={"label": donki.LABEL, "flares": []},
            fetched_at=fetched,
        )
        resp = self.client.get(reverse("core:sources"))
        html = resp.content.decode("utf-8")
        self.assertIn(donki.SAMPLE_LABEL, html)
        self.assertIn("2026-11-15 12:30:00", html)

    def test_donki_snapshot_uses_model_source_label(self):
        fetched = datetime(2026, 11, 14, 8, 0, 0, tzinfo=dt_timezone.utc)
        SpaceWeatherSnapshot.objects.create(
            source=donki.SOURCE_DONKI,
            payload={"label": donki.LABEL},
            fetched_at=fetched,
        )
        resp = self.client.get(reverse("core:sources"))
        html = resp.content.decode("utf-8")
        self.assertIn("NASA DONKI", html)
        self.assertIn("2026-11-14", html)

    def test_login_required_middleware_does_not_redirect_sources(self):
        # Settings already include LoginRequiredMiddleware; this is an explicit
        # logged-out fetch of a protected-looking path that must stay public.
        with override_settings(ALLOWED_HOSTS=["testserver", "localhost"]):
            resp = self.client.get("/sources/", follow=False)
        self.assertEqual(resp.status_code, 200)
