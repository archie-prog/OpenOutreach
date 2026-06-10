import time
from django.utils import timezone
from django.contrib.auth.models import User
from linkedin.browser.registry import get_first_active_profile, get_or_create_session
from linkedin.models import Sequence, SequenceStep as S, Campaign, LeadList, LeadCampaignState, MessageThread, Message
from linkedin.sequences import executor
from linkedin.inbox import poller
from crm.models import Lead

profile = get_first_active_profile()
session = get_or_create_session(profile)
owner = User.objects.filter(is_superuser=True).first() or profile.user
print("LOGGED_IN_AS:", profile.linkedin_username)

seq, created = Sequence.objects.get_or_create(name="Toby Live Test", defaults={"owner": owner})
if created:
    like = S.objects.create(sequence=seq, branch=S.Branch.ROOT, step_type=S.StepType.LIKE_POST)
    msg = S.objects.create(sequence=seq, parent=like, branch=S.Branch.SUCCESS, step_type=S.StepType.MESSAGE, config={"template": "Hi {first_name}, automated test message 1 - please reply so we can test the inbox!", "fallback": "Hi, test 1"})
    S.objects.create(sequence=seq, parent=msg, branch=S.Branch.FAILURE, step_type=S.StepType.MESSAGE, config={"template": "Hi {first_name}, automated follow-up (message 2) - reply any time!", "fallback": "follow up"})

ll, _ = LeadList.objects.get_or_create(name="Toby Test List", defaults={"owner": owner, "source_type": LeadList.SourceType.MANUAL})
lead, _ = Lead.objects.get_or_create(public_identifier="toby-claxton", defaults={"linkedin_url": "https://www.linkedin.com/in/toby-claxton/"})
lead.lead_list = ll
if not lead.first_name:
    lead.first_name = "Toby"
lead.save()

camp, _ = Campaign.objects.get_or_create(name="Toby Live Test Campaign")
camp.sequence = seq; camp.lead_list = ll; camp.status = Campaign.Status.ACTIVE; camp.save()
camp.users.add(profile.user)

root = seq.root_step
st, _ = LeadCampaignState.objects.get_or_create(lead=lead, campaign=camp)
st.current_step = root; st.current_branch = S.Branch.ROOT; st.state = LeadCampaignState.State.ACTIVE; st.awaiting_decision = False; st.next_action_due_at = timezone.now(); st.save()

for i in range(8):
    active = LeadCampaignState.objects.filter(campaign=camp, state=LeadCampaignState.State.ACTIVE)
    if not active.exists():
        break
    active.update(next_action_due_at=timezone.now())
    executor.run_due_states(session, campaign=camp)
    time.sleep(1)

poller.poll_replies(session, campaign=camp)
th = MessageThread.objects.filter(lead=lead).first()
print("FINAL_STATE:", LeadCampaignState.objects.get(lead=lead, campaign=camp).state)
print("THREAD:", th)
print("MESSAGES:", list(Message.objects.filter(thread=th).values_list("direction", "body")) if th else "NONE")
