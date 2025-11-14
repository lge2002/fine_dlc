# merger/urls.py
from django.urls import path
from . import views

urlpatterns = [
    path('run/<str:script_name>/', views.run_script, name='run_script'),
]
