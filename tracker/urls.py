from django.urls import path
from . import views

urlpatterns = [
    path('', views.dashboard, name='dashboard'),
    path('login/', views.login_view, name='login'),
    path('logout/', views.logout_view, name='logout'),
    path('account/password/', views.PasswordChange.as_view(), name='password_change'),
    path('account/password/changed/', views.PasswordChangeDone.as_view(), name='password_change_done'),
    path('uploads/new/', views.upload_new, name='upload_new'),
    path('uploads/<uuid:upload_id>/', views.upload_detail, name='upload_detail'),
    path('uploads/<uuid:upload_id>/publish/', views.upload_publish, name='upload_publish'),
    path('uploads/<uuid:upload_id>/type/', views.upload_classify, name='upload_classify'),
    path('uploads/<uuid:upload_id>/close/', views.upload_close, name='upload_close'),
    path('b/<uuid:batch_id>/', views.batch_detail, name='batch_detail'),
    path('b/<uuid:batch_id>/close/', views.batch_close, name='batch_close'),
    path('b/<uuid:batch_id>/status/', views.batch_status, name='batch_status'),
    path('audit/', views.audit_log, name='audit_log'),
    path('team/', views.team, name='team'),
    path('team/<int:rep_id>/', views.team_detail, name='team_detail'),
]
