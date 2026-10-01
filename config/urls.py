from django.urls import include, path

urlpatterns = [path('', include('tracker.urls'))]
handler400 = 'tracker.views.error_400'
handler403 = 'tracker.views.error_403'
handler404 = 'tracker.views.error_404'
handler500 = 'tracker.views.error_500'
