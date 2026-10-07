import enum
import logging
from functools import partial
from hashlib import sha1

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.utils.translation import gettext_lazy as _

from partner.models import Partner
from coleman.utils.mail import send_mail_async as send_mail


logger = logging.getLogger(__name__)

number_tr = _("number")

# Fields used to create an index in the DB and sort the tasks in the Admin
TASK_PRIORITY_FIELDS = ('state', '-priority', '-deadline')


class State(enum.Enum):
    """
    Status of completion of the task
    (codes are prefixed with numbers to be easily sorted in the DB).
    """
    TO_DO = '00-to-do'
    IN_PROGRESS = '10-in-progress'
    BLOCKED = '20-blocked'
    DONE = '30-done'
    DISMISSED = '40-dismissed'


class Priority(enum.Enum):
    """
    The priority of the task
    (codes are prefixed with numbers to be easily sorted in the DB).
    """
    LOW = '00-low'
    NORMAL = '10-normal'
    HIGH = '20-high'
    CRITICAL = '30-critical'


# ---------------------------------------------------------------------------
# State transition allow-table
#
# Single source of truth for the legal state edges. Task.clean (cross-field
# validation) and Task.save (hard guard so no caller can persist an illegal
# edge) both read from this table; the serializer delegates to Task.clean.
# Self loops are listed so plain re-saves without a real transition are valid.
# ---------------------------------------------------------------------------
ALLOWED_STATE_TRANSITIONS = {
    State.TO_DO.value: frozenset((
        State.TO_DO.value,
        State.IN_PROGRESS.value,
        State.BLOCKED.value,
        State.DONE.value,
        State.DISMISSED.value,
    )),
    State.IN_PROGRESS.value: frozenset((
        State.IN_PROGRESS.value,
        State.BLOCKED.value,
        State.DONE.value,
        State.DISMISSED.value,
    )),
    State.BLOCKED.value: frozenset((
        State.BLOCKED.value,
        State.IN_PROGRESS.value,
        State.DONE.value,
        State.DISMISSED.value,
    )),
    State.DONE.value: frozenset((
        State.DONE.value,
    )),
    State.DISMISSED.value: frozenset((
        State.DISMISSED.value,
    )),
}

# Validation provenance labels shown by the task flow diagnostic page
VALIDATION_SOURCE_CLEAN = 'models.py: Task.clean (cross-field rules)'
VALIDATION_SOURCE_SERIALIZER_FIELDS = 'serializers.py: TaskSerializer (field-level rules)'


def is_allowed_state_edge(from_state: str, to_state: str) -> bool:
    return to_state in ALLOWED_STATE_TRANSITIONS.get(from_state, frozenset())


def validate_state_edge(from_state: str, to_state: str):
    """Cross-field rule (old DB state vs new state). Raises ValidationError."""
    if not is_allowed_state_edge(from_state, to_state):
        raise ValidationError({
            'state': _('State transition from "%(from)s" to "%(to)s" is not allowed.') % {
                'from': from_state,
                'to': to_state,
            },
        })


def validate_task_title(title):
    """Field-level rule for the title, shared with the serializer."""
    if title is not None and not title.strip():
        raise ValidationError(_('Title cannot be blank.'))
    return title


class TaskManager(models.Manager):

    def others(self, pk, **kwargs):
        """
        Return queryset with all objects
        excluding the one with the "pk" passed, but
        applying the filters passed in "kwargs".
        """
        return self.exclude(pk=pk).filter(**kwargs)


class Task(models.Model):
    class Meta:
        verbose_name = _("Task")
        verbose_name_plural = _("Tasks")
        indexes = [
            models.Index(fields=TASK_PRIORITY_FIELDS, name='mtasks_task_priority_idx'),
        ]

    STATES = (
        (State.TO_DO.value, _('To Do')),
        (State.IN_PROGRESS.value, _('In Progress')),
        (State.BLOCKED.value, _('Blocked')),
        (State.DONE.value, _('Done')),
        (State.DISMISSED.value, _('Dismissed'))
    )

    PRIORITIES = (
        (Priority.LOW.value, _('Low')),
        (Priority.NORMAL.value, _('Normal')),
        (Priority.HIGH.value, _('High')),
        (Priority.CRITICAL.value, _('Critical')),
    )

    title = models.CharField(_("title"), max_length=200)
    partner = models.ForeignKey(Partner, blank=True, null=True, on_delete=models.PROTECT)
    description = models.TextField(_("description"), max_length=2000, null=True, blank=True)
    resolution = models.TextField(_("resolution"), max_length=2000, null=True, blank=True)
    deadline = models.DateField(_("deadline"), null=True, blank=True)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, related_name='tasks_assigned', verbose_name=_('assigned to'),
                             on_delete=models.SET_NULL, null=True, blank=True)
    state = models.CharField(_("state"), max_length=20, choices=STATES, default=State.TO_DO.value)
    priority = models.CharField(_("priority"), max_length=20, choices=PRIORITIES, default=Priority.NORMAL.value)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, related_name='users_created', verbose_name=_('created by'),
                                   on_delete=models.SET_NULL, null=True)
    created_at = models.DateTimeField(_("created at"), auto_now_add=True, editable=False)
    last_modified = models.DateTimeField(_("last modified"), auto_now=True, editable=False)

    objects = TaskManager()

    def __str__(self):
        return "[%s] %s" % (self.number, self.title)

    @property
    def number(self) -> str:
        return "{:08d}".format(self.pk)

    # Attribute used to mark who validated a write (the diagnostic page
    # exposes it). It is never persisted. Defaults to the model layer.
    validation_sources = None

    def save(self, *args, **kwargs):
        is_new = self.pk is None
        old_task = None
        if not is_new:
            old_task = Task.objects.get(pk=self.pk)
            # The allow-table is the single gate for state edges: run the
            # same cross-field rule here so illegal edges cannot be persisted
            # even when clean() was bypassed (raw .save() calls).
            validate_state_edge(old_task.state, self.state)
        super().save(*args, **kwargs)
        refresh_from_db = False
        if is_new:
            # New task: "created" step, notification scheduled post-commit.
            self._record_flow_event(
                kind=TaskFlowEvent.KIND_CREATED,
                from_state=None,
                to_state=self.state,
                notification_kind='new_task',
                idem_key='task-%s:created' % self.pk,
            )
        else:
            if old_task.state != self.state:
                self._record_flow_event(
                    kind=TaskFlowEvent.KIND_STATE_CHANGED,
                    from_state=old_task.state,
                    to_state=self.state,
                    notification_kind='state_change',
                    idem_key='task-%s:state:%s->%s' % (self.pk, old_task.state, self.state),
                )
                refresh_from_db = True
            if old_task.partner_id != self.partner_id:
                self._record_flow_event(
                    kind=TaskFlowEvent.KIND_PARTNER_CHANGED,
                    from_state=self.state,
                    to_state=self.state,
                    notification_kind='new_task',
                    idem_key='task-%s:partner:%s' % (self.pk, self.partner_id or 0),
                )
                refresh_from_db = True
        if refresh_from_db:
            self.refresh_from_db()

    def get_validation_sources(self):
        sources = self.validation_sources
        if not sources:
            return [VALIDATION_SOURCE_CLEAN]
        return list(sources)

    def _task_field_snapshot(self):
        return {
            'number': self.number if self.pk is not None else None,
            'title': self.title,
            'partner_id': self.partner_id,
            'partner': str(self.partner) if self.partner_id else None,
            'user_id': self.user_id,
            'user': str(self.user) if self.user_id else None,
            'description': self.description,
            'resolution': self.resolution,
            'deadline': self.deadline.isoformat() if self.deadline else None,
            'state': self.state,
            'priority': self.priority,
        }

    def _record_flow_event(self, kind, from_state, to_state,
                           notification_kind, idem_key):
        """
        Append one timeline step (idempotent on ``idem_key``) and schedule
        its notification for AFTER the surrounding transaction commits.

        The idempotency key is content based (task + edge), not save based:
        if the transaction rolls back the row disappears with it, and the
        same key reused later cannot produce a second notification once it
        has been delivered.
        """
        sources = self.get_validation_sources()
        event, created = TaskFlowEvent.objects.get_or_create(
            idem_key=idem_key,
            defaults={
                'task': self,
                'kind': kind,
                'from_state': from_state,
                'to_state': to_state,
                'field_snapshot': self._task_field_snapshot(),
                'validation_sources': sources,
            },
        )
        if not created:
            # Repeated save carrying the same business step (e.g. another
            # save after the transition was already recorded) must never
            # reschedule/resend.
            logger.info("[Task #%s] Flow event %s already recorded, "
                        "notification kept as %s", self.number, idem_key,
                        event.notification_status)
            return event
        transaction.on_commit(
            partial(self._dispatch_notification, event.pk, notification_kind)
        )
        return event

    @staticmethod
    def _dispatch_notification(event_id, notification_kind):
        try:
            event = TaskFlowEvent.objects.select_related('task').get(pk=event_id)
        except TaskFlowEvent.DoesNotExist:
            return
        task = event.task
        if event.notification_status != TaskFlowEvent.NOTIFICATION_PENDING:
            # Already sent (or suppressed) - hard idempotency guard.
            return
        emails_to = task.get_notification_recipients()
        if not emails_to:
            event.mark_notification(
                TaskFlowEvent.NOTIFICATION_SUPPRESSED,
                reason='no recipients',
            )
            logger.info("[Task #%s] Notification for %s suppressed: "
                        "no recipients", task.number, event.idem_key)
            return
        subject, body = task.build_notification_email(notification_kind, event)
        try:
            send_mail(subject, body, settings.APP_EMAIL, emails_to)
        except Exception as e:
            logger.warning("[Task #%s] Error trying to send the task "
                           "notification - %s: %s", task.number,
                           e.__class__.__name__, str(e))
            event.mark_notification(
                TaskFlowEvent.NOTIFICATION_SUPPRESSED,
                reason='sending error: %s: %s' % (
                    e.__class__.__name__, str(e)),
            )
            return
        event.mark_notification(
            TaskFlowEvent.NOTIFICATION_SENT,
            recipients=emails_to,
            subject=subject,
        )
        logger.info("[Task #%s] Notification for %s sent to %s",
                    task.number, event.idem_key, emails_to)

    def clean(self):
        validation_errors = {}

        # ---- Cross-field rule: state edge from the allow-table ------------
        if self.pk is not None:
            try:
                old_state = Task.objects.filter(pk=self.pk).values_list(
                    'state', flat=True).first()
            except Task.DoesNotExist:
                old_state = None
            if old_state is not None:
                try:
                    validate_state_edge(old_state, self.state)
                except ValidationError as err:
                    validation_errors.update(err.message_dict)

        # ---- Cross-field rule: duplicate open task title ------------------
        title = self.title.strip() if self.title else self.title
        if self.partner:
            if Task.objects \
                    .others(self.pk, title=title, partner=self.partner) \
                    .exclude(state__in=(State.DONE.value, State.DISMISSED.value)) \
                    .exists():
                validation_errors['title'] = _('Open task with this title and partner already exists.')
        else:
            if Task.objects \
                    .others(self.pk, title=title, partner=None) \
                    .exclude(state__in=(State.DONE.value, State.DISMISSED.value)) \
                    .exists():
                validation_errors['title'] = _('Open task with this title and no partner already exists.')

        # Add more validations HERE

        if len(validation_errors):
            raise ValidationError(validation_errors)

    def get_notification_recipients(self):
        emails_to = []
        if settings.TASKS_SEND_EMAILS_TO_PARTNERS and self.partner and self.partner.email:
            emails_to.append(self.partner.email)
        if settings.TASKS_SEND_EMAILS_TO_ASSIGNED and self.user and self.user.email:
            emails_to.append(self.user.email)
        return emails_to

    def build_notification_email(self, notification_kind, event=None):
        vals = {
            "id": self.number,
            "user": str(self.user) if self.user else '(Not assigned yet)',
            "title": self.title,
            "description": self.description or '-',
            "sign": settings.SITE_HEADER,
        }
        if notification_kind == 'state_change' and event is not None:
            state_labels = dict(Task.STATES)
            vals["from_state"] = state_labels.get(event.from_state, event.from_state)
            vals["to_state"] = state_labels.get(event.to_state, event.to_state)
            subject = '[{app}] [#{id}] Task State Changed: {to}'.format(
                app=settings.APP_NAME, id=self.number, to=vals["to_state"])
            body = settings.MTASKS_EMAIL_STATE_CHANGE.format(**vals)
        else:
            if settings.TASKS_VIEWER_ENABLED:
                vals["url"] = self.get_tasks_viewer_url()
                body = settings.MTASKS_EMAIL_WITH_URL.format(**vals)
            else:
                body = settings.MTASKS_EMAIL_WITHOUT_URL.format(**vals)
            subject = '[{app}] [#{id}] New Task Created'.format(
                app=settings.APP_NAME, id=self.number)
        return subject, body

    def send_new_task_email(self):
        """
        Override with a custom email
        """
        emails_to = self.get_notification_recipients()
        if len(emails_to):
            logger.info("[Task #%s] Sending task creation email to: %s", self.number, emails_to)
            subject, email_body = self.build_notification_email('new_task')
            try:
                send_mail(
                    subject, email_body,
                    settings.APP_EMAIL,
                    emails_to,
                )
            except Exception as e:
                logger.warning("[Task #%s] Error trying to send the task creation email - %s: %s",
                               self.number, e.__class__.__name__, str(e))

    def get_tasks_viewer_url(self):
        """
        Verification token added to the Tasks Viewer URL so each one
        sent through email cannot be used to change the order number and
        access to other orders.

        It uses as input a salt code configured and the ID number.

        See: coleman/settings_emails.py
             https://github.com/mrsarm/tornado-dcoleman-mtasks-viewer
        """
        salt = settings.TASKS_VIEWER_HASH_SALT
        if not settings.DEBUG and salt == '1two3':
            logger.warning("Insecure salt code used to send email orders, do NOT use it in PRODUCTION")
        # created_at_as_iso = self.created_at.strftime("%Y-%m-%dT%H:%M:%S.%fZ") # This ISO format is the same used
                                                                                # used by the REST serializer
        token = "{}-{}".format(salt, self.pk)                                   # SHA-1 is enough secure for
        token = sha1(token.encode('utf-8')).hexdigest()                         # this purpose (SHA-2 is too long)
        return settings.TASKS_VIEWER_ENDPOINT.format(number=self.number, token=token)


class TaskFlowEvent(models.Model):
    """
    One step in a task's flow timeline: creation, a state transition,
    a partner reassignment and the notification that step produced.

    ``idem_key`` makes every business step unique, so a rollback (the row
    disappears together with its transaction) or a repeated save cannot
    yield a duplicate step nor a duplicate notification.
    """
    KIND_CREATED = 'created'
    KIND_STATE_CHANGED = 'state_changed'
    KIND_PARTNER_CHANGED = 'partner_changed'
    KINDS = (
        (KIND_CREATED, _('Created')),
        (KIND_STATE_CHANGED, _('State changed')),
        (KIND_PARTNER_CHANGED, _('Partner changed')),
    )

    NOTIFICATION_PENDING = 'pending'
    NOTIFICATION_SENT = 'sent'
    NOTIFICATION_SUPPRESSED = 'suppressed'
    NOTIFICATION_STATUSES = (
        (NOTIFICATION_PENDING, _('Pending')),
        (NOTIFICATION_SENT, _('Sent')),
        (NOTIFICATION_SUPPRESSED, _('Suppressed')),
    )

    task = models.ForeignKey(
        Task, related_name='flow_events', on_delete=models.CASCADE)
    created_at = models.DateTimeField(_("created at"), auto_now_add=True)
    kind = models.CharField(_("kind"), max_length=20, choices=KINDS)
    from_state = models.CharField(
        _("from state"), max_length=20, choices=Task.STATES,
        null=True, blank=True)
    to_state = models.CharField(
        _("to state"), max_length=20, choices=Task.STATES,
        null=True, blank=True)
    field_snapshot = models.JSONField(_("raw fields"), default=dict)
    validation_sources = models.JSONField(
        _("validation sources"), default=list)
    idem_key = models.CharField(_("idempotency key"), max_length=120, unique=True)
    notification_status = models.CharField(
        _("notification status"), max_length=12,
        choices=NOTIFICATION_STATUSES, default=NOTIFICATION_PENDING)
    notification_reason = models.CharField(
        _("notification reason"), max_length=200, blank=True, default='')
    notification_recipients = models.JSONField(
        _("notification recipients"), default=list, blank=True)
    notification_subject = models.CharField(
        _("notification subject"), max_length=200, blank=True, default='')

    class Meta:
        verbose_name = _("Task flow event")
        verbose_name_plural = _("Task flow events")
        ordering = ('created_at', 'id')

    def __str__(self):
        return '#%s %s (%s)' % (self.task_id, self.kind, self.idem_key)

    @property
    def kind_label(self):
        return dict(self.KINDS).get(self.kind, self.kind)

    @property
    def state_label(self):
        labels = dict(Task.STATES)
        if self.from_state and self.from_state != self.to_state:
            return '%s → %s' % (
                labels.get(self.from_state, self.from_state),
                labels.get(self.to_state, self.to_state))
        return labels.get(self.to_state, self.to_state or '')

    def mark_notification(self, status, reason='', recipients=None, subject=''):
        self.notification_status = status
        self.notification_reason = reason
        self.notification_recipients = recipients or []
        self.notification_subject = subject
        self.save(update_fields=(
            'notification_status', 'notification_reason',
            'notification_recipients', 'notification_subject'))


class Item(models.Model):
    class Meta:
        verbose_name = _("Item")
        verbose_name_plural = _("Check List")

    task = models.ForeignKey(Task, on_delete=models.CASCADE)
    item_description = models.CharField(_("description"), max_length=200)
    is_done = models.BooleanField(_("done?"), default=False)

    def __str__(self):
        return self.item_description
