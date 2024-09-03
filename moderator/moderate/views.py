from dal import autocomplete
from dateutil.relativedelta import relativedelta
from django.conf import settings
from django.contrib import auth, messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.core.mail import send_mail
from django.core.paginator import EmptyPage, PageNotAnInteger, Paginator
from django.db.models import Count, Q
from django.http import Http404, JsonResponse
from django.http.response import HttpResponseRedirect
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.timezone import now as django_now
from mozilla_django_oidc.views import OIDCAuthenticationCallbackView

from moderator.moderate.forms import EventForm, QuestionForm
from moderator.moderate.models import Event, Question, Vote
from moderator.moderate.templatetags.helpers import can_moderate_event

SUBJECT = "Question moderation update"


class OIDCCallbackView(OIDCAuthenticationCallbackView):
    """Override OIDC callback view."""

    def login_failure(self, msg=""):
        """Returns a custom message in case of login failure."""

        if not msg:
            msg = "Login failed. Access is allowed only to staff and NDA members."
        messages.error(self.request, msg)
        return super(OIDCCallbackView, self).login_failure()


def main(request):
    """Render main page."""
    user = request.user
    if user.is_authenticated:
        events = (
            Event.objects.filter(archived=False)
            .prefetch_related("moderators")
            .annotate(
                approved_count=Count(
                    "questions", filter=Q(questions__is_accepted=True)
                ),
                rejected_count=Count(
                    "questions", filter=Q(questions__is_accepted=False)
                ),
                pending_count=Count("questions", filter=Q(questions__is_accepted=None)),
            )
        )

        if not user.userprofile.is_nda_member:
            events = events.exclude(is_nda=True)
        return render(request, "index.jinja", {"events": events, "user": user})
    return render(request, "index.jinja", {"user": user})


@login_required(login_url="/")
def archive(request):
    """List of all archived events."""
    q_args = {
        "archived": True,
    }
    # Filter out NDA events for non-NDA users
    if not request.user.userprofile.is_nda_member and not request.user.is_superuser:
        q_args["is_nda"] = False
    events_list = (
        Event.objects.filter(**q_args)
        .annotate(
            approved_count=Count("questions", filter=Q(questions__is_accepted=True))
        )
        .order_by("-created_at")
    )
    paginator = Paginator(events_list, settings.ITEMS_PER_PAGE)
    page = request.GET.get("page")

    try:
        events = paginator.page(page)
    except PageNotAnInteger:
        events = paginator.page(1)
    except EmptyPage:
        events = paginator.page(paginator.num_pages)

    return render(request, "archive.jinja", {"events": events})


@login_required(login_url="/")
def edit_event(request, slug=None):
    """Creates a new event."""
    user = request.user
    query_args = []
    user_can_edit = False

    if not user.is_superuser:
        query_args = [Q(moderators=user)]

    event = get_object_or_404(Event, *query_args, slug=slug) if slug else None
    # if there is an event or no slug, the user can edit
    if event or not slug:
        user_can_edit = True
    event_form = EventForm(request.POST or None, instance=event, user=user)

    if event_form.is_valid():
        e = event_form.save(commit=False)
        if not event:
            e.created_by = user
        e.save()
        event_form.save_m2m()

        if slug:
            msg = "Event successfully edited."
        else:
            msg = "Event successfully created."
        messages.success(request, msg)
        if e.archived:
            return redirect(reverse("archive"))
        return redirect(reverse("main"))

    ctx = {
        "slug": event.slug if event else None,
        "event_form": event_form,
        "event": event,
        "profile": user.userprofile,
        "user_can_edit": user_can_edit,
    }

    return render(request, "create_event.jinja", ctx)


@login_required(login_url="/")
def moderate_event(request, slug, q_id=None, accepted=None):
    event = get_object_or_404(Event, slug=slug)
    user = request.user

    if not (event.moderators.filter(pk=user.pk).exists() or user.is_superuser):
        raise Http404

    questions = Question.objects.filter(event=event, is_accepted__isnull=True)
    question_form = QuestionForm()

    # Update the question if it's accepted or rejected
    if q_id:
        try:
            question = Question.objects.get(id=q_id)
        except Question.DoesNotExist:
            raise ValidationError("This question is not valid")
        question.is_accepted = accepted
        question.save()

        if request.method == "POST":
            question_form = QuestionForm(request.POST, instance=question)
            # update the question with moderator's reply
            if question_form.is_valid():
                reason = question_form.cleaned_data.get("rejection_reason")
                Question.objects.filter(id=question.pk).update(rejection_reason=reason)
                if (
                    reason
                    and not question.is_accepted
                    and question.submitter_contact_info
                ):
                    send_mail(
                        SUBJECT,
                        reason,
                        settings.FROM_NOREPLY,
                        [question.submitter_contact_info],
                    )
                    messages.success(request, "Email sent successfully")
                return redirect(reverse("moderate_event", kwargs={"slug": event.slug}))

    return render(
        request,
        "moderation.jinja",
        {"user": user, "event": event, "questions": questions, "q_form": question_form},
    )


@login_required(login_url="/")
def delete_event(request, slug):
    """Delete an event."""
    user = request.user
    query_args = {"slug": slug, "moderators__in": [user]}
    # Allow superusers to edit all events
    if user.is_superuser:
        del query_args["moderators__in"]

    event = get_object_or_404(Event, **query_args)
    event.delete()
    msg = "Event successfully deleted."
    messages.success(request, msg)
    return redirect(reverse("main"))


@login_required(login_url="/")
def show_event(request, e_slug, q_id=None):
    """Render event questions."""
    event = get_object_or_404(Event, slug=e_slug)
    question = None
    user = request.user

    # Do not display NDA events to non NDA members or non employees.
    if event.is_nda and not user.userprofile.is_nda_member:
        raise Http404

    if q_id:
        question = Question.objects.get(id=q_id)

    questions_q = Question.objects.filter(event=event, is_accepted=True).annotate(
        vote_count=Count("votes")
    )
    if user.userprofile.is_admin or event.archived:
        questions = questions_q.order_by("-vote_count")
    else:
        questions = questions_q.order_by("?")

    question_form = QuestionForm(
        request.POST or None, instance=question, **{"is_locked": True}
    )

    is_new_question = False
    if question_form.is_valid() and not event.archived:
        question_obj = question_form.save(commit=False)
        # Do not change the user if posting a reply
        moderator_ids = event.moderators.values_list("id", flat=True)
        accept_question = not event.is_moderated or user.id in moderator_ids
        if not question_obj.id:
            is_new_question = True
            # mark as accepted for non moderated events
            if accept_question:
                question_obj.is_accepted = True
            if not question_obj.is_anonymous:
                question_obj.asked_by = user
                question_obj.submitter_contact_info = user.email
        elif not can_moderate_event(event, user):
            raise Http404
        question_obj.event = event
        question_obj.save()
        msg = "Your question has been successfully submitted. "
        if not accept_question:
            msg += "Review is pending by an event moderator."
        if is_new_question:
            messages.success(request, msg)

        return redirect(reverse("event", kwargs={"e_slug": event.slug}))

    return render(
        request,
        "questions.jinja",
        {
            "user": user,
            "open": not event.archived,
            "event": event,
            "questions": questions,
            "q_form": question_form,
        },
    )


@login_required
def upvote(request, q_id):
    """Upvote question"""

    question = Question.objects.get(pk=q_id)
    event = question.event
    user = request.user
    if not (
        user_can_vote := (
            event.users_can_vote or (event.is_nda and user.userprofile.is_nda_member)
        )
    ):
        msg = "Voting is not allowed for this event."
        messages.warning(request, msg)

    if (
        request.headers.get("x-requested-with") == "XMLHttpRequest"
        and not question.event.archived
        and user_can_vote
    ):
        if not Vote.objects.filter(user=user, question=question).exists():
            Vote.objects.create(user=user, question=question)
        else:
            Vote.objects.filter(user=user, question=question).delete()

        response_dict = {}
        if request.user.is_superuser or event.created_by == request.user:
            response_dict = {"current_vote_count": question.votes.count()}

        return JsonResponse(response_dict)

    return redirect(reverse("event", kwargs={"e_slug": event.slug}))


class ModeratorsAutocomplete(autocomplete.Select2QuerySetView):
    def get_queryset(self):
        if not self.request.user.is_authenticated:
            return User.objects.none()

        # exclude users that are not admins
        # and are not active or haven't logged in for 6 months
        last_login_date = django_now().date() - relativedelta(months=6)
        qs = User.objects.exclude(
            Q(is_active=False) | Q(is_superuser=False, last_login__lt=last_login_date)
        ).filter()

        if self.q:
            qs = qs.filter(
                Q(first_name__icontains=self.q)
                | Q(email__icontains=self.q)
                | Q(userprofile__username__icontains=self.q)
            )
        return qs


def login_local_user(request, username=""):
    """Allow a user to login for local dev."""
    if not (settings.DEV and settings.ENABLE_DEV_LOGIN) or not username:
        raise Http404

    user = None
    try:
        user = User.objects.get(username=username)
    except User.DoesNotExist:
        raise Http404

    if user:
        auth.login(request, user, backend="django.contrib.auth.backends.ModelBackend")
    return HttpResponseRedirect(reverse("main"))
