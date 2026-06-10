from linkedin.models import Campaign, LeadCampaignState, SequenceStep as S
from django.utils import timezone
for name in ["Reply Test Campaign", "Connect Test Campaign"]:
    c = Campaign.objects.filter(name=name).first()
    if not c:
        continue
    root = c.sequence.root_step
    for st in LeadCampaignState.objects.filter(campaign=c):
        st.current_step = root; st.current_branch = S.Branch.ROOT; st.state = LeadCampaignState.State.ACTIVE
        st.awaiting_decision = False; st.next_action_due_at = timezone.now(); st.last_action_at = None; st.save()
print("RESET_DONE")
