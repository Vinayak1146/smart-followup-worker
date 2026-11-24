"""API routes. Everything the console needs, grouped by the noun it acts on."""
from django.urls import path

from . import views

urlpatterns = [
    path('meta/', views.meta, name='meta'),
    path('dashboard/', views.dashboard, name='dashboard'),

    path('customers/', views.customer_list, name='customer-list'),
    path('customers/<int:pk>/', views.customer_detail, name='customer-detail'),
    path('customers/<int:pk>/follow-up/', views.start_follow_up, name='customer-follow-up'),
    path('customers/<int:pk>/reply/', views.customer_reply, name='customer-reply'),
    path('customers/<int:pk>/purchase/', views.record_purchase, name='customer-purchase'),

    path('runs/', views.run_list, name='run-list'),
    path('runs/<uuid:run_id>/', views.run_detail, name='run-detail'),
    path('runs/<uuid:run_id>/decision/', views.decide_run, name='run-decision'),
    path('runs/<uuid:run_id>/retry/', views.retry_run, name='run-retry'),

    path('approvals/', views.approval_list, name='approval-list'),
    path('approvals/<int:pk>/decide/', views.decide_approval, name='approval-decide'),

    path('simulation/', views.simulation, name='simulation'),
    path('demo/seed/', views.seed_demo, name='demo-seed'),
    path('demo/reset/', views.reset_demo, name='demo-reset'),
]
