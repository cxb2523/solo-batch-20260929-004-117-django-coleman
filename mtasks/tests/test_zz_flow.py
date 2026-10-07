import json
from unittest import mock

import pytest

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import transaction

from mtasks.models import (
    Task,
    TaskFlowEvent,
    State,
    is_allowed_state_edge,
    VALIDATION_SOURCE_CLEAN,
    VALIDATION_SOURCE_SERIALIZER_FIELDS,
)
from mtasks.serializers import TaskSerializer, TaskViewSet
from rest_framework.test import APIRequestFactory, force_authenticate


pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture()
def staff_user(db):
    return User.objects.create_superuser(
        username='admin', email='admin@example.com', password='pass1234')


@pytest.fixture()
def mailoutbox(settings):
    settings.TASKS_SEND_EMAILS_TO_ASSIGNED = True
    sent = []

    def fake_send(subject, body, from_email, recipient_list, *args, **kwargs):
        sent.append({
            'subject': subject,
            'body': body,
            'from_email': from_email,
            'recipients': list(recipient_list),
        })

    with mock.patch('mtasks.models.send_mail', side_effect=fake_send):
        yield sent


@pytest.fixture()
def assigned_task(mailoutbox, staff_user):
    task = Task(title='Flow task', user=staff_user)
    task.save()
    return task


# ---------------------------------------------------------------- allow-table

def test_allowed_state_table_drives_edges():
    assert is_allowed_state_edge(State.TO_DO.value, State.IN_PROGRESS.value)
    assert is_allowed_state_edge(State.IN_PROGRESS.value, State.BLOCKED.value)
    assert not is_allowed_state_edge(State.DONE.value, State.IN_PROGRESS.value)
    assert not is_allowed_state_edge(State.DISMISSED.value, State.DONE.value)
    # self loops are legal so plain re-saves stay valid
    assert is_allowed_state_edge(State.BLOCKED.value, State.BLOCKED.value)


def test_clean_rejects_illegal_edge(assigned_task):
    assigned_task.state = State.DONE.value
    assigned_task.save()

    reopened = Task.objects.get(pk=assigned_task.pk)
    reopened.state = State.IN_PROGRESS.value
    with pytest.raises(ValidationError) as exc:
        reopened.clean()
    assert 'state' in exc.value.message_dict


def test_save_hard_guards_illegal_edge(assigned_task):
    assigned_task.state = State.DISMISSED.value
    assigned_task.save()

    reopened = Task.objects.get(pk=assigned_task.pk)
    reopened.state = State.IN_PROGRESS.value
    with pytest.raises(ValidationError):
        reopened.save()
    assert Task.objects.get(pk=assigned_task.pk).state == State.DISMISSED.value


# ------------------------------------------------------------- notifications

def test_notification_sent_after_commit_per_step(mailoutbox, assigned_task, staff_user):
    # Creation notification
    assert len(mailoutbox) == 1
    assert mailoutbox[0]['recipients'] == ['admin@example.com']

    assigned_task.state = State.IN_PROGRESS.value
    assigned_task.save()
    assert len(mailoutbox) == 2
    assert 'State Changed' in mailoutbox[1]['subject']

    assigned_task.state = State.BLOCKED.value
    assigned_task.save()
    assert len(mailoutbox) == 3

    events = list(assigned_task.flow_events.order_by('id'))
    assert [e.kind for e in events] == [
        TaskFlowEvent.KIND_CREATED,
        TaskFlowEvent.KIND_STATE_CHANGED,
        TaskFlowEvent.KIND_STATE_CHANGED,
    ]
    assert all(
        e.notification_status == TaskFlowEvent.NOTIFICATION_SENT for e in events)


def test_repeated_save_does_not_resend(mailoutbox, assigned_task):
    assert len(mailoutbox) == 1

    assigned_task.title = 'Flow task renamed'
    assigned_task.save()      # no state/partner change: no new step
    assigned_task.save()      # again
    assert len(mailoutbox) == 1
    assert assigned_task.flow_events.count() == 1

    assigned_task.state = State.IN_PROGRESS.value
    assigned_task.save()
    assigned_task.state = State.IN_PROGRESS.value
    assigned_task.save()      # same edge repeated: suppressed, no resend
    assert len(mailoutbox) == 2

    transition = assigned_task.flow_events.filter(
        kind=TaskFlowEvent.KIND_STATE_CHANGED).get()
    assert transition.idem_key == 'task-%s:state:%s->%s' % (
        assigned_task.pk, State.TO_DO.value, State.IN_PROGRESS.value)


def test_rolled_back_transition_sends_nothing(mailoutbox, assigned_task):
    assert len(mailoutbox) == 1
    with pytest.raises(ValueError):
        with transaction.atomic():
            assigned_task.state = State.DONE.value
            assigned_task.save()
            # event exists inside the transaction...
            assert TaskFlowEvent.objects.filter(
                task=assigned_task,
                kind=TaskFlowEvent.KIND_STATE_CHANGED).exists()
            raise ValueError('boom')

    # ...but the rollback removes it and on_commit never fires
    assert len(mailoutbox) == 1
    assert Task.objects.get(pk=assigned_task.pk).state == State.TO_DO.value
    assert not assigned_task.flow_events.filter(
        kind=TaskFlowEvent.KIND_STATE_CHANGED).exists()

    # the same edge can still be recorded afterwards with exactly one email
    assigned_task.state = State.DONE.value
    assigned_task.save()
    assert len(mailoutbox) == 2


def test_notification_suppressed_without_recipients(mailoutbox, staff_user):
    task = Task(title='No recipients')
    task.save()
    created_event = task.flow_events.get(kind=TaskFlowEvent.KIND_CREATED)
    assert created_event.notification_status == \
        TaskFlowEvent.NOTIFICATION_SUPPRESSED
    assert created_event.notification_reason == 'no recipients'
    assert mailoutbox == []


# --------------------------------------------------------------- serializer

def test_serializer_reuses_clean_for_cross_field_rules():
    Task.objects.create(title='Dup title')
    serializer = TaskSerializer(data={
        'title': 'Dup title',
        'state': State.TO_DO.value,
    })
    assert not serializer.is_valid()
    assert 'title' in serializer.errors


def test_serializer_illegal_edge_matches_clean():
    user = User.objects.create_superuser(
        username='apiadmin', email='api@example.com', password='pass1234')
    task = Task.objects.create(title='Edge task', state=State.DONE.value)
    request = APIRequestFactory().patch(
        '/api/v1/tasks/%s/' % task.number,
        data=json.dumps({'state': State.IN_PROGRESS.value}),
        content_type='application/json')
    force_authenticate(request, user=user)
    view = TaskViewSet.as_view({'patch': 'partial_update'})
    response = view(request, pk=task.pk)
    response.render()
    assert response.status_code == 400
    assert 'state' in json.loads(response.content)
    task.refresh_from_db()
    assert task.state == State.DONE.value


def test_serializer_field_level_blank_title():
    serializer = TaskSerializer(data={'title': '   '})
    assert not serializer.is_valid()
    assert 'title' in serializer.errors


def test_serializer_marks_validation_sources():
    serializer = TaskSerializer(data={
        'title': 'Via API',
        'state': State.TO_DO.value,
    })
    assert serializer.is_valid(), serializer.errors
    task = serializer.save()
    sources = task.flow_events.get(
        kind=TaskFlowEvent.KIND_CREATED).validation_sources
    assert VALIDATION_SOURCE_SERIALIZER_FIELDS in sources
    assert VALIDATION_SOURCE_CLEAN in sources


def test_model_save_marks_clean_source():
    task = Task(title='Via admin')
    task.save()
    sources = task.flow_events.get(
        kind=TaskFlowEvent.KIND_CREATED).validation_sources
    assert sources == [VALIDATION_SOURCE_CLEAN]


def test_api_response_structure_unchanged():
    task = Task.objects.create(title='Shape')
    user = User.objects.create_superuser(
        username='reader', email='r@example.com', password='pass1234')
    request = APIRequestFactory().get('/api/v1/tasks/%s/' % task.number)
    force_authenticate(request, user=user)
    view = TaskViewSet.as_view({'get': 'retrieve'})
    response = view(request, pk=task.pk)
    response.render()
    data = json.loads(response.content)
    assert set(data.keys()) == {
        'number', 'title', 'partner', 'user', 'description',
        'resolution', 'deadline', 'state', 'created_at',
    }
    assert data['number'] == task.number


# ------------------------------------------------------------- the web page

def test_flow_page_requires_staff(client):
    response = client.get('/mtasks/1/flow/')
    assert response.status_code == 302
    assert '/admin/login/' in response.url


def test_flow_page_timeline_and_badges(client, staff_user, mailoutbox):
    task = Task(title='Page task', user=staff_user)
    task.save()
    task.state = State.IN_PROGRESS.value
    task.save()
    task.state = State.DONE.value
    task.save()

    client.force_login(staff_user)
    response = client.get('/mtasks/%s/flow/' % task.pk)
    assert response.status_code == 200
    content = response.content.decode()
    assert 'Page task' in content
    assert content.count('notification: sent') == 3
    assert 'Idempotency key' in content
    assert 'Validated by' in content
    assert 'Allowed state transitions' in content

    # a step with no recipient renders the suppressed badge
    other = Task(title='Silent')
    other.save()
    response = client.get('/mtasks/%s/flow/' % other.pk)
    assert 'notification: suppressed' in response.content.decode()
