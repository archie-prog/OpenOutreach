from django.contrib.auth.models import User
from django.utils import timezone
from linkedin.models import Sequence, SequenceStep as S, Campaign, LeadList, LeadCampaignState, LinkedInProfile
from linkedin.dashboard.views import _build_search_url
from linkedin.leads import importer
from crm.models import Lead

owner = User.objects.filter(is_superuser=True).first()
prof = LinkedInProfile.objects.filter(active=True).first()

def enroll(camp, leadlist):
    root = camp.sequence.root_step
    for lead in leadlist.leads.all():
        st, _ = LeadCampaignState.objects.get_or_create(lead=lead, campaign=camp)
        st.current_step = root; st.current_branch = S.Branch.ROOT; st.state = LeadCampaignState.State.ACTIVE; st.awaiting_decision = False; st.next_action_due_at = timezone.now(); st.save()

seq1, c1 = Sequence.objects.get_or_create(name="Reply Test Seq", defaults={"owner": owner})
if c1:
    m1 = S.objects.create(sequence=seq1, branch=S.Branch.ROOT, step_type=S.StepType.MESSAGE, config={"template": "Hi {first_name}, automated sequence test - reply or ignore.", "fallback": "hi"})
    w1 = S.objects.create(sequence=seq1, parent=m1, branch=S.Branch.FAILURE, step_type=S.StepType.WAIT, config={"days": 1})
    S.objects.create(sequence=seq1, parent=w1, branch=S.Branch.SUCCESS, step_type=S.StepType.MESSAGE, config={"template": "Hi {first_name}, following up 1 day later - you did not reply. Automated test.", "fallback": "follow up"})
ll1, _ = LeadList.objects.get_or_create(name="Reply Test List", defaults={"owner": owner, "source_type": LeadList.SourceType.MANUAL})
for pid, url, fn in [("toby-claxton", "https://www.linkedin.com/in/toby-claxton/", "Toby"), ("josh-s-young", "https://www.linkedin.com/in/josh-s-young/", "Josh")]:
    lead, _ = Lead.objects.get_or_create(public_identifier=pid, defaults={"linkedin_url": url})
    if not lead.first_name: lead.first_name = fn
    lead.lead_list = ll1; lead.save()
camp1, _ = Campaign.objects.get_or_create(name="Reply Test Campaign")
camp1.sequence = seq1; camp1.lead_list = ll1; camp1.status = Campaign.Status.ACTIVE; camp1.save(); camp1.users.add(prof.user)
enroll(camp1, ll1)

seq2, c2 = Sequence.objects.get_or_create(name="Connect Test Seq", defaults={"owner": owner})
if c2:
    cstep = S.objects.create(sequence=seq2, branch=S.Branch.ROOT, step_type=S.StepType.CONNECT, config={"wait_days_before_branch_decision": 1})
    S.objects.create(sequence=seq2, parent=cstep, branch=S.Branch.SUCCESS, step_type=S.StepType.MESSAGE, config={"template": "Hi {first_name}, pleasure to connect.", "fallback": "Pleasure to connect."})
ll2, _ = LeadList.objects.get_or_create(name="Connect Test List", defaults={"owner": owner, "source_type": LeadList.SourceType.MANUAL})
jess, _ = Lead.objects.get_or_create(public_identifier="jess-mcallister-0b2330143", defaults={"linkedin_url": "https://www.linkedin.com/in/jess-mcallister-0b2330143/"})
if not jess.first_name: jess.first_name = "Jess"
jess.lead_list = ll2; jess.save()
camp2, _ = Campaign.objects.get_or_create(name="Connect Test Campaign")
camp2.sequence = seq2; camp2.lead_list = ll2; camp2.status = Campaign.Status.ACTIVE; camp2.save(); camp2.users.add(prof.user)
enroll(camp2, ll2)

url = _build_search_url({"keywords": "London marketing angel investor", "network": ["S", "O"]})
sl = importer.create_lead_list(name="London Marketing Angel Investors", owner=owner, source_type=LeadList.SourceType.SEARCH_URL, source_url=url)
sl.pending_search = True; sl.save()

print("REPLY_STATES", LeadCampaignState.objects.filter(campaign=camp1, state="active").count())
print("CONNECT_STATES", LeadCampaignState.objects.filter(campaign=camp2, state="active").count())
print("SEARCH_QUEUED", sl.pk)
