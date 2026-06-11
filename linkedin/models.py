# linkedin/models.py
from __future__ import annotations

import logging
from datetime import date

from django.contrib.auth.models import User
from django.db import models
from django.utils import timezone

logger = logging.getLogger(__name__)

# action_type → daily_limit_field
_RATE_LIMIT_FIELDS = {
    "connect": "connect_daily_limit",
    "follow_up": "follow_up_daily_limit",
}

# Per-account default daily caps (M6) — 25 connects/day is the ban-safe target.
def default_daily_caps():
    return {"connect": 25, "message": 50, "inmail": 5, "profile_visit": 100, "like_post": 100, "follow_up": 50}


def default_send_weekdays():
    return [0, 1, 2, 3, 4]  # Mon–Fri (Python weekday(): Mon=0 … Sun=6)


class SiteConfig(models.Model):
    """Singleton model for global site configuration (LLM keys, etc.)."""

    class LLMProvider(models.TextChoices):
        OPENAI = "openai", "OpenAI"
        ANTHROPIC = "anthropic", "Anthropic"
        GOOGLE = "google", "Google"
        GROQ = "groq", "Groq"
        MISTRAL = "mistral", "Mistral"
        COHERE = "cohere", "Cohere"
        OPENAI_COMPATIBLE = "openai_compatible", "OpenAI-compatible"

    llm_provider = models.CharField(
        max_length=32,
        choices=LLMProvider.choices,
        default=LLMProvider.OPENAI,
    )
    llm_api_key = models.CharField(max_length=500, blank=True, default="")
    ai_model = models.CharField(max_length=200, blank=True, default="")
    llm_api_base = models.CharField(max_length=500, blank=True, default="")
    # Free-text "what we sell + who the ideal lead is" — drives AI lead scoring,
    # search keyword generation, and message personalisation.
    ai_context = models.TextField(blank=True, default="")
    # Slack notifications (separate from any other project's Slack): a dedicated
    # Incoming Webhook URL for a LinkedIn channel. Pinged when a lead replies.
    slack_webhook_url = models.CharField(max_length=500, blank=True, default="")
    slack_notify_replies = models.BooleanField(default=True)
    # ── Workspace + billing (commercial) ──────────────────────────────────
    # Branding/identity for the workspace shown across the app.
    workspace_name = models.CharField(max_length=120, blank=True, default="Grantgunner")
    billing_email = models.CharField(max_length=200, blank=True, default="")
    # Plan + billing state. Stripe-ready: when stripe_customer_id is set the
    # Settings page can deep-link to the Stripe customer portal to manage the
    # subscription; until then billing shows as "self-hosted / not connected".
    plan = models.CharField(max_length=40, blank=True, default="self-hosted")
    billing_status = models.CharField(max_length=40, blank=True, default="active")
    stripe_customer_id = models.CharField(max_length=120, blank=True, default="")
    stripe_portal_url = models.CharField(max_length=500, blank=True, default="")

    class Meta:
        app_label = "linkedin"
        verbose_name = "Site Configuration"
        verbose_name_plural = "Site Configuration"

    def __str__(self):
        return "Site Configuration"

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)

    @classmethod
    def load(cls) -> "SiteConfig":
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj


class Campaign(models.Model):
    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        ACTIVE = "active", "Active"
        PAUSED = "paused", "Paused"
        COMPLETED = "completed", "Completed"
        ARCHIVED = "archived", "Archived"

    name = models.CharField(max_length=200, unique=True)
    users = models.ManyToManyField(User, blank=True, related_name="campaigns")
    product_docs = models.TextField(blank=True)
    campaign_objective = models.TextField(blank=True)
    booking_link = models.URLField(max_length=500, blank=True)
    is_freemium = models.BooleanField(default=False)
    action_fraction = models.FloatField(default=0.2)
    seed_public_ids = models.JSONField(default=list, blank=True)
    model_blob = models.BinaryField(null=True, blank=True)
    # Sequence-driven campaigns (M2): when ``sequence`` is set the campaign is
    # driven step-by-step over ``lead_list`` leads; when null it stays on the
    # autonomous AI-discovery path.
    sequence = models.ForeignKey(
        "linkedin.Sequence", null=True, blank=True,
        on_delete=models.SET_NULL, related_name="campaigns",
    )
    lead_list = models.ForeignKey(
        "linkedin.LeadList", null=True, blank=True,
        on_delete=models.SET_NULL, related_name="campaigns",
    )
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.DRAFT)

    def __str__(self):
        return self.name

    @property
    def is_sequence_driven(self) -> bool:
        return self.sequence_id is not None

    class Meta:
        app_label = "linkedin"


class LeadList(models.Model):
    """A named collection of leads — the manual-import counterpart to AI
    auto-discovery. Soft-delete only: set ``archived_at`` to retire a list;
    rows are never hard-deleted.
    """

    class SourceType(models.TextChoices):
        CSV = "csv", "CSV upload"
        SEARCH_URL = "search_url", "LinkedIn search URL"
        AI = "ai", "AI lead finder"
        MANUAL = "manual", "Manual"

    name = models.CharField(max_length=200)
    owner = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="lead_lists",
    )
    source_type = models.CharField(
        max_length=20,
        choices=SourceType.choices,
        default=SourceType.MANUAL,
    )
    source_url = models.URLField(max_length=2000, null=True, blank=True)
    # Set when a dashboard search list is created; the browser worker scrapes it.
    pending_search = models.BooleanField(default=False)
    # How many leads to gather for this list — the worker keeps filling toward it.
    target_count = models.PositiveIntegerField(default=30)
    created_at = models.DateTimeField(default=timezone.now)
    archived_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        app_label = "linkedin"

    def __str__(self):
        label = self.name or f"LeadList#{self.pk}"
        return f"(Archived) {label}" if self.archived_at else label

    @property
    def is_archived(self) -> bool:
        return self.archived_at is not None

    def archive(self) -> None:
        """Soft-delete: retire the list without removing any rows."""
        if self.archived_at is None:
            self.archived_at = timezone.now()
            self.save(update_fields=["archived_at"])


class LeadListEvent(models.Model):
    """One entry in a lead list's activity thread — the "AI chat": the user's
    prompts/refinements and what the finder did each run (keywords, leads added).
    Append-only; never edited or deleted (it's the record of what happened).
    """

    class Role(models.TextChoices):
        USER = "user", "You"
        AI = "ai", "AI"
        SYSTEM = "system", "System"

    lead_list = models.ForeignKey(LeadList, on_delete=models.CASCADE, related_name="events")
    role = models.CharField(max_length=10, choices=Role.choices)
    text = models.TextField(blank=True, default="")
    meta = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        app_label = "linkedin"
        ordering = ["created_at"]

    def __str__(self):
        return f"{self.role}:{self.text[:40]}"


class LinkedInProfile(models.Model):
    user = models.OneToOneField(
        User,
        on_delete=models.CASCADE,
        related_name="linkedin_profile",
    )
    self_lead = models.ForeignKey(
        "crm.Lead",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    linkedin_username = models.CharField(max_length=200)
    linkedin_password = models.CharField(max_length=200)
    subscribe_newsletter = models.BooleanField(default=True)
    active = models.BooleanField(default=True)
    connect_daily_limit = models.PositiveIntegerField(default=25)
    follow_up_daily_limit = models.PositiveIntegerField(default=25)
    legal_accepted = models.BooleanField(default=False)
    cookie_data = models.JSONField(null=True, blank=True)
    newsletter_processed = models.BooleanField(default=False)
    # True when the account has Sales Navigator / Recruiter / Premium InMail.
    has_inmail = models.BooleanField(default=False)
    # Premium gives a limited monthly InMail allowance (default 15).
    inmail_monthly_cap = models.PositiveIntegerField(default=15)
    # M6: per-action daily caps + least-recently-used marker for round-robin.
    daily_caps_json = models.JSONField(default=default_daily_caps, blank=True)
    last_used_at = models.DateTimeField(null=True, blank=True)
    # Base32 TOTP secret for native 2FA (Google Authenticator). Sensitive — when
    # set, login auto-fills the 6-digit code instead of waiting for a human.
    totp_secret = models.CharField(max_length=128, blank=True, default="")
    # Per-account sending schedule: messages/connects/etc. only go out inside this
    # local window, on these weekdays, optionally skipping bank holidays.
    send_start_hour = models.PositiveSmallIntegerField(default=9)   # inclusive, local
    send_end_hour = models.PositiveSmallIntegerField(default=17)    # exclusive, local
    send_timezone = models.CharField(max_length=64, default="Europe/London")
    send_weekdays = models.JSONField(default=default_send_weekdays, blank=True)  # Mon=0…Sun=6
    skip_bank_holidays = models.BooleanField(default=False)
    holiday_country = models.CharField(max_length=8, default="GB")
    # Randomise the day's connection-request cap within [min, max] instead of a
    # fixed number, so daily volume varies (more human). Stable per (account, day).
    connect_random_enabled = models.BooleanField(default=False)
    connect_random_min = models.PositiveSmallIntegerField(default=19)
    connect_random_max = models.PositiveSmallIntegerField(default=25)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._exhausted: dict[str, date] = {}

    def can_execute(self, action_type: str) -> bool:
        """Check if the action is allowed under the daily rate limit."""
        # Reset exhaustion flag on a new day
        exhausted_date = self._exhausted.get(action_type)
        if exhausted_date is not None and exhausted_date != date.today():
            del self._exhausted[action_type]
        if action_type in self._exhausted:
            return False

        daily_field = _RATE_LIMIT_FIELDS[action_type]
        self.refresh_from_db(fields=[daily_field])

        daily_limit = getattr(self, daily_field)
        if daily_limit is not None and self._daily_count(action_type) >= daily_limit:
            return False

        return True

    def record_action(self, action_type: str, campaign: Campaign) -> None:
        """Persist a rate-limited action."""
        ActionLog.objects.create(
            linkedin_profile=self, campaign=campaign, action_type=action_type,
        )

    def mark_exhausted(self, action_type: str) -> None:
        """Mark the action type as externally exhausted for today."""
        self._exhausted[action_type] = date.today()
        logger.warning("Rate limit: %s externally exhausted for today", action_type)

    def _daily_count(self, action_type: str) -> int:
        today_start = timezone.now().replace(hour=0, minute=0, second=0, microsecond=0)
        return ActionLog.objects.filter(
            linkedin_profile=self, action_type=action_type,
            created_at__gte=today_start,
        ).count()

    def __str__(self):
        return f"{self.user.username} ({self.linkedin_username})"

    class Meta:
        app_label = "linkedin"


class SearchKeyword(models.Model):
    campaign = models.ForeignKey(
        Campaign,
        on_delete=models.CASCADE,
        related_name="search_keywords",
    )
    keyword = models.CharField(max_length=500)
    used = models.BooleanField(default=False)
    used_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        app_label = "linkedin"
        unique_together = [("campaign", "keyword")]

    def __str__(self):
        return self.keyword


class ActionLog(models.Model):
    class ActionType(models.TextChoices):
        CONNECT = "connect", "Connect"
        FOLLOW_UP = "follow_up", "Follow Up"
        MESSAGE = "message", "Message"
        INMAIL = "inmail", "InMail"
        PROFILE_VISIT = "profile_visit", "Profile Visit"
        LIKE_POST = "like_post", "Like Post"
        CONNECT_ACCEPTED = "connect_accepted", "Connection accepted"

    linkedin_profile = models.ForeignKey(
        LinkedInProfile,
        on_delete=models.CASCADE,
        related_name="action_logs",
    )
    campaign = models.ForeignKey(
        Campaign,
        on_delete=models.CASCADE,
        related_name="action_logs",
    )
    # Which lead this action/event was for — lets the dashboard show who.
    lead = models.ForeignKey(
        "crm.Lead", null=True, blank=True,
        on_delete=models.SET_NULL, related_name="action_logs",
    )
    action_type = models.CharField(max_length=20, choices=ActionType.choices)
    # Optional target the action touched — e.g. the URL of a post that was liked.
    target_url = models.CharField(max_length=600, blank=True, default="")
    sequence_step = models.ForeignKey(
        "linkedin.SequenceStep", null=True, blank=True,
        on_delete=models.SET_NULL, related_name="action_logs",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        app_label = "linkedin"
        indexes = [
            models.Index(fields=["linkedin_profile", "action_type", "created_at"]),
        ]

    def __str__(self):
        return f"{self.action_type} by {self.linkedin_profile} at {self.created_at}"


class Sequence(models.Model):
    """An ordered, branching outreach playbook (HeyReach-style). Steps form a
    tree via ``SequenceStep.parent`` + ``branch``. Soft-delete via ``archived_at``.
    """

    name = models.CharField(max_length=200)
    owner = models.ForeignKey(User, on_delete=models.CASCADE, related_name="sequences")
    created_at = models.DateTimeField(default=timezone.now)
    archived_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        app_label = "linkedin"

    def __str__(self):
        label = self.name or f"Sequence#{self.pk}"
        return f"(Archived) {label}" if self.archived_at else label

    @property
    def root_step(self):
        return self.steps.filter(branch=SequenceStep.Branch.ROOT).order_by("order_in_branch").first()

    def archive(self):
        if self.archived_at is None:
            self.archived_at = timezone.now()
            self.save(update_fields=["archived_at"])


class SequenceStep(models.Model):
    """One node in a Sequence tree. ``parent`` + ``branch`` place it; ``step_type``
    + ``config`` define the action. ``success`` branch = accepted/replied;
    ``failure`` branch = not-accepted/no-reply (exact meaning per step_type).
    """

    class Branch(models.TextChoices):
        ROOT = "root", "Root"
        SUCCESS = "success", "Success"
        FAILURE = "failure", "Failure"

    class StepType(models.TextChoices):
        CONNECT = "connect", "Send Connection Request"
        MESSAGE = "message", "Send Message"
        INMAIL = "inmail", "Send InMail"
        WAIT = "wait", "Wait"
        PROFILE_VISIT = "profile_visit", "View Profile"
        LIKE_POST = "like_post", "Like Post"
        END = "end", "End — stop here"
        BLANK = "blank", "Blank — no action (resume)"

    sequence = models.ForeignKey(Sequence, on_delete=models.CASCADE, related_name="steps")
    parent = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.CASCADE, related_name="children",
    )
    branch = models.CharField(max_length=10, choices=Branch.choices, default=Branch.ROOT)
    step_type = models.CharField(max_length=20, choices=StepType.choices)
    order_in_branch = models.PositiveIntegerField(default=0)
    config = models.JSONField(default=dict, blank=True)

    class Meta:
        app_label = "linkedin"
        ordering = ["order_in_branch"]

    def __str__(self):
        return f"{self.step_type}#{self.pk}"

    def next_step(self, branch):
        """The child step on the given branch (``success``/``failure``), or None."""
        return self.children.filter(branch=branch).order_by("order_in_branch").first()


class LeadCampaignState(models.Model):
    """Per-(lead, campaign) progress through a sequence — the executor's cursor.
    Never deleted: retired leads move to a terminal ``state``.
    """

    class State(models.TextChoices):
        ACTIVE = "active", "Active"
        COMPLETED = "completed", "Completed"
        STOPPED_REPLY = "stopped_reply", "Stopped — replied"
        STOPPED_ERROR = "stopped_error", "Stopped — error"
        PAUSED_MANUAL = "paused_manual", "Paused (manual)"
        ARCHIVED = "archived", "Archived"

    lead = models.ForeignKey("crm.Lead", on_delete=models.CASCADE, related_name="campaign_states")
    campaign = models.ForeignKey(Campaign, on_delete=models.CASCADE, related_name="lead_states")
    current_step = models.ForeignKey(
        SequenceStep, null=True, blank=True, on_delete=models.SET_NULL, related_name="+",
    )
    current_branch = models.CharField(
        max_length=10, choices=SequenceStep.Branch.choices, default=SequenceStep.Branch.ROOT,
    )
    state = models.CharField(max_length=20, choices=State.choices, default=State.ACTIVE)
    # True while a connect step waits to learn if the request was accepted.
    awaiting_decision = models.BooleanField(default=False)
    next_action_due_at = models.DateTimeField(null=True, blank=True, db_index=True)
    last_action_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        app_label = "linkedin"
        constraints = [
            models.UniqueConstraint(fields=["lead", "campaign"], name="unique_lead_campaign_state"),
        ]
        indexes = [
            models.Index(fields=["state", "next_action_due_at"]),
        ]

    def __str__(self):
        return f"{self.lead_id}@{self.campaign_id} [{self.state}]"


class MessageThread(models.Model):
    """One conversation between an account and a lead — the inbox unit (M3/M5)."""

    lead = models.ForeignKey("crm.Lead", on_delete=models.CASCADE, related_name="threads")
    account = models.ForeignKey(LinkedInProfile, on_delete=models.CASCADE, related_name="threads")
    linkedin_thread_id = models.CharField(max_length=255, blank=True, default="")
    last_polled_at = models.DateTimeField(null=True, blank=True)
    last_message_at = models.DateTimeField(null=True, blank=True)
    has_inbound_reply = models.BooleanField(default=False)
    # True once THIS tool has actually messaged the lead (sequence or manual
    # Unibox send). The Unibox only surfaces threads we initiated — never the
    # account's pre-existing LinkedIn conversations from other tools.
    contacted_by_tool = models.BooleanField(default=False)
    read_at = models.DateTimeField(null=True, blank=True)  # null = unread (M5)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        app_label = "linkedin"
        constraints = [
            models.UniqueConstraint(fields=["lead", "account"], name="unique_thread_per_lead_account"),
        ]

    def __str__(self):
        return f"thread:{self.lead_id}/{self.account_id}"


class Message(models.Model):
    class Direction(models.TextChoices):
        IN = "in", "Incoming"
        OUT = "out", "Outgoing"

    thread = models.ForeignKey(MessageThread, on_delete=models.CASCADE, related_name="messages")
    direction = models.CharField(max_length=3, choices=Direction.choices)
    sender_account = models.ForeignKey(
        LinkedInProfile, null=True, blank=True, on_delete=models.SET_NULL, related_name="+",
    )
    body = models.TextField(blank=True, default="")
    sent_at = models.DateTimeField(null=True, blank=True)
    linkedin_message_id = models.CharField(max_length=255)
    sent_via_tool = models.BooleanField(default=False)  # M5 inbox privacy default
    # A manual reply typed in the Unibox, queued for the worker to actually send.
    pending_send = models.BooleanField(default=False)
    # Worker send bookkeeping for queued manual replies: how many send attempts
    # have been made, and the last error. Bounds retries (a permanently-failing
    # send must not re-attempt a live browser action every cycle forever) and
    # lets the Unibox show a queued/failed state instead of a fake "sent".
    send_attempts = models.PositiveSmallIntegerField(default=0)
    send_error = models.CharField(max_length=300, blank=True, default="")
    fetched_at = models.DateTimeField(default=timezone.now)

    class Meta:
        app_label = "linkedin"
        constraints = [
            models.UniqueConstraint(
                fields=["thread", "linkedin_message_id"], name="unique_message_per_thread",
            ),
        ]
        ordering = ["sent_at"]

    def __str__(self):
        return f"{self.direction}:{self.linkedin_message_id}"


class AccountDailyCounter(models.Model):
    """Per-(account, day, action_type) action tally (M6). Append/increment only;
    never deleted — history is the audit trail.
    """

    account = models.ForeignKey(LinkedInProfile, on_delete=models.CASCADE, related_name="daily_counters")
    date = models.DateField()
    action_type = models.CharField(max_length=20, choices=ActionLog.ActionType.choices)
    count = models.PositiveIntegerField(default=0)

    class Meta:
        app_label = "linkedin"
        constraints = [
            models.UniqueConstraint(
                fields=["account", "date", "action_type"], name="unique_counter_per_account_day_action",
            ),
        ]
        indexes = [models.Index(fields=["account", "date"])]

    def __str__(self):
        return f"{self.account_id}/{self.date}/{self.action_type}={self.count}"


class TaskQuerySet(models.QuerySet):
    def pending(self):
        return self.filter(status=Task.Status.PENDING).order_by("scheduled_at")

    def claim_next(self) -> "Task | None":
        return self.pending().filter(scheduled_at__lte=timezone.now()).first()

    def seconds_to_next(self) -> float | None:
        """Seconds until the next pending task, or None if queue is empty."""
        next_task = self.pending().only("scheduled_at").first()
        if next_task is None:
            return None
        return max((next_task.scheduled_at - timezone.now()).total_seconds(), 0)


class Task(models.Model):
    class TaskType(models.TextChoices):
        CONNECT = "connect"
        CHECK_PENDING = "check_pending"
        FOLLOW_UP = "follow_up"

    class Status(models.TextChoices):
        PENDING = "pending"
        RUNNING = "running"
        COMPLETED = "completed"
        FAILED = "failed"

    task_type = models.CharField(max_length=20, choices=TaskType.choices)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    scheduled_at = models.DateTimeField()
    payload = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    objects = TaskQuerySet.as_manager()

    class Meta:
        app_label = "linkedin"
        indexes = [
            models.Index(fields=["status", "scheduled_at"]),
        ]

    def __str__(self):
        return f"{self.task_type} [{self.status}] scheduled={self.scheduled_at}"

    def mark_running(self):
        self.status = self.Status.RUNNING
        self.started_at = timezone.now()
        self.save(update_fields=["status", "started_at"])

    def mark_completed(self):
        self.status = self.Status.COMPLETED
        self.completed_at = timezone.now()
        self.save(update_fields=["status", "completed_at"])

    def mark_failed(self):
        self.status = self.Status.FAILED
        self.save(update_fields=["status"])
