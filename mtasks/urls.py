from django.urls import path

from .views import task_flow


urlpatterns = [
    path('<int:pk>/flow/', task_flow, name='mtasks_task_flow'),
]
