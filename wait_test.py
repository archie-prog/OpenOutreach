import time
from datetime import timedelta
from django.utils import timezone
from django.contrib.auth.models import User
from linkedin.browser.registry import get_first_active_profile, get_or_create_session
from linkedin.models import Sequence, SequenceStep as S, Campaign, LeadList, LeadCampaignState, ActionLog
from linkedin.sequences import executor
from linkedin.inbox import poller
from crm.models import Lead

profile = get_first_active_profile()
session = get_or_create_session(profile)
owner = User.objects.filter(is_superuser=True).first() or profile.user
print("LOGGED_IN", profile.linkedin_username, flush=True)

seq, created = Sequence.objects.get_or_create(name="Toby Wait Test", defaults={"owner": owner})
if created:
    m1 = S.objects.create(sequence=seq, branch=S.Branch.ROOT, step_type=S.StepType.MESSAGE, config={"template": "Hi {first_name}, wait-test message 1 - please do not reply yet so we can test the follow-up.", "fallback": "test1"})
    w = S.objects.create(sequence=seq, parent=m1, branch=S.Branch.FAILURE, step_type=S.StepType.WAIT, config={"days": 1})
    S.objects.create(sequence=seq, parent=w, branch=S.Branch.SUCCESS, step_type=S.StepType.MESSAGE, config={"template": "Hi {first_name}, wait-test FOLLOW-UP - 1 day passed with no reply. Please ignore.", "fallback": "followup"})

ll, _ = LeadList.objects.get_or_create(name="Toby Test List", defaults={"owner": owner, "source_type": LeadList.SourceType.MANUAL})
lead, _ = Lead.objects.get_or_create(public_identifier="toby-claxton", defaults={"linkedin_url": "https://www.linkedin.com/in/toby-claxton/"})
if not lead.first_name:
    lead.first_name = "Toby"
lead.lead_list = ll; lead.save()
camp, _ = Campaign.objects.get_or_create(name="Toby Wait Campaign")
camp.sequence = seq; camp.lead_list = ll; camp.status = Campaign.Status.ACTIVE; camp.save()
camp.users.add(profile.user)

root = seq.root_step
st, _ = LeadCampaignState.objects.get_or_create(lead=lead, campaign=camp)
st.current_step = root; st.current_branch = S.Branch.ROOT; st.state = LeadCampaignState.State.ACTIVE; st.awaiting_decision = False; st.next_action_due_at = timezone.now(); st.save()

def msgs():
    return ActionLog.objects.filter(campaign=camp, action_type="message").count()
base = msgs()

for i in range(6):
    st.refresh_from_db()
    if st.state != LeadCampaignState.State.ACTIVE:
        break
    if st.next_action_due_at and st.next_action_due_at > timezone.now() + timedelta(hours=2):
        break
    LeadCampaignState.objects.filter(pk=st.pk).update(next_action_due_at=timezone.now())
    executor.run_due_states(session, campaign=camp)
    time.sleep(1)

st.refresh_from_db()
waiting = bool(st.next_action_due_at and st.next_action_due_at > timezone.now() + timedelta(hours=2))
print("STEP1_DONE messages_sent=", msgs()-base, "waiting=", waiting, "next_due=", st.next_action_due_at, flush=True)

poller.poll_replies(session, campaign=camp)
st.refresh_from_db()
if st.state == LeadCampaignState.State.STOPPED_REPLY:
    print("RESULT: Toby REPLIED during the wait -> sequence STOPPED, no follow-up sent", flush=True)
else:
    print("FAST_FORWARD simulating 1 day, no reply...", flush=True)
    for i in range(4):
        st.refresh_from_db()
        if st.state != LeadCampaignState.State.ACTIVE:
            break
        LeadCampaignState.objects.filter(pk=st.pk).update(next_action_due_at=timezone.now())
        executor.run_due_states(session, campaign=camp)
        time.sleep(1)
    print("RESULT: no reply -> FOLLOW-UP sent. total messages this run=", msgs()-base, "(expect 2)", flush=True)
