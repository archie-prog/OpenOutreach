# linkedin/admin.py
from django import forms
from django.contrib import admin, messages
from django.db.models import Count
from django.shortcuts import redirect, render
from django.urls import path, reverse
from django.utils import timezone
from django.utils.html import format_html

from crm.models import Lead

from linkedin.leads import importer
from linkedin.models import (
    AccountDailyCounter, ActionLog, Campaign, LeadCampaignState, LeadList, LinkedInProfile,
    Message, MessageThread, Sequence, SequenceStep, SiteConfig,
)
from linkedin.sequences import executor


@admin.register(SiteConfig)
class SiteConfigAdmin(admin.ModelAdmin):
    list_display = ("__str__", "llm_provider", "ai_model", "llm_api_base")

    def has_add_permission(self, request):
        return not SiteConfig.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Campaign)
class CampaignAdmin(admin.ModelAdmin):
    list_display = ("name", "status", "sequence", "lead_list", "is_freemium")
    list_filter = ("status", "is_freemium")
    filter_horizontal = ("users",)
    raw_id_fields = ("sequence", "lead_list")
    actions = ("activate_and_enroll",)

    @admin.action(description="Activate + enroll lead_list into the sequence")
    def activate_and_enroll(self, request, queryset):
        total = 0
        dup = 0
        for campaign in queryset:
            if campaign.sequence_id and campaign.lead_list_id:
                campaign.status = Campaign.Status.ACTIVE
                campaign.save(update_fields=["status"])
                result = executor.enroll_campaign(campaign)
                total += result["enrolled"]
                dup += result["skipped_duplicate"]
        msg = f"Enrolled {total} lead(s) across {queryset.count()} campaign(s)."
        if dup:
            msg += f" ⚠ Skipped {dup} already live in another campaign (duplicate-protected — not contacted twice)."
        self.message_user(request, msg, messages.WARNING if dup else messages.SUCCESS)


class SequenceStepInline(admin.TabularInline):
    model = SequenceStep
    extra = 0
    fields = ("step_type", "branch", "parent", "order_in_branch", "config")
    raw_id_fields = ("parent",)


@admin.register(Sequence)
class SequenceAdmin(admin.ModelAdmin):
    list_display = ("name", "owner", "step_count", "created_at", "archived_at")
    list_filter = ("archived_at",)
    search_fields = ("name",)
    inlines = (SequenceStepInline,)
    actions = ("archive_selected",)

    @admin.display(description="steps")
    def step_count(self, obj):
        return obj.steps.count()

    def has_delete_permission(self, request, obj=None):
        return False

    @admin.action(description="Archive selected sequences (soft-delete)")
    def archive_selected(self, request, queryset):
        for seq in queryset:
            seq.archive()
        self.message_user(request, f"Archived {queryset.count()} sequence(s).", messages.SUCCESS)


@admin.register(LeadCampaignState)
class LeadCampaignStateAdmin(admin.ModelAdmin):
    list_display = ("lead", "campaign", "state", "current_step", "awaiting_decision", "next_action_due_at")
    list_filter = ("state", "campaign", "awaiting_decision")
    raw_id_fields = ("lead", "campaign", "current_step")
    readonly_fields = ("created_at", "last_action_at")
    date_hierarchy = "next_action_due_at"

    def has_delete_permission(self, request, obj=None):
        return False


class MessageInline(admin.TabularInline):
    model = Message
    extra = 0
    fields = ("direction", "body", "sent_at", "sent_via_tool")
    readonly_fields = fields
    can_delete = False


class UnreadFilter(admin.SimpleListFilter):
    title = "read status"
    parameter_name = "read"

    def lookups(self, request, model_admin):
        return (("unread", "Unread"), ("read", "Read"))

    def queryset(self, request, qs):
        if self.value() == "unread":
            return qs.filter(read_at__isnull=True)
        if self.value() == "read":
            return qs.filter(read_at__isnull=False)
        return qs


class ToolScopeFilter(admin.SimpleListFilter):
    """Privacy default: only threads this tool messaged into, unless 'all' is chosen."""

    title = "scope"
    parameter_name = "scope"

    def lookups(self, request, model_admin):
        return (("all", "All LinkedIn conversations"),)

    def queryset(self, request, qs):
        if self.value() == "all":
            return qs
        from django.db.models import Q

        # Tool-initiated = we messaged into it, or the lead is campaign-managed.
        return qs.filter(
            Q(messages__direction="out", messages__sent_via_tool=True)
            | Q(lead__campaign_states__isnull=False)
        ).distinct()


class LeadSourceFilter(admin.SimpleListFilter):
    title = "lead source"
    parameter_name = "lead_list"

    def lookups(self, request, model_admin):
        return [(ll.pk, ll.name) for ll in LeadList.objects.all()[:50]]

    def queryset(self, request, qs):
        if self.value():
            return qs.filter(lead__lead_list_id=self.value())
        return qs


@admin.register(MessageThread)
class MessageThreadAdmin(admin.ModelAdmin):
    """Unified inbox (M5). Privacy default hides threads not initiated via this tool."""

    list_display = ("lead", "account", "has_inbound_reply", "unread", "last_message_at")
    list_filter = (ToolScopeFilter, UnreadFilter, "has_inbound_reply", "account", LeadSourceFilter)
    search_fields = ("lead__public_identifier", "messages__body")
    raw_id_fields = ("lead", "account")
    readonly_fields = ("created_at", "last_polled_at", "last_message_at")
    inlines = (MessageInline,)
    actions = ("mark_read", "pause_campaign")
    ordering = ("-last_message_at",)

    @admin.display(boolean=True, description="unread")
    def unread(self, obj):
        return obj.read_at is None

    def has_delete_permission(self, request, obj=None):
        return False

    @admin.action(description="Mark read")
    def mark_read(self, request, queryset):
        n = queryset.update(read_at=timezone.now())
        self.message_user(request, f"Marked {n} thread(s) read.", messages.SUCCESS)

    @admin.action(description="Pause campaign for these leads")
    def pause_campaign(self, request, queryset):
        n = 0
        for thread in queryset:
            n += LeadCampaignState.objects.filter(
                lead=thread.lead, state=LeadCampaignState.State.ACTIVE,
            ).update(state=LeadCampaignState.State.PAUSED_MANUAL)
        self.message_user(request, f"Paused {n} active sequence state(s).", messages.SUCCESS)

    def get_urls(self):
        custom = [
            path(
                "<int:thread_id>/mark-read/",
                self.admin_site.admin_view(self.mark_read_view),
                name="linkedin_messagethread_mark_read",
            ),
        ]
        return custom + super().get_urls()

    def mark_read_view(self, request, thread_id):
        MessageThread.objects.filter(pk=thread_id).update(read_at=timezone.now())
        self.message_user(request, "Thread marked read.", messages.SUCCESS)
        return redirect("admin:linkedin_messagethread_changelist")


@admin.register(Message)
class MessageAdmin(admin.ModelAdmin):
    list_display = ("thread", "direction", "sent_at", "sent_via_tool", "fetched_at")
    list_filter = ("direction", "sent_via_tool")
    raw_id_fields = ("thread", "sender_account")
    readonly_fields = ("fetched_at",)

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(LinkedInProfile)
class LinkedInProfileAdmin(admin.ModelAdmin):
    list_display = ("user", "linkedin_username", "active", "legal_accepted", "has_inmail", "usage_link")
    list_filter = ("active", "has_inmail")
    raw_id_fields = ("user", "self_lead")

    @admin.display(description="usage")
    def usage_link(self, obj):
        if not obj.pk:
            return ""
        url = reverse("admin:linkedin_linkedinprofile_daily_usage", args=[obj.pk])
        return format_html('<a href="{}">daily usage</a>', url)

    def get_urls(self):
        custom = [
            path(
                "<int:account_id>/daily-usage/",
                self.admin_site.admin_view(self.daily_usage_view),
                name="linkedin_linkedinprofile_daily_usage",
            ),
        ]
        return custom + super().get_urls()

    def daily_usage_view(self, request, account_id):
        from datetime import timedelta

        account = LinkedInProfile.objects.get(pk=account_id)
        today = timezone.now().date()
        today_counts = {
            c.action_type: c.count
            for c in AccountDailyCounter.objects.filter(account=account, date=today)
        }
        caps = account.daily_caps_json or {}
        usage_rows = [
            {"action": action, "used": today_counts.get(action, 0), "cap": cap}
            for action, cap in caps.items()
        ]
        history = (
            AccountDailyCounter.objects
            .filter(account=account, date__gte=today - timedelta(days=30))
            .order_by("-date", "action_type")
        )
        context = {
            **self.admin_site.each_context(request),
            "title": f"Daily usage — {account}",
            "account": account,
            "usage_rows": usage_rows,
            "history": history,
            "opts": self.model._meta,
        }
        return render(request, "admin/linkedin/daily_usage.html", context)


@admin.register(AccountDailyCounter)
class AccountDailyCounterAdmin(admin.ModelAdmin):
    list_display = ("account", "date", "action_type", "count")
    list_filter = ("action_type", "account")
    date_hierarchy = "date"
    readonly_fields = ("account", "date", "action_type", "count")

    def has_delete_permission(self, request, obj=None):
        return False

    def has_add_permission(self, request):
        return False


@admin.register(ActionLog)
class ActionLogAdmin(admin.ModelAdmin):
    list_display = ("action_type", "linkedin_profile", "campaign", "created_at")
    list_filter = ("action_type", "campaign")
    raw_id_fields = ("linkedin_profile", "campaign")
    date_hierarchy = "created_at"
    readonly_fields = ("linkedin_profile", "campaign", "action_type", "created_at")


class EnrichmentStatusFilter(admin.SimpleListFilter):
    """Filter Leads by whether they have been enriched (have an embedding)."""

    title = "enrichment status"
    parameter_name = "enriched"

    def lookups(self, request, model_admin):
        return (("yes", "Enriched"), ("no", "Not enriched"))

    def queryset(self, request, queryset):
        if self.value() == "yes":
            return queryset.filter(embedding__isnull=False)
        if self.value() == "no":
            return queryset.filter(embedding__isnull=True)
        return queryset


class LeadListCsvImportForm(forms.Form):
    name = forms.CharField(max_length=200)
    csv_file = forms.FileField()


@admin.register(LeadList)
class LeadListAdmin(admin.ModelAdmin):
    list_display = ("name", "source_type", "lead_count", "created_at", "archived_at", "view_leads")
    list_filter = ("source_type", "archived_at")
    search_fields = ("name",)
    readonly_fields = ("created_at", "archived_at", "view_leads")
    actions = ("archive_selected",)
    change_list_template = "admin/linkedin/leadlist/change_list.html"

    def get_queryset(self, request):
        return super().get_queryset(request).annotate(_lead_count=Count("leads"))

    @admin.display(description="leads", ordering="_lead_count")
    def lead_count(self, obj):
        return getattr(obj, "_lead_count", obj.leads.count())

    @admin.display(description="")
    def view_leads(self, obj):
        if not obj.pk:
            return ""
        url = reverse("admin:crm_lead_changelist") + f"?lead_list__id__exact={obj.pk}"
        count = getattr(obj, "_lead_count", obj.leads.count())
        return format_html('<a href="{}">View {} leads</a>', url, count)

    # Soft-delete only: never expose hard delete (spec §0.1).
    def has_delete_permission(self, request, obj=None):
        return False

    @admin.action(description="Archive selected lead lists (soft-delete)")
    def archive_selected(self, request, queryset):
        count = 0
        for lead_list in queryset:
            lead_list.archive()
            count += 1
        self.message_user(request, f"Archived {count} lead list(s).", messages.SUCCESS)

    def get_urls(self):
        custom = [
            path(
                "import-csv/",
                self.admin_site.admin_view(self.import_csv_view),
                name="linkedin_leadlist_import_csv",
            ),
        ]
        return custom + super().get_urls()

    def import_csv_view(self, request):
        if request.method == "POST":
            form = LeadListCsvImportForm(request.POST, request.FILES)
            if form.is_valid():
                lead_list = importer.create_lead_list(
                    name=form.cleaned_data["name"],
                    owner=request.user,
                    source_type=LeadList.SourceType.CSV,
                )
                lines = form.cleaned_data["csv_file"].read().decode("utf-8").splitlines()
                try:
                    result = importer.import_csv(lead_list, iter(lines))
                except ValueError as exc:
                    self.message_user(request, str(exc), messages.ERROR)
                    return redirect("admin:linkedin_leadlist_import_csv")
                self.message_user(
                    request,
                    f"Imported '{lead_list.name}': created={result['created']} "
                    f"skipped={result['skipped']} invalid={result['invalid']}",
                    messages.SUCCESS,
                )
                return redirect("admin:linkedin_leadlist_changelist")
        else:
            form = LeadListCsvImportForm()
        context = {
            **self.admin_site.each_context(request),
            "form": form,
            "title": "Import leads from CSV",
            "opts": self.model._meta,
        }
        return render(request, "admin/linkedin/leadlist/import_csv.html", context)


@admin.register(Lead)
class LeadAdmin(admin.ModelAdmin):
    list_display = ("public_identifier", "lead_list", "is_enriched", "disqualified", "creation_date")
    list_filter = ("lead_list", "disqualified", EnrichmentStatusFilter)
    search_fields = ("public_identifier", "linkedin_url")
    raw_id_fields = ("lead_list",)
    readonly_fields = ("creation_date", "update_date", "urn")

    @admin.display(boolean=True, description="enriched")
    def is_enriched(self, obj):
        return obj.embedding is not None

    def has_delete_permission(self, request, obj=None):
        return False
