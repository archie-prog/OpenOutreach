from linkedin.models import Campaign, LeadCampaignState, ActionLog, LeadList
for name in ["Reply Test Campaign", "Connect Test Campaign"]:
    c = Campaign.objects.filter(name=name).first()
    if not c:
        continue
    print(name, flush=True)
    for st in LeadCampaignState.objects.filter(campaign=c).select_related("lead", "current_step"):
        step = st.current_step.step_type if st.current_step else None
        print("   ", st.lead.public_identifier, "| state=", st.state, "| step=", step, "| awaiting=", st.awaiting_decision, "| due=", st.next_action_due_at, flush=True)
print("CONNECT_LOGS", ActionLog.objects.filter(action_type="connect").count(), flush=True)
print("MESSAGE_LOGS", ActionLog.objects.filter(action_type="message").count(), flush=True)
sl = LeadList.objects.filter(name="London Marketing Angel Investors").first()
print("LONDON_LIST leads=", sl.leads.count() if sl else "n/a", "pending=", sl.pending_search if sl else "n/a", flush=True)
