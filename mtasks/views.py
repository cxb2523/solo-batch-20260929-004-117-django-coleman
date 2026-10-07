from django.contrib.admin.views.decorators import staff_member_required
from django.shortcuts import get_object_or_404, render

from .models import Task, TaskFlowEvent, ALLOWED_STATE_TRANSITIONS


@staff_member_required
def task_flow(request, pk):
    """
    Task flow diagnostic page: timeline of every step from creation through
    transitions and notifications, with the raw fields and the validation
    provenance (serializers.py vs models.py: Task.clean) of each step.
    """
    task = get_object_or_404(Task, pk=pk)
    events = list(task.flow_events.all())
    state_labels = dict(Task.STATES)
    allow_table = [
        {
            'from': state_labels.get(from_state, from_state),
            'allowed_to': [
                state_labels.get(to_state, to_state)
                for to_state in sorted(to_states)
            ],
        }
        for from_state, to_states in ALLOWED_STATE_TRANSITIONS.items()
    ]
    context = {
        **site_admin_context(),
        'task': task,
        'events': events,
        'allow_table': allow_table,
        'notification_statuses': dict(TaskFlowEvent.NOTIFICATION_STATUSES),
        'title': 'Task #%s flow' % task.number,
    }
    return render(request, 'mtasks/task_flow.html', context)


def site_admin_context():
    from django.contrib import admin
    from django.conf import settings
    return {
        'site_header': admin.site.site_header or settings.SITE_HEADER,
        'site_title': admin.site.site_title or settings.SITE_HEADER,
    }
