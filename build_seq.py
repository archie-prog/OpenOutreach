from django.contrib.auth.models import User
from linkedin.models import Sequence, SequenceStep as S
owner = User.objects.filter(is_superuser=True).first()
B = S.Branch; T = S.StepType
seq = Sequence.objects.create(name="Grantgunner Outreach", owner=owner)
connect = S.objects.create(sequence=seq, branch=B.ROOT, step_type=T.CONNECT, config={"wait_days_before_branch_decision": 1})
w1 = S.objects.create(sequence=seq, parent=connect, branch=B.SUCCESS, step_type=T.WAIT, config={"days": 1})
lp = S.objects.create(sequence=seq, parent=w1, branch=B.SUCCESS, step_type=T.LIKE_POST, config={})
w2 = S.objects.create(sequence=seq, parent=lp, branch=B.SUCCESS, step_type=T.WAIT, config={"days": 1})
m1 = S.objects.create(sequence=seq, parent=w2, branch=B.SUCCESS, step_type=T.MESSAGE, config={"template": "Hi {first_name}, thanks for connecting. I am building Grantgunner, an AI tool that finds and applies to grants/funding for startups - worth a quick chat?", "fallback": "Hi, thanks for connecting!"})
w3 = S.objects.create(sequence=seq, parent=m1, branch=B.FAILURE, step_type=T.WAIT, config={"days": 2})
m2 = S.objects.create(sequence=seq, parent=w3, branch=B.SUCCESS, step_type=T.MESSAGE, config={"template": "Hi {first_name}, just following up on Grantgunner - happy to share how it could help with funding.", "fallback": "Just following up!"})
w4 = S.objects.create(sequence=seq, parent=m2, branch=B.FAILURE, step_type=T.WAIT, config={"days": 2})
m3 = S.objects.create(sequence=seq, parent=w4, branch=B.SUCCESS, step_type=T.MESSAGE, config={"template": "Hi {first_name}, last note from me - if grant funding is ever useful, Grantgunner can help. Cheers!", "fallback": "Last follow-up."})
w5 = S.objects.create(sequence=seq, parent=connect, branch=B.FAILURE, step_type=T.WAIT, config={"days": 21})
im = S.objects.create(sequence=seq, parent=w5, branch=B.SUCCESS, step_type=T.INMAIL, config={"subject": "Grantgunner for {first_name}", "subject_fallback": "Grant funding", "body": "Hi {first_name}, I am building Grantgunner, an AI tool that finds and applies to grants/funding for startups. Thought it might be relevant - open to a quick chat?", "body_fallback": "Hi, thought Grantgunner might be relevant!"})
print("SEQ_CREATED", seq.pk, "steps", seq.steps.count())
