from datetime import UTC, datetime

from tokmo.programs.news_event_admission import AdmissionInput, admit_news_event

UTC = UTC


def test_activation_off_is_shadow_and_cutoff_mute_no_change_are_closed():
    base = dict(
        episode_id="nep-1",
        revision=1,
        event_class="CORE_BREAKING",
        observed_at=datetime(2026, 8, 21, tzinfo=UTC),
        cutoff=datetime(2026, 8, 21, tzinfo=UTC),
        muted=False,
        activation_enabled=False,
    )
    assert (
        admit_news_event(
            AdmissionInput(material_change="OFFICIAL_CONFIRMATION", **base)
        )["status"]
        == "SHADOW"
    )
    assert (
        admit_news_event(AdmissionInput(material_change=None, **base))["status"]
        == "REJECTED_NO_CHANGE"
    )
    assert (
        admit_news_event(
            AdmissionInput(material_change="SCALE_CHANGE", **(base | {"muted": True}))
        )["status"]
        == "REJECTED_MUTED"
    )


def test_core_night_immediate_and_ordinary_held():
    base = dict(
        episode_id="nep-2",
        revision=1,
        material_change="SCALE_CHANGE",
        observed_at=datetime(2026, 8, 21, 18, tzinfo=UTC),
        cutoff=datetime(2026, 8, 21, tzinfo=UTC),
        muted=False,
        activation_enabled=True,
    )
    assert (
        admit_news_event(AdmissionInput(event_class="CORE_BREAKING", **base))["status"]
        == "PENDING"
    )
    assert (
        admit_news_event(AdmissionInput(event_class="ORDINARY", **base))["status"]
        == "HELD_MORNING"
    )


def test_weak_core_label_is_held_for_morning_not_sent_immediately():
    item = AdmissionInput(
        episode_id="nep-weak",
        revision=1,
        event_class="CORE_BREAKING",
        material_change="SCALE_CHANGE",
        observed_at=datetime(2026, 8, 21, 21, tzinfo=UTC),
        cutoff=datetime(2026, 8, 21, tzinfo=UTC),
        muted=False,
        activation_enabled=True,
        immediate_eligible=False,
    )
    assert admit_news_event(item)["status"] == "HELD_MORNING"
