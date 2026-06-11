# tests/dashboard/test_dashboard_api.py
"""Dashboard JSON API + page (visual layer v1)."""
from __future__ import annotations

import pytest


@pytest.fixture
def admin_client(db, client):
    from django.contrib.auth.models import User

    user = User.objects.create_superuser("admin", "admin@example.com", "pw")
    client.force_login(user)
    return client


def _profile():
    from django.contrib.auth.models import User
    from linkedin.models import LinkedInProfile

    u = User.objects.create(username=f"acct{User.objects.count()}")
    return LinkedInProfile.objects.create(
        user=u, linkedin_username="a@b.c", linkedin_password="p", has_inmail=True,
    )


@pytest.mark.django_db
class TestDashboardApi:
    def test_kpis(self, admin_client):
        from crm.models import Lead
        from linkedin.models import ActionLog, Campaign, LeadCampaignState

        prof = _profile()
        camp = Campaign.objects.create(name="C")
        ActionLog.objects.create(linkedin_profile=prof, campaign=camp, action_type="connect")
        ActionLog.objects.create(linkedin_profile=prof, campaign=camp, action_type="message")
        lead = Lead.objects.create(linkedin_url="https://www.linkedin.com/in/x/", public_identifier="x")
        # Replies KPI = conversations WE started (contacted_by_tool) with an inbound message.
        from linkedin.models import Message, MessageThread

        thread = MessageThread.objects.create(lead=lead, account=prof, contacted_by_tool=True)
        Message.objects.create(thread=thread, direction="in", linkedin_message_id="r1")

        data = admin_client.get("/dashboard/api/kpis/").json()
        assert data["connection_requests"] == 1
        assert data["messages_sent"] == 1
        assert data["replies"] == 1

    def test_senders(self, admin_client):
        prof = _profile()
        data = admin_client.get("/dashboard/api/senders/").json()
        ids = [s["id"] for s in data["senders"]]
        assert prof.pk in ids
        sender = next(s for s in data["senders"] if s["id"] == prof.pk)
        assert sender["has_inmail"] is True
        assert "connect" in sender["usage"]

    def test_sequence_graph(self, admin_client):
        from linkedin.models import Sequence
        from linkedin.models import SequenceStep as S
        from tests.factories import UserFactory

        seq = Sequence.objects.create(name="Seq", owner=UserFactory())
        connect = S.objects.create(sequence=seq, branch=S.Branch.ROOT, step_type=S.StepType.CONNECT)
        S.objects.create(sequence=seq, parent=connect, branch=S.Branch.SUCCESS, step_type=S.StepType.WAIT)
        S.objects.create(sequence=seq, parent=connect, branch=S.Branch.FAILURE, step_type=S.StepType.INMAIL)

        data = admin_client.get(f"/dashboard/api/sequence/{seq.pk}/").json()
        assert data["root"]["step_type"] == "connect"
        assert data["root"]["success"][0]["step_type"] == "wait"
        assert data["root"]["failure"][0]["step_type"] == "inmail"

    def test_page_loads(self, admin_client):
        resp = admin_client.get("/dashboard/")
        assert resp.status_code == 200
        assert b"Dashboard" in resp.content

    def test_leads_csv_import_and_export(self, admin_client):
        import json

        from crm.models import Lead

        csv_text = "linkedin_url,first_name\nhttps://www.linkedin.com/in/jane/,Jane\nhttps://www.linkedin.com/in/bob/,Bob"
        r = admin_client.post(
            "/dashboard/api/leads/csv/", data=json.dumps({"name": "L", "csv_text": csv_text}),
            content_type="application/json",
        ).json()
        assert r["created"] == 2
        assert Lead.objects.filter(lead_list_id=r["list_id"], first_name="Jane").exists()
        exp = admin_client.get(f"/dashboard/api/leadlist/{r['list_id']}/export/")
        assert exp.status_code == 200
        assert b"jane" in exp.content.lower()

    def test_leads_search_queues(self, admin_client):
        import json

        from linkedin.models import LeadList

        r = admin_client.post(
            "/dashboard/api/leads/search/", data=json.dumps({"name": "S", "query": "fintech founder"}),
            content_type="application/json",
        ).json()
        assert r["queued"] is True
        ll = LeadList.objects.get(pk=r["list_id"])
        assert ll.pending_search is True
        assert "keywords=fintech" in ll.source_url

    def test_requires_staff(self, client, db):
        resp = client.get("/dashboard/api/kpis/")
        assert resp.status_code in (302, 403)

    def test_create_sequence_and_branching_steps(self, admin_client):
        import json

        from linkedin.models import Sequence, SequenceStep

        seq_id = admin_client.post(
            "/dashboard/api/sequences/create/", data=json.dumps({"name": "Built"}),
            content_type="application/json",
        ).json()["id"]
        assert Sequence.objects.filter(pk=seq_id, name="Built").exists()

        # Root connect, then a success (accepted) and failure (not-accepted) branch.
        root = admin_client.post(
            f"/dashboard/api/sequence/{seq_id}/step/",
            data=json.dumps({"parent_id": None, "branch": "root", "step_type": "connect"}),
            content_type="application/json",
        ).json()["id"]
        admin_client.post(
            f"/dashboard/api/sequence/{seq_id}/step/",
            data=json.dumps({"parent_id": root, "branch": "success", "step_type": "message"}),
            content_type="application/json",
        )
        admin_client.post(
            f"/dashboard/api/sequence/{seq_id}/step/",
            data=json.dumps({"parent_id": root, "branch": "failure", "step_type": "inmail"}),
            content_type="application/json",
        )
        connect = SequenceStep.objects.get(pk=root)
        assert connect.next_step(SequenceStep.Branch.SUCCESS).step_type == "message"
        assert connect.next_step(SequenceStep.Branch.FAILURE).step_type == "inmail"

    def test_insert_step_mid_chain(self, admin_client):
        import json

        from linkedin.models import Sequence, SequenceStep
        from tests.factories import UserFactory

        seq = Sequence.objects.create(name="I", owner=UserFactory())
        root = SequenceStep.objects.create(sequence=seq, branch=SequenceStep.Branch.ROOT, step_type=SequenceStep.StepType.CONNECT)
        child = SequenceStep.objects.create(sequence=seq, parent=root, branch=SequenceStep.Branch.SUCCESS, step_type=SequenceStep.StepType.MESSAGE)

        # Insert a like_post between root and its existing success child.
        admin_client.post(
            f"/dashboard/api/sequence/{seq.pk}/step/",
            data=json.dumps({"parent_id": root.pk, "branch": "success", "step_type": "like_post"}),
            content_type="application/json",
        )
        inserted = root.next_step(SequenceStep.Branch.SUCCESS)
        child.refresh_from_db()
        assert inserted.step_type == "like_post"
        assert child.parent_id == inserted.pk
        assert child.branch == SequenceStep.Branch.SUCCESS

    def test_update_step_config(self, admin_client):
        import json

        from linkedin.models import Sequence, SequenceStep
        from tests.factories import UserFactory

        seq = Sequence.objects.create(name="S", owner=UserFactory())
        step = SequenceStep.objects.create(sequence=seq, branch=SequenceStep.Branch.ROOT, step_type=SequenceStep.StepType.MESSAGE)
        admin_client.post(
            f"/dashboard/api/step/{step.pk}/",
            data=json.dumps({"config": {"template": "Hi {first_name}!"}}),
            content_type="application/json",
        )
        step.refresh_from_db()
        assert step.config["template"] == "Hi {first_name}!"


@pytest.mark.django_db
class TestUniboxArchive:
    def _thread(self):
        from crm.models import Lead
        from linkedin.models import Message, MessageThread

        prof = _profile()
        lead = Lead.objects.create(linkedin_url="https://www.linkedin.com/in/z/", public_identifier="z")
        thread = MessageThread.objects.create(lead=lead, account=prof, contacted_by_tool=True)
        Message.objects.create(thread=thread, direction="out", linkedin_message_id="m1", sent_via_tool=True)
        return thread

    def _ids(self, admin_client, f):
        return [t["id"] for t in admin_client.get(f"/dashboard/api/inbox/threads/?filter={f}").json()["threads"]]

    def test_archive_hides_then_unarchive_restores(self, admin_client):
        import json

        t = self._thread()
        # Visible by default.
        assert t.pk in self._ids(admin_client, "all")
        assert t.pk not in self._ids(admin_client, "archived")

        # Archive it.
        r = admin_client.post(
            f"/dashboard/api/inbox/thread/{t.pk}/archive/",
            data=json.dumps({"archived": True}), content_type="application/json",
        ).json()
        assert r["ok"] and r["archived"] is True
        t.refresh_from_db()
        assert t.archived_at is not None
        # Gone from the default list, present in the archived view.
        assert t.pk not in self._ids(admin_client, "all")
        assert t.pk in self._ids(admin_client, "archived")

        # Unarchive.
        r2 = admin_client.post(
            f"/dashboard/api/inbox/thread/{t.pk}/archive/",
            data=json.dumps({"archived": False}), content_type="application/json",
        ).json()
        assert r2["archived"] is False
        assert t.pk in self._ids(admin_client, "all")

    def test_archive_missing_thread_404s(self, admin_client):
        import json

        r = admin_client.post(
            "/dashboard/api/inbox/thread/999999/archive/",
            data=json.dumps({"archived": True}), content_type="application/json",
        )
        assert r.status_code == 404
